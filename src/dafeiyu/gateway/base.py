from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from dafeiyu.models import ChatType, Message


class GatewayError(Exception):
    """IM 网关不可用、调用失败或超时。"""


class Adapter(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, chat_type: ChatType, chat_id: int, text: str) -> None: ...

    def on_message(self, handler: Callable[[Message], Awaitable[None]]) -> None: ...
