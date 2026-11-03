from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .gateway.base import Adapter
from .models import ChatType
from .timeutil import SHANGHAI

log = logging.getLogger(__name__)

KEEPALIVE_TEXT = "（保活探针）"
# 失败后最多 30 秒就重试，别等满一个 interval。
RETRY_SECONDS = 30.0


class Keepalive:
    """定时给自己发一条私聊，保持 QQ 会话活跃并暴露发送链路故障。

    只发给机器人自己的 QQ 号：群成员和用户都看不到，不会打扰任何人。
    半离线（isOffline=true）时发送仍可能返回成功，这种情况由 supervisor
    的 NapCat 登录态探测负责告警，这里只管「发送动作是否失败」。
    """

    def __init__(
        self,
        adapter: Adapter,
        bot_qq: str,
        interval_seconds: float,
        *,
        text: str = KEEPALIVE_TEXT,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.adapter = adapter
        self.bot_qq = str(bot_qq)
        self.interval_seconds = float(interval_seconds)
        self.text = text
        self._clock = clock or time.time
        self._ok: bool | None = None
        self._last_at: float | None = None
        self._last_error = ""

    async def probe(self) -> bool:
        """发一次保活消息；失败只记录，不向调用方抛异常。"""
        try:
            await self.adapter.send(ChatType.PRIVATE, self.bot_qq, self.text)
        except Exception as exc:  # noqa: BLE001 - 保活失败不能带走 bot
            self._ok = False
            self._last_at = self._clock()
            self._last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("保活消息发送失败：%s", self._last_error)
            return False
        self._ok = True
        self._last_at = self._clock()
        self._last_error = ""
        log.info("保活消息已发送 -> 自己(%s)", self.bot_qq)
        return True

    async def run(self) -> None:
        # 启动时网关往往还没连上 NapCat：先等连接，别把一次必然的失败写进状态。
        await self._wait_connected()
        await self.probe()
        while True:
            delay = self.interval_seconds
            if self._ok is not True:
                # 刚失败过就早点重试，别让一次断线把 ok=False 钉满整个间隔。
                delay = min(delay, RETRY_SECONDS)
            await asyncio.sleep(delay)
            if not self._connected():
                await self._wait_connected()
            await self.probe()

    def _connected(self) -> bool:
        return bool(getattr(self.adapter, "connected", True))

    async def _wait_connected(self) -> None:
        while not self._connected():
            await asyncio.sleep(0.5)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "interval_seconds": self.interval_seconds,
            "ok": self._ok,
            "last_at": (
                datetime.fromtimestamp(self._last_at, SHANGHAI).isoformat(
                    timespec="seconds"
                )
                if self._last_at is not None
                else None
            ),
            "last_error": self._last_error,
        }


__all__ = ["KEEPALIVE_TEXT", "Keepalive"]
