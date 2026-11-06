"""入站语音/图片的「收到了」表情回应（P3）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from dafeiyu.models import Attachment, ChatType, Message
from dafeiyu.service import Service

GROUP = "qq:1001"


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def on_message(self, handler) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        return "bot-1"

    async def set_msg_emoji_like(
        self, chat_type: ChatType, chat_id: str, message_id: str, emoji_id: str
    ) -> bool:
        self.calls.append((chat_type, chat_id, message_id, emoji_id))
        return True


def make_service(adapter: FakeAdapter, **mind_overrides: Any) -> Service:
    mind = SimpleNamespace(
        emoji_reactions=False,
        emoji_reaction_id="128",
        inbound_media_reactions=True,
        inbound_media_reaction_cooldown_seconds=600,
    )
    for key, value in mind_overrides.items():
        setattr(mind, key, value)
    service = object.__new__(Service)
    service.adapters = {"qq": adapter}
    service.settings = cast(Any, SimpleNamespace(mind=mind))
    service._bg_tasks = set()
    service._inbound_reaction_at = {}
    return service


def make_msg(
    kind: str = "record",
    *,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = GROUP,
    message_id: str = "qq:88",
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id="qq:2",
        nickname="阿明",
        content="[语音]",
        message_id=message_id,
        attachments=[Attachment(kind=kind)],
    )


async def drain(service: Service) -> None:
    tasks = list(service._bg_tasks)
    if tasks:
        await asyncio.gather(*tasks)


async def test_voice_and_image_trigger_reaction() -> None:
    adapter = FakeAdapter()
    service = make_service(adapter)

    service._schedule_inbound_reaction(make_msg("record"))
    await drain(service)
    service._inbound_reaction_at.clear()
    service._schedule_inbound_reaction(make_msg("image", message_id="qq:89"))
    await drain(service)

    assert adapter.calls == [
        (ChatType.GROUP, GROUP, "qq:88", "128"),
        (ChatType.GROUP, GROUP, "qq:89", "128"),
    ]


async def test_disabled_flag_does_nothing() -> None:
    adapter = FakeAdapter()
    service = make_service(adapter, inbound_media_reactions=False)

    service._schedule_inbound_reaction(make_msg())
    await drain(service)

    assert adapter.calls == []
    assert service._bg_tasks == set()


async def test_non_media_message_is_ignored() -> None:
    adapter = FakeAdapter()
    service = make_service(adapter)

    service._schedule_inbound_reaction(make_msg("file"))
    await drain(service)

    assert adapter.calls == []


async def test_private_chat_is_ignored() -> None:
    adapter = FakeAdapter()
    service = make_service(adapter)

    service._schedule_inbound_reaction(
        make_msg(chat_type=ChatType.PRIVATE, chat_id="qq:2")
    )
    await drain(service)

    assert adapter.calls == []


async def test_cooldown_limits_one_reaction_per_chat() -> None:
    adapter = FakeAdapter()
    service = make_service(adapter)

    service._schedule_inbound_reaction(make_msg("record", message_id="qq:1"))
    service._schedule_inbound_reaction(make_msg("image", message_id="qq:2"))
    await drain(service)

    assert adapter.calls == [(ChatType.GROUP, GROUP, "qq:1", "128")]
