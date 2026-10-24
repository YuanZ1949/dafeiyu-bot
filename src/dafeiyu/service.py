from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from .brain.client import BrainError, BrainReply
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
FORGET_REPLY = "记忆清空了，本鱼失忆中……🫧"
BRAIN_ERROR_REPLY = "本鱼刚刚和大脑断线了…你过一会儿再问我？🐋"


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

    def set_brain(self, brain: Any) -> Any:
        """原子换脑；返回旧实例，由调用方决定何时关闭。

        进行中的消息在入口处已取到旧引用，会继续跑完，不会被打断。
        """
        previous = self.brain
        self.brain = brain
        worker = self.impressions
        if worker is not None and hasattr(worker, "brain"):
            worker.brain = brain
        return previous

    def _lock(self, chat_type: ChatType, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault((chat_type, chat_id), asyncio.Lock())

    async def handle(self, msg: Message) -> None:
        # 机器人自己的消息也要先落库（作为 assistant 轮次），但不触发任何回复。
        if not self.store.add_message(msg):
            return
        if msg.is_bot:
            return

        # 印象抽取按消息条数触发，与是否回复无关，必须在早退分支之前调度。
        self._schedule_impressions(msg)

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

    async def _ask_brain(self, msg: Message) -> BrainReply:
        """OpenCode 模式走会话接口；本地模式仍走原来的上下文拼装。"""
        responder = getattr(self.brain, "reply", None)
        if callable(responder):
            # 一个群共用一个 OpenCode session，必须让模型看到是谁在说话，
            # 否则多人对话会糊成一团。私聊也保留昵称，语气更自然。
            prompt = f"{msg.nickname}：{msg.content}"
            return await responder(msg.chat_type, msg.chat_id, prompt)

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
            upto_message_id=msg.message_id,
        )
        return await self.brain.decide(ctx.system, ctx.messages)

    async def _reply(self, msg: Message) -> None:
        try:
            reply = await self._ask_brain(msg)
        except BrainError as exc:
            # 失败也占一次调用额度，避免异常时无限重试烧钱。
            self.store.record_usage(msg.chat_type, msg.chat_id, "error", 0, 0)
            log.warning("模型调用失败 %s/%s: %s", msg.chat_id, msg.message_id, exc)
            await self.adapter.send(msg.chat_type, msg.chat_id, BRAIN_ERROR_REPLY)
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

        sent: list[str] = []
        try:
            for chunk in chunks:
                await self.adapter.send(msg.chat_type, msg.chat_id, chunk)
                sent.append(chunk)
        except Exception as exc:  # 网络抖动不应拖垮整条链路
            log.warning("发送失败 %s/%s: %s", msg.chat_id, msg.message_id, exc)
        else:
            self.router.note_bot_spoke(msg.chat_type, msg.chat_id)
        finally:
            # 即使中途失败，已经发出去的分片也要进入历史，避免上下文断裂。
            self._store_bot_chunks(msg, sent)

    def _store_bot_chunks(self, msg: Message, chunks: list[str]) -> None:
        now_ts = int(now_shanghai().timestamp())
        for index, chunk in enumerate(chunks):
            self.store.add_message(
                Message(
                    chat_type=msg.chat_type,
                    chat_id=msg.chat_id,
                    user_id=self.settings.bot_qq,
                    nickname=self.settings.persona.name,
                    content=chunk,
                    message_id=f"{msg.message_id}:bot:{index}",
                    is_bot=True,
                    timestamp=now_ts,
                )
            )

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
        calls, tokens = self.store.usage_today(msg.chat_type, msg.chat_id, today)
        return (
            calls < self.settings.budget.daily_calls
            and tokens < self.settings.budget.daily_tokens
        )

    async def _forget_chat(self, msg: Message) -> None:
        self.store.forget_chat(msg.chat_type, msg.chat_id)
        forget = getattr(self.brain, "forget", None)
        if forget is not None:
            result = forget(msg.chat_type, msg.chat_id)
            if inspect.isawaitable(result):
                await result
        await self.adapter.send(msg.chat_type, msg.chat_id, FORGET_REPLY)

    async def _run_command(self, msg: Message, name: str) -> bool:
        cmd = name.casefold()
        if msg.chat_type is ChatType.GROUP:
            if cmd not in ("闭嘴", "shutup", "醒来", "wake", "forget"):
                return False
            if msg.user_id not in self.settings.groups.admins:
                log.info(
                    "非管理员尝试执行群指令 %s/%s: user=%s cmd=%s",
                    msg.chat_id,
                    msg.message_id,
                    msg.user_id,
                    cmd,
                )
                return True
            if cmd == "forget":
                await self._forget_chat(msg)
                return True
            if cmd in ("闭嘴", "shutup"):
                self.router.quiet(msg.chat_type, msg.chat_id, SHUTUP_MINUTES)
            else:
                self.router.wake(msg.chat_type, msg.chat_id)
            return True

        if cmd == "stats":
            today = now_shanghai().date().isoformat()
            calls, tokens = self.store.usage_today(msg.chat_type, msg.chat_id, today)
            await self.adapter.send(
                msg.chat_type, msg.chat_id, f"今天被叫了 {calls} 次，吃了 {tokens} 个 token。🐋"
            )
            return True
        if cmd == "forget":
            await self._forget_chat(msg)
            return True
        return False
