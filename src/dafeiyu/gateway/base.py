from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from dafeiyu.models import ChatType, Message


class GatewayError(Exception):
    """IM 网关不可用、调用失败或超时。

    ``error_code`` 是桥/网关给出的可分类失败原因（如 ``not_connected``、
    ``rate_limited``、``timeout``）；调用方据此决定是否重试。为空表示未分类。

    ``not_sent`` 表示传输层能**证明**这条消息没发出去（连接被拒、后端明确
    ``sent:false`` 等）；超时、写到一半断开等结果不明的情况一律为 ``False``，
    调用方据此决定「重试安全」还是「宁可丢一条也不要重复发送」。
    """

    def __init__(
        self,
        message: str = "",
        *,
        error_code: str = "",
        not_sent: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.not_sent = not_sent


class Adapter(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str | None:
        """发一条消息，返回网关给出的真实 message_id；拿不到时返回 None。"""
        ...

    def on_message(self, handler: Callable[[Message], Awaitable[None]]) -> None: ...
