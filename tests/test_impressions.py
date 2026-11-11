import json
import logging
from pathlib import Path

import pytest

from dafeiyu.brain.client import BrainError
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.workers.impressions import MAX_SOURCE_MESSAGES, ImpressionWorker

CHAT = ChatType.GROUP
CHAT_ID = 90001


class FakeBrain:
    def __init__(self, response: str = "[]", exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.calls = 0
        self.last_messages: list[dict] = []

    async def complete(self, system: str, messages: list[dict], max_tokens: int = 400) -> str:
        self.calls += 1
        self.last_messages = messages
        if self.exc is not None:
            raise self.exc
        return self.response


def make_message(
    i: int,
    chat_id: int = CHAT_ID,
    user_id: str = "qq:123",
    *,
    content: str | None = None,
    is_bot: bool = False,
) -> Message:
    return Message(
        chat_type=CHAT,
        chat_id=chat_id,
        user_id=user_id,
        nickname="鱼片",
        content=f"消息 {i}" if content is None else content,
        message_id=f"m{i}",
        is_bot=is_bot,
    )


@pytest.fixture
def store(tmp_path: Path):
    s = Store(str(tmp_path / "test.db"))
    s.init_schema()
    yield s
    s.close()


async def test_no_extraction_before_every(store: Store) -> None:
    brain = FakeBrain()
    worker = ImpressionWorker(store, brain, every=20)
    for i in range(19):
        store.add_message(make_message(i))
        assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert brain.calls == 0


async def test_twentieth_message_triggers_extraction(store: Store) -> None:
    brain = FakeBrain(json.dumps([{"user_id": "qq:123", "kind": "nickname", "content": "外号叫鱼片", "confidence": 0.8}]))
    worker = ImpressionWorker(store, brain, every=20)
    for i in range(20):
        store.add_message(make_message(i))
        await worker.maybe_extract(CHAT, CHAT_ID)
    assert brain.calls == 1
    impressions = store.list_impressions(CHAT, CHAT_ID, topk=10)
    assert [im.content for im in impressions] == ["外号叫鱼片"]
    assert "消息 19" in brain.last_messages[0]["content"]


async def test_invalid_json_returns_zero(store: Store) -> None:
    brain = FakeBrain("这不是 JSON")
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert store.list_impressions(CHAT, CHAT_ID, topk=10) == []


async def test_invalid_items_are_skipped(store: Store) -> None:
    brain = FakeBrain(json.dumps([
        {"kind": "nickname", "content": "缺 user_id"},
        {"user_id": "qq:123", "content": "缺 kind"},
        {"user_id": "qq:123", "kind": "nickname", "content": ""},
        {"user_id": "qq:123", "kind": "fact", "content": ""},
    ]))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert store.list_impressions(CHAT, CHAT_ID, topk=10) == []


async def test_impression_rejects_unknown_user(store: Store) -> None:
    """模型编造本批不存在的 user_id 时，该条印象不落库、不计入 added。"""
    chat_id = "qq:90001"
    brain = FakeBrain(json.dumps([
        {"user_id": "qq:123", "kind": "fact", "content": "住在杭州", "confidence": 0.8},
        {"user_id": "qq:999", "kind": "fact", "content": "捏造发言人的印象", "confidence": 0.9},
    ]))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(
        Message(
            chat_type=CHAT,
            chat_id=chat_id,
            user_id="qq:123",
            nickname="鱼片",
            content="消息 1",
            message_id="m1",
        )
    )

    assert await worker.maybe_extract(CHAT, chat_id) == 1

    impressions = store.list_impressions(CHAT, chat_id, topk=10)
    assert [im.content for im in impressions] == ["住在杭州"]
    assert all(im.subject_user_id != "qq:999" for im in impressions)


async def test_duplicate_extraction_does_not_duplicate_rows(store: Store) -> None:
    brain = FakeBrain(json.dumps([{"user_id": "qq:123", "kind": "nickname", "content": "外号叫鱼片", "confidence": 0.8}]))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))

    assert await worker.maybe_extract(CHAT, CHAT_ID) == 1
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert len(store.list_impressions(CHAT, CHAT_ID, topk=10)) == 1


async def test_extraction_records_latest_human_source(store: Store) -> None:
    brain = FakeBrain(
        json.dumps(
            [{"user_id": "qq:123", "kind": "fact", "content": "住在杭州", "confidence": 0.8}]
        )
    )
    worker = ImpressionWorker(store, brain, every=1)
    long_source = "杭州" + "补充" * 50
    store.add_message(make_message(1, user_id="qq:123", content="我先说一句"))
    store.add_message(make_message(2, user_id="qq:456", content="别人说的"))
    store.add_message(make_message(3, user_id="qq:123", content=long_source))
    store.add_message(
        make_message(4, user_id="qq:123", content="bot 的最后一句", is_bot=True)
    )

    assert await worker.maybe_extract(CHAT, CHAT_ID) == 1
    impression = store.list_impressions(CHAT, CHAT_ID, topk=10)[0]
    assert impression.source_message_id == "m3"
    assert impression.source_excerpt == long_source[:80]
    assert len(impression.source_excerpt) == 80


async def test_content_is_truncated_to_60_chars(store: Store) -> None:
    long_text = "鱼" * 100
    brain = FakeBrain(json.dumps([{"user_id": "qq:123", "kind": "fact", "content": long_text, "confidence": 0.5}]))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    await worker.maybe_extract(CHAT, CHAT_ID)
    impressions = store.list_impressions(CHAT, CHAT_ID, topk=10)
    assert len(impressions[0].content) == 60


async def test_brain_error_returns_zero(store: Store) -> None:
    brain = FakeBrain(exc=RuntimeError("boom"))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0


async def test_missing_content_is_treated_as_empty(store: Store) -> None:
    brain = FakeBrain(json.dumps([{"user_id": "qq:123", "kind": "nickname", "content": None, "confidence": 0.8}]))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0


async def test_provider_failure_warns_without_traceback(store: Store, caplog) -> None:
    brain = FakeBrain(exc=BrainError("provider.quota: Free usage limit reached"))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))

    with caplog.at_level(logging.WARNING, logger="dafeiyu.workers.impressions"):
        assert await worker.maybe_extract(CHAT, CHAT_ID) == 0

    records = [r for r in caplog.records if "provider.quota" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert records[0].levelno == logging.WARNING


async def test_unexpected_failure_keeps_traceback(store: Store, caplog) -> None:
    brain = FakeBrain(exc=RuntimeError("boom"))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))

    with caplog.at_level(logging.ERROR, logger="dafeiyu.workers.impressions"):
        assert await worker.maybe_extract(CHAT, CHAT_ID) == 0

    records = [r for r in caplog.records if "boom" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is not None


async def test_failure_cooldown_blocks_retry_until_expired(store: Store) -> None:
    clock = {"t": 0.0}
    brain = FakeBrain(exc=BrainError("provider.timeout"))
    worker = ImpressionWorker(
        store,
        brain,
        every=1,
        backoff_seconds=60,
        monotonic=lambda: clock["t"],
    )
    store.add_message(make_message(1))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert brain.calls == 1

    store.add_message(make_message(2))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert brain.calls == 1  # 冷却期内不再撞配额

    clock["t"] = 61.0
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert brain.calls == 2  # 冷却过期后允许重试


# ---- 去重与降权 ---------------------------------------------------


def test_nickname_keeps_only_latest_per_user(store: Store) -> None:
    store.upsert_impression(CHAT, CHAT_ID, "qq:456", "nickname", "外号叫鱼干", 0.7)
    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "nickname", "外号叫鱼片", 0.8)
    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "nickname", "外号叫小鱼", 0.6)

    impressions = store.list_impressions(CHAT, CHAT_ID, topk=10)
    mine = [im for im in impressions if im.subject_user_id == "qq:123"]
    assert [im.content for im in mine] == ["外号叫小鱼"]
    others = [im for im in impressions if im.subject_user_id == "qq:456"]
    assert [im.content for im in others] == ["外号叫鱼干"]


def test_preference_dedupes_normalized_copy(store: Store) -> None:
    store.upsert_impression(
        CHAT, CHAT_ID, "qq:123", "preference", "喜欢 Coffee 。", 0.6, source_message_id="m1"
    )
    first = store.list_impressions(CHAT, CHAT_ID, topk=10)[0]

    store.upsert_impression(
        CHAT, CHAT_ID, "qq:123", "preference", "喜欢coffee！", 0.9, source_message_id="m2"
    )
    impressions = store.list_impressions(CHAT, CHAT_ID, topk=10)

    assert len(impressions) == 1
    assert impressions[0].id == first.id  # 保留原 id，不新增行
    assert impressions[0].confidence == 0.9  # 取较大值
    assert impressions[0].content == "喜欢coffee！"  # 内容更新为新写法
    assert impressions[0].source_message_id == "m2"


def test_distinct_preferences_are_kept(store: Store) -> None:
    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "preference", "喜欢咖啡", 0.6)
    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "preference", "喜欢茶", 0.6)

    contents = {im.content for im in store.list_impressions(CHAT, CHAT_ID, topk=10)}
    assert contents == {"喜欢咖啡", "喜欢茶"}


def test_stale_impression_ranks_after_fresh(store: Store) -> None:
    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "preference", "喜欢咖啡", 0.9)
    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "preference", "喜欢茶", 0.2)
    stale = next(
        im
        for im in store.list_impressions(CHAT, CHAT_ID, topk=10)
        if im.content == "喜欢咖啡"
    )
    store._conn.execute(
        "UPDATE impressions SET last_seen_at = datetime('now', '-40 days') WHERE id = ?",
        (stale.id,),
    )
    store._conn.commit()

    impressions = store.list_impressions(CHAT, CHAT_ID, topk=10)
    assert [im.content for im in impressions] == ["喜欢茶", "喜欢咖啡"]  # 降权但不删


def test_stale_impression_still_searchable_but_ranked_last(store: Store) -> None:
    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "preference", "喜欢咖啡", 0.9)
    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "preference", "喜欢茶", 0.2)
    stale = next(
        im
        for im in store.list_impressions(CHAT, CHAT_ID, topk=10)
        if im.content == "喜欢咖啡"
    )
    store._conn.execute(
        "UPDATE impressions SET last_seen_at = datetime('now', '-40 days') WHERE id = ?",
        (stale.id,),
    )
    store._conn.commit()

    results = store.search_impressions(CHAT, CHAT_ID, "喜欢", topk=10)
    assert [im.content for im in results] == ["喜欢茶", "喜欢咖啡"]


def test_repeat_write_refreshes_last_seen_at(store: Store) -> None:
    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "nickname", "外号叫鱼片", 0.8)
    store._conn.execute("UPDATE impressions SET last_seen_at = datetime('now', '-40 days')")
    store._conn.commit()

    store.upsert_impression(CHAT, CHAT_ID, "qq:123", "nickname", "外号叫鱼片", 0.8)

    row = store._conn.execute(
        "SELECT last_seen_at >= datetime('now', '-30 days') AS fresh FROM impressions"
    ).fetchone()
    assert row["fresh"] == 1


async def test_restart_resumes_from_persisted_cursor(store: Store) -> None:
    brain = FakeBrain(
        json.dumps(
            [{"user_id": "qq:123", "kind": "fact", "content": "住在杭州", "confidence": 0.8}]
        )
    )
    worker = ImpressionWorker(store, brain, every=20)
    for i in range(20):
        store.add_message(make_message(i))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 1
    assert brain.calls == 1

    restarted = ImpressionWorker(store, brain, every=20)
    assert await restarted.maybe_extract(CHAT, CHAT_ID) == 0
    assert brain.calls == 1  # 重启后不重抽同一批消息

    for i in range(20, 40):
        store.add_message(make_message(i))
    await restarted.maybe_extract(CHAT, CHAT_ID)
    assert brain.calls == 2  # 新消息到了仍然会抽


async def test_extraction_stores_subject_nickname(store: Store) -> None:
    brain = FakeBrain(
        json.dumps(
            [{"user_id": "qq:123", "kind": "preference", "content": "喜欢猫", "confidence": 0.9}]
        )
    )
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1, content="我喜欢猫"))
    await worker.maybe_extract(CHAT, CHAT_ID)
    assert store.list_impressions(CHAT, CHAT_ID, topk=10)[0].subject_nickname == "鱼片"


async def test_failure_does_not_advance_cursor(store: Store) -> None:
    brain = FakeBrain(exc=BrainError("provider.timeout"))
    worker = ImpressionWorker(store, brain, every=20, backoff_seconds=0)
    for i in range(20):
        store.add_message(make_message(i))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert store.get_impression_cursor(CHAT, CHAT_ID) == 0


async def test_window_feeds_new_messages_plus_bounded_context(store: Store) -> None:
    """每轮只喂游标之后的新消息 + 固定 10 条上文，避免把上一批整段重喂。"""
    brain = FakeBrain()
    worker = ImpressionWorker(store, brain, every=20)
    for i in range(20):
        store.add_message(make_message(i))
    await worker.maybe_extract(CHAT, CHAT_ID)
    assert brain.calls == 1
    assert len(brain.last_messages[0]["content"].splitlines()) == 20

    for i in range(20, 40):
        store.add_message(make_message(i))
    await worker.maybe_extract(CHAT, CHAT_ID)
    assert brain.calls == 2
    lines = brain.last_messages[0]["content"].splitlines()
    assert len(lines) == 30  # 20 条新消息 + 10 条上文
    assert lines[0].endswith(": 消息 10")  # 上文起点
    assert lines[10].endswith(": 消息 20")  # 新消息起点
    assert lines[-1].endswith(": 消息 39")
    assert not any(line.endswith(": 消息 9") for line in lines)


async def test_window_caps_at_max_source_messages(store: Store) -> None:
    """积压很大时仍封顶 40 条，并且保留最新消息。"""
    brain = FakeBrain()
    worker = ImpressionWorker(store, brain, every=20)
    for i in range(100):
        store.add_message(make_message(i))
    await worker.maybe_extract(CHAT, CHAT_ID)

    lines = brain.last_messages[0]["content"].splitlines()
    assert len(lines) == MAX_SOURCE_MESSAGES
    assert lines[0].endswith(": 消息 60")
    assert lines[-1].endswith(": 消息 99")


async def test_manual_extract_uses_recent_window(store: Store) -> None:
    """面板手动触发不带 messages，仍按最近 40 条抽取。"""
    brain = FakeBrain()
    worker = ImpressionWorker(store, brain, every=20)
    for i in range(50):
        store.add_message(make_message(i))
    await worker.extract(CHAT, CHAT_ID)

    lines = brain.last_messages[0]["content"].splitlines()
    assert len(lines) == MAX_SOURCE_MESSAGES
    assert lines[0].endswith(": 消息 10")
    assert lines[-1].endswith(": 消息 49")


def test_list_impressions_prioritizes_focus_users(store: Store) -> None:
    store.upsert_impression(CHAT, CHAT_ID, "qq:1", "preference", "甲喜欢咖啡", 0.9)
    store.upsert_impression(CHAT, CHAT_ID, "qq:2", "preference", "乙喜欢茶", 0.8)
    store.upsert_impression(CHAT, CHAT_ID, "qq:3", "preference", "丙喜欢酒", 0.7)

    focused = store.list_impressions(CHAT, CHAT_ID, 2, focus_user_ids=["qq:3"])
    assert [im.subject_user_id for im in focused] == ["qq:3", "qq:1"]
    assert [
        im.subject_user_id for im in store.list_impressions(CHAT, CHAT_ID, 2)
    ] == ["qq:1", "qq:2"]


async def test_batch_of_impressions_inserts_all_rows(store: Store) -> None:
    """漏改 user_id 前缀时 3 条抽取结果会被静默丢弃（0 行落库），必须插入 3 行。"""
    brain = FakeBrain(
        json.dumps(
            [
                {"user_id": "qq:123", "kind": "nickname", "content": "外号叫鱼片", "confidence": 0.8},
                {"user_id": "qq:123", "kind": "preference", "content": "喜欢咖啡", "confidence": 0.9},
                {"user_id": "qq:456", "kind": "fact", "content": "住在杭州", "confidence": 0.7},
            ]
        )
    )
    worker = ImpressionWorker(store, brain, every=20)
    store.add_message(make_message(0, user_id="qq:456"))
    for i in range(20):
        store.add_message(make_message(i))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 3

    row = store._conn.execute("SELECT COUNT(*) AS n FROM impressions").fetchone()
    assert row["n"] == 3



