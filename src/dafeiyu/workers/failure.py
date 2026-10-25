from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Hashable

from dafeiyu.brain.client import BrainError
from dafeiyu.models import ChatType

# 配额/网络这类外部失败，短时间内重试只会继续撞墙，默认冷却半小时。
FAILURE_BACKOFF_SECONDS = 1800.0
# 配额是账号级的：某个会话撞上以后，别的会话继续试也只会撞同一堵墙。
# 全局暂停一小时，到期自动再试；成功一次即解除。
QUOTA_PAUSE_SECONDS = 3600.0
QUOTA_KEY = "__quota__"


class MemoryPaused(Exception):
    """配额熔断生效中：手动触发也不要再撞墙。"""


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


def is_quota_error(exc: BaseException | None) -> bool:
    """配额耗尽的错误文本来自各 provider 的 BrainError，统一按关键词识别。"""
    if exc is None:
        return False
    return "quota" in str(exc).casefold()


def _normalize_key(key: Hashable) -> Hashable:
    """内存与持久化用同一种 key 形状，避免 ChatType 与 str 的 dict 查找错位。"""
    if isinstance(key, tuple):
        return tuple(_normalize_key(item) for item in key)
    if isinstance(key, ChatType):
        return key.value
    return key


def _encode_key(key: Hashable) -> str:
    # 字符串 key 直接存，库里看得懂；元组用 JSON 数组，读回来再还原成元组。
    if isinstance(key, str):
        return key
    return json.dumps(key, ensure_ascii=False, default=str)


def _decode_key(raw: str) -> Hashable:
    if raw.startswith("["):
        return tuple(json.loads(raw))
    return raw


class FailureBackoff:
    """按 key 记录失败截止时间，窗口内跳过重试；配额错误升级为全局暂停。

    key 由调用方决定（摘要用 (chat_type, chat_id, date)，印象用
    (chat_type, chat_id)）。时间源可注入，便于测试。

    ``persistence`` 需要提供 ``load_backoff() / save_backoff(key, until) /
    clear_backoff(key)``；传 Store 即可让退避跨进程重启存活。
    """

    def __init__(
        self,
        seconds: float = FAILURE_BACKOFF_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        quota_seconds: float = QUOTA_PAUSE_SECONDS,
        persistence: Any | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._seconds = seconds
        self._quota_seconds = quota_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._persistence = persistence
        self._until: dict[Hashable, float] = {}
        if persistence is not None:
            # 库里存的是墙钟时间（跨进程可用），内存里算的是单调时钟（不受校时影响）。
            mono_now = self._monotonic()
            wall_now = self._wall_clock()
            for raw, until in persistence.load_backoff().items():
                try:
                    mono_until = mono_now + (float(until) - wall_now)
                except (ValueError, TypeError):
                    continue
                if mono_until <= mono_now:
                    continue
                self._until[_decode_key(raw)] = mono_until

    def _remember(self, key: Hashable, until: float) -> None:
        key = _normalize_key(key)
        self._until[key] = until
        if self._persistence is not None:
            remaining = until - self._monotonic()
            self._persistence.save_backoff(
                _encode_key(key), self._wall_clock() + remaining
            )

    def _forget(self, key: Hashable) -> None:
        key = _normalize_key(key)
        if self._until.pop(key, None) is not None and self._persistence is not None:
            self._persistence.clear_backoff(_encode_key(key))

    def should_skip(self, key: Hashable) -> bool:
        key = _normalize_key(key)
        if key != QUOTA_KEY and self._active(QUOTA_KEY):
            return True
        return self._active(key)

    def _active(self, key: Hashable) -> bool:
        until = self._until.get(key)
        if until is None:
            return False
        if until <= self._monotonic():
            self._forget(key)
            return False
        return True

    def is_paused(self) -> bool:
        """配额熔断是否生效：生效时所有会话都不该再调模型。"""
        return self._active(QUOTA_KEY)

    def mark_failed(self, key: Hashable, exc: BaseException | None = None) -> None:
        if is_quota_error(exc):
            self._remember(QUOTA_KEY, self._monotonic() + self._quota_seconds)
            return
        self._remember(key, self._monotonic() + self._seconds)

    def mark_ok(self, key: Hashable) -> None:
        self._forget(key)
        if key != QUOTA_KEY:
            self._forget(QUOTA_KEY)
