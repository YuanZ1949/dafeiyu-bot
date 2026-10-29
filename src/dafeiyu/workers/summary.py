from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from dafeiyu.brain.client import BrainError
from dafeiyu.brain.tokens import resolve_usage
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import SHANGHAI, now_shanghai

from .failure import (
    FAILURE_BACKOFF_SECONDS,
    FailureBackoff,
    MemoryPaused,
    log_memory_failure,
)

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM = """你是群聊记录整理助手。请把给定日期的一段群聊整理成不超过 200 字的摘要：
- 记下主要话题、达成的结论、出现的梗和外号
- 用第三人称陈述，不评论、不发挥
- 只输出摘要正文，不要标题、前缀或解释
"""

MAX_SUMMARY_CHARS = 200
MAX_SOURCE_LINES = 400
# 回补窗口：worker 每轮除了昨天，还会捡最近 7 天里漏掉的 (会话, 日期)。
BACKFILL_DAYS = 7
# 每轮最多补几个槽位：一次补太多会顶满当轮预算，剩下的留给下一轮。
MAX_BACKFILL_PER_RUN = 3

ROTATION_SYSTEM = """你是群聊记录整理助手。请把下面这段对话压缩成不超过 200 字的交接摘要，供换 session 后继续聊天：
- 记下正在聊的话题、已达成的结论、称呼与梗
- 用第三人称陈述，不评论、不发挥
- 只输出摘要正文，不要标题、前缀或解释
"""

MERGE_SYSTEM = """你是群聊记录整理助手。下面是同一段聊天记录拆成两半后各自生成的摘要，请合并成不超过 200 字的最终摘要：
- 保留主要话题、结论、梗和外号，去掉重复
- 用第三人称陈述，不评论、不发挥
- 只输出摘要正文，不要标题、前缀或解释
"""

# 只有本地等 opencode 回包超时才值得切半重试；provider 自己报的 timeout
# （如 provider.timeout）说明上游已明确失败，重试只会重复撞墙，交给退避。
TIMEOUT_HINT = "OpenCode 超时"


def _day_bounds(date: str) -> tuple[int, int]:
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def _is_timeout_error(exc: BaseException) -> bool:
    return isinstance(exc, BrainError) and TIMEOUT_HINT in str(exc)


async def _complete_once(
    brain, system: str, prompt: str, max_tokens: int = 600
) -> tuple[str, int, int, bool]:
    """一次模型调用，顺带把 token 用量折算好。"""
    detailed = getattr(brain, "complete_detailed", None)
    if detailed is not None:
        raw, prompt_tokens, completion_tokens = await detailed(
            system, [{"role": "user", "content": prompt}], max_tokens=max_tokens
        )
    else:
        raw = await brain.complete(
            system, [{"role": "user", "content": prompt}], max_tokens=max_tokens
        )
        prompt_tokens = completion_tokens = 0
    prompt_tokens, completion_tokens, estimated = resolve_usage(
        f"{system}\n{prompt}", raw or "", prompt_tokens, completion_tokens
    )
    return (raw or ""), prompt_tokens, completion_tokens, estimated


async def _summarize_lines(
    brain,
    system: str,
    merge_system: str,
    header: str,
    lines: list[str],
) -> tuple[str, int, int, bool]:
    """整段摘要；本地超时且有多行时切成两半各摘一次再合并。只切一层。"""
    prompt = f"{header}\n" + "\n".join(lines)
    try:
        return await _complete_once(brain, system, prompt)
    except Exception as exc:  # 只有超时才走分段，其余原样抛出
        if not _is_timeout_error(exc) or len(lines) < 2:
            raise

    mid = len(lines) // 2
    text_parts: list[str] = []
    prompt_tokens = completion_tokens = 0
    estimated = False
    for chunk in (lines[:mid], lines[mid:]):
        raw, used_in, used_out, est = await _complete_once(
            brain, system, f"{header}\n" + "\n".join(chunk)
        )
        text_parts.append(raw.strip())
        prompt_tokens += used_in
        completion_tokens += used_out
        estimated = estimated or est

    merge_prompt = f"第一段摘要：\n{text_parts[0]}\n\n第二段摘要：\n{text_parts[1]}"
    try:
        raw, used_in, used_out, est = await _complete_once(
            brain, merge_system, merge_prompt
        )
    except Exception as exc:  # noqa: BLE001 - 合并失败就退化成拼接，保住已摘的部分
        logger.warning("摘要合并失败，退回拼接两段：%s", exc)
        raw = "\n".join(part for part in text_parts if part)
    else:
        prompt_tokens += used_in
        completion_tokens += used_out
        estimated = estimated or est
    return raw, prompt_tokens, completion_tokens, estimated


async def summarize_chat(
    store: Store,
    brain,
    chat_type: ChatType,
    chat_id: int,
    date: str,
    on_error: Callable[[BaseException], None] | None = None,
) -> str | None:
    """返回摘要正文；当天没有消息返回 ""，模型调用失败返回 None。"""
    start_ts, end_ts = _day_bounds(date)
    messages = store.messages_between(chat_type, chat_id, start_ts, end_ts)
    if not messages:
        return ""

    lines = [m.to_prompt_line() for m in messages][-MAX_SOURCE_LINES:]
    header = f"日期：{date}\n以下是当天的群聊记录："
    try:
        raw, prompt_tokens, completion_tokens, estimated = await _summarize_lines(
            brain, SUMMARY_SYSTEM, MERGE_SYSTEM, header, lines
        )
    except Exception as exc:  # noqa: BLE001 - 摘要失败不影响主链路
        log_memory_failure(logger, f"生成摘要 {date}", exc, chat_type, chat_id)
        if on_error is not None:
            on_error(exc)
        return None

    store.record_usage(
        chat_type, chat_id, "summary", prompt_tokens, completion_tokens, estimated
    )
    text = (raw or "").strip()[:MAX_SUMMARY_CHARS]
    if not text:
        return ""
    store.upsert_summary(chat_type, chat_id, date, text)
    return text


async def summarize_for_rotation(
    brain,
    store: Store,
    chat_type: ChatType,
    chat_id: int,
    messages: list[Message],
) -> str:
    """换 session 前的一次性交接摘要；失败返回空串，调用方据此放弃轮换。"""
    if not messages:
        return ""
    lines = [m.to_prompt_line() for m in messages][-MAX_SOURCE_LINES:]
    header = "以下是需要交接的对话："
    try:
        raw, prompt_tokens, completion_tokens, estimated = await _summarize_lines(
            brain, ROTATION_SYSTEM, MERGE_SYSTEM, header, lines
        )
    except Exception as exc:  # noqa: BLE001 - 摘要失败不影响主链路
        log_memory_failure(logger, "生成轮换摘要", exc, chat_type, chat_id)
        return ""

    store.record_usage(
        chat_type, chat_id, "summary", prompt_tokens, completion_tokens, estimated
    )
    text = (raw or "").strip()[:MAX_SUMMARY_CHARS]
    if not text:
        return ""
    store.add_session_summary(chat_type, chat_id, text)
    return text


class SummaryScheduler:
    def __init__(
        self,
        store: Store,
        brain,
        clock: Callable[[], datetime] = now_shanghai,
        hour: int = 4,
        brain_getter: Callable[[], Any] | None = None,
        backoff_seconds: float = FAILURE_BACKOFF_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        backoff: FailureBackoff | None = None,
    ) -> None:
        self._store = store
        self._brain = brain
        self._clock = clock
        self._hour = hour
        # dashboard 换脑只更新 Service.brain，拿不到 scheduler 手里的引用；
        # 有 getter 就每轮现取，避免摘要一直用旧 provider 的 client。
        self._brain_getter = brain_getter
        # 失败后按 (会话, 日期) 退避，避免配额耗尽时每轮都重试同一份摘要。
        # 与印象抽取共享同一个实例时，配额熔断才能一次管住两条旁路。
        self._backoff = backoff or FailureBackoff(backoff_seconds, monotonic)

    def _current_brain(self):
        return self._brain_getter() if self._brain_getter is not None else self._brain

    @property
    def paused(self) -> bool:
        return self._backoff.is_paused()

    async def summarize_now(
        self, chat_type: ChatType, chat_id: int, date: str
    ) -> str | None:
        """面板手动触发：跳过「已存在」与按会话退避，但仍尊重全局配额熔断。"""
        if self._backoff.is_paused():
            raise MemoryPaused("配额暂停中，稍后再试")
        key = (chat_type, chat_id, date)
        text = await summarize_chat(
            self._store,
            self._current_brain(),
            chat_type,
            chat_id,
            date,
            on_error=lambda exc: self._backoff.mark_failed(key, exc),
        )
        if text is None:
            return None
        self._backoff.mark_ok(key)
        return text

    async def run_pending(self) -> int:
        now = self._clock()
        if now.hour < self._hour:
            return 0
        if self._backoff.is_paused():
            return 0

        # 只补「昨天」的话，某天失败后目标日期就永久前移，那一整天再也没人补。
        # 这里把最近 7 天的缺口一起捞出来（截止昨天，今天本来就不该有摘要）。
        until = (now.date() - timedelta(days=1)).isoformat()
        slots = self._store.pending_summary_slots(BACKFILL_DAYS, until=until)
        created = 0
        for slot in slots:
            if created >= MAX_BACKFILL_PER_RUN or self._backoff.is_paused():
                break
            key = (slot.chat_type, slot.chat_id, slot.date)
            if self._store.get_summary(slot.chat_type, slot.chat_id, slot.date):
                self._backoff.mark_ok(key)
                continue
            if self._backoff.should_skip(key):
                continue
            text = await summarize_chat(
                self._store,
                self._current_brain(),
                slot.chat_type,
                slot.chat_id,
                slot.date,
                on_error=lambda exc, key=key: self._backoff.mark_failed(key, exc),
            )
            if text is None:
                continue
            self._backoff.mark_ok(key)
            if text:
                created += 1
        return created


async def summary_loop(
    scheduler: Any,
    interval: float = 600,
    stop: asyncio.Event | None = None,
) -> None:
    """后台长期任务：周期性调用 scheduler.run_pending()，直到 stop 被置位。"""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            await scheduler.run_pending()
        except Exception:  # noqa: BLE001 - 摘要失败不能拖垮主循环
            logger.exception("摘要调度执行失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
