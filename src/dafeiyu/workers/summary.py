from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from dafeiyu.brain.client import BrainError
from dafeiyu.brain.tokens import estimate_tokens, resolve_usage
from dafeiyu.memory.store import Store
from dafeiyu.memory.summary_text import clean_summary, clip_summary
from dafeiyu.models import ChatType, Message, channel_of
from dafeiyu.timeutil import SHANGHAI, now_shanghai

from .failure import (
    FAILURE_BACKOFF_SECONDS,
    FailureBackoff,
    MemoryPaused,
    log_memory_failure,
)

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM_TEMPLATE = """你是群聊记录整理助手。请把给定日期的一段群聊整理成不超过 {limit} 字的摘要：
- 记下主要话题、达成的结论、出现的梗和外号
- 用第三人称陈述，不评论、不发挥
- 只输出摘要正文，不要标题、前缀或解释
"""

MAX_SUMMARY_CHARS = 200
# 当天消息越多，摘要预算越大；闲群不必写满，忙群也不该被 200 字压平。
SUMMARY_BUDGET_QUIET = 150
SUMMARY_BUDGET_NORMAL = 200
SUMMARY_BUDGET_BUSY = 300
QUIET_DAY_MESSAGES = 30
BUSY_DAY_MESSAGES = 200
MAX_SOURCE_LINES = 400
# 单次请求的经验预算：整段超时就按它切成若干块分别再摘，块内还可以继续细分。
MAX_CHUNK_TOKENS = 1200
# 递归深度上限：切两层还超时就放弃，避免在小模型上无限重试烧配额。
MAX_SPLIT_DEPTH = 2
# 回补窗口：worker 每轮除了昨天，还会捡最近 7 天里漏掉的 (会话, 日期)。
BACKFILL_DAYS = 14
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

WEEKLY_MAX_CHARS = 300
# 周一生成失败时下一轮还能补：回补最近 2 个完整周，不只在周一跑。
WEEKLY_BACKFILL_WEEKS = 2

WEEKLY_SYSTEM = """你是群聊记录整理助手。下面是某个群一周的每日摘要，请归纳成不超过 300 字的周汇总：
- 按话题、事件、人物关系归纳，不要逐日复述
- 保留结论、梗和外号
- 信息不足时宁可短，不要编造
- 用第三人称陈述，只输出正文，不要标题、前缀或解释
"""

# 只有本地等 opencode 回包超时才值得切半重试；provider 自己报的 timeout
# （如 provider.timeout）说明上游已明确失败，重试只会重复撞墙，交给退避。
TIMEOUT_HINT = "OpenCode 超时"

def _summary_budget(message_count: int) -> int:
    """按当天全部消息数给摘要字数：≤30 条 150 字，31–200 条 200 字，更多 300 字。"""
    if message_count <= QUIET_DAY_MESSAGES:
        return SUMMARY_BUDGET_QUIET
    if message_count <= BUSY_DAY_MESSAGES:
        return SUMMARY_BUDGET_NORMAL
    return SUMMARY_BUDGET_BUSY


def _clip_summary(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """收敛模型输出：先清 agent 元文本，再按句末断点截断并补「…」。"""
    return clip_summary(clean_summary(text), limit)


def _day_bounds(date: str) -> tuple[int, int]:
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def week_start_of(day: date) -> date:
    """该日期所在自然周的周一（Asia/Shanghai 的日历日）。"""
    return day - timedelta(days=day.weekday())


def completed_weeks(now: date, count: int = WEEKLY_BACKFILL_WEEKS) -> list[str]:
    """最近 count 个「已结束」的周，返回周一日期，新的在前；当前周不算。"""
    last_monday = week_start_of(now) - timedelta(days=7)
    return [
        (last_monday - timedelta(days=7 * index)).isoformat()
        for index in range(max(1, count))
    ]


def _is_timeout_error(exc: BaseException) -> bool:
    return isinstance(exc, BrainError) and TIMEOUT_HINT in str(exc)


def _reply_target_label(user_id: str) -> str:
    """回复目标已被清理时退化成人可读的 id：按通道加标签，如 QQ123 / 微信ab12。"""
    channel = channel_of(user_id)
    raw = user_id.split(":", 1)[1]
    return f"{'微信' if channel == 'wechat' else 'QQ'}{raw}"


def _summary_line(message: Message, reply_names: dict[str, str] | None = None) -> str:
    """摘要专用的记录行：带上海时刻，方便模型判断先后与间隔。

    被回复的消息还在库里就补「（回复 昵称）」；已清理则退化成带通道标签的 id，
    比丢掉整条回复线索有用。
    """
    stamp = datetime.fromtimestamp(message.timestamp, SHANGHAI).strftime("%H:%M")
    line = f"[{stamp}] {message.nickname}: {message.content}"
    target = ""
    if reply_names and message.reply_to_message_id:
        target = reply_names.get(message.reply_to_message_id, "")
    if not target and message.reply_to_user_id:
        target = _reply_target_label(message.reply_to_user_id)
    if target:
        line += f"（回复 {target}）"
    return line


def _source_lines(
    messages: list[Message], reply_names: dict[str, str] | None = None
) -> list[str]:
    """取最近 MAX_SOURCE_LINES 条；被截掉的部分用一行说明，别静默丢失。"""
    shown = messages[-MAX_SOURCE_LINES:]
    lines = [_summary_line(m, reply_names) for m in shown]
    hidden = len(messages) - len(shown)
    if hidden > 0:
        lines.insert(0, f"（更早还有 {hidden} 条未展开）")
    return lines


def _split_by_tokens(
    lines: list[str], limit: int = MAX_CHUNK_TOKENS
) -> list[list[str]]:
    """按 token 预算贪心切块；单行超预算时也独占一块，至少保证能往下走。"""
    chunks: list[list[str]] = []
    current: list[str] = []
    used = 0
    for line in lines:
        cost = estimate_tokens(line)
        if current and used + cost > limit:
            chunks.append(current)
            current = []
            used = 0
        current.append(line)
        used += cost
    if current:
        chunks.append(current)
    return chunks


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
    depth: int = 0,
) -> tuple[str, int, int, bool]:
    """整段摘要；本地超时就按 token 分块递归再摘，最后合并。

    只在超时且还有细分空间时递归（深度上限 ``MAX_SPLIT_DEPTH``）；
    其他异常、只剩一行、深度用尽都原样抛给调用方记失败。
    """
    prompt = f"{header}\n" + "\n".join(lines)
    try:
        return await _complete_once(brain, system, prompt)
    except Exception as exc:  # 只有超时才走分段，其余原样抛出
        if not _is_timeout_error(exc) or len(lines) < 2 or depth >= MAX_SPLIT_DEPTH:
            raise

    chunks = _split_by_tokens(lines)
    if len(chunks) < 2:
        # 行数少到装不满一个 token 预算，仍按旧逻辑对半切，给超时留一次机会。
        mid = len(lines) // 2
        chunks = [lines[:mid], lines[mid:]]

    text_parts: list[str] = []
    prompt_tokens = completion_tokens = 0
    estimated = False
    for chunk in chunks:
        raw, used_in, used_out, est = await _summarize_lines(
            brain, system, merge_system, header, chunk, depth + 1
        )
        text_parts.append(raw.strip())
        prompt_tokens += used_in
        completion_tokens += used_out
        estimated = estimated or est

    merge_prompt = "\n\n".join(
        f"第{index}段摘要：\n{part}"
        for index, part in enumerate(text_parts, start=1)
    )
    try:
        raw, used_in, used_out, est = await _complete_once(
            brain, merge_system, merge_prompt
        )
    except Exception as exc:  # noqa: BLE001 - 合并失败就退化成拼接，保住已摘的部分
        logger.warning("摘要合并失败，退回拼接各段：%s", exc)
        raw = "\n".join(part for part in text_parts if part)
    else:
        prompt_tokens += used_in
        completion_tokens += used_out
        estimated = estimated or est
    return raw, prompt_tokens, completion_tokens, estimated


def _reply_names(
    store: Store, chat_type: ChatType, chat_id: str, messages: list[Message]
) -> dict[str, str]:
    """当天回复目标的昵称表；没有回复消息时直接跳过查询。"""
    ids = [m.reply_to_message_id for m in messages if m.reply_to_message_id]
    if not ids:
        return {}
    return store.message_nicknames(chat_type, chat_id, ids)


async def summarize_week(
    store: Store,
    brain,
    chat_type: ChatType,
    chat_id: str,
    week_start: str,
    on_error: Callable[[BaseException], None] | None = None,
) -> str | None:
    """把一周内已有的日摘要归纳成周汇总；该周没有日摘要返回 ""，失败返回 None。"""
    start = date.fromisoformat(week_start)
    end = (start + timedelta(days=6)).isoformat()
    rows = store.summaries_between(chat_type, chat_id, week_start, end)
    if not rows:
        return ""
    lines = [f"{row.date[5:]}：{row.content}" for row in rows]
    header = f"周起始：{week_start}\n以下是这一周的每日摘要："
    try:
        raw, prompt_tokens, completion_tokens, estimated = await _complete_once(
            brain, WEEKLY_SYSTEM, f"{header}\n" + "\n".join(lines)
        )
    except Exception as exc:  # noqa: BLE001 - 周汇总失败不影响主链路
        log_memory_failure(logger, f"生成周汇总 {week_start}", exc, chat_type, chat_id)
        if on_error is not None:
            on_error(exc)
        return None

    store.record_usage(
        chat_type, chat_id, "summary", prompt_tokens, completion_tokens, estimated
    )
    text = _clip_summary(raw, WEEKLY_MAX_CHARS)
    if not text:
        return ""
    store.upsert_weekly_summary(chat_type, chat_id, week_start, text)
    return text


async def summarize_chat(
    store: Store,
    brain,
    chat_type: ChatType,
    chat_id: str,
    date: str,
    on_error: Callable[[BaseException], None] | None = None,
) -> str | None:
    """返回摘要正文；当天没有消息返回 ""，模型调用失败返回 None。"""
    start_ts, end_ts = _day_bounds(date)
    messages = store.messages_between(chat_type, chat_id, start_ts, end_ts)
    if not messages:
        return ""

    lines = _source_lines(messages, _reply_names(store, chat_type, chat_id, messages))
    header = f"日期：{date}\n以下是当天的群聊记录："
    # 预算按当天全部消息数算，不受 MAX_SOURCE_LINES 截断影响。
    budget = _summary_budget(len(messages))
    try:
        raw, prompt_tokens, completion_tokens, estimated = await _summarize_lines(
            brain, SUMMARY_SYSTEM_TEMPLATE.format(limit=budget), MERGE_SYSTEM, header, lines
        )
    except Exception as exc:  # noqa: BLE001 - 摘要失败不影响主链路
        log_memory_failure(logger, f"生成摘要 {date}", exc, chat_type, chat_id)
        if on_error is not None:
            on_error(exc)
        return None

    store.record_usage(
        chat_type, chat_id, "summary", prompt_tokens, completion_tokens, estimated
    )
    text = _clip_summary(raw, budget)
    if not text:
        return ""
    store.upsert_summary(chat_type, chat_id, date, text)
    return text


async def summarize_for_rotation(
    brain,
    store: Store,
    chat_type: ChatType,
    chat_id: str,
    messages: list[Message],
) -> str:
    """换 session 前的一次性交接摘要；失败返回空串，调用方据此放弃轮换。"""
    if not messages:
        return ""
    lines = _source_lines(messages)
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
    text = _clip_summary(raw)
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
        self, chat_type: ChatType, chat_id: str, date: str
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

    async def _run_weekly(self, now: datetime, budget: int) -> int:
        """补最近两个完整周的周汇总；与日摘要共享每轮上限和同一个退避实例。"""
        if budget <= 0:
            return 0
        created = 0
        for week_start in completed_weeks(now.date()):
            if created >= budget or self._backoff.is_paused():
                break
            for slot in self._store.pending_weekly_slots([week_start]):
                if created >= budget or self._backoff.is_paused():
                    break
                key = (slot.chat_type, slot.chat_id, week_start)
                if self._store.get_weekly_summary(
                    slot.chat_type, slot.chat_id, week_start
                ):
                    self._backoff.mark_ok(key)
                    continue
                if self._backoff.should_skip(key):
                    continue
                text = await summarize_week(
                    self._store,
                    self._current_brain(),
                    slot.chat_type,
                    slot.chat_id,
                    week_start,
                    on_error=lambda exc, key=key: self._backoff.mark_failed(key, exc),
                )
                if text is None:
                    continue
                self._backoff.mark_ok(key)
                if text:
                    created += 1
        return created

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
        created += await self._run_weekly(
            now, budget=max(0, MAX_BACKFILL_PER_RUN - created)
        )
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
        except Exception:
            logger.exception("摘要调度执行失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
