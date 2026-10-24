from __future__ import annotations

import asyncio
import logging
from typing import Any

from .brain.client import BrainError
from .brain.persona import build_system_prompt
from .gateway.base import Adapter
from .memory.context import build_context
from .memory.store import Store
from .models import ChatType, Message
from .router import Router, now_shanghai, parse_command
from .sender import is_technical_question, shape_reply
from .config import Settings

log = logging.getLogger(__name__)

SHUTUP_MINUTES = 60


class Service:
    """把网关、路由、记忆、模型和发送串成一条链路。"""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        brain: Any,
        adapter: Adapter,
        router: Router,
        persona_text: str,
        impressions: Any = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.brain = brain
        self.adapter = adapter
        self.router = router
        self.persona_text = persona_text
        self.impressions = impressions
        self._locks: dict[tuple[ChatType, int], asyncio.Lock] = {}

    async def start(self) -> None:
        self.adapter.on_message(self.handle)
        await self.adapter.start()

    async def stop(self) -> None:
        await self.adapter.stop()
        closer = getattr(self.brain, "aclose", None)
        if closer is not None:
            await closer()

    def _lock(self, chat_type: ChatType, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault((chat_type, chat_id), asyncio.Lock())

    async def handle(self, msg: Message) -> None:
        if msg.is_bot:
            return
        if not self.store.add_message(msg):
            return

        async with self._lock(msg.chat_type, msg.chat_id):
            if msg.reply_to_bot:
                self.router.note_reply(msg.chat_type, msg.chat_id)

            command = parse_command(msg.content)
            if command is not None and await self._run_command(msg, command.name):
                return

            if msg.chat_type is ChatType.GROUP:
                decision = self.router.should_handle(msg)
                if not decision.call_model:
                    log.debug("skip %s/%s: %s", msg.chat_id, msg.message_id, decision.reason)
                    return
            elif not self._private_budget_ok(msg):
                return

            await self._reply(msg)

    async def _reply(self, msg: Message) -> None:
        today = now_shanghai().date().isoformat()
        chat_title = "本群" if msg.chat_type is ChatType.GROUP else msg.nickname
        system = build_system_prompt(
            self.persona_text,
            chat_title,
            today,
            self.settings.persona.max_reply_chars,
            self.settings.persona.max_reply_chars_technical,
        )
        ctx = build_context(
            self.store,
            msg.chat_type,
            msg.chat_id,
            self.settings.memory.window_size,
            self.settings.memory.impression_topk,
            system,
            today,
        )

        try:
            reply = await self.brain.decide(ctx.system, ctx.messages)
        except BrainError as exc:
            log.warning("模型调用失败 %s/%s: %s", msg.chat_id, msg.message_id, exc)
            return

        self.store.record_usage(
            msg.chat_type,
            msg.chat_id,
            "reply",
            reply.prompt_tokens,
            reply.completion_tokens,
        )

        if not reply.speak:
            self.router.note_no_reply(msg.chat_type, msg.chat_id)
            return

        chunks = shape_reply(
            reply.reply,
            self.settings.persona.max_reply_chars,
            self.settings.persona.max_reply_chars_technical,
            is_technical=is_technical_question(msg.content),
        )
        if not chunks:
            self.router.note_no_reply(msg.chat_type, msg.chat_id)
            return

        try:
            for chunk in chunks:
                await self.adapter.send(msg.chat_type, msg.chat_id, chunk)
        except Exception as exc:  # 网络抖动不应拖垮整条链路
            log.warning("发送失败 %s/%s: %s", msg.chat_id, msg.message_id, exc)
            return

        self.router.note_bot_spoke(msg.chat_type, msg.chat_id)
        self._schedule_impressions(msg)

    def _schedule_impressions(self, msg: Message) -> None:
        if self.impressions is None:
            return

        async def run() -> None:
            try:
                await self.impressions.maybe_extract(msg.chat_type, msg.chat_id)
            except Exception:
                log.exception("印象抽取失败 %s/%s", msg.chat_type, msg.chat_id)

        asyncio.create_task(run())

    def _private_budget_ok(self, msg: Message) -> bool:
        today = now_shanghai().date().isoformat()
        calls, _ = self.store.usage_today(msg.chat_type, msg.chat_id, today)
        return calls < self.settings.budget.daily_calls

    async def _run_command(self, msg: Message, name: str) -> bool:
        cmd = name.casefold()
        if msg.chat_type is ChatType.GROUP:
            if cmd in ("闭嘴", "shutup"):
                self.router.quiet(msg.chat_type, msg.chat_id, SHUTUP_MINUTES)
                return True
            if cmd in ("醒来", "wake"):
                self.router.wake(msg.chat_type, msg.chat_id)
                return True
            return False

        if cmd == "stats":
            today = now_shanghai().date().isoformat()
            calls, tokens = self.store.usage_today(msg.chat_type, msg.chat_id, today)
            await self.adapter.send(
                msg.chat_type, msg.chat_id, f"今天被叫了 {calls} 次，吃了 {tokens} 个 token。🐋"
            )
            return True
        if cmd == "forget":
            self.store.forget_chat(msg.chat_type, msg.chat_id)
            await self.adapter.send(msg.chat_type, msg.chat_id, "记忆清空了，本鱼失忆中……🫧")
            return True
        return False
