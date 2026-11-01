"""跨通道共享的测试替身：字符串会话 id + 可注入事件。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from itertools import count
from typing import Any

from dafeiyu.models import ChatType, Message, RecallEvent


class FakeChannelAdapter:
    """微信形态的假适配器：不连网、不碰 UIA，只记录发送与分发事件。

    ``send()`` 返回 ``<epoch>:<seq>``，与桥生成的 occurrence id 同形，
    方便上层按字符串 id 入库；``bot_id`` 固定为 ``wxg:self``。
    """

    def __init__(
        self,
        *,
        bot_id: str = "wxg:self",
        chat_id: str = "wxg:0d5c1f2a3b4c",
        epoch: str = "deadbeef",
    ) -> None:
        self.bot_id = bot_id
        self.chat_id = chat_id
        self.connected = True
        self.degraded = False
        self.last_event_at: float | None = None
        self.last_disconnect_at: float | None = None
        self.last_send_at: float | None = None
        self.last_chat_id: str | None = None
        self.sent: list[tuple[ChatType, str, str]] = []
        self.recalls: list[RecallEvent] = []
        self.started = False
        self.stopped = False
        self._epoch = epoch
        self._seq = count(1)
        self._message_handlers: list[Callable[[Message], Awaitable[None]]] = []
        self._recall_handlers: list[Callable[[RecallEvent], Awaitable[None]]] = []

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

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        self.last_chat_id = chat_id
        self.last_send_at = time.time()
        return f"{self._epoch}:{next(self._seq)}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "degraded": self.degraded,
            "last_event_at": self.last_event_at,
            "last_disconnect_at": self.last_disconnect_at,
            "last_send_at": self.last_send_at,
        }
