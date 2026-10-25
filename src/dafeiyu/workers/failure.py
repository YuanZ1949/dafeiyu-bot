from __future__ import annotations

import logging
import time
from typing import Any, Callable, Hashable

from dafeiyu.brain.client import BrainError
from dafeiyu.models import ChatType

# 配额/网络这类外部失败，短时间内重试只会继续撞墙，默认冷却半小时。
FAILURE_BACKOFF_SECONDS = 1800.0


def log_memory_failure(
    logger: logging.Logger,
    label: str,
    exc: BaseException,
    chat_type: ChatType,
    chat_id: int,
) -> None:
    """记忆类任务失败时的日志分级。

    外部失败（BrainError：配额、限流、服务不可达）只留一行 warning，
    避免每轮刷整段 traceback 把真正的 bug 埋掉；其它异常按内部错误记全栈。
    必须在 except 块内调用，logger.exception 依赖当前异常上下文。
    """
    if isinstance(exc, BrainError):
        logger.warning(
            "%s失败（外部）：%s/%s %s", label, chat_type.value, chat_id, exc
        )
    else:
        logger.exception(
            "%s失败（内部）：%s/%s %s", label, chat_type.value, chat_id, exc
        )


class FailureBackoff:
    """按 key 记录失败时间，窗口内跳过重试。

    key 由调用方决定（摘要用 (chat_type, chat_id, date)，印象用
    (chat_type, chat_id)）。时间源可注入，便于测试。
    """

    def __init__(
        self,
        seconds: float = FAILURE_BACKOFF_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._monotonic = monotonic
        self._failed_at: dict[Hashable, float] = {}

    def should_skip(self, key: Hashable) -> bool:
        failed_at = self._failed_at.get(key)
        if failed_at is None:
            return False
        if self._monotonic() - failed_at >= self._seconds:
            self._failed_at.pop(key, None)
            return False
        return True

    def mark_failed(self, key: Hashable) -> None:
        self._failed_at[key] = self._monotonic()

    def mark_ok(self, key: Hashable) -> None:
        self._failed_at.pop(key, None)
