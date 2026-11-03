"""共享测试替身的契约：字符串 id 形态，同时不破坏既有 QQ FakeAdapter。"""

from __future__ import annotations

import asyncio
import re

from dafeiyu.models import ChatType, Message, channel_of
from tests.fakes import FakeChannelAdapter
from tests.test_service import FakeAdapter


def _message() -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="wxg:abc123",
        user_id="wxg:user1",
        nickname="阿猫",
        content="hi",
        message_id="deadbeef:1",
        timestamp=0,
    )


def test_import_path_and_bot_id() -> None:
    adapter = FakeChannelAdapter()

    assert adapter.bot_id == "wxg:self"
    assert channel_of(adapter.chat_id) == "wechat"


def test_send_returns_occurrence_id_and_tracks_chat() -> None:
    adapter = FakeChannelAdapter()

    first = asyncio.run(adapter.send(ChatType.GROUP, "wxg:abc123", "在吗"))
    second = asyncio.run(adapter.send(ChatType.GROUP, "wxg:abc123", "在吗"))

    assert re.fullmatch(r"[0-9a-f]{8}:\d+", first)
    assert re.fullmatch(r"[0-9a-f]{8}:\d+", second)
    assert first != second
    assert adapter.sent == [
        (ChatType.GROUP, "wxg:abc123", "在吗"),
        (ChatType.GROUP, "wxg:abc123", "在吗"),
    ]
    assert channel_of(adapter.last_chat_id) == "wechat"
    assert adapter.last_send_at is not None


def test_emit_dispatches_to_registered_handlers() -> None:
    adapter = FakeChannelAdapter()
    seen = []

    async def handler(message):
        seen.append(message)

    adapter.on_message(handler)
    message = _message()
    asyncio.run(adapter.emit(message))

    assert seen == [message]
    assert adapter.last_event_at is not None


def test_legacy_fake_adapter_keeps_int_chat_ids() -> None:
    adapter = FakeAdapter()

    assert adapter.bot_id  # 非 None：Service 的回复路径会读它
    assert asyncio.run(adapter.send(ChatType.GROUP, 1, "hi")) == "bot-1"
    assert adapter.sent == [(ChatType.GROUP, 1, "hi")]
