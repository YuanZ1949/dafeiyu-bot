from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from typing import Any

from .brain.client import BrainError, BrainReply
from .brain.persona import build_system_prompt
from .config import InjectionConfig, Settings
from .delivery.scheduler import BatchScheduler
from .gateway.base import Adapter
from .memory.context import build_context, build_injection
from .memory.rotation import apply_rotation, needs_rotation
from .memory.store import Store
from .memory.timeline import build_batch_prompt, build_delivery
from .memory.workspace import Workspace
from .models import ChatType, Message
from .router import Router, now_shanghai, parse_command
from .sender import is_technical_question, shape_reply
from .workers.summary import summarize_for_rotation

log = logging.getLogger(__name__)

SHUTUP_MINUTES = 60
FORGET_REPLY = "记忆清空了，本鱼失忆中……🫧"
BRAIN_ERROR_REPLY = "本鱼刚刚和大脑断线了…你过一会儿再问我？🐋"
TOOL_HINT_REPLY = "本鱼去网上查一下，稍等…"
FOLLOWUP_REPLY = "没查到什么有用的，本鱼先不瞎说了"
# 同一会话 60s 内不重复发「去查一下」；每轮最多一次由 brain 侧保证。
TOOL_HINT_INTERVAL_SECONDS = 60.0
# 轮换摘要的输入窗口（spec §9.2：最近 40 条）。
ROTATION_WINDOW = 40


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
        workspace: Workspace | None = None,
        injection: InjectionConfig | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.brain = brain
        self.adapter = adapter
        self.router = router
        self.persona_text = persona_text
        self.impressions = impressions
        self.workspace = workspace
        self.injection = injection
        self.scheduler: BatchScheduler | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._locks: dict[tuple[ChatType, int], asyncio.Lock] = {}
        self._batch_locks: dict[tuple[ChatType, int], asyncio.Lock] = {}
        # 点名消息的 id（store 不落 at_bot/reply_to_bot），批次据此决定 purpose 与标记。
        self._addressed_ids: set[str] = set()
        # 轮换后的接续文本：成功投递才弹出，失败留到下一批重试。
        self._continuations: dict[tuple[ChatType, int], str] = {}
        self._hint_at: dict[tuple[ChatType, int], float] = {}

    async def start(self) -> None:
        self._start_scheduler()
        self.adapter.on_message(self.handle)
        await self.adapter.start()

    async def stop(self) -> None:
        task = self._scheduler_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._scheduler_task = None
        await self.adapter.stop()
        closer = getattr(self.brain, "aclose", None)
        if closer is not None:
            await closer()

    async def drain(self) -> None:
        """测试与停机用：把当前所有到期批次同步跑完。"""
        if self.scheduler is not None:
            await self.scheduler.drain()

    def _uses_scheduler(self) -> bool:
        """model 模式且大脑有会话接口才走批次；否则回落到 rules 同步路径。"""
        return self.settings.triggers.mode == "model" and callable(
            getattr(self.brain, "reply", None)
        )

    def _ensure_scheduler(self) -> BatchScheduler:
        if self.scheduler is None:
            self.scheduler = BatchScheduler(self.store, self.settings, self._run_batch)
        return self.scheduler

    def _start_scheduler(self) -> None:
        if not self._uses_scheduler():
            return
        scheduler = self._ensure_scheduler()
        now = int(now_shanghai().timestamp())
        # 重启后只补最近 5 分钟的积压，避免把整夜消息一次灌进模型。
        scheduler.resume(self.store.chats_with_pending(since_ts=now - 300))
        if self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(scheduler.run())

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

            if self._uses_scheduler():
                await self._handle_batched(msg)
            else:
                await self._handle_sync(msg)

    async def _handle_sync(self, msg: Message) -> None:
        """rules 模式：规则判定通过就同步等模型（旧行为，回滚用）。"""
        if msg.chat_type is ChatType.GROUP:
            decision = self.router.should_handle(msg)
            if not decision.call_model:
                log.debug("skip %s/%s: %s", msg.chat_id, msg.message_id, decision.reason)
                return
        elif not self._private_budget_ok(msg):
            return

        await self._reply(msg)

    async def _handle_batched(self, msg: Message) -> None:
        """model 模式：只做判定与排队，绝不 await 模型（私聊除外）。"""
        scheduler = self._ensure_scheduler()

        if msg.chat_type is ChatType.PRIVATE:
            # 私聊延迟敏感，不进批次窗口，但仍受双闸预算约束。
            if not self._private_budget_ok(msg):
                return
            await self._reply(msg)
            return

        addressed = self.router.is_addressed(msg)
        reason = self._group_skip_reason(msg, addressed=addressed)
        if reason is not None:
            self.store.mark_skipped(msg.chat_type, msg.chat_id, [msg.message_id], reason)
            log.debug("skip %s/%s: %s", msg.chat_id, msg.message_id, reason)
            return

        if addressed:
            self._addressed_ids.add(msg.message_id)
        scheduler.note(msg, addressed=addressed)

    def _group_skip_reason(self, msg: Message, *, addressed: bool) -> str | None:
        if self.router.is_muted(msg.chat_type, msg.chat_id):
            return "muted"
        if self.router.is_quiet(msg.chat_type, msg.chat_id):
            return "quiet"
        if msg.chat_id in self.settings.groups.blacklist:
            return "blacklist"
        whitelist = self.settings.groups.whitelist
        # 被点名的群一定排批，与 rules 模式「硬触发优先于白名单」一致。
        if not addressed and whitelist and msg.chat_id not in whitelist:
            return "whitelist"
        return None

    async def _run_batch(self, chat_type: ChatType, chat_id: int) -> None:
        """定时器到点：取批 → 预算 → 轮换 → 注入 prompt → 调模型 → 发送/静默 → 记账。

        同一会话串行：调度器的 inflight 已挡住 tick 重复触发，这里的锁再兜一层
        （drain 与 run 并发、或将来从 dashboard 直接调用时）。
        """
        lock = self._batch_locks.setdefault((chat_type, chat_id), asyncio.Lock())
        async with lock:
            await self._run_batch_locked(chat_type, chat_id)

    async def _run_batch_locked(self, chat_type: ChatType, chat_id: int) -> None:
        latest = self.store.recent_messages(chat_type, chat_id, 1)
        if not latest:
            return
        batch = self.store.pending_batch(chat_type, chat_id, latest[0].message_id)
        if not batch:
            return

        ids = [m.message_id for m in batch]
        addressed_ids = [mid for mid in ids if mid in self._addressed_ids]
        addressed = bool(addressed_ids)
        current_id = addressed_ids[-1] if addressed_ids else ids[-1]
        key = (chat_type, chat_id)

        if not self._budget_for(chat_type, chat_id, addressed=addressed):
            self.store.mark_skipped(chat_type, chat_id, ids, "budget")
            self._addressed_ids.difference_update(ids)
            log.debug("批次跳过（预算）%s/%s", chat_id, current_id)
            return

        await self._maybe_rotate(chat_type, chat_id)

        injection = build_injection(
            self.store,
            self.workspace,
            chat_type,
            chat_id,
            self.injection or InjectionConfig(),
            today=now_shanghai().date().isoformat(),
        )
        prompt = build_batch_prompt(
            batch,
            injection,
            continuation=self._continuations.get(key),
            addressed=addressed,
            current_id=current_id,
        )

        hint = self._tool_hint(chat_type, chat_id)
        try:
            reply = await self.brain.reply(chat_type, chat_id, prompt, on_tool=hint)
        except BrainError as exc:
            # 失败也占一次调用额度；游标随 mark_skipped 推进，不重投（补话已经告诉用户了）。
            self.store.record_usage(chat_type, chat_id, "error", 0, 0)
            self.store.mark_skipped(chat_type, chat_id, ids, "error")
            self._addressed_ids.difference_update(ids)
            log.warning("批次调用失败 %s/%s: %s", chat_id, current_id, exc)
            await self.adapter.send(
                chat_type, chat_id, FOLLOWUP_REPLY if hint.sent else BRAIN_ERROR_REPLY
            )
            self._check_workspace(chat_type, chat_id)
            return

        sent = await self._send_batch_reply(batch, reply, chat_type, chat_id)
        if not sent:
            if hint.sent:
                # 已经开口说「去查一下」，不能没有下文。
                try:
                    await self.adapter.send(chat_type, chat_id, FOLLOWUP_REPLY)
                except Exception as exc:  # noqa: BLE001 - 补话失败不影响记账
                    log.warning("补话发送失败 %s/%s: %s", chat_id, current_id, exc)
            else:
                self.router.note_no_reply(chat_type, chat_id)

        self.store.record_usage(
            chat_type,
            chat_id,
            "reply" if addressed else "ambient",
            reply.prompt_tokens,
            reply.completion_tokens,
        )
        self.store.mark_delivered(chat_type, chat_id, *ids)
        self.store.bump_session_stats(
            chat_type,
            chat_id,
            messages=len(batch),
            tokens=reply.prompt_tokens + reply.completion_tokens,
        )
        self._addressed_ids.difference_update(ids)
        self._continuations.pop(key, None)
        self._check_workspace(chat_type, chat_id)

    async def _send_batch_reply(
        self, batch: list[Message], reply: BrainReply, chat_type: ChatType, chat_id: int
    ) -> list[str]:
        """把批次回复分片发出并入库；返回真正发出去的分片。"""
        if not reply.speak:
            return []
        chunks = shape_reply(
            reply.reply,
            self.settings.persona.max_reply_chars,
            self.settings.persona.max_reply_chars_technical,
            is_technical=is_technical_question(batch[-1].content),
        )
        if not chunks:
            return []
        sent: list[str] = []
        try:
            for chunk in chunks:
                await self.adapter.send(chat_type, chat_id, chunk)
                sent.append(chunk)
        except Exception as exc:  # 网络抖动不应拖垮整条链路
            log.warning("发送失败 %s/%s: %s", chat_id, batch[-1].message_id, exc)
        else:
            self.router.note_bot_spoke(chat_type, chat_id)
        finally:
            # 即使中途失败，已经发出去的分片也要进入历史，避免上下文断裂。
            self._store_bot_chunks(batch[-1], sent)
        return sent

    def _budget_for(self, chat_type: ChatType, chat_id: int, *, addressed: bool) -> bool:
        """80/20 预算分层：非点名只能用 ambient 额度，点名可用剩下的全部。"""
        budget = self.settings.budget
        today = now_shanghai().date().isoformat()
        calls, tokens = self.store.usage_today_by_purpose(
            chat_type, chat_id, today, ("reply", "ambient")
        )
        if calls >= budget.daily_calls or tokens >= budget.daily_tokens:
            return False
        if addressed:
            return True
        ambient_calls, ambient_tokens = self.store.usage_today_by_purpose(
            chat_type, chat_id, today, ("ambient",)
        )
        return (
            ambient_calls < budget.daily_calls * budget.ambient_ratio
            and ambient_tokens < budget.daily_tokens * budget.ambient_ratio
        )

    def _tool_hint(self, chat_type: ChatType, chat_id: int):
        """返回 on_tool 回调：每轮最多一次（brain 侧保证），同 chat 60s 内不重复。"""
        key = (chat_type, chat_id)

        async def hint(tool: str) -> None:
            now = time.monotonic()
            last = self._hint_at.get(key)
            if last is not None and now - last < TOOL_HINT_INTERVAL_SECONDS:
                return
            try:
                await self.adapter.send(chat_type, chat_id, TOOL_HINT_REPLY)
            except Exception as exc:  # noqa: BLE001 - 提示语失败不影响主流程
                log.warning("提示语发送失败 %s/%s: %s", chat_id, tool, exc)
                return
            self._hint_at[key] = now
            hint.sent = True

        hint.sent = False
        return hint

    async def _maybe_rotate(self, chat_type: ChatType, chat_id: int) -> None:
        """到阈值就换 session；摘要写不出来就继续用旧的，下一批再试。"""
        if not callable(getattr(self.brain, "reset_session", None)):
            return
        memory = self.settings.memory
        stats = self.store.session_stats(chat_type, chat_id)
        if not needs_rotation(
            stats,
            now=now_shanghai(),
            rotation_messages=memory.rotation_messages,
            rotation_days=memory.rotation_days,
            rotation_tokens=memory.rotation_tokens,
        ):
            return
        recent = self.store.recent_messages(chat_type, chat_id, ROTATION_WINDOW)
        continuation = await apply_rotation(
            self.brain, self.store, summarize_for_rotation, chat_type, chat_id, recent
        )
        if continuation:
            self._continuations[(chat_type, chat_id)] = continuation

    def _check_workspace(self, chat_type: ChatType, chat_id: int) -> None:
        """每轮结束后 stat 一次；超 64KB 保留尾部并留截断注释。"""
        if self.workspace is None:
            return
        self.workspace.enforce_limit(self.workspace.global_path())
        self.workspace.enforce_limit(self.workspace.chat_path(chat_type, chat_id))

    async def _ask_brain(self, msg: Message) -> BrainReply:
        """OpenCode 模式走会话接口；本地模式仍走原来的上下文拼装。"""
        responder = getattr(self.brain, "reply", None)
        if callable(responder):
            # 一个群共用一个 OpenCode session，必须让模型看到是谁在说话；
            # 被规则跳过的消息也要补上，否则模型会把「你没回我」当成网络问题。
            prompt, delivered = build_delivery(self.store, msg)
            reply = await responder(msg.chat_type, msg.chat_id, prompt)
            # 只有真正送到模型那里才记账；失败时整批留到下一次重投。
            self.store.mark_delivered(msg.chat_type, msg.chat_id, *delivered)
            return reply

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
