from __future__ import annotations

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


def test_duplicate_message_id_ignored(store: Store) -> None:
    assert store.add_message(make_msg("m1", "第一句")) is True
    assert store.add_message(make_msg("m1", "重复投递")) is False
    assert store.count_messages(ChatType.GROUP, 1) == 1


def test_recent_messages_old_to_new_and_limited(store: Store) -> None:
    for i in range(1, 4):
        store.add_message(make_msg(f"m{i}", f"第{i}句", timestamp=i))
    recent = store.recent_messages(ChatType.GROUP, 1, limit=2)
    assert [m.content for m in recent] == ["第2句", "第3句"]


def test_chat_type_isolated(store: Store) -> None:
    store.add_message(make_msg("g1", "群里的", chat_type=ChatType.GROUP, chat_id=42))
    store.add_message(make_msg("p1", "私聊的", chat_type=ChatType.PRIVATE, chat_id=42))
    assert store.count_messages(ChatType.GROUP, 42) == 1
    assert store.count_messages(ChatType.PRIVATE, 42) == 1


def test_chinese_impression_searchable(store: Store) -> None:
    store.upsert_impression(
        ChatType.GROUP, 1, None, "topic", "喜欢玩显卡和跑分，最近在装机", 0.8
    )
    hits = store.search_impressions(ChatType.GROUP, 1, "显卡", topk=5)
    assert any("显卡" in hit.content for hit in hits)


def test_impression_upsert_dedupes(store: Store) -> None:
    store.upsert_impression(ChatType.GROUP, 1, 7, "nickname", "被叫做鱼片", 0.4)
    store.upsert_impression(ChatType.GROUP, 1, 7, "nickname", "被叫做鱼片", 0.9)
    rows = store.list_impressions(ChatType.GROUP, 1, topk=10)
    assert len(rows) == 1
    assert rows[0].confidence == pytest.approx(0.9)


def test_list_impressions_sorted_by_confidence(store: Store) -> None:
    store.upsert_impression(ChatType.GROUP, 1, 7, "topic", "低置信", 0.2)
    store.upsert_impression(ChatType.GROUP, 1, 8, "topic", "高置信", 0.95)
    rows = store.list_impressions(ChatType.GROUP, 1, topk=2)
    assert rows[0].content == "高置信"


def test_summary_roundtrip(store: Store) -> None:
    assert store.get_summary(ChatType.GROUP, 1, "2026-10-23") is None
    store.upsert_summary(ChatType.GROUP, 1, "2026-10-23", "昨天聊了显卡")
    store.upsert_summary(ChatType.GROUP, 1, "2026-10-23", "昨天聊了显卡和装机")
    assert store.get_summary(ChatType.GROUP, 1, "2026-10-23") == "昨天聊了显卡和装机"


def test_usage_today_accumulates(store: Store) -> None:
    store.record_usage(ChatType.GROUP, 1, "chat", 10, 5)
    store.record_usage(ChatType.GROUP, 1, "chat", 10, 5)
    calls, tokens = store.usage_today(ChatType.GROUP, 1, store.today())
    assert (calls, tokens) == (2, 30)


def test_forget_chat_clears_everything(store: Store) -> None:
    store.add_message(make_msg("m1", "聊过"))
    store.upsert_impression(ChatType.GROUP, 1, 7, "topic", "爱装机", 0.7)
    store.upsert_summary(ChatType.GROUP, 1, "2026-10-23", "摘要")
    store.record_usage(ChatType.GROUP, 1, "chat", 1, 1)
    store.forget_chat(ChatType.GROUP, 1)
    assert store.count_messages(ChatType.GROUP, 1) == 0
    assert store.list_impressions(ChatType.GROUP, 1, topk=5) == []
    assert store.get_summary(ChatType.GROUP, 1, "2026-10-23") is None
    assert store.usage_today(ChatType.GROUP, 1, store.today()) == (0, 0)


def test_forget_chat_keeps_other_chats(store: Store) -> None:
    store.add_message(make_msg("m1", "群A", chat_id=1))
    store.add_message(make_msg("m2", "群B", chat_id=2))
    store.forget_chat(ChatType.GROUP, 1)
    assert store.count_messages(ChatType.GROUP, 2) == 1


def test_opencode_session_roundtrip_and_isolated(store: Store) -> None:
    assert store.get_opencode_session(ChatType.GROUP, 1) is None
    store.set_opencode_session(ChatType.GROUP, 1, "ses_group")
    store.set_opencode_session(ChatType.PRIVATE, 1, "ses_private")
    assert store.get_opencode_session(ChatType.GROUP, 1) == "ses_group"
    assert store.get_opencode_session(ChatType.PRIVATE, 1) == "ses_private"

    store.set_opencode_session(ChatType.GROUP, 1, "ses_group_2")
    assert store.get_opencode_session(ChatType.GROUP, 1) == "ses_group_2"

    store.clear_opencode_session(ChatType.GROUP, 1)
    assert store.get_opencode_session(ChatType.GROUP, 1) is None
    assert store.get_opencode_session(ChatType.PRIVATE, 1) == "ses_private"


def test_forget_chat_clears_opencode_session(store: Store) -> None:
    store.set_opencode_session(ChatType.GROUP, 1, "ses_x")
    store.forget_chat(ChatType.GROUP, 1)
    assert store.get_opencode_session(ChatType.GROUP, 1) is None


def test_forget_all_clears_opencode_session(store: Store) -> None:
    store.set_opencode_session(ChatType.GROUP, 1, "ses_x")
    store.set_opencode_session(ChatType.PRIVATE, 2, "ses_y")
    store.forget_all()
    assert store.get_opencode_session(ChatType.GROUP, 1) is None
    assert store.get_opencode_session(ChatType.PRIVATE, 2) is None


def test_persistence_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    first = Store(path)
    first.init_schema()
    first.add_message(make_msg("m1", "记住这句"))
    first.close()

    second = Store(path)
    second.init_schema()
    try:
        assert second.count_messages(ChatType.GROUP, 1) == 1
    finally:
        second.close()
