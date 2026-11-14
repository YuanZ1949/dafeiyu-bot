from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

GROUP = ChatType.GROUP


def make_msg(
    idx: int,
    *,
    chat_id: str = "1",
    ts: int,
    is_bot: bool = False,
) -> Message:
    return Message(
        chat_type=GROUP,
        chat_id=chat_id,
        user_id="qq:bot" if is_bot else "qq:2",
        nickname="本鱼" if is_bot else "鱼片",
        content=f"第{idx}句",
        message_id=f"m{idx}",
        is_bot=is_bot,
        timestamp=ts,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "test.db")
    s.init_schema()
    yield s
    s.close()


def test_pending_hold_info_counts_after_cursor(store: Store) -> None:
    """(a) 有游标 + 2 条未投递：count == 2，oldest_ts 是更早那条。"""
    store.add_message(make_msg(0, ts=1000))
    store.add_message(make_msg(1, ts=1100))
    store.add_message(make_msg(2, ts=1200))
    store.mark_delivered(GROUP, "1", "m0")  # 钉住游标；m0 已投递

    assert store.oldest_undelivered_ts(GROUP, "1") == 1100
    assert store.pending_hold_info() == [
        {"chat_type": "group", "chat_id": "1", "count": 2, "oldest_ts": 1100}
    ]


def test_pending_hold_info_first_delivery_without_cursor(store: Store) -> None:
    """(b) 没有游标行：按首次投递语义只算最新一条（count == 1）。"""
    store.add_message(make_msg(0, ts=1000))
    store.add_message(make_msg(1, ts=1100))
    store.add_message(make_msg(2, ts=1200))

    assert store.oldest_undelivered_ts(GROUP, "1") == 1200
    assert store.pending_hold_info() == [
        {"chat_type": "group", "chat_id": "1", "count": 1, "oldest_ts": 1200}
    ]
    assert store.undelivered_message_ids(GROUP, "1") == ["m2"]


def test_pending_hold_info_omits_fully_delivered_chat(store: Store) -> None:
    """(c) 全部投递完的会话不出现在 pending_hold_info 里。"""
    store.add_message(make_msg(0, ts=1000))
    store.add_message(make_msg(1, ts=1100))
    store.mark_delivered(GROUP, "1", "m0", "m1")

    assert store.pending_hold_info() == []
    assert store.oldest_undelivered_ts(GROUP, "1") is None
    assert store.undelivered_message_ids(GROUP, "1") == []


def test_mark_skipped_drops_messages_from_aggregate(store: Store) -> None:
    """(d) mark_skipped 之后该消息不再计入，oldest_ts 前移（不是只增不减的残留计数）。"""
    store.add_message(make_msg(0, ts=1000))
    store.add_message(make_msg(1, ts=1100))
    store.add_message(make_msg(2, ts=1200))
    store.mark_delivered(GROUP, "1", "m0")

    assert store.pending_hold_info()[0]["count"] == 2
    assert store.oldest_undelivered_ts(GROUP, "1") == 1100

    store.mark_skipped(GROUP, "1", ["m1"], "hold_discard")

    assert store.pending_hold_info() == [
        {"chat_type": "group", "chat_id": "1", "count": 1, "oldest_ts": 1200}
    ]
    assert store.oldest_undelivered_ts(GROUP, "1") == 1200
    assert store.undelivered_message_ids(GROUP, "1") == ["m2"]


def test_oldest_ts_agrees_with_pending_hold_info(store: Store) -> None:
    """(e) 两个查询对同一会话给出一致的最早时间；多会话各算各的。"""
    # 有游标的会话：m1 / m2 未投递
    store.add_message(make_msg(0, chat_id="1", ts=1000))
    store.add_message(make_msg(1, chat_id="1", ts=1500))
    store.add_message(make_msg(2, chat_id="1", ts=1600))
    store.mark_delivered(GROUP, "1", "m0")
    # 没有游标的会话：只看最新一条
    store.add_message(make_msg(3, chat_id="2", ts=2000))
    store.add_message(make_msg(4, chat_id="2", ts=2200))

    by_chat = {item["chat_id"]: item for item in store.pending_hold_info()}
    assert set(by_chat) == {"1", "2"}
    for chat_id, item in by_chat.items():
        assert item["oldest_ts"] == store.oldest_undelivered_ts(GROUP, chat_id)
    assert by_chat["1"]["count"] == 2 and by_chat["1"]["oldest_ts"] == 1500
    assert by_chat["2"]["count"] == 1 and by_chat["2"]["oldest_ts"] == 2200


def test_unknown_timestamp_yields_no_age(store: Store) -> None:
    """(f) R1-B2：timestamp <= 0 不参与取最小值；全未知时返回 None，但仍算进 count。"""
    # 有游标的会话：唯一未投递消息时间未知
    store.add_message(make_msg(0, chat_id="1", ts=1000))
    store.mark_delivered(GROUP, "1", "m0")
    store.add_message(make_msg(1, chat_id="1", ts=0))
    # 没有游标的会话：最新一条时间未知
    store.add_message(make_msg(2, chat_id="2", ts=0))

    assert store.oldest_undelivered_ts(GROUP, "1") is None
    assert store.oldest_undelivered_ts(GROUP, "2") is None

    by_chat = {item["chat_id"]: item for item in store.pending_hold_info()}
    assert by_chat["1"]["count"] == 1 and by_chat["1"]["oldest_ts"] is None
    assert by_chat["2"]["count"] == 1 and by_chat["2"]["oldest_ts"] is None


def test_undelivered_message_ids_is_not_capped_at_20(store: Store) -> None:
    """(g) R1-M7：25 条积压时返回全部 id，不止 pending_batch 的 20 条。"""
    for i in range(26):
        store.add_message(make_msg(i, ts=1000 + i))
    store.mark_delivered(GROUP, "1", "m0")  # m1..m25 共 25 条未投递

    ids = store.undelivered_message_ids(GROUP, "1")
    assert len(ids) == 25
    assert ids == [f"m{i}" for i in range(1, 26)]
    assert len(store.pending_batch(GROUP, "1", anchor_id="m25")) == 20
    assert store.pending_hold_info()[0]["count"] == 25


def test_pending_hold_info_ignores_bot_messages(store: Store) -> None:
    """机器人自己的消息不算积压。"""
    store.add_message(make_msg(0, ts=1000))
    store.mark_delivered(GROUP, "1", "m0")
    store.add_message(make_msg(1, ts=1100, is_bot=True))

    assert store.pending_hold_info() == []
    assert store.oldest_undelivered_ts(GROUP, "1") is None


def test_latest_decision_per_chat_returns_only_latest_row(store: Store) -> None:
    """每个会话只返回最新一条决策，不统计 held 行数（held 有 60 秒去重）。"""
    store.record_decision(
        GROUP, "1", outcome="held", checkpoint="opportunity_hold"
    )
    store.record_decision(
        GROUP, "1", outcome="held", checkpoint="opportunity_hold"
    )  # 60 秒去重：不会落库
    store.record_decision(GROUP, "1", outcome="replied")
    store.record_decision(
        GROUP, "2", outcome="held", checkpoint="intensity_quiet"
    )

    latest = {item["chat_id"]: item for item in store.latest_decision_per_chat()}
    assert set(latest) == {"1", "2"}
    assert latest["1"]["outcome"] == "replied"
    assert latest["1"]["checkpoint"] == ""
    assert latest["2"]["outcome"] == "held"
    assert latest["2"]["checkpoint"] == "intensity_quiet"
    assert set(latest["1"]) == {
        "chat_type",
        "chat_id",
        "outcome",
        "checkpoint",
        "created_at",
    }
    # 去重确实让 chat 1 只留下两条 held / replied 中的一条 held
    held_rows = store.list_decisions(outcome="held")
    assert [row["chat_id"] for row in held_rows] == ["2", "1"]
