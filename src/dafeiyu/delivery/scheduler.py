from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from dafeiyu.delivery.window import ChatWindow, due_at, note as note_window, on_flushed
from dafeiyu.models import ChatType, Message

ChatKey = tuple[ChatType, int]


class BatchScheduler:
    """到点就把会话交给 on_flush；同一会话同时最多一个在投。

    store 目前只做透传（Task 11 的 start() 用它恢复未投递会话），
    调度器本身不读库——预算与跳过都由 service 判断。
    """

    def __init__(
        self,
        store,
        cfg,
        on_flush: Callable[[ChatType, int], Awaitable[None]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.on_flush = on_flush
        self.clock = clock
        self._windows: dict[ChatKey, ChatWindow] = {}
        self._inflight: set[ChatKey] = set()
        # 投递期间又来了消息时，靠这个计数判断窗口是不是被换过。
        self._revisions: dict[ChatKey, int] = {}

    def _window(self, chat_type: ChatType, chat_id: int) -> ChatWindow:
        key = (chat_type, chat_id)
        window = self._windows.get(key)
        if window is None:
            window = self._windows[key] = ChatWindow()
        return window

    def _params(self) -> dict[str, float]:
        # Settings 上叫 triggers；单测里传的是 SimpleNamespace(trigger=...)。
        t = getattr(self.cfg, "triggers", None) or self.cfg.trigger
        return {
            "batch_seconds": t.batch_seconds,
            "debounce": t.addressed_debounce_seconds,
            "max_wait": t.addressed_max_wait_seconds,
            "ambient_gap": t.ambient_min_interval_seconds,
        }

    def note(self, msg: Message, *, addressed: bool) -> None:
        key = (msg.chat_type, msg.chat_id)
        note_window(self._window(*key), now=self.clock(), addressed=addressed)
        self._revisions[key] = self._revisions.get(key, 0) + 1

    def tick(self) -> list[ChatKey]:
        now = self.clock()
        params = self._params()
        due: list[ChatKey] = []
        for key, window in self._windows.items():
            if key in self._inflight:
                continue
            at = due_at(window, **params)
            if at is not None and at <= now:
                due.append(key)
        for key in due:
            self._inflight.add(key)
        return sorted(due, key=lambda k: (k[0].value, k[1]))

    def done(self, chat_type: ChatType, chat_id: int) -> None:
        self._inflight.discard((chat_type, chat_id))

    def resume(self, chats) -> None:
        """进程重启后把未投递的会话排到"立刻可发"。"""
        now = self.clock()
        for chat_type, chat_id in chats:
            window = self._window(chat_type, chat_id)
            if window.first_queued_at is None:
                window.first_queued_at = now - 10**9

    def pending_chats(self) -> list[ChatKey]:
        """当前有排队消息的会话（按 chat 键排序），测试与观测用。"""
        return sorted(
            (
                key
                for key, window in self._windows.items()
                if window.first_queued_at is not None
            ),
            key=lambda key: (key[0].value, key[1]),
        )

    def pending_count(self) -> int:
        """排队中的会话数（健康快照用；语义与 pending_chats 一致）。"""
        return len(self.pending_chats())

    def inflight_count(self) -> int:
        """正在投递的会话数。"""
        return len(self._inflight)

    async def drain(self) -> None:
        """同步跑完当前所有到期批次（测试与停机用，不等下一拍）。"""
        while True:
            keys = self.tick()
            if not keys:
                return
            for key in keys:
                await self._flush(*key)

    async def run(self, interval: float = 0.5) -> None:
        while True:
            for key in self.tick():
                asyncio.create_task(self._flush(*key))
            await asyncio.sleep(interval)

    async def _flush(self, chat_type: ChatType, chat_id: int) -> None:
        key = (chat_type, chat_id)
        window = self._windows.get(key)
        if window is None:
            self.done(chat_type, chat_id)
            return
        revision = self._revisions.get(key, 0)
        addressed = window.addressed_first_at is not None
        try:
            await self.on_flush(chat_type, chat_id)
        finally:
            now = self.clock()
            if self._revisions.get(key, 0) == revision:
                on_flushed(window, now=now, addressed=addressed)
            elif not addressed:
                # 投递期间进了新消息：窗口留着，但这次发言仍占用了环境发言名额。
                window.last_ambient_at = now
            self.done(chat_type, chat_id)
