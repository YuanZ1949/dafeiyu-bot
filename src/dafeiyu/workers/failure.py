from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Hashable

from dafeiyu.brain.client import BrainError
from dafeiyu.models import ChatType

# 配额/网络这类外部失败，短时间内重试只会继续撞墙，默认冷却半小时。
FAILURE_BACKOFF_SECONDS = 1800.0
# 429 多是瞬时限流：半小时太长，只让本会话歇五分钟。
RATE_LIMIT_BACKOFF_SECONDS = 300.0
# 配额是账号级的：某个会话撞上以后，别的会话继续试也只会撞同一堵墙。
# 全局暂停一小时，到期自动再试；成功一次即解除。
QUOTA_PAUSE_SECONDS = 3600.0
QUOTA_KEY = "__quota__"
# 有的 provider 只在 429 里写 rate limit，不写 quota。单次 429 可能是瞬时抖动，
# 连续三次说明是账号级限流，按配额同样处理，别让 12 个会话各撞一遍。
RATE_LIMIT_THRESHOLD = 3
# 「连续」必须带时间窗：跨天残留的计数不该继续累加。
RATE_LIMIT_WINDOW_SECONDS = 600.0


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


def is_rate_limit_error(exc: BaseException | None) -> bool:
    """429 / rate limit 的识别：优先看 HTTP 状态码，拿不到才退回文案。"""
    if exc is None:
        return False
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status == 429:
        return True
    text = str(exc).casefold()
    return "429" in text or "rate limit" in text or "too many requests" in text


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
        rate_limit_threshold: int = RATE_LIMIT_THRESHOLD,
        rate_limit_seconds: float = RATE_LIMIT_BACKOFF_SECONDS,
        rate_limit_window: float = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._seconds = seconds
        self._quota_seconds = quota_seconds
        self._rate_limit_seconds = rate_limit_seconds
        self._rate_limit_window = rate_limit_window
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._persistence = persistence
        self._rate_limit_threshold = max(1, int(rate_limit_threshold))
        self._rate_limit_streak = 0
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
            self._rate_limit_streak = self._load_streak()

    def _load_streak(self) -> int:
        """重启后接着数：只有落在窗口内的旧计数才算「连续」。"""
        loader = getattr(self._persistence, "load_rate_limit", None)
        if loader is None:
            return 0
        try:
            streak, updated = loader()
        except (TypeError, ValueError):
            return 0
        if int(streak) <= 0:
            return 0
        if self._wall_clock() - float(updated) > self._rate_limit_window:
            cleaner = getattr(self._persistence, "clear_rate_limit", None)
            if cleaner is not None:
                cleaner()
            return 0
        return int(streak)

    def _save_streak(self) -> None:
        saver = getattr(self._persistence, "save_rate_limit", None)
        if saver is not None:
            saver(self._rate_limit_streak, self._wall_clock())

    def _drop_streak(self) -> None:
        if self._rate_limit_streak == 0:
            return
        self._rate_limit_streak = 0
        cleaner = getattr(self._persistence, "clear_rate_limit", None)
        if cleaner is not None:
            cleaner()

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

    def _pause_quota(self) -> None:
        self._remember(QUOTA_KEY, self._monotonic() + self._quota_seconds)

    def mark_failed(self, key: Hashable, exc: BaseException | None = None) -> None:
        if is_quota_error(exc):
            self._drop_streak()
            self._pause_quota()
            return
        if is_rate_limit_error(exc):
            self._rate_limit_streak += 1
            self._save_streak()
            if self._rate_limit_streak >= self._rate_limit_threshold:
                # 连续 429：当成账号级限流，升级为全局暂停，不再让别的会话挨个撞。
                self._drop_streak()
                self._pause_quota()
                return
            # 429 是瞬时限流，本会话只短歇，不跟着吃满半小时。
            self._remember(key, self._monotonic() + self._rate_limit_seconds)
            return
        self._drop_streak()
        self._remember(key, self._monotonic() + self._seconds)

    def mark_ok(self, key: Hashable) -> None:
        self._drop_streak()
        self._forget(key)
        if key != QUOTA_KEY:
            self._forget(QUOTA_KEY)
