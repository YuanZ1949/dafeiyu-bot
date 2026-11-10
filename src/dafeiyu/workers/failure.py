from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Hashable
from typing import Any

from dafeiyu.brain.client import BrainError
from dafeiyu.models import ChatType

log = logging.getLogger(__name__)

# 配额/网络这类外部失败，短时间内重试只会继续撞墙，默认冷却半小时。
FAILURE_BACKOFF_SECONDS = 1800.0
# 429 多是瞬时限流：半小时太长，只让本会话歇五分钟。
RATE_LIMIT_BACKOFF_SECONDS = 300.0
# 配额是账号级的：某个会话撞上以后，别的会话继续试也只会撞同一堵墙。
# 全局暂停一小时，到期自动再试；成功一次即解除。
QUOTA_PAUSE_SECONDS = 3600.0
QUOTA_KEY = "__quota__"
# 有的 provider 只在 429 里写 rate limit，不写 quota。单次 429 可能是瞬时抖动，
# 但 10 分钟内累计三次说明是账号级限流，按配额同样处理，别让 12 个会话各撞一遍。
# 用「窗口内累计」而不是「连续」：多会话轮流撞、中间夹着成功，同样算限流。
RATE_LIMIT_THRESHOLD = 3
RATE_LIMIT_WINDOW_SECONDS = 600.0


class MemoryPaused(Exception):
    """配额熔断生效中：手动触发也不要再撞墙。"""


#: 旁路任务（摘要/印象/观察/反思）最近的失败事件：只留内存，供健康快照告警。
#: 不进库、不带消息正文——它只回答「最近失败得是不是太频繁」。
_FAILURE_EVENTS: list[tuple[float, str]] = []
_FAILURE_RETENTION_SECONDS = 24 * 3600


def note_memory_failure(label: str, exc: BaseException) -> None:
    """记一次旁路任务失败（时间 + 标签），供告警读最近窗口的计数。"""
    now = time.time()
    _FAILURE_EVENTS.append((now, f"{label}: {type(exc).__name__}"))
    cutoff = now - _FAILURE_RETENTION_SECONDS
    while _FAILURE_EVENTS and _FAILURE_EVENTS[0][0] < cutoff:
        _FAILURE_EVENTS.pop(0)


def recent_failure_count(window_seconds: float = 1800.0) -> int:
    """最近 ``window_seconds`` 内旁路失败的次数。"""
    cutoff = time.time() - float(window_seconds)
    return sum(1 for stamp, _label in _FAILURE_EVENTS if stamp >= cutoff)


def recent_failure_labels(limit: int = 3) -> list[str]:
    """最近几次失败的标签（去重、最多 ``limit`` 条），用于告警详情。"""
    labels: list[str] = []
    for _stamp, label in reversed(_FAILURE_EVENTS):
        if label in labels:
            continue
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def reset_memory_failures() -> None:
    """清空失败事件（测试与面板手动复位用）。"""
    _FAILURE_EVENTS.clear()


def log_memory_failure(
    logger: logging.Logger,
    label: str,
    exc: BaseException,
    chat_type: ChatType,
    chat_id: str,
) -> None:
    """记忆类任务失败时的日志分级。

    外部失败（BrainError：配额、限流、服务不可达）只留一行 warning，
    避免每轮刷整段 traceback 把真正的 bug 埋掉；其它异常按内部错误记全栈。
    必须在 except 块内调用，logger.exception 依赖当前异常上下文。
    """
    note_memory_failure(label, exc)
    if isinstance(exc, BrainError):
        logger.warning(
            "%s失败（外部）：%s/%s %s", label, chat_type.value, chat_id, exc
        )
    else:
        logger.exception(
            "%s失败（内部）：%s/%s %s", label, chat_type.value, chat_id, exc
        )


# 各 provider 的「额度用完」文案：opencode 免费档叫 free-limit / free usage
# limit reached，别家叫 quota exceeded / insufficient_quota。只查 "quota" 会漏掉
# 前者，于是账号级耗尽被当成单会话 429 短歇，每 10 分钟重试一次撞两小时。
QUOTA_HINTS = ("quota", "free-limit", "free limit", "usage limit", "insufficient credit")

# opencode 的 session 绑定创建时的 project 目录；项目被移动/复制后，旧 session
# 会在一个空目录里找 agent，必然报 Agent not found。清掉映射重建 session 即可恢复。
AGENT_MISSING_HINTS = ("agent not found",)


def is_quota_error(exc: BaseException | None) -> bool:
    """配额耗尽的错误文本来自各 provider 的 BrainError，统一按关键词识别。"""
    if exc is None:
        return False
    text = str(exc).casefold()
    return any(hint in text for hint in QUOTA_HINTS)


def is_rate_limit_error(exc: BaseException | None) -> bool:
    """429 / rate limit 的识别：优先看 HTTP 状态码，拿不到才退回文案。"""
    if exc is None:
        return False
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    try:
        if status is not None and int(status) == 429:
            return True
    except (TypeError, ValueError):
        pass
    text = str(exc).casefold()
    return "429" in text or "rate limit" in text or "too many requests" in text


def is_agent_missing_error(exc: BaseException | None) -> bool:
    """opencode 旧 session 指向已不存在的项目目录时，会报 Agent not found。"""
    if exc is None:
        return False
    text = str(exc).casefold()
    return any(hint in text for hint in AGENT_MISSING_HINTS)


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
    clear_backoff(key)``；传 Store 即可让退避跨进程重启存活。429 事件窗口
    用可选的 ``load_rate_limits() / save_rate_limits(events, updated) /
    clear_rate_limit()``，缺这几个方法时只在内存里数。
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
        self._rate_limit_events: list[float] = []
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
            self._rate_limit_events = self._load_events()

    def _load_events(self) -> list[float]:
        """重启后接着数：只保留窗口内的 429 事件，过期的一并清掉。"""
        loader = getattr(self._persistence, "load_rate_limits", None)
        if loader is None:
            return []
        try:
            events = list(loader())
        except (TypeError, ValueError):
            return []
        now = self._wall_clock()
        # 墙钟被往回调时，事件时间会落在「未来」；夹到 now，否则它会一直不过期。
        fresh = [
            min(float(item), now)
            for item in events
            if now - float(item) <= self._rate_limit_window
        ]
        if len(fresh) != len(events):
            self._persist_events(fresh)
        return fresh

    def _persist_events(self, events: list[float]) -> None:
        saver = getattr(self._persistence, "save_rate_limits", None)
        if saver is not None:
            saver(events, self._wall_clock())

    def _drop_events(self) -> None:
        if not self._rate_limit_events:
            return
        self._rate_limit_events = []
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

    def reset(self) -> None:
        """丢弃内存里的全部退避（面板手动解除熔断用）。

        只清内存，不动持久化：库里的记录由控制口统一清理，避免同一个动作
        在两个地方重复写。配额暂停期间 worker 不会回查数据库，所以不清
        内存的话，熔断要等自然到期才真正恢复。
        """
        self._until.clear()

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
            self._drop_events()
            self._pause_quota()
            return
        if is_rate_limit_error(exc):
            now = self._wall_clock()
            self._rate_limit_events = [
                min(at, now)
                for at in self._rate_limit_events
                if now - at <= self._rate_limit_window
            ]
            self._rate_limit_events.append(now)
            if len(self._rate_limit_events) >= self._rate_limit_threshold:
                # 窗口内累计触顶：当成账号级限流，升级为全局暂停，不再让别的会话挨个撞。
                log.warning(
                    "%d 分钟内第 %d 次 429（阈值 %d），记忆任务暂停 %.0f 分钟：%s",
                    self._rate_limit_window / 60,
                    len(self._rate_limit_events),
                    self._rate_limit_threshold,
                    self._quota_seconds / 60,
                    exc,
                )
                self._drop_events()
                self._pause_quota()
                return
            self._persist_events(self._rate_limit_events)
            # 429 是瞬时限流，本会话只短歇，不跟着吃满半小时。
            self._remember(key, self._monotonic() + self._rate_limit_seconds)
            return
        self._remember(key, self._monotonic() + self._seconds)

    def mark_ok(self, key: Hashable) -> None:
        # 成功不清 429 事件：别的会话刚撞过的限流不会因为这一次成功就消失。
        self._forget(key)
        if key != QUOTA_KEY:
            self._forget(QUOTA_KEY)
