"""微信贴纸真实 GIF 出站 + 图片降级（dev-opportunities todo 32）。

``Service`` 不整实例化：``object.__new__(Service)`` 只挂 adapters，
send/send_media 全部在测试内自建（fakes.py 的 FakeChannelAdapter 只有 send）。
覆盖：贴纸带 path → 只推一次 sticker；被拒/异常 → 降级 image；
无 path → 跳过媒体只发文本；image 也失败 → 文本照发、不抛异常。
"""

from __future__ import annotations

import asyncio
from typing import Any

from dafeiyu.gateway.base import GatewayError
from dafeiyu.models import Attachment, ChatType
from dafeiyu.service import Service

CHAT_ID = "wxg:1001"
TEXT = "在吗"


class FakeWechatAdapter:
    """send_media 结果表驱动：记录每次 (kind, path, name) 调用与文本发送。"""

    def __init__(self, outcomes: dict[str, Any] | None = None) -> None:
        self.sent: list[str] = []
        self.media_calls: list[tuple[str, str, str]] = []
        self._outcomes = outcomes or {}

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append(text)
        return "wx-1"

    async def send_media(
        self, chat_type: ChatType, chat_id: str, kind: str, path: str, name: str = ""
    ) -> bool:
        self.media_calls.append((kind, path, name))
        outcome = self._outcomes.get(kind, True)
        if isinstance(outcome, Exception):
            raise outcome
        return bool(outcome)


def make_service(adapter: FakeWechatAdapter) -> Service:
    service = object.__new__(Service)
    setattr(service, "adapters", {"wechat": adapter})
    return service


def run(service: Service, attachments: tuple[Attachment, ...]) -> None:
    asyncio.run(
        service._send_chunk(
            ChatType.GROUP,
            CHAT_ID,
            TEXT,
            attachments=attachments,
        )
    )


def test_sticker_with_path_pushes_sticker_only() -> None:
    adapter = FakeWechatAdapter()
    service = make_service(adapter)

    run(
        service,
        (Attachment(kind="sticker", name="摸鱼", path="replies/x.gif"),),
    )

    assert adapter.media_calls == [("sticker", "replies/x.gif", "摸鱼")]
    assert adapter.sent == [TEXT]


def test_sticker_push_false_falls_back_to_image() -> None:
    adapter = FakeWechatAdapter({"sticker": False})
    service = make_service(adapter)

    run(
        service,
        (Attachment(kind="sticker", name="摸鱼", path="replies/x.gif"),),
    )

    assert adapter.media_calls == [
        ("sticker", "replies/x.gif", "摸鱼"),
        ("image", "replies/x.gif", "摸鱼"),
    ]
    assert adapter.sent == [TEXT]


def test_sticker_gateway_error_falls_back_to_image() -> None:
    adapter = FakeWechatAdapter({"sticker": GatewayError("微信桥未连接")})
    service = make_service(adapter)

    run(
        service,
        (Attachment(kind="sticker", name="摸鱼", path="replies/x.gif"),),
    )

    assert adapter.media_calls == [
        ("sticker", "replies/x.gif", "摸鱼"),
        ("image", "replies/x.gif", "摸鱼"),
    ]
    assert adapter.sent == [TEXT]


def test_sticker_without_path_skips_media_and_sends_text() -> None:
    adapter = FakeWechatAdapter()
    service = make_service(adapter)

    run(service, (Attachment(kind="sticker", name="178"),))

    assert adapter.media_calls == []
    assert adapter.sent == [TEXT]


def test_sticker_and_image_both_fail_text_still_sent() -> None:
    adapter = FakeWechatAdapter(
        {"sticker": GatewayError("微信桥未连接"), "image": False}
    )
    service = make_service(adapter)

    run(
        service,
        (Attachment(kind="sticker", name="摸鱼", path="replies/x.gif"),),
    )

    assert adapter.media_calls == [
        ("sticker", "replies/x.gif", "摸鱼"),
        ("image", "replies/x.gif", "摸鱼"),
    ]
    assert adapter.sent == [TEXT]
