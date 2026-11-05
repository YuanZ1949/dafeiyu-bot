"""发送回执校验：用本地库回读自己刚发的消息，把 ``typed`` 升级为 ``observed``。

为什么需要它：hook 后端（以及部分社区实现）会无条件返回 ``ret:0``，UIA 的
回轮询也可能看不到气泡。真正可信的送达证据只有一个——微信自己写进本地库里
的那条「我发的」消息。

约束（协议 §7.6）：

- 只把成功回执升级成 ``observed``，**从不**把未确认改成失败：失败会让客户端
  换新 id 重发，而消息可能已经发出，结果就是重复刷屏；
- 未确认只写日志 + 保留 ``typed``（``error_code`` 为空时补
  ``receipt_unverified``），让运维能看到「发了但没证实」；
- 本地库不可用时立刻返回原回执，不空等。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["ReceiptVerifier"]

#: 回执未在本地库确认时的稳定错误码（仍是 ok=True，仅用于诊断）。
UNVERIFIED = "receipt_unverified"


class ReceiptVerifier:
    """轮询本地库确认「自己刚发的那条」是否落库。

    参数全部可注入，测试不碰真库、不真的 sleep：

    - `source`：``LocalDbSource``（鸭子类型：``available`` +
      ``find_self_message(display_name, text, *, since)``）；
    - `timeout` / `interval`：确认窗口与轮询间隔；
    - `skew_seconds`：本地库 ``create_time`` 与墙钟的容差（默认 2s，防秒级取整）；
    - `clock` / `sleep`：测试用假时钟（``sleep`` 为 async callable）。
    """

    def __init__(
        self,
        source: Any,
        *,
        timeout: float = 8.0,
        interval: float = 0.5,
        skew_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        lookup: Callable[..., Any] | None = None,
        required: bool = True,
    ) -> None:
        self._source = source
        self._timeout = max(0.0, float(timeout))
        self._interval = max(0.001, float(interval))
        self._skew = max(0.0, float(skew_seconds))
        self._clock = clock
        self._sleep = sleep
        self._lookup = lookup
        #: true 时未观察到会标记 ``receipt_unverified``；false 只写日志。
        self._required = bool(required)

    # ------------------------------------------------------------- 公共 API
    async def apply(
        self,
        chat_name: str,
        text: str,
        started_at: float,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """返回（可能升级过的）回执；输入回执不会被原地修改。"""
        if not isinstance(receipt, dict) or not receipt.get("ok"):
            return receipt
        if str(receipt.get("delivered") or "") == "observed":
            return receipt
        if not self._available():
            return receipt

        since = float(started_at) - self._skew
        deadline = self._clock() + self._timeout
        while True:
            found = await self._find(chat_name, text, since)
            if found is not None:
                return self._observed(receipt, found)
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            await self._sleep(min(self._interval, remaining))

        log.warning(
            "发送回执未能在本地库确认（%s，%d 秒内）：%r",
            chat_name,
            int(self._timeout),
            str(text)[:40],
        )
        out = dict(receipt)
        out["delivered"] = "typed"
        if self._required and not out.get("error_code"):
            out["error_code"] = UNVERIFIED
        return out

    # ------------------------------------------------------------- 内部
    @staticmethod
    def _observed(
        receipt: dict[str, Any], found: dict[str, Any]
    ) -> dict[str, Any]:
        message_id = receipt.get("message_id")
        if not message_id:
            ts = found.get("ts")
            message_id = f"localdb:{ts}" if ts is not None else None
        return {
            "ok": True,
            "error_code": None,
            "delivered": "observed",
            "message_id": message_id,
        }

    async def _find(
        self, chat_name: str, text: str, since: float
    ) -> dict[str, Any] | None:
        lookup = self._lookup
        try:
            if lookup is not None:
                result = await asyncio.to_thread(lookup, chat_name, text, since)
            else:
                result = await asyncio.to_thread(
                    self._source.find_self_message,
                    chat_name,
                    text,
                    since=since,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 回读失败只代表「未确认」
            log.debug("本地库回读失败（%s）：%s", chat_name, exc)
            return None
        return result if isinstance(result, dict) else None

    def _available(self) -> bool:
        try:
            return bool(getattr(self._source, "available", False))
        except Exception:  # noqa: BLE001 - 状态读取失败按不可用处理
            return False
