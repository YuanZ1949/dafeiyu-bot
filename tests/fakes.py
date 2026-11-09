"""跨通道共享的测试替身：字符串会话 id + 可注入事件。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from itertools import count
from typing import Any

from dafeiyu.models import ChatType, Message, NoticeEvent, RecallEvent


class FakeChannelAdapter:
    """微信形态的假适配器：不连网、不碰 UIA，只记录发送与分发事件。

    ``send()`` 返回 ``<epoch>:<seq>``，与桥生成的 occurrence id 同形，
    方便上层按字符串 id 入库；``bot_id`` 固定为 ``wxg:self``。

    能力（消息段/媒体/notice）默认全关：``Service`` 用
    ``callable(getattr(adapter, ...))`` 探测能力，所以打开时才把方法挂到
    **实例**上——类上直接定义会让两条通道都走同一分支，旧用例也会跟着变。
    关闭时行为与只实现三参 ``send`` 的旧替身逐字节一致。
    """

    def __init__(
        self,
        *,
        bot_id: str = "wxg:self",
        chat_id: str = "wxg:0d5c1f2a3b4c",
        epoch: str = "deadbeef",
        segments: bool = False,
        media: bool = False,
        notice: bool = False,
        media_ok: bool = True,
        media_error: Exception | None = None,
        segments_error: Exception | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.chat_id = chat_id
        self.connected = True
        self.degraded = False
        #: 与真实 adapter 一致的显式能力探测；缺失时上层按默认 False 处理。
        self.supports_dedupe_key = False
        self.last_event_at: float | None = None
        self.last_disconnect_at: float | None = None
        self.last_send_at: float | None = None
        self.last_chat_id: str | None = None
        self.sent: list[tuple[ChatType, str, str]] = []
        self.recalls: list[RecallEvent] = []
        # 消息段/媒体投递记录：列表恒存在，能力是否打开只看实例方法。
        self.segments: list[tuple[ChatType, str, list[dict[str, Any]]]] = []
        self.media: list[tuple[ChatType, str, str, str, str]] = []
        self.media_sent: list[tuple[ChatType, str, str, str, str]] = []
        self.media_ok = media_ok
        self.started = False
        self.stopped = False
        self._media_error = media_error
        self._segments_error = segments_error
        self._epoch = epoch
        self._seq = count(1)
        self._message_handlers: list[Callable[[Message], Awaitable[None]]] = []
        self._recall_handlers: list[Callable[[RecallEvent], Awaitable[None]]] = []
        self._notice_handlers: list[Callable[[NoticeEvent], Awaitable[None]]] = []
        if segments:
            self.send_segments = self._send_segments
        if media:
            self.send_media = self._send_media
        if notice:
            self.on_notice = self._on_notice

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self.connected = False

    def on_message(self, handler: Callable[[Message], Awaitable[None]]) -> None:
        self._message_handlers.append(handler)

    def on_recall(self, handler: Callable[[RecallEvent], Awaitable[None]]) -> None:
        self._recall_handlers.append(handler)

    async def emit(self, message: Message) -> None:
        self.last_event_at = time.time()
        for handler in self._message_handlers:
            await handler(message)

    async def emit_recall(self, event: RecallEvent) -> None:
        self.last_event_at = time.time()
        self.recalls.append(event)
        for handler in self._recall_handlers:
            await handler(event)

    def _on_notice(self, handler: Callable[[NoticeEvent], Awaitable[None]]) -> None:
        self._notice_handlers.append(handler)

    async def emit_notice(self, event: NoticeEvent) -> None:
        self.last_event_at = time.time()
        for handler in self._notice_handlers:
            await handler(event)

    @property
    def notice_handlers(self) -> list[Callable[[NoticeEvent], Awaitable[None]]]:
        """已注册的 notice 处理器；能力未打开时恒为空。"""
        return list(self._notice_handlers)

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        self.last_chat_id = chat_id
        self.last_send_at = time.time()
        return f"{self._epoch}:{next(self._seq)}"

    async def _send_segments(
        self, chat_type: ChatType, chat_id: str, segments: list[dict[str, Any]]
    ) -> str:
        """QQ 形态的消息段发送；``segments_error`` 用来模拟 NapCat 拒收。"""
        if self._segments_error is not None:
            raise self._segments_error
        self.segments.append((chat_type, chat_id, segments))
        self.last_send_at = time.time()
        return f"{self._epoch}:{next(self._seq)}"

    async def _send_media(
        self, chat_type: ChatType, chat_id: str, kind: str, path: str, name: str = ""
    ) -> bool:
        """微信形态的媒体发送：``media_ok=False`` 模拟桥回 ``unsupported_media``。"""
        self.media.append((chat_type, chat_id, kind, path, name))
        self.last_send_at = time.time()
        if self._media_error is not None:
            raise self._media_error
        if self.media_ok:
            self.media_sent.append((chat_type, chat_id, kind, path, name))
        return self.media_ok

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "degraded": self.degraded,
            "last_event_at": self.last_event_at,
            "last_disconnect_at": self.last_disconnect_at,
            "last_send_at": self.last_send_at,
        }
