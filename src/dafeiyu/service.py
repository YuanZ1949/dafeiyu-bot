from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .brain.client import BrainError, BrainReply
from .brain.persona import build_system_prompt, os_probability_hint
from .budget import BudgetAction
from .commands import CommandRegistry, CommandSpec
from .config import InjectionConfig, Settings
from .delivery.scheduler import BatchScheduler
from .gateway.base import Adapter
from .memory.context import build_injection
from .memory.rotation import apply_rotation, needs_rotation
from .memory.store import Store
from .memory.timeline import TIMELINE_LIMIT, build_batch_prompt
from .memory.workspace import Workspace
from .models import ChatType, Message
from .router import Router, now_shanghai
from .sender import is_technical_question, shape_reply
from .workers.failure import QUOTA_KEY
from .workers.summary import summarize_for_rotation

if TYPE_CHECKING:
    from .supervisor.audit import AuditLog

log = logging.getLogger(__name__)

SHUTUP_MINUTES = 60
ADMIN_ONLY_REPLY = "这个命令只有管理员能用。"
FORGET_REPLY = "记忆清空了，本鱼失忆中……🫧"
BRAIN_ERROR_REPLY = "本鱼刚刚和大脑断线了…你过一会儿再问我？🐋"
TOOL_HINT_REPLY = "本鱼去网上查一下，稍等…"
FOLLOWUP_REPLY = "没查到什么有用的，本鱼先不瞎说了"
# 同一会话 60s 内不重复发「去查一下」；每轮最多一次由 brain 侧保证。
TOOL_HINT_INTERVAL_SECONDS = 60.0
# 同一会话 30s 内不重复发「和大脑断线了」；失败本身照常记账。
BRAIN_ERROR_COOLDOWN_SECONDS = 30.0
# 轮换摘要的输入窗口（spec §9.2：最近 40 条）。
ROTATION_WINDOW = 40
# 批次被跳过的原因。error 与 record_usage(purpose="error") 同口径
# （spec §8.4 原写作 failed，实现统一取 error，避免同一件事两个名字）。
SKIP_BUDGET = "budget"
SKIP_ERROR = "error"

_REASON_TEXT = {
    "self": "机器人自己的消息",
    "command": "命中内置命令",
    "muted": "群聊已静音",
    "quiet": "夜间静默",
    "blacklist": "群在黑名单",
    "budget": "预算已用完",
    "hard": "被点名",
    "whitelist": "群不在白名单",
    "duplicate": "冷却窗口内重复",
    "question": "命中提问规则",
    "cooldown": "机器人刚说过话",
    "hot": "群聊正热",
    "sample": "随机抽样命中",
    "sample_skip": "随机抽样未命中",
    "batch": "已进入批次，等待模型判定",
    "private": "私聊预算通过，直接调用模型",
}

_BUDGET_SCOPE_TEXT = {
    "per_chat": "本会话每日预算已用完",
    "global_daily": "全局日预算已用完",
    "monthly": "月度预算已用完",
    "cost": "月度费用已达上限",
    "storage_error": "预算读取失败，暂时按降级处理",
}

_VERDICT_TEXT = {
    "model": "会进入模型",
    "batch": "会进入批次，等待模型判定",
    "skip": "不会进入模型",
    "command": "会走命令分支",
}

_CHECK_LABELS = {
    "self": "自身消息",
    "command": "内置命令",
    "muted": "静音",
    "quiet": "夜间静默",
    "blacklist": "群黑名单",
    "hard": "硬触发",
    "whitelist": "群白名单",
    "batch": "进入批次",
    "budget": "每日预算",
    "model": "模型路径",
    "private": "私聊直回",
}


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
        audit: AuditLog | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.brain = brain
        self.adapter = adapter
        self.router = router
        self.budget_guard = router.budget_guard
        self.persona_text = persona_text
        self.impressions = impressions
        self.workspace = workspace
        self.injection = injection
        self.audit = audit
        self.commands = CommandRegistry()
        self._command_clock: Callable[[], float] = time.monotonic
        self._command_last: dict[tuple[str, str, int], float] = {}
        self.scheduler: BatchScheduler | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._locks: dict[tuple[ChatType, int], asyncio.Lock] = {}
        self._batch_locks: dict[tuple[ChatType, int], asyncio.Lock] = {}
        self._started_at = time.monotonic()
        # 轮换后的接续文本：成功投递才弹出，失败留到下一批重试。
        self._continuations: dict[tuple[ChatType, int], str] = {}
        self._hint_at: dict[tuple[ChatType, int], float] = {}
        self._error_reply_at: dict[tuple[ChatType, int], float] = {}

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

            matched = self.commands.match(msg.content, msg.chat_type)
            if matched is not None:
                if not self._command_allowed(msg):
                    return
                spec, args = matched
                if await self._run_command(msg, spec, args):
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

        scheduler.note(msg, addressed=addressed)

    def _command_allowed(self, msg: Message) -> bool:
        """命令也必须遵守群黑名单/白名单，避免黑名单群查询统计或探活。"""
        if msg.chat_type is not ChatType.GROUP:
            return True
        if msg.chat_id in self.settings.groups.blacklist:
            return False
        whitelist = self.settings.groups.whitelist
        return not whitelist or msg.chat_id in whitelist

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

    # ---- trigger debugger -----------------------------------------
    def explain_trigger(
        self,
        msg: Message,
        *,
        now: Any = None,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """只读推演一条消息会走哪条路径；不写消息、不调模型、不动调度器。"""
        now = now or now_shanghai()
        configured_mode = self.settings.triggers.mode
        mode = "model" if self._uses_scheduler() else "rules"
        mode_note = None
        if configured_mode == "model" and mode == "rules":
            mode_note = "配置为 model，但大脑没有 reply 接口，按 rules 路径判定"
        hard_source = self.router.addressed_source(msg)
        metrics = self._usage_metrics(msg.chat_type, msg.chat_id, now)

        if msg.is_bot:
            checks = [
                self._check("self", "blocked", "机器人自己的消息，直接忽略"),
                self._check("model", "skipped", "自身消息不会进入模型"),
            ]
            return self._explain_result(
                msg,
                verdict="skip",
                reason="self",
                mode=mode,
                checks=checks,
                metrics=metrics,
                hard_source=hard_source,
                mode_note=mode_note,
            )

        matched = self.commands.match(msg.content, msg.chat_type)
        if matched is not None:
            spec = matched[0]
            checks = [
                self._check("self", "pass", "普通用户消息"),
                self._check("command", "pass", f"/{spec.name} 会走命令分支"),
                self._check("model", "skipped", "命令分支已接管，不会进入模型"),
            ]
            return self._explain_result(
                msg,
                verdict="command",
                reason="command",
                mode=mode,
                checks=checks,
                metrics=metrics,
                hard_source=hard_source,
                mode_note=mode_note,
            )

        if msg.chat_type is ChatType.PRIVATE:
            return self._explain_private(
                msg,
                now=now,
                mode=mode,
                mode_note=mode_note,
                hard_source=hard_source,
                metrics=metrics,
            )
        if mode == "model":
            return self._explain_model_group(
                msg,
                now=now,
                seed=seed,
                mode_note=mode_note,
                hard_source=hard_source,
            )
        return self._explain_rules(
            msg, now=now, seed=seed, mode=mode, mode_note=mode_note
        )

    def _explain_rules(
        self,
        msg: Message,
        *,
        now: Any,
        seed: int | None,
        mode: str,
        mode_note: str | None,
    ) -> dict[str, Any]:
        result = self.router.explain(msg, now=now, seed=seed)
        verdict = "model" if result["decision"]["call_model"] else "skip"
        reason = result["decision"]["reason"]
        result.update(
            {
                "verdict": verdict,
                "verdict_text": _VERDICT_TEXT[verdict],
                "reason": reason,
                "reason_text": self._reason_text(reason, result.get("metrics", {})),
                "mode": mode,
                "configured_mode": self.settings.triggers.mode,
                "chat_type": msg.chat_type.value,
                "chat_id": msg.chat_id,
                "user_id": msg.user_id,
            }
        )
        if mode_note:
            result["mode_note"] = mode_note
        return result

    def _explain_private(
        self,
        msg: Message,
        *,
        now: Any,
        mode: str,
        mode_note: str | None,
        hard_source: str | None,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        decision = self._budget_decision(msg, now, addressed=True)
        metrics.update(self._decision_metrics(decision))
        ok = decision.action is not BudgetAction.DENY
        metrics["budget_ok"] = ok
        reason = "private" if ok else "budget"
        checks = [
            self._check("self", "pass", "普通用户消息"),
            self._check(
                "budget",
                "pass" if ok else "blocked",
                self._budget_detail(decision, metrics, addressed=True),
            ),
            self._check(
                "private",
                "pass" if ok else "skipped",
                "私聊直接调用模型" if ok else "预算已用完",
            ),
        ]
        return self._explain_result(
            msg,
            verdict="model" if ok else "skip",
            reason=reason,
            mode=mode,
            checks=checks,
            metrics=metrics,
            hard_source=hard_source,
            mode_note=mode_note,
        )

    def _explain_model_group(
        self,
        msg: Message,
        *,
        now: Any,
        seed: int | None,
        mode_note: str | None,
        hard_source: str | None,
    ) -> dict[str, Any]:
        probe = self.router.clone(now=now, seed=seed)
        if msg.reply_to_bot:
            probe.note_reply(msg.chat_type, msg.chat_id)
        source = probe.addressed_source(msg)
        addressed = source is not None

        reason = None
        if probe.is_muted(msg.chat_type, msg.chat_id):
            reason = "muted"
        elif probe.is_quiet(msg.chat_type, msg.chat_id):
            reason = "quiet"
        elif msg.chat_id in self.settings.groups.blacklist:
            reason = "blacklist"
        elif (
            not addressed
            and self.settings.groups.whitelist
            and msg.chat_id not in self.settings.groups.whitelist
        ):
            reason = "whitelist"

        metrics = self._usage_metrics(msg.chat_type, msg.chat_id, now)
        decision = self._budget_decision(msg, now, addressed=addressed)
        metrics.update(
            self._ambient_budget_metrics(
                msg, now, addressed=addressed, decision=decision
            )
        )
        if reason is None and self._budget_blocked(decision, addressed):
            reason = "budget"
        st = probe._state(msg.chat_type, msg.chat_id)
        muted_left = (
            max(0.0, (st.muted_until - now).total_seconds())
            if st.muted_until is not None
            else 0.0
        )
        cooldown = self.settings.triggers.cooldown_seconds
        cooldown_left = (
            max(0.0, cooldown - (now - st.last_bot_spoke).total_seconds())
            if st.last_bot_spoke is not None
            else 0.0
        )
        metrics.update(
            {
                "muted_remaining_seconds": round(muted_left, 3),
                "cooldown_remaining_seconds": round(cooldown_left, 3),
                "no_reply_streak": st.no_reply_streak,
                "quiet_hours": list(self.settings.triggers.quiet_hours),
                "in_quiet_hours": self.router._in_quiet_hours(now),
            }
        )

        keys = [
            "self",
            "muted",
            "quiet",
            "blacklist",
            "hard",
            "whitelist",
            "batch",
            "budget",
        ]
        stop_key = reason or "batch"
        stop_index = keys.index(stop_key)
        checks = []
        for index, key in enumerate(keys):
            if key == "budget":
                status = "blocked" if reason == "budget" else "info"
            elif key == "hard":
                status = "pass" if addressed else "info"
            elif index < stop_index:
                status = "pass"
            elif index == stop_index:
                status = "pass" if key == "batch" else "blocked"
            else:
                status = "skipped"
            checks.append(self._check(key, status, self._model_detail(key, msg, metrics, source, status)))

        return self._explain_result(
            msg,
            verdict="batch" if reason is None else "skip",
            reason=reason or "batch",
            mode="model",
            checks=checks,
            metrics=metrics,
            hard_source=source or hard_source,
            mode_note=mode_note,
        )

    def _usage_metrics(
        self, chat_type: ChatType, chat_id: int, now: Any
    ) -> dict[str, Any]:
        budget = self.settings.budget
        metrics: dict[str, Any] = {
            "usage_calls": 0,
            "usage_tokens": 0,
            "daily_calls": budget.daily_calls,
            "daily_tokens": budget.daily_tokens,
            "global_calls": 0,
            "global_tokens": 0,
            "global_limit_calls": budget.global_daily_calls,
            "global_limit_tokens": budget.global_daily_tokens,
            "monthly_calls": 0,
            "monthly_tokens": 0,
            "monthly_limit_calls": budget.monthly_calls,
            "monthly_limit_tokens": budget.monthly_tokens,
            "monthly_cost": 0.0,
            "monthly_cost_limit": budget.monthly_cost_limit,
            "currency": budget.currency,
        }
        try:
            report = self.budget_guard.report(chat_type, chat_id, now=now)
        except Exception:  # noqa: BLE001 - explain 不能因读库失败而中断
            return metrics
        metrics.update(
            {
                "usage_calls": report.per_chat["calls"],
                "usage_tokens": report.per_chat["tokens"],
                "global_calls": report.global_daily["calls"],
                "global_tokens": report.global_daily["tokens"],
                "monthly_calls": report.monthly["calls"],
                "monthly_tokens": report.monthly["tokens"],
                "monthly_cost": report.cost["cost"],
            }
        )
        return metrics

    def _budget_decision(
        self, msg: Message, now: Any, *, addressed: bool
    ):
        return self.budget_guard.evaluate(
            msg.chat_type,
            msg.chat_id,
            addressed=addressed,
            purpose="reply" if addressed else "ambient",
            now=now,
        )

    @staticmethod
    def _budget_blocked(decision, addressed: bool) -> bool:
        return decision.action is BudgetAction.DENY or (
            decision.action is BudgetAction.DEGRADE and not addressed
        )

    @staticmethod
    def _decision_metrics(decision) -> dict[str, Any]:
        return {
            "budget_action": decision.action.value,
            "budget_scope": decision.scope,
            "budget_reason": decision.reason,
            "budget_warnings": list(decision.warnings),
        }

    @staticmethod
    def _budget_detail(decision, metrics: dict[str, Any], *, addressed: bool) -> str:
        return Service._budget_detail_text(
            decision.action.value,
            decision.scope,
            metrics,
            addressed=addressed,
        )

    @staticmethod
    def _budget_detail_text(
        action: str,
        scope: str,
        metrics: dict[str, Any],
        *,
        addressed: bool,
    ) -> str:
        if scope == "storage_error":
            return "预算检查读库失败，按降级处理"
        scope_labels = {
            "per_chat": "每日预算",
            "global_daily": "全局日预算",
            "monthly": "月度预算",
            "cost": "月度费用",
        }
        if action == BudgetAction.DENY.value:
            label = scope_labels.get(scope, "每日预算")
            return f"{label}已用完，本次不会调用模型"
        if action == BudgetAction.DEGRADE.value:
            if addressed:
                return "全局/月度额度已超限，点名消息仍可继续"
            return "当前额度不足，批次投递时会被跳过"
        if addressed:
            return (
                f"点名消息可用总额度；已用 {metrics['usage_calls']}/"
                f"{metrics['daily_calls']} 次"
            )
        limit = metrics["daily_calls"] * metrics["ambient_ratio"]
        return (
            f"普通消息受 {metrics['ambient_ratio']:.0%} 额度限制；"
            f"ambient 已用 {metrics['ambient_calls']}/{limit:.0f} 次"
        )

    def _ambient_budget_metrics(
        self,
        msg: Message,
        now: Any,
        *,
        addressed: bool,
        decision=None,
    ) -> dict[str, Any]:
        budget = self.settings.budget
        if decision is None:
            decision = self._budget_decision(msg, now, addressed=addressed)
        today = now.date().isoformat()
        try:
            ambient_calls, ambient_tokens = self.store.usage_today_by_purpose(
                msg.chat_type, msg.chat_id, today, ("ambient",)
            )
        except Exception:  # noqa: BLE001 - 展示用数据失败不影响 explain
            ambient_calls, ambient_tokens = 0, 0
        metrics = {
            "ambient_calls": ambient_calls,
            "ambient_tokens": ambient_tokens,
            "ambient_ratio": budget.ambient_ratio,
            "budget_ok": not self._budget_blocked(decision, addressed),
            "budget_addressed": addressed,
        }
        metrics.update(self._decision_metrics(decision))
        return metrics

    def _model_detail(
        self,
        key: str,
        msg: Message,
        metrics: dict[str, Any],
        source: str | None,
        status: str,
    ) -> str:
        if key == "self":
            return "普通用户消息" if status != "blocked" else "机器人自己的消息"
        if key == "muted":
            left = metrics["muted_remaining_seconds"]
            return f"剩余 {int(left)} 秒" if left > 0 else "未静音"
        if key == "quiet":
            start, end = metrics["quiet_hours"]
            return (
                f"静默时段 {start}–{end}"
                if metrics["in_quiet_hours"]
                else "不在静默时段"
            )
        if key == "blacklist":
            return "命中黑名单" if status == "blocked" else "不在黑名单"
        if key == "hard":
            labels = {"at": "@ 机器人", "reply": "引用回复", "alias": "正文别名"}
            return labels.get(source or "", "未点名")
        if key == "whitelist":
            if not self.settings.groups.whitelist:
                return "白名单为空"
            return "群不在白名单" if status == "blocked" else "群在白名单"
        if key == "batch":
            return (
                "通过 gate，进入批次；预算在批次投递前检查"
                if status == "pass"
                else "前序 gate 已拦下"
            )
        if key == "budget":
            if not metrics["budget_ok"]:
                return self._budget_detail_text(
                    metrics["budget_action"],
                    metrics["budget_scope"],
                    metrics,
                    addressed=metrics["budget_addressed"],
                )
            if metrics["budget_addressed"]:
                return (
                    f"点名消息可用总额度；已用 {metrics['usage_calls']}/"
                    f"{metrics['daily_calls']} 次"
                )
            limit = metrics["daily_calls"] * metrics["ambient_ratio"]
            return (
                f"普通消息受 {metrics['ambient_ratio']:.0%} 额度限制；"
                f"ambient 已用 {metrics['ambient_calls']}/{limit:.0f} 次"
            )
        return ""

    @staticmethod
    def _reason_text(reason: str, metrics: dict[str, Any]) -> str:
        if reason == "budget":
            scope = str(metrics.get("budget_scope") or "")
            return _BUDGET_SCOPE_TEXT.get(scope, _REASON_TEXT["budget"])
        return _REASON_TEXT.get(reason, reason)

    @staticmethod
    def _check(key: str, status: str, detail: str) -> dict[str, str]:
        return {
            "key": key,
            "label": _CHECK_LABELS.get(key, key),
            "status": status,
            "detail": detail,
        }

    def _explain_result(
        self,
        msg: Message,
        *,
        verdict: str,
        reason: str,
        mode: str,
        checks: list[dict[str, str]],
        metrics: dict[str, Any],
        hard_source: str | None,
        mode_note: str | None,
    ) -> dict[str, Any]:
        result = {
            "verdict": verdict,
            "verdict_text": _VERDICT_TEXT[verdict],
            "reason": reason,
            "reason_text": self._reason_text(reason, metrics),
            "mode": mode,
            "configured_mode": self.settings.triggers.mode,
            "chat_type": msg.chat_type.value,
            "chat_id": msg.chat_id,
            "user_id": msg.user_id,
            "addressed": hard_source is not None,
            "hard_source": hard_source,
            "checks": checks,
            "metrics": metrics,
        }
        if mode_note:
            result["mode_note"] = mode_note
        return result

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
        addressed_ids = [m.message_id for m in batch if m.at_bot or m.reply_to_bot]
        addressed = bool(addressed_ids)
        current_id = addressed_ids[-1] if addressed_ids else ids[-1]
        key = (chat_type, chat_id)

        budget = self._chat_budget_decision(chat_type, chat_id, addressed=addressed)
        if not self._budget_allows(budget, addressed):
            self.store.mark_skipped(chat_type, chat_id, ids, SKIP_BUDGET)
            log.debug(
                "批次跳过（预算）%s/%s scope=%s reason=%s",
                chat_id,
                current_id,
                budget.scope,
                budget.reason,
            )
            return

        await self._maybe_rotate(chat_type, chat_id)

        prompt = self._build_prompt(
            chat_type,
            chat_id,
            batch,
            continuation=self._continuations.get(key),
        )

        log.info(
            "批次送审 %s/%s ids=%d addressed=%s",
            chat_type.value,
            chat_id,
            len(batch),
            addressed,
        )
        hint = self._tool_hint(chat_type, chat_id)
        try:
            reply = await self._call_brain(
                chat_type, chat_id, batch, prompt, on_tool=hint
            )
        except BrainError as exc:
            # 失败也占一次调用额度；游标随 mark_skipped 推进，不重投（补话已经告诉用户了）。
            self.store.record_usage(chat_type, chat_id, "error", 0, 0)
            self.store.mark_skipped(chat_type, chat_id, ids, SKIP_ERROR)
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
            reply.estimated,
        )
        self.store.mark_delivered(chat_type, chat_id, *ids)
        self.store.bump_session_stats(
            chat_type,
            chat_id,
            messages=len(batch),
            tokens=reply.prompt_tokens + reply.completion_tokens,
        )
        self._continuations.pop(key, None)
        self._check_workspace(chat_type, chat_id)

    async def _send_batch_reply(
        self, batch: list[Message], reply: BrainReply, chat_type: ChatType, chat_id: int
    ) -> list[str]:
        """把批次回复分片发出并入库；返回真正发出去的分片。"""
        if not reply.speak:
            log.info(
                "模型选择不说 %s/%s ids=%d reason=%s",
                chat_type.value,
                chat_id,
                len(batch),
                (reply.reason or "-").strip()[:120],
            )
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

    def _chat_budget_decision(
        self, chat_type: ChatType, chat_id: int, *, addressed: bool
    ):
        return self.budget_guard.evaluate(
            chat_type,
            chat_id,
            addressed=addressed,
            purpose="reply" if addressed else "ambient",
            now=now_shanghai(),
        )

    @staticmethod
    def _budget_allows(decision, addressed: bool) -> bool:
        if decision.action is BudgetAction.DENY:
            return False
        return not (
            decision.action is BudgetAction.DEGRADE and not addressed
        )

    def _budget_for(self, chat_type: ChatType, chat_id: int, *, addressed: bool) -> bool:
        """80/20 预算分层：非点名只能用 ambient 额度，点名可用剩下的全部。"""
        return self._budget_allows(
            self._chat_budget_decision(chat_type, chat_id, addressed=addressed),
            addressed,
        )

    def _tool_hint(self, chat_type: ChatType, chat_id: int):
        """返回 on_tool 回调：每轮最多一次（brain 侧保证），同 chat 60s 内不重复。

        节流时间戳是进程内状态：多实例部署时各算各的，最坏情况同一分钟内多发
        一条提示语（提示语只是过渡消息，重复无害），因此不为此建表。
        """
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
        """每轮结束后 stat 一次；超上限保留尾部并留截断注释。"""
        if self.workspace is None:
            return
        limit = self.settings.workspace.max_file_bytes
        self.workspace.enforce_limit(self.workspace.global_path(), max_bytes=limit)
        self.workspace.enforce_limit(
            self.workspace.chat_path(chat_type, chat_id), max_bytes=limit
        )

    async def _ask_brain(self, msg: Message) -> BrainReply:
        """单条消息与批次共用同一 prompt 构建；provider 差异只在 _call_brain 里。"""
        omitted, pending = self.store.pending_messages(
            msg.chat_type, msg.chat_id, msg.message_id, TIMELINE_LIMIT
        )
        current = pending or [msg]
        prompt = self._build_prompt(
            msg.chat_type, msg.chat_id, current, omitted=omitted
        )
        hint = os_probability_hint(self.settings.persona.os_probability)
        if hint:
            # 人设文件写死了默认概率，配置偏离时只能逐轮纠偏。
            prompt = f"{prompt}\n\n{hint}"
        log.info(
            "消息送审 %s/%s id=%s ids=%d",
            msg.chat_type.value,
            msg.chat_id,
            msg.message_id,
            len(current),
        )
        reply = await self._call_brain(msg.chat_type, msg.chat_id, current, prompt)
        # 只有真正送到模型那里才记账；失败时留到下一次重投。
        self.store.mark_delivered(
            msg.chat_type, msg.chat_id, *[m.message_id for m in current]
        )
        return reply

    def _build_prompt(
        self,
        chat_type: ChatType,
        chat_id: int,
        messages: list[Message],
        *,
        continuation: str | None = None,
        omitted: int = 0,
    ) -> str:
        """三种模式共用的 prompt：长期记忆注入 + 时间线（单条也走这里）。"""
        injection_cfg = self.injection or InjectionConfig()
        recall_queries = [
            message.content
            for message in messages
            if not message.is_bot and message.content.strip()
        ][-3:]
        exclude_ids = {message.message_id for message in messages}
        if injection_cfg.recall_enabled:
            exclude_ids.update(
                message.message_id
                for message in self.store.recent_messages(
                    chat_type, chat_id, self.settings.memory.window_size
                )
            )
        injection = build_injection(
            self.store,
            self.workspace,
            chat_type,
            chat_id,
            injection_cfg,
            today=now_shanghai().date().isoformat(),
            recall_queries=recall_queries,
            exclude_ids=exclude_ids,
        )
        return build_batch_prompt(
            messages, injection, continuation=continuation, omitted=omitted
        )

    async def _call_brain(
        self,
        chat_type: ChatType,
        chat_id: int,
        messages: list[Message],
        prompt: str,
        *,
        on_tool: Any = None,
    ) -> BrainReply:
        """唯一的大脑调用入口；会话型与无状态型的差异都收在这里。"""
        responder = getattr(self.brain, "reply", None)
        if not callable(responder):
            # 旧 brain 只有 decide：保留兜底，切换期也不至于直接崩。
            return await self.brain.decide(
                self._system_prompt(chat_type, messages),
                [{"role": "user", "content": prompt}],
            )
        if getattr(self.brain, "accepts_history", False):
            return await responder(
                chat_type,
                chat_id,
                prompt,
                system=self._system_prompt(chat_type, messages),
                history=self._history_before(chat_type, chat_id, messages),
                on_tool=on_tool,
            )
        if on_tool is None:
            # 单条路径历来不传 on_tool，旧的 brain/fake 只认三个位置参数。
            return await responder(chat_type, chat_id, prompt)
        return await responder(chat_type, chat_id, prompt, on_tool=on_tool)

    def _system_prompt(self, chat_type: ChatType, messages: list[Message]) -> str:
        chat_title = (
            "本群"
            if chat_type is ChatType.GROUP
            else (messages[-1].nickname if messages else "私聊")
        )
        return build_system_prompt(
            self.persona_text,
            chat_title,
            now_shanghai().date().isoformat(),
            self.settings.persona.max_reply_chars,
            self.settings.persona.max_reply_chars_technical,
            self.settings.persona.os_probability,
            tools_enabled=self._tools_enabled(),
        )

    def _tools_enabled(self) -> bool:
        """只有 opencode 且工作区开着时，模型才真的有 read/edit/webfetch。"""
        return (
            bool(getattr(self.brain, "supports_tools", False))
            and self.workspace is not None
        )

    def _history_before(
        self, chat_type: ChatType, chat_id: int, messages: list[Message]
    ) -> list[dict[str, str]]:
        """无状态大脑的历史：本批之前的最近 window_size 条，role 按是否 bot 区分。"""
        window = max(0, int(self.settings.memory.window_size))
        if not window or not messages:
            return []
        batch_ids = {message.message_id for message in messages}
        rows = self.store.recent_messages(
            chat_type,
            chat_id,
            window + len(messages),
            upto_message_id=messages[0].message_id,
        )
        history: list[dict[str, str]] = []
        for message in rows:
            if message.message_id in batch_ids or not message.content.strip():
                continue
            history.append(
                {
                    "role": "assistant" if message.is_bot else "user",
                    "content": message.to_prompt_line(),
                }
            )
        return history[-window:]

    async def _reply(self, msg: Message) -> None:
        try:
            reply = await self._ask_brain(msg)
        except BrainError as exc:
            # 失败也占一次调用额度，避免异常时无限重试烧钱。
            self.store.record_usage(msg.chat_type, msg.chat_id, "error", 0, 0)
            log.warning("模型调用失败 %s/%s: %s", msg.chat_id, msg.message_id, exc)
            key = (msg.chat_type, msg.chat_id)
            now = time.monotonic()
            last = self._error_reply_at.get(key)
            if last is not None and now - last < BRAIN_ERROR_COOLDOWN_SECONDS:
                log.debug("错误回复冷却中，跳过 %s/%s", msg.chat_id, msg.message_id)
                return
            self._error_reply_at[key] = now
            await self.adapter.send(msg.chat_type, msg.chat_id, BRAIN_ERROR_REPLY)
            return

        self.store.record_usage(
            msg.chat_type,
            msg.chat_id,
            "reply",
            reply.prompt_tokens,
            reply.completion_tokens,
            reply.estimated,
        )

        if not reply.speak:
            log.info(
                "模型选择不说 %s/%s id=%s reason=%s",
                msg.chat_type.value,
                msg.chat_id,
                msg.message_id,
                (reply.reason or "-").strip()[:120],
            )
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
        # 私聊视为点名：存储读失败时只降级不拦死。
        decision = self.budget_guard.evaluate(
            msg.chat_type,
            msg.chat_id,
            addressed=True,
            purpose="reply",
            now=now_shanghai(),
        )
        return decision.action is not BudgetAction.DENY

    async def forget_chat(self, chat_type: ChatType, chat_id: int) -> None:
        """硬忘记：先删远端 session（它要读映射），再清本地全部数据。

        控制口的 `DELETE /api/sessions/...` 复用这里；不带群聊回复。
        """
        forget = getattr(self.brain, "forget", None)
        if forget is not None:
            result = forget(chat_type, chat_id)
            if inspect.isawaitable(result):
                await result
        self.store.forget_chat(chat_type, chat_id)

    async def _forget_chat(self, msg: Message) -> None:
        await self.forget_chat(msg.chat_type, msg.chat_id)
        await self.adapter.send(msg.chat_type, msg.chat_id, FORGET_REPLY)

    async def _run_command(self, msg: Message, spec: CommandSpec, args: str) -> bool:
        """权限 → 冷却 → 分发 → 审计；所有分支都返回「命令分支已接管」。"""
        is_admin = msg.user_id in self.settings.groups.admins
        private_forget = spec.name == "forget" and msg.chat_type is ChatType.PRIVATE
        if spec.admin_only and not is_admin and not private_forget:
            if msg.chat_type is ChatType.GROUP:
                # 群内保持原有静默行为，避免在群里公开点名权限问题。
                log.info(
                    "非管理员尝试执行管理员命令 %s/%s: user=%s cmd=%s",
                    msg.chat_id,
                    msg.message_id,
                    msg.user_id,
                    spec.name,
                )
                return True
            await self.adapter.send(msg.chat_type, msg.chat_id, ADMIN_ONLY_REPLY)
            return True

        cooldown = self._command_cooldown(spec, msg.chat_type)
        if cooldown > 0:
            if msg.chat_type is ChatType.GROUP:
                key = (spec.name, "group", msg.chat_id)
            else:
                key = (spec.name, "private", msg.user_id)
            now = self._command_clock()
            last = self._command_last.get(key)
            if last is not None and now - last < cooldown:
                return True
            # 实际执行前记录，失败重试也要等冷却，避免刷屏。
            self._command_last[key] = now

        if spec.name == "help":
            await self._cmd_help(msg, is_admin=is_admin)
        elif spec.name == "stats":
            await self._cmd_stats(msg)
        elif spec.name == "status":
            await self._cmd_status(msg)
        elif spec.name == "budget":
            await self._cmd_budget(msg)
        elif spec.name == "forget":
            await self._forget_chat(msg)
        elif spec.name == "shutup":
            self.router.quiet(msg.chat_type, msg.chat_id, SHUTUP_MINUTES)
        elif spec.name == "wake":
            self.router.wake(msg.chat_type, msg.chat_id)
        else:
            return False

        # 私聊非管理员 /forget 是显式放行，不应被记成管理员命令。
        if spec.admin_only and is_admin:
            self._write_command_audit(msg, spec)
        return True

    @staticmethod
    def _command_cooldown(spec: CommandSpec, chat_type: ChatType) -> int:
        # 私聊延迟敏感：统计命令固定 5 秒，其余沿用注册表配置。
        if spec.name == "stats" and chat_type is ChatType.PRIVATE:
            return 5
        return spec.cooldown_seconds

    def _write_command_audit(self, msg: Message, spec: CommandSpec) -> None:
        if self.audit is None:
            return
        try:
            self.audit.write(
                "command.admin",
                actor=str(msg.user_id),
                target=f"/{spec.name}",
                detail={
                    "chat_type": msg.chat_type.value,
                    "chat_id": msg.chat_id,
                    "user_id": msg.user_id,
                },
            )
        except Exception:
            log.warning("命令审计写入失败：%s", spec.name, exc_info=True)

    async def _cmd_help(self, msg: Message, *, is_admin: bool) -> None:
        text = self.commands.help_text(msg.chat_type, is_admin=is_admin)
        await self.adapter.send(msg.chat_type, msg.chat_id, text)

    async def _cmd_stats(self, msg: Message) -> None:
        today = now_shanghai().date().isoformat()
        budget = self.settings.budget
        local = self.store.usage_between(
            today,
            today,
            purposes=("reply", "ambient"),
            chat_type=msg.chat_type,
            chat_id=msg.chat_id,
        )
        global_today = self.store.usage_global_today(today)
        lines = [
            "今日统计：",
            (
                f"本会话 calls：{local.calls} 次，剩余："
                f"{self._remaining_text(local.calls, budget.daily_calls)}"
            ),
            (
                f"本会话 tokens：{local.tokens}，剩余："
                f"{self._remaining_text(local.tokens, budget.daily_tokens)}"
            ),
            (
                f"全局今日 calls：{global_today.calls} 次，剩余："
                f"{self._remaining_text(global_today.calls, budget.global_daily_calls)}"
            ),
            (
                f"全局今日 tokens：{global_today.tokens}，剩余："
                f"{self._remaining_text(global_today.tokens, budget.global_daily_tokens)}"
            ),
        ]
        await self.adapter.send(msg.chat_type, msg.chat_id, "\n".join(lines))

    async def _cmd_status(self, msg: Message) -> None:
        connected = bool(getattr(self.adapter, "connected", False))

        def count(name: str) -> int:
            scheduler = self.scheduler
            if scheduler is None:
                return 0
            value = getattr(scheduler, name, 0)
            if callable(value):
                try:
                    value = value()
                except Exception:  # noqa: BLE001 - 计数失败只降级为 0
                    return 0
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        try:
            quota_until = self.store.load_backoff().get(QUOTA_KEY)
        except Exception as exc:  # noqa: BLE001 - 状态命令不能因读库失败而中断
            log.warning("读取配额状态失败：%s", exc)
            quota_until = None
        paused = bool(quota_until and float(quota_until) > time.time())
        if paused:
            remaining = max(0, int(float(quota_until) - time.time()))
            quota_text = f"暂停中（剩余 {remaining} 秒）"
        else:
            quota_text = "正常"

        decision = self.budget_guard.evaluate(
            msg.chat_type,
            msg.chat_id,
            addressed=True,
            purpose="reply",
        )
        action_text = {
            BudgetAction.ALLOW.value: "正常",
            BudgetAction.DEGRADE.value: "降级",
            BudgetAction.DENY.value: "已限流",
        }.get(decision.action.value, decision.action.value)
        uptime = int(time.monotonic() - self._started_at)
        lines = [
            "运行状态：",
            f"网关：{'已连接' if connected else '未连接'}",
            f"调度：待投 {count('pending_count')}，进行中 {count('inflight_count')}",
            f"配额：{quota_text}",
            f"预算：{decision.action.value}（{action_text}）",
            f"已运行：{uptime} 秒",
        ]
        await self.adapter.send(msg.chat_type, msg.chat_id, "\n".join(lines))

    async def _cmd_budget(self, msg: Message) -> None:
        report = self.budget_guard.report(msg.chat_type, msg.chat_id)
        per_chat = report.per_chat
        global_daily = report.global_daily
        monthly = report.monthly
        cost = report.cost
        cost_limit = float(cost["cost_limit"])
        cost_text = (
            "未启用"
            if cost_limit <= 0
            else f"{float(cost['cost']):.4f}/{cost_limit:.4f} {cost['currency']}"
        )
        warnings = (
            "、".join(
                f"{item.get('scope')} {float(item.get('ratio', 0)):.0%}"
                f"（{item.get('calls')}/{item.get('limit')}）"
                for item in report.warnings
            )
            or "无"
        )
        lines = [
            "预算报告：",
            (
                f"本会话：calls "
                f"{self._used_limit_text(per_chat['calls'], per_chat['daily_calls'])}，"
                f"tokens "
                f"{self._used_limit_text(per_chat['tokens'], per_chat['daily_tokens'])}"
            ),
            (
                f"全局今日：calls "
                f"{self._used_limit_text(global_daily['calls'], global_daily['limit_calls'])}，"
                f"tokens "
                f"{self._used_limit_text(global_daily['tokens'], global_daily['limit_tokens'])}"
            ),
            (
                f"月度：calls "
                f"{self._used_limit_text(monthly['calls'], monthly['limit_calls'])}，"
                f"tokens "
                f"{self._used_limit_text(monthly['tokens'], monthly['limit_tokens'])}"
            ),
            f"费用：{cost_text}（估算：{'是' if cost['estimated'] else '否'}）",
            f"预警：{warnings}",
        ]
        await self.adapter.send(msg.chat_type, msg.chat_id, "\n".join(lines))

    @staticmethod
    def _remaining_text(used: int, limit: int) -> str:
        if limit <= 0:
            return "未启用"
        return str(max(0, int(limit) - int(used)))

    @staticmethod
    def _used_limit_text(used: int, limit: int) -> str:
        if limit <= 0:
            return "未启用"
        return f"{int(used)}/{int(limit)}"
