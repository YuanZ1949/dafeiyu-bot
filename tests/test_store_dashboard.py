from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message


def make_msg(
    message_id: str,
    content: str,
    *,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: int = 1,
    user_id: int = 2,
    timestamp: int = 0,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname="鱼片",
        content=content,
        message_id=message_id,
        timestamp=timestamp,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "test.db")
    s.init_schema()
    yield s
    s.close()


def _move_usage_to(store: Store, date: str) -> None:
    store._conn.execute("UPDATE usage SET date = ?", (date,))
    store._conn.commit()


def test_search_messages_escapes_like_wildcards(store: Store) -> None:
    store.add_message(make_msg("m1", "100% 装机"))
    store.add_message(make_msg("m2", "1000 装机"))
    store.add_message(make_msg("m3", "聊_天"))
    store.add_message(make_msg("m4", "聊天"))

    total, hits = store.search_messages("100%")
    assert total == 1
    assert [m.message_id for m in hits] == ["m1"]

    total, hits = store.search_messages("聊_天")
    assert total == 1
    assert [m.message_id for m in hits] == ["m3"]


def test_search_messages_filters_and_pages_newest_first(store: Store) -> None:
    for i in range(3):
        store.add_message(make_msg(f"g{i}", f"装机 {i}", chat_id=1))
    store.add_message(make_msg("p0", "装机 私聊", chat_type=ChatType.PRIVATE, chat_id=9))

    total, hits = store.search_messages("装机")
    assert total == 4
    assert [m.message_id for m in hits] == ["p0", "g2", "g1", "g0"]

    total, hits = store.search_messages("装机", chat_type=ChatType.GROUP, chat_id=1)
    assert total == 3
    assert [m.message_id for m in hits[:1]] == ["g2"]

    total, hits = store.search_messages("装机", limit=1, offset=1)
    assert total == 4
    assert [m.message_id for m in hits] == ["g2"]


def test_usage_series_aggregates_reply_ambient_and_errors(store: Store) -> None:
    store.record_usage(ChatType.GROUP, 1, "reply", 10, 5)
    store.record_usage(ChatType.GROUP, 1, "ambient", 4, 2)
    store.record_usage(ChatType.GROUP, 1, "error", 0, 0)
    store.record_usage(ChatType.GROUP, 1, "summary", 7, 3)
    _move_usage_to(store, "2026-10-24")

    store.record_usage(ChatType.PRIVATE, 2, "reply", 1, 1)
    # 第二条仍在今天，单独改到 25 号（只改最新一行）。
    store._conn.execute(
        """
        UPDATE usage SET date = '2026-10-25'
        WHERE id = (SELECT MAX(id) FROM usage)
        """
    )
    store._conn.commit()

    series = store.usage_series("2026-10-24", "2026-10-25")
    assert series == [
        {"date": "2026-10-24", "calls": 2, "errors": 1, "tokens": 31},
        {"date": "2026-10-25", "calls": 1, "errors": 0, "tokens": 2},
    ]
    assert store.usage_series("2026-10-25", "2026-10-26") == [
        {"date": "2026-10-25", "calls": 1, "errors": 0, "tokens": 2}
    ]


def test_usage_top_and_recent_errors(store: Store) -> None:
    store.record_usage(ChatType.GROUP, 1, "reply", 10, 5)
    store.record_usage(ChatType.GROUP, 1, "reply", 10, 5)
    store.record_usage(ChatType.PRIVATE, 2, "reply", 2, 2)
    store.record_usage(ChatType.PRIVATE, 2, "error", 0, 0)
    _move_usage_to(store, "2026-10-24")

    top = store.usage_top("2026-10-24", limit=5)
    assert [(row["chat_type"], row["chat_id"], row["calls"]) for row in top] == [
        ("group", "1", 2),
        ("private", "2", 1),
    ]
    assert top[0]["tokens"] == 30
    assert store.usage_top("2026-10-25") == []

    errors = store.recent_errors(limit=5)
    assert len(errors) == 1
    assert errors[0]["chat_type"] == "private"
    assert errors[0]["chat_id"] == "2"
    assert re.fullmatch(r"\d\d-\d\d \d\d:\d\d", str(errors[0]["at"]))


def test_chat_overview_merges_sources_and_session(store: Store) -> None:
    store.add_message(make_msg("m1", "群消息", chat_id=1, timestamp=100))
    store.add_message(make_msg("m2", "更近", chat_id=1, timestamp=200))
    store.upsert_summary(ChatType.GROUP, 2, "2026-10-20", "只有摘要的会话")
    store.upsert_impression(ChatType.GROUP, 1, 7, "topic", "爱装机", 0.7)
    store.set_brain_session("opencode", ChatType.GROUP, 1, "ses_1")

    overview = store.chat_overview()
    assert [row["chat_id"] for row in overview] == ["1", "2"]
    first, second = overview
    assert first["message_count"] == 2
    assert first["last_message_at"] == 200
    assert first["summary_count"] == 0
    assert first["impression_count"] == 1
    assert first["provider"] == "opencode"
    assert first["session_id"] == "ses_1"
    assert second["message_count"] == 0
    assert second["summary_count"] == 1
    assert second["session_id"] is None


def test_forget_messages_keeps_summaries_and_impressions(store: Store) -> None:
    store.add_message(make_msg("m1", "聊过"))
    store.upsert_summary(ChatType.GROUP, 1, "2026-10-23", "摘要")
    store.upsert_impression(ChatType.GROUP, 1, 7, "topic", "爱装机", 0.7)
    store.set_brain_session("opencode", ChatType.GROUP, 1, "ses_1")

    store.forget_messages(ChatType.GROUP, 1)

    assert store.count_messages(ChatType.GROUP, 1) == 0
    assert store.get_summary(ChatType.GROUP, 1, "2026-10-23") == "摘要"
    assert len(store.list_impressions(ChatType.GROUP, 1, topk=5)) == 1
    assert store.get_brain_session("opencode", ChatType.GROUP, 1) is None


def test_reset_chat_keeps_messages(store: Store) -> None:
    store.add_message(make_msg("m1", "聊过"))
    store.upsert_summary(ChatType.GROUP, 1, "2026-10-23", "摘要")
    store.set_brain_session("opencode", ChatType.GROUP, 1, "ses_1")
    store.add_session_summary(ChatType.GROUP, 1, "轮换摘要")

    store.reset_chat(ChatType.GROUP, 1)

    assert store.count_messages(ChatType.GROUP, 1) == 1
    assert store.get_summary(ChatType.GROUP, 1, "2026-10-23") == "摘要"
    assert store.get_brain_session("opencode", ChatType.GROUP, 1) is None
    assert store.latest_session_summary(ChatType.GROUP, 1) is None
