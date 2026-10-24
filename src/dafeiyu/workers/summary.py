from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType
from dafeiyu.router import SHANGHAI, now_shanghai

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM = """你是群聊记录整理助手。请把给定日期的一段群聊整理成不超过 200 字的摘要：
- 记下主要话题、达成的结论、出现的梗和外号
- 用第三人称陈述，不评论、不发挥
- 只输出摘要正文，不要标题、前缀或解释
"""

MAX_SUMMARY_CHARS = 200
MAX_SOURCE_LINES = 400


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
    try:
        raw = await brain.complete(
            SUMMARY_SYSTEM, [{"role": "user", "content": prompt}], max_tokens=600
        )
    except Exception:  # noqa: BLE001 - 摘要失败不影响主链路
        logger.warning("生成摘要失败：%s/%s %s", chat_type.value, chat_id, date, exc_info=True)
        return ""

    text = (raw or "").strip()[:MAX_SUMMARY_CHARS]
    if not text:
        return ""
    store.upsert_summary(chat_type, chat_id, date, text)
    return text


class SummaryScheduler:
    def __init__(
        self,
        store: Store,
        brain,
        clock: Callable[[], datetime] = now_shanghai,
        hour: int = 4,
    ) -> None:
        self._store = store
        self._brain = brain
        self._clock = clock
        self._hour = hour

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
            if await summarize_chat(self._store, self._brain, chat_type, chat_id, target):
                created += 1
        return created
