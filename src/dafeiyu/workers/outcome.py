from __future__ import annotations

import asyncio
import json
import logging
import time

from dafeiyu.memory.store import Store
from dafeiyu.models import Message

logger = logging.getLogger(__name__)

# 观察窗：自己的话发出去 3 分钟内有没有人接。与 spec §8.1 一致。
OUTCOME_WINDOW_SECONDS = 180
# 单轮最多回算多少条，避免积压时一次锁库太久。
OUTCOME_BATCH = 200


class OutcomeWorker:
    """纯 SQL 回算「我刚才那句有没有人接」，零模型、零进程内定时器。

    重启后从 ``messages`` 与 ``reply_outcomes`` 对账即可继续，不依赖任何
    内存状态；因此只提供同步的 ``run_once``，由 ``outcome_loop`` 周期调用。
    """

    def __init__(
        self,
        store: Store,
        *,
        window_seconds: int = OUTCOME_WINDOW_SECONDS,
        now=time.time,
    ) -> None:
        self.store = store
        self.window_seconds = max(1, int(window_seconds))
        self._now = now

    def run_once(self, limit: int = OUTCOME_BATCH) -> int:
        """给已过观察窗的 bot 发言打标；返回本轮处理条数。"""
        until = int(self._now()) - self.window_seconds
        pending = self.store.bot_messages_pending_outcome(until, limit)
        handled = 0
        for message in pending:
            try:
                self.tag(message)
            except Exception:
                logger.exception(
                    "结果打标失败：%s %s %s",
                    message.chat_type.value,
                    message.chat_id,
                    message.message_id,
                )
                continue
            handled += 1
        return handled

    def tag(self, message: Message) -> None:
        start = int(message.timestamp)
        if start <= 0:
            return
        end = start + self.window_seconds
        rows = self.store.messages_between(
            message.chat_type, message.chat_id, start, end + 1
        )
        others = [
            row
            for row in rows
            if not row.is_bot and row.message_id != message.message_id
        ]
        quoted = any(
            row.reply_to_message_id == message.message_id for row in others
        )
        replied = quoted or any(row.reply_to_bot for row in others)
        engaged = any(row.content.strip() for row in others)
        detail = json.dumps(
            {
                "messages": len(others),
                "quoted": quoted,
                "replied": replied,
                "first_user_id": others[0].user_id if others else "",
            },
            ensure_ascii=False,
        )
        silence = not others
        self.store.upsert_reply_outcome(
            message.chat_type,
            message.chat_id,
            message.message_id,
            start,
            self.window_seconds,
            engaged=engaged,
            quoted=quoted,
            replied=replied,
            silence_after=silence,
            detail=detail,
        )
        if silence:
            self._note_silence(message)

    def _note_silence(self, message: Message) -> None:
        """最近一句自己刚说完就冷场 → 记一句心情；旧的积压不翻旧账。"""
        if int(message.timestamp) < int(self._now()) - 3600:
            return
        latest = self.store.recent_bot_messages_in(
            message.chat_type, message.chat_id, 1
        )
        if not latest or latest[0].message_id != message.message_id:
            return
        self.store.note_bot_mood("刚才那句没人接，有点闷")


async def outcome_loop(
    worker: OutcomeWorker,
    interval: float = 600,
    stop: asyncio.Event | None = None,
) -> None:
    """后台长期任务：周期性回算结果，直到 stop 被置位。"""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            worker.run_once()
        except Exception:
            logger.exception("结果回算失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
