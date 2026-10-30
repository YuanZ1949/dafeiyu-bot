"""撤回消息：事件解析、落库标记、时间线渲染。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.gateway.qq import parse_event
from dafeiyu.memory.store import Store
from dafeiyu.memory.timeline import format_message_line
from dafeiyu.models import ChatType, Message, RecallEvent


def make_msg(*, message_id: str = "m0", chat_id: int = 1) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id=2,
        nickname="鱼片",
        content="说错话了",
        message_id=message_id,
        timestamp=10_000,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "test.db")
    s.init_schema()
    yield s
    s.close()


def test_parse_group_recall_event() -> None:
    event = parse_event(
        {
            "post_type": "notice",
            "notice_type": "group_recall",
            "group_id": 1001,
            "user_id": 2,
            "operator_id": 3,
            "message_id": "m7",
        },
        bot_qq=999,
    )

    assert isinstance(event, RecallEvent)
    assert event.chat_type is ChatType.GROUP
    assert event.chat_id == 1001
    assert event.message_id == "m7"
    assert event.operator_id == 3


def test_parse_friend_recall_event() -> None:
    event = parse_event(
        {
            "post_type": "notice",
            "notice_type": "friend_recall",
            "user_id": 2002,
            "message_id": "p9",
        },
        bot_qq=999,
    )

    assert isinstance(event, RecallEvent)
    assert event.chat_type is ChatType.PRIVATE
    assert event.chat_id == 2002
    assert event.message_id == "p9"


def test_parse_ignores_other_notices() -> None:
    assert (
        parse_event(
            {"post_type": "notice", "notice_type": "group_increase", "group_id": 1},
            bot_qq=999,
        )
        is None
    )


def test_mark_recalled_sets_flag(store: Store) -> None:
    store.add_message(make_msg())

    assert store.mark_recalled(ChatType.GROUP, 1, "m0") is True
    assert store.is_recalled(ChatType.GROUP, 1, "m0") is True

    got = store.recent_messages(ChatType.GROUP, 1, 1)[0]
    assert got.recalled_at


def test_mark_recalled_unknown_message_is_noop(store: Store) -> None:
    assert store.mark_recalled(ChatType.GROUP, 1, "nope") is False
    assert store.is_recalled(ChatType.GROUP, 1, "nope") is False


def test_timeline_hides_recalled_content() -> None:
    msg = make_msg()
    msg.recalled_at = "2026-10-30 12:00:00"

    line = format_message_line(msg)

    assert "[已撤回]" in line
    assert "说错话了" not in line
