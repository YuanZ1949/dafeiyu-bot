from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import SHANGHAI, now_shanghai

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM = """你是群聊记录整理助手。请把给定日期的一段群聊整理成不超过 200 字的摘要：
- 记下主要话题、达成的结论、出现的梗和外号
- 用第三人称陈述，不评论、不发挥
- 只输出摘要正文，不要标题、前缀或解释
"""

MAX_SUMMARY_CHARS = 200
MAX_SOURCE_LINES = 400

ROTATION_SYSTEM = """你是群聊记录整理助手。请把下面这段对话压缩成不超过 200 字的交接摘要，供换 session 后继续聊天：
- 记下正在聊的话题、已达成的结论、称呼与梗
- 用第三人称陈述，不评论、不发挥
- 只输出摘要正文，不要标题、前缀或解释
"""


def _day_bounds(date: str) -> tuple[int, int]:
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


async def summarize_chat(
    store: Store,
    brain,
    chat_type: ChatType,
    chat_id: int,
    date: str,
) -> str:
    start_ts, end_ts = _day_bounds(date)
    messages = store.messages_between(chat_type, chat_id, start_ts, end_ts)
    if not messages:
        return ""

    lines = [m.to_prompt_line() for m in messages][-MAX_SOURCE_LINES:]
    prompt = f"日期：{date}\n以下是当天的群聊记录：\n" + "\n".join(lines)
    detailed = getattr(brain, "complete_detailed", None)
    try:
        if detailed is not None:
            raw, prompt_tokens, completion_tokens = await detailed(
                SUMMARY_SYSTEM, [{"role": "user", "content": prompt}], max_tokens=600
            )
        else:
            raw = await brain.complete(
                SUMMARY_SYSTEM, [{"role": "user", "content": prompt}], max_tokens=600
            )
            prompt_tokens = completion_tokens = 0
    except Exception:  # noqa: BLE001 - 摘要失败不影响主链路
        logger.warning("生成摘要失败：%s/%s %s", chat_type.value, chat_id, date, exc_info=True)
        return ""

    store.record_usage(chat_type, chat_id, "summary", prompt_tokens, completion_tokens)
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
    prompt = "以下是需要交接的对话：\n" + "\n".join(lines)
    detailed = getattr(brain, "complete_detailed", None)
    try:
        if detailed is not None:
            raw, prompt_tokens, completion_tokens = await detailed(
                ROTATION_SYSTEM, [{"role": "user", "content": prompt}], max_tokens=600
            )
        else:
            raw = await brain.complete(
                ROTATION_SYSTEM, [{"role": "user", "content": prompt}], max_tokens=600
            )
            prompt_tokens = completion_tokens = 0
    except Exception:  # noqa: BLE001 - 摘要失败不影响主链路
        logger.warning(
            "生成轮换摘要失败：%s/%s", chat_type.value, chat_id, exc_info=True
        )
        return ""

    store.record_usage(chat_type, chat_id, "summary", prompt_tokens, completion_tokens)
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
    ) -> None:
        self._store = store
        self._brain = brain
        self._clock = clock
        self._hour = hour
        # dashboard 换脑只更新 Service.brain，拿不到 scheduler 手里的引用；
        # 有 getter 就每轮现取，避免摘要一直用旧 provider 的 client。
        self._brain_getter = brain_getter

    def _current_brain(self):
        return self._brain_getter() if self._brain_getter is not None else self._brain

    async def run_pending(self) -> int:
        now = self._clock()
        if now.hour < self._hour:
            return 0

        target = (now.date() - timedelta(days=1)).isoformat()
        start_ts, end_ts = _day_bounds(target)
        created = 0
        for chat_type, chat_id in self._store.active_chats_between(start_ts, end_ts):
            if self._store.get_summary(chat_type, chat_id, target):
                continue
            if await summarize_chat(
                self._store, self._current_brain(), chat_type, chat_id, target
            ):
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
