from __future__ import annotations

from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

GROUP = ChatType.GROUP
CHAT_ID = 100


def make_msg(message_id: str, content: str, timestamp: int = 0) -> Message:
    return Message(
        chat_type=GROUP,
        chat_id=CHAT_ID,
        user_id=7,
        nickname="鱼片",
        content=content,
        message_id=message_id,
        timestamp=timestamp,
    )


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "timeline.db")
    s.init_schema()
    yield s
    s.close()


def seed(store: Store, count: int = 3) -> None:
    for index in range(count):
        store.add_message(
            make_msg(f"m{index}", f"消息 {index}", timestamp=1_700_000_000 + index)
        )


def test_messages_page_returns_latest_page_then_loads_earlier_without_repeat(
    store: Store,
) -> None:
    seed(store)

    first = store.messages_page(GROUP, CHAT_ID, limit=2)

    assert [m.message_id for m in first.messages] == ["m1", "m2"]
    assert first.has_more is True
    assert first.next_before is not None
    assert first.anchor_found is False

    older = store.messages_page(GROUP, CHAT_ID, limit=2, before=first.next_before)

    assert [m.message_id for m in older.messages] == ["m0"]
    assert older.has_more is False
    assert older.next_before is None
    assert {m.message_id for m in first.messages}.isdisjoint(
        {m.message_id for m in older.messages}
    )


def test_messages_page_anchor_ends_page_at_target_and_reports_found(
    store: Store,
) -> None:
    seed(store)

    page = store.messages_page(GROUP, CHAT_ID, limit=2, anchor_message_id="m1")

    assert [m.message_id for m in page.messages] == ["m0", "m1"]
    assert page.anchor_found is True
    assert page.has_more is False

    missing = store.messages_page(
        GROUP, CHAT_ID, limit=2, anchor_message_id="not-there"
    )

    assert [m.message_id for m in missing.messages] == ["m1", "m2"]
    assert missing.anchor_found is False


def test_messages_page_empty_chat_and_limit_boundary(store: Store) -> None:
    empty = store.messages_page(GROUP, CHAT_ID, limit=50)
    assert empty.messages == []
    assert empty.has_more is False
    assert empty.next_before is None
    assert empty.anchor_found is False

    seed(store)
    tiny = store.messages_page(GROUP, CHAT_ID, limit=0)

    assert [m.message_id for m in tiny.messages] == ["m2"]
    assert tiny.has_more is True
