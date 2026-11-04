import pytest

from dafeiyu.memory.store import (
    CHAT_SCOPED_TABLES,
    MESSAGE_SCOPED_TABLES,
    Store,
)
from dafeiyu.models import (
    ChatType,
    Episode,
    Message,
    OpenLoop,
)

GROUP = ChatType.GROUP
CHAT = "qq:1"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "mind.db")
    s.init_schema()
    yield s
    s.close()


def _message(message_id: str = "m1") -> Message:
    return Message(
        chat_type=GROUP,
        chat_id=CHAT,
        user_id="qq:2",
        nickname="阿猫",
        content="大家好呀",
        message_id=message_id,
        timestamp=1000,
    )


def _episode() -> Episode:
    return Episode(
        chat_type=GROUP,
        chat_id=CHAT,
        start_message_id="m1",
        last_message_id="m1",
        topic="鱼缸",
        summary="大家在聊水草和灯",
    )


def _seed_memory(store: Store) -> None:
    """写全五类聚合记忆 + 原文 + 游标，作为 forget 各入口的共同前置。"""
    store.add_message(_message())
    store.upsert_impression(GROUP, CHAT, "qq:2", "preference", "阿猫喜欢三湖", 0.8)
    store.set_impression_cursor(GROUP, CHAT, 5)
    store.save_episode(_episode())
    store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "阿猫问的灯怎么选，没人接"))
    store.upsert_summary(GROUP, CHAT, "2026-11-03", "那天聊了灯")
    store.upsert_weekly_summary(GROUP, CHAT, "2026-11-02", "这一周都在聊鱼缸")


# ---- 常量防呆 ---------------------------------------------------------------


def test_chat_scoped_tables_have_no_duplicates() -> None:
    assert len(CHAT_SCOPED_TABLES) == len(set(CHAT_SCOPED_TABLES))


def test_message_scoped_tables_are_a_subset() -> None:
    assert set(MESSAGE_SCOPED_TABLES) <= set(CHAT_SCOPED_TABLES)
    # 聚合记忆不进软忘记清单。
    assert "messages" in MESSAGE_SCOPED_TABLES
    assert "impressions" not in MESSAGE_SCOPED_TABLES
    assert "summaries" not in MESSAGE_SCOPED_TABLES
    assert "weekly_summaries" not in MESSAGE_SCOPED_TABLES
    assert "episodes" not in MESSAGE_SCOPED_TABLES
    assert "open_loops" not in MESSAGE_SCOPED_TABLES
    assert "usage" not in MESSAGE_SCOPED_TABLES


def test_fixed_vulnerability_tables_are_in_chat_scoped() -> None:
    """本次修的漏洞：这两个表要留在硬忘记清单里，防止以后被随手删掉。"""
    assert "weekly_summaries" in CHAT_SCOPED_TABLES
    assert "impression_cursors" in CHAT_SCOPED_TABLES


def test_chat_scoped_tables_all_exist_in_schema(store: Store) -> None:
    existing = {
        row["name"]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = set(CHAT_SCOPED_TABLES) - existing
    assert missing == set()


def test_chat_scoped_tables_all_carry_chat_columns(store: Store) -> None:
    for table in CHAT_SCOPED_TABLES:
        cols = {
            row["name"]
            for row in store._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert {"chat_type", "chat_id"} <= cols, table


# ---- forget_chat / forget_all ----------------------------------------------


@pytest.mark.parametrize("use_forget_all", [False, True])
def test_hard_forget_clears_everything(store: Store, use_forget_all: bool) -> None:
    _seed_memory(store)

    if use_forget_all:
        store.forget_all()
    else:
        store.forget_chat(GROUP, CHAT)

    assert store.count_messages(GROUP, CHAT) == 0
    assert store.list_impressions(GROUP, CHAT, 5) == []
    assert store.get_impression_cursor(GROUP, CHAT) == 0
    assert store.latest_episode(GROUP, CHAT) is None
    assert store.open_loops(GROUP, CHAT) == []
    assert store.get_summary(GROUP, CHAT, "2026-11-03") is None
    assert store.latest_weekly_summary(GROUP, CHAT) is None


# ---- forget_messages --------------------------------------------------------


def test_soft_forget_keeps_aggregate_memory(store: Store) -> None:
    _seed_memory(store)

    store.forget_messages(GROUP, CHAT)

    # 原文清掉，聚合记忆全部保留。
    assert store.count_messages(GROUP, CHAT) == 0
    assert [x.content for x in store.list_impressions(GROUP, CHAT, 5)] == [
        "阿猫喜欢三湖"
    ]
    assert store.get_impression_cursor(GROUP, CHAT) == 5
    assert store.latest_episode(GROUP, CHAT) is not None
    assert store.open_loops(GROUP, CHAT) != []
    assert store.get_summary(GROUP, CHAT, "2026-11-03") == "那天聊了灯"
    weekly = store.latest_weekly_summary(GROUP, CHAT)
    assert weekly is not None and weekly.content == "这一周都在聊鱼缸"


def test_soft_forget_only_touches_target_chat(store: Store) -> None:
    _seed_memory(store)
    other = Episode(
        chat_type=GROUP,
        chat_id="qq:2",
        start_message_id="m1",
        last_message_id="m1",
        topic="别的群",
        summary="别的群的对话",
    )
    store.save_episode(other)

    store.forget_messages(GROUP, CHAT)

    assert store.latest_episode(GROUP, "qq:2") is not None
    assert store.count_messages(GROUP, CHAT) == 0


# shared_facts 也带会话来源，forget_chat 要一并清掉（回归既有语义）。
def test_forget_chat_purges_shared_facts(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[ChatType, str]] = []
    original = Store.purge_shared_facts_for_chat

    def spy(self: Store, chat_type: ChatType, chat_id: str) -> int:
        calls.append((chat_type, chat_id))
        return original(self, chat_type, chat_id)

    monkeypatch.setattr(Store, "purge_shared_facts_for_chat", spy)
    store.forget_chat(GROUP, CHAT)

    assert calls == [(GROUP, CHAT)]


def test_forget_all_clears_shared_facts(store: Store) -> None:
    _seed_memory(store)
    store._conn.execute(
        "INSERT INTO shared_facts"
        " (question_key, question_text, answer_text, source_chat_type, source_chat_id)"
        " VALUES ('k', 'q', 'a', ?, ?)",
        (GROUP.value, CHAT),
    )
    store._conn.commit()

    store.forget_all()

    row = store._conn.execute("SELECT 1 FROM shared_facts").fetchone()
    assert row is None
