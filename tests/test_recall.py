"""撤回消息：事件解析、落库标记、时间线渲染。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.gateway.qq import parse_event
from dafeiyu.memory.store import Store
from dafeiyu.memory.timeline import format_message_line
from dafeiyu.models import ChatType, Message, NoticeEvent, RecallEvent

# 会话 id 一律带通道前缀：裸 int 只存在于 v1 时代。
CHAT = "qq:1"


def make_msg(*, message_id: str = "m0", chat_id: str = CHAT) -> Message:
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
    assert event.chat_id == "qq:1001"
    assert event.message_id == "m7"
    assert event.operator_id == "qq:3"


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
    assert event.chat_id == "qq:2002"
    assert event.message_id == "p9"


def test_parse_other_notices_as_notice_event() -> None:
    """非撤回 notice 现在产出 NoticeEvent（只记录，不触发回复）。"""
    event = parse_event(
        {"post_type": "notice", "notice_type": "group_increase", "group_id": 1},
        bot_qq=999,
    )

    assert isinstance(event, NoticeEvent)
    assert event.kind == "group_increase"
    assert event.chat_id == "qq:1"


def test_parse_ignores_unknown_notices() -> None:
    assert (
        parse_event(
            {"post_type": "notice", "notice_type": "group_card", "group_id": 1},
            bot_qq=999,
        )
        is None
    )


def test_parsed_recall_marks_stored_message(store: Store) -> None:
    """解析出的带前缀 id 必须能真的命中库里的消息（漏改则 rowcount 恒为 0）。"""
    store.add_message(make_msg(message_id="m7", chat_id="qq:1001"))
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
    assert store.mark_recalled(
        event.chat_type, event.chat_id, event.message_id
    ) is True
    assert store.is_recalled(event.chat_type, event.chat_id, event.message_id)


def test_mark_recalled_sets_flag(store: Store) -> None:
    store.add_message(make_msg())

    assert store.mark_recalled(ChatType.GROUP, CHAT, "m0") is True
    assert store.is_recalled(ChatType.GROUP, CHAT, "m0") is True

    got = store.recent_messages(ChatType.GROUP, CHAT, 1)[0]
    assert got.recalled_at


def test_mark_recalled_unknown_message_is_noop(store: Store) -> None:
    assert store.mark_recalled(ChatType.GROUP, CHAT, "nope") is False
    assert store.is_recalled(ChatType.GROUP, CHAT, "nope") is False


def test_timeline_hides_recalled_content() -> None:
    msg = make_msg()
    msg.recalled_at = "2026-10-30 12:00:00"

    line = format_message_line(msg)

    assert "[已撤回]" in line
    assert "说错话了" not in line
