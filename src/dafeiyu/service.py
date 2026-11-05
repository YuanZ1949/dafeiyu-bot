from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .behavior import BehaviorSettings
from .brain.client import BrainError, BrainReply
from .brain.guard import (
    REPEAT_WINDOW,
    has_leading_os,
    opening_fragments,
    polish_chunk,
    strip_banned_sentences,
    strip_leading_os,
)
from .brain.persona import build_system_prompt, os_probability_hint
from .brain.tokens import estimate_tokens, resolve_usage
from .budget import BudgetAction, BudgetDecision, BudgetReport, estimate_cost
from .commands import CommandRegistry, CommandSpec
from .config import GroupsConfig, InjectionConfig, Settings
from .delivery.scheduler import BatchScheduler
from .gateway.base import Adapter
from .images import materialize
from .keepalive import Keepalive
from .media import materialize_attachments
from .memory import shared_facts
from .memory.context import build_injection
from .memory.rotation import apply_rotation, needs_rotation
from .memory.store import Store
from .memory.timeline import TIMELINE_LIMIT, build_batch_prompt, time_label
from .memory.workspace import Workspace
from .models import QQ_PREFIX, Attachment, ChatType, Message, RecallEvent, channel_of
from .moment import analyze, answers_bot_question, reopened_by_question
from .reply_media import build_onebot_segments, resolve_at_names, resolve_attachments
from .router import Router, now_shanghai
from .sender import is_technical_question, segment_reply, shape_reply
from .workers.failure import (
    QUOTA_KEY,
    is_agent_missing_error,
    is_quota_error,
    is_rate_limit_error,
)
from .workers.summary import summarize_for_rotation

if TYPE_CHECKING:
    from .supervisor.audit import AuditLog

log = logging.getLogger(__name__)

# images.materialize 写进 content 的路径注记；这里反过来抽取路径做「图片」提示。
_IMAGE_PATH_RE = re.compile(r"图存\s+([^\s，)）]+)")

# bot 出站附件的纯文本注记：content 是唯一持久载体，面板靠它做预览。
# 路径写成 workspace 相对 posix 形式（replies/a.png），与 /api/workspace/file 对齐。
_ATTACHMENT_NOTE_BY_KIND = {
    "image": "\n[图片] {path}",
    "voice": "\n[语音] {path}",
    "record": "\n[语音] {path}",
    "video": "\n[视频] {path}",
    "file": "\n[文件] {name} ({path})",
}


def _forward_message_nodes(item: Attachment) -> list[dict[str, Any]]:
    """把模型友好的 ``{name, content}`` 节点包成 OneBot ``node`` 段。"""
    return [
        {
            "type": "node",
            "data": {
                "name": str(node.get("name") or ""),
                "content": [
                    {"type": "text", "data": {"text": str(node.get("content") or "")}}
                ],
            },
        }
        for node in item.nodes
        if str(node.get("content") or "").strip()
    ]

SHUTUP_MINUTES = 60
ADMIN_ONLY_REPLY = "这个命令只有管理员能用。"
FORGET_REPLY = "记忆清空了，本鱼失忆中……🫧"
# 待发队列：通道恢复后补发的轮询间隔 / 重试上限 / 消息保鲜期。
OUTBOX_RETRY_SECONDS = 15.0
OUTBOX_MAX_ATTEMPTS = 8
OUTBOX_MAX_AGE_SECONDS = 6 * 3600
# 轮换摘要的输入窗口（spec §9.2：最近 40 条）。
ROTATION_WINDOW = 40
# 「此刻」注入用的常量：群名缓存一小时，查不到就省略那半句。
GROUP_NAME_TTL_SECONDS = 3600.0
GROUP_NAME_TIMEOUT_SECONDS = 2.0
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _period_of_day(hour: int) -> str:
    """把 24 小时切成模型能直接用的「时段」词，而不是让它自己算。"""
    if hour < 5:
        return "凌晨"
    if hour < 8:
        return "清晨"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


def _duration_text(seconds: int) -> str:
    if seconds < 3600:
        return f"{max(1, seconds // 60)} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


# 批次被跳过的原因。error 与 record_usage(purpose="error") 同口径
# （spec §8.4 原写作 failed，实现统一取 error，避免同一件事两个名字）。
SKIP_BUDGET = "budget"
SKIP_ERROR = "error"

# 机会检测（规则版）的阈值：命中纯热聊负分信号时先攒着不投。
# 「纯热聊」= 60 秒内 ≥5 条、没有问句、也没有求助词；单条短句不在此列。
OPPORTUNITY_HOLD_SCORE = 0.2
# 待投队列超过 pending_batch 的 20 条上限时不再攒，照旧截断送审，避免热聊撑爆队列。
PENDING_BATCH_LIMIT = 20
# 禁句 regenerate：每批最多改写这么多次，之后一律走确定性删句。
REGENERATE_ATTEMPTS = 2
# 改写走一次性补全，必须封顶等待时间，不能拖住发送链路。
REGENERATE_TIMEOUT_SECONDS = 20.0

# 跨群查证缓存注入段：只当参考，明确告诉模型可能过时、拿不准就重查。
SHARED_FACT_HEADER = "【别的群问过的同一问题（只作参考，可能过时）】"
SHARED_FACT_HINT = "这条是别的群查过的结论，别直接当事实复述；拿不准就自己再查一遍。"


@dataclass
class ToolUse:
    """本轮模型是否调用过工具、调过哪些；只做内部判定，不往群里发任何提示。"""

    used: bool = False
    names: list[str] = field(default_factory=list)

    async def record(self, name: str) -> None:
        self.used = True
        self.names.append(name)


# 调试器预览一次最多回 20000 字 prompt：够看清注入与时间线，又不会把面板撑爆。
PREVIEW_PROMPT_MAX_CHARS = 20000
PREVIEW_TRUNCATED_MARK = "\n…（已截断）"


@dataclass(frozen=True)
class BatchPreview:
    """一次「如果现在投递，会发生什么」的只读快照。

    字段与 `_run_batch_locked` 一一对应：批次组成、预算判定、最终 prompt、
    token/费用估算、下一次发送时间。预览不写库、不调模型、不动调度器。
    """

    chat_type: ChatType
    chat_id: str
    generated_at: str
    messages: tuple[Message, ...]
    addressed: bool
    addressed_message_ids: tuple[str, ...]
    current_message_id: str | None
    budget: BudgetDecision
    budget_report: BudgetReport
    would_call_model: bool
    prompt: str
    prompt_truncated: bool
    token_estimate: int
    cost_estimate: float
    next_send_at: str | None
    window: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        decision = self.budget
        report = self.budget_report
        return {
            "chat_type": self.chat_type.value,
            "chat_id": self.chat_id,
            "generated_at": self.generated_at,
            "messages": [
                {
                    "message_id": msg.message_id,
                    "nickname": msg.nickname,
                    "user_id": msg.user_id,
                    "content": msg.content,
                    "timestamp": msg.timestamp,
                    "time_label": time_label(msg.timestamp),
                    "at_bot": msg.at_bot,
                    "reply_to_bot": msg.reply_to_bot,
                    "recalled": bool(msg.recalled_at),
                    "is_current": msg.message_id == self.current_message_id,
                }
                for msg in self.messages
            ],
            "addressed": self.addressed,
            "addressed_message_ids": list(self.addressed_message_ids),
            "current_message_id": self.current_message_id,
            "budget": {
                "action": decision.action.value,
                "reason": decision.reason,
                "scope": decision.scope,
                "warnings": list(decision.warnings),
                "remaining_calls": decision.remaining_calls,
                "remaining_tokens": decision.remaining_tokens,
                "remaining_cost": decision.remaining_cost,
            },
            "budget_report": {
                "per_chat": report.per_chat,
                "global_daily": report.global_daily,
                "monthly": report.monthly,
                "cost": report.cost,
                "warnings": [dict(item) for item in report.warnings],
            },
            "would_call_model": self.would_call_model,
            "prompt": self.prompt,
            "prompt_truncated": self.prompt_truncated,
            "token_estimate": self.token_estimate,
            "cost_estimate": self.cost_estimate,
            "next_send_at": self.next_send_at,
            "window": self.window,
        }


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
        adapters: dict[str, Adapter],
        router: Router,
        persona_text: str,
        impressions: Any = None,
        workspace: Workspace | None = None,
        injection: InjectionConfig | None = None,
        audit: AuditLog | None = None,
        sleep=None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.brain = brain
        # 通道名 -> 适配器：发送按消息自带的通道前缀路由，两条通道互不越界。
        self.adapters: dict[str, Adapter] = dict(adapters)
        self.router = router
        self.budget_guard = router.budget_guard
        self.persona_text = persona_text
        self.impressions = impressions
        self.workspace = workspace
        self.injection = injection
        self.audit = audit
        self._sleep = sleep or asyncio.sleep
        self.commands = CommandRegistry()
        self._command_clock: Callable[[], float] = time.monotonic
        self._command_last: dict[tuple[str, str, str], float] = {}
        self.scheduler: BatchScheduler | None = None
        self._scheduler_task: asyncio.Task | None = None
        self.keepalive: Keepalive | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._locks: dict[tuple[ChatType, str], asyncio.Lock] = {}
        self._batch_locks: dict[tuple[ChatType, str], asyncio.Lock] = {}
        self._started_at = time.monotonic()
        # 轮换后的接续文本：成功投递才弹出，失败留到下一批重试。
        self._continuations: dict[tuple[ChatType, str], str] = {}
        # 已从库里补投过接续语的会话：同一进程内不再重复注入（重启后内存为空，
        # 会再从 session_summaries 捞一次，然后同样只注入一次）。
        self._continuation_used: set[tuple[ChatType, str]] = set()
        # 待发队列的补发任务（通道恢复后把没发出去的分片补上）。
        self._outbox_task: asyncio.Task[None] | None = None
        # 面板可改的聊天强度/言论限制：整体替换，避免逐字段同步。
        self.behavior: BehaviorSettings = settings.behavior
        # 面板可改的群白名单：Service 与 Router 都读这一份，整体替换。
        self.groups: GroupsConfig = settings.groups
        # 「此刻」用的群名缓存：chat_id -> (群名, 抓取时刻)。查不到就不写。
        self._group_names: dict[str, tuple[str, float]] = {}
        # 群名刷新是后台任务：网关慢/断线不能拖住消息处理。
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    def _adapter_for(self, chat_id: str) -> Adapter:
        """按会话 id 的通道前缀选适配器；单一派生点是 channel_of。"""
        return self.adapters[channel_of(chat_id)]

    def channel_status(self) -> dict[str, dict[str, Any]]:
        """每个通道的状态快照；未实现 snapshot 的适配器回落到裸属性。"""
        status: dict[str, dict[str, Any]] = {}
        for channel, adapter in self.adapters.items():
            snapshot = getattr(adapter, "snapshot", None)
            if callable(snapshot):
                try:
                    status[channel] = dict(snapshot())
                    continue
                except Exception:
                    log.warning("通道 %s 状态读取失败", channel, exc_info=True)
            status[channel] = {
                "connected": bool(getattr(adapter, "connected", False)),
                "degraded": bool(getattr(adapter, "degraded", False)),
                "last_event_at": getattr(adapter, "last_event_at", None),
                "last_disconnect_at": getattr(adapter, "last_disconnect_at", None),
                "last_send_at": getattr(adapter, "last_send_at", None),
            }
            # 微信形态的适配器额外暴露登录态/桌面状态；QQ 没有这两个属性，
            # 保持原来的 5 键，避免健康快照凭空多出字段。
            if hasattr(adapter, "logged_in"):
                status[channel]["logged_in"] = adapter.logged_in
            if hasattr(adapter, "desktop"):
                status[channel]["desktop"] = adapter.desktop
        return status

    async def start(self) -> None:
        self._start_scheduler()
        for adapter in self.adapters.values():
            adapter.on_message(self.handle)
            on_recall = getattr(adapter, "on_recall", None)
            if callable(on_recall):
                on_recall(self.handle_recall)
            # 微信桥重启后 resync 时靠它从 messages 表重建回声；没有该接口的
            # 假/旧适配器自然跳过，不影响 QQ。
            seed = getattr(adapter, "set_echo_seed", None)
            if callable(seed):
                seed(self._wechat_echo_seed)
        for adapter in self.adapters.values():
            await adapter.start()
        self._start_keepalive()
        self._start_outbox()

    async def stop(self) -> None:
        task = self._outbox_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._outbox_task = None
        task = self._keepalive_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._keepalive_task = None
        task = self._scheduler_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._scheduler_task = None
        for adapter in self.adapters.values():
            await adapter.stop()
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

    def _start_keepalive(self) -> None:
        """启动保活：只对 QQ 通道生效，微信小号不发探针。"""
        seconds = float(self.settings.napcat.keepalive_seconds)
        adapter = self.adapters.get("qq")
        if adapter is None:
            return
        # 适配器给的已是带前缀的字符串 id；这里不再做 int() 强转。
        bot_qq = str(getattr(adapter, "bot_qq", "") or "")
        if seconds <= 0 or not bot_qq or bot_qq == f"{QQ_PREFIX}0":
            return
        if self._keepalive_task is not None:
            return
        self.keepalive = Keepalive(adapter, bot_qq, seconds)
        self._keepalive_task = asyncio.create_task(self.keepalive.run())

    def _wechat_echo_seed(self) -> list[tuple[str, str, int]]:
        """回声重建源：echo_window 内的机器人消息（messages 表 is_bot=1）。"""
        window = int(self.settings.wechat.echo_window_seconds)
        return self.store.recent_bot_messages(int(time.time()) - window)

    def keepalive_status(self) -> dict[str, Any]:
        if self.keepalive is None:
            return {"enabled": False}
        return self.keepalive.status()

    # -------------------------------------------------------------- 待发队列
    def _start_outbox(self) -> None:
        if self._outbox_task is not None:
            return
        # 旧替身/只读场景没有这些方法：不做补发，行为与以前一致。
        if not callable(getattr(self.store, "due_outbox", None)):
            return
        self._outbox_task = asyncio.create_task(self._outbox_loop(), name="outbox")

    async def _outbox_loop(self) -> None:
        """通道恢复后把没发出去的分片补发；失败退避，过期/超限就丢。"""
        while True:
            await asyncio.sleep(OUTBOX_RETRY_SECONDS)
            try:
                await self._flush_outbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("待发队列冲刷失败")

    async def _flush_outbox(self) -> int:
        """补发到期的行；返回成功条数（测试直接调它，不用等定时器）。"""
        now = time.time()
        sent = 0
        for row in self.store.due_outbox(now):
            try:
                chat_type = ChatType(row["chat_type"])
                adapter = self._adapter_for(str(row["chat_id"]))
            except (ValueError, KeyError) as exc:
                log.warning("待发队列里有无法路由的行 %r：%s", row.get("chat_id"), exc)
                self.store.drop_outbox(int(row["id"]))
                continue
            chat_id = str(row["chat_id"])
            row_id = int(row["id"])
            lock = self._batch_locks.get((chat_type, chat_id))
            # 正在进行的那一轮回复优先：别让补发的旧分片插进新对话里。
            if lock is not None and lock.locked():
                continue
            age = now - float(row["enqueued_at"] or 0.0)
            if age > OUTBOX_MAX_AGE_SECONDS:
                log.warning(
                    "待发消息超过 %.0f 分钟，丢弃 %s: %s",
                    age / 60,
                    chat_id,
                    str(row["text"])[:40],
                )
                self.store.drop_outbox(row_id)
                continue
            if not bool(getattr(adapter, "connected", True)):
                # 通道还没恢复：留在队列里，等下一轮，不算尝试次数。
                continue
            try:
                message_id = await adapter.send(chat_type, chat_id, str(row["text"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 失败只退避，不丢队列
                attempts = self.store.defer_outbox(
                    row_id,
                    error=f"{type(exc).__name__}: {exc}",
                    next_attempt_at=now
                    + min(OUTBOX_RETRY_SECONDS * (2 ** min(int(row["attempts"]), 6)), 600.0),
                )
                log.warning("补发失败（第 %d 次）%s: %s", attempts, chat_id, exc)
                if attempts >= OUTBOX_MAX_ATTEMPTS:
                    log.warning("补发超限，丢弃 %s: %s", chat_id, str(row["text"])[:40])
                    self.store.drop_outbox(row_id)
                continue
            self.store.drop_outbox(row_id)
            self._store_outbox_message(
                chat_type, chat_id, str(row["text"]), message_id, row_id
            )
            log.info("补发成功 %s/%s（入队 %.0fs）", chat_id, row_id, age)
            sent += 1
        return sent

    def _store_outbox_message(
        self,
        chat_type: ChatType,
        chat_id: str,
        text: str,
        message_id: str | None,
        row_id: int,
    ) -> None:
        """补发成功后写回历史，保证下一轮上下文连贯。"""
        self.store.add_message(
            Message(
                chat_type=chat_type,
                chat_id=chat_id,
                user_id=self._adapter_for(chat_id).bot_id,
                nickname=self.settings.persona.name,
                content=text,
                message_id=message_id or f"outbox:{row_id}",
                is_bot=True,
                timestamp=int(now_shanghai().timestamp()),
            )
        )

    async def _send_outbox(
        self, chat_type: ChatType, chat_id: str, text: str, *, origin: str = ""
    ) -> bool:
        """尽力立即发送；失败就进待发队列（返回是否已发出）。

        用于「已经给过承诺」的补话：桥断了也不能让用户等到天荒地老。
        """
        try:
            await self._adapter_for(chat_id).send(chat_type, chat_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败转队列，不打断主流程
            log.warning("发送失败，转入待发队列 %s/%s: %s", chat_id, origin or "-", exc)
            self.store.enqueue_outbox(
                chat_type, chat_id, text, origin_message_id=origin
            )
            return False
        return True

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

    def set_behavior(self, behavior: BehaviorSettings) -> None:
        """面板保存后即时替换行为快照；下一批消息就按新设置走。"""
        self.behavior = behavior

    def set_groups(self, groups: GroupsConfig) -> None:
        """面板保存群白名单后即时生效；Service 与 Router 必须同时看到新值。"""
        self.groups = groups
        self.settings = replace(self.settings, groups=groups)
        self.router.settings = replace(self.router.settings, groups=groups)

    def _previous_message(self, msg: Message) -> Message | None:
        """紧挨着当前消息的前一条（含 bot）；查不到时返回 None。"""
        rows = self.store.recent_messages(
            msg.chat_type,
            msg.chat_id,
            2,
            upto_message_id=msg.message_id,
        )
        for index, row in enumerate(rows):
            if row.message_id == msg.message_id:
                return rows[index - 1] if index > 0 else None
        return rows[-1] if rows else None

    def _answers_bot_question(self, msg: Message) -> bool:
        """隐式点名：紧接在 bot 的提问后面回了一句，视为在跟它说话。"""
        if msg.chat_type is not ChatType.GROUP or msg.is_bot:
            return False
        return answers_bot_question(msg, self._previous_message(msg))

    def _is_addressed(self, msg: Message) -> bool:
        """显式点名（@/引用/别名）+ 隐式接话（回答 bot 的上一句提问）。"""
        return self.router.is_addressed(msg) or self._answers_bot_question(msg)

    def _lock(self, chat_type: ChatType, chat_id: str) -> asyncio.Lock:
        return self._locks.setdefault((chat_type, chat_id), asyncio.Lock())

    # 心情规则：只认「在跟它说话」的消息，避免群里聊别人也改写它的状态。
    _MOOD_JAB = ("笨", "蠢", "傻", "垃圾", "闭嘴", "没用", "讨厌", "烦死")
    _MOOD_PRAISE = ("厉害", "牛", "聪明", "可爱", "谢谢", "喜欢你", "爱你", "靠谱", "棒")

    def _note_mood(self, msg: Message) -> None:
        """被夸/被怼时改一句心情；同一句连续命中会被 store 抑制。"""
        if not self.settings.mind.enabled:
            return
        text = (msg.content or "").strip()
        if not text:
            return
        if not self._is_addressed(msg):
            return
        if any(word in text for word in self._MOOD_JAB):
            self.store.note_bot_mood("被怼了，不太高兴")
        elif any(word in text for word in self._MOOD_PRAISE):
            self.store.note_bot_mood("被夸了一句，有点飘")

    async def handle(self, msg: Message) -> None:
        annotate = self._tools_enabled()
        # 图片物化必须在入库前：content 字符串是图片路径到达大脑的唯一载体。
        # materialize 内部永不抛异常，失败保持 [图片] 占位。
        # 没有 read 工具的大脑看不到图，路径字符串只是噪音：直接跳过物化。
        await materialize(msg, self.settings, annotate=annotate)
        if annotate:
            # 微信本地库附件同理：只有能被 read 看到时才值得落盘。
            decode = None
            if self.settings.wechat.media.enabled:
                try:
                    from .gateway.wechat_bridge.media.dat import decode_dat

                    decode = decode_dat
                except Exception:
                    log.debug("微信 .dat 解码模块不可用，按原字节复制", exc_info=True)
            await materialize_attachments(msg, self.settings, decode=decode)
        # 机器人自己的消息也要先落库（作为 assistant 轮次），但不触发任何回复。
        if not self.store.add_message(msg):
            return
        if msg.is_bot:
            return
        self._note_mood(msg)

        # 重启后网关内存里的 bot_message_ids 是空的；引用老消息时回库确认
        # 「被引用的是不是我」，否则会漏掉一次点名。
        if (
            msg.chat_type is ChatType.GROUP
            and not msg.reply_to_bot
            and msg.reply_to_message_id
            and self.store.is_bot_message(
                msg.chat_type, msg.chat_id, msg.reply_to_message_id
            )
        ):
            msg.reply_to_bot = True

        # 印象抽取按消息条数触发，与是否回复无关，必须在早退分支之前调度；
        # 只对群聊抽印象（私聊内容不进 impressions，也不花抽取配额）。
        self._schedule_impressions(msg)
        # 群名是「此刻」注入的一半内容：后台刷新，过期才查，查不到就省略。
        if msg.chat_type is ChatType.GROUP:
            self._schedule_group_name(msg.chat_id)

        async with self._lock(msg.chat_type, msg.chat_id):
            # 先记用户消息：被点名会重开已收尾的话题，普通消息只刷新时间。
            self.router.note_user_message(
                msg.chat_type,
                msg.chat_id,
                addressed=self._is_addressed(msg),
            )
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

    async def handle_recall(self, event: RecallEvent) -> None:
        """撤回只做标记：原文留库，时间线渲染成占位，摘要/抽取跳过。"""
        self.store.mark_recalled(event.chat_type, event.chat_id, event.message_id)

    async def _handle_sync(self, msg: Message) -> None:
        """rules 模式：规则判定通过就同步等模型（旧行为，回滚用）。"""
        if msg.chat_type is ChatType.GROUP:
            # 安静强度：非点名消息连规则筛都不进，直接留在库里等下次点名。
            if (
                self.behavior.chat_intensity == "quiet"
                and not self._is_addressed(msg)
            ):
                log.debug("skip %s/%s: intensity_quiet", msg.chat_id, msg.message_id)
                return
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

        addressed = self._is_addressed(msg)
        reason = self._group_skip_reason(msg, addressed=addressed)
        if reason == "intensity_quiet":
            # 安静强度不是终局：消息留在待投队列，下次被点名时一起送模型。
            log.debug("skip %s/%s: intensity_quiet", msg.chat_id, msg.message_id)
            return
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
        # 安静强度：非点名消息不进批次。对消息不做 mark_skipped，留在待投队列，
        # 等下次点名时一起给模型看，避免「这条永远不进 prompt」。
        if not addressed and self.behavior.chat_intensity == "quiet":
            return "intensity_quiet"
        # 活跃强度：放开「话题收尾」和「连续空转降频」两个软闸门；
        # 静音、黑白名单、预算仍然照旧。
        if self.behavior.chat_intensity != "active":
            reopened = (
                reopened_by_question(msg.content)
                or self._answers_bot_question(msg)
            )
            # 话题已收尾：明确问句/求助/回答 bot 的提问可以重开，否则按低概率抽样。
            if (
                not addressed
                and self.router.is_thread_closed(msg.chat_type, msg.chat_id)
                and not reopened
                and not self.router.closed_sample(msg.chat_type, msg.chat_id)
            ):
                return "closed"
            # 连续三次选择不说后进入降频窗口：问句仍然可以破例，避免追问被吞。
            if (
                not addressed
                and not reopened
                and self.router.is_downshifted(msg.chat_type, msg.chat_id)
            ):
                return "downshift"
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
        self, chat_type: ChatType, chat_id: str, now: Any
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

    async def _run_batch(self, chat_type: ChatType, chat_id: str) -> None:
        """定时器到点：取批 → 预算 → 轮换 → 注入 prompt → 调模型 → 发送/静默 → 记账。

        同一会话串行：调度器的 inflight 已挡住 tick 重复触发，这里的锁再兜一层
        （drain 与 run 并发、或将来从 dashboard 直接调用时）。
        """
        lock = self._batch_locks.setdefault((chat_type, chat_id), asyncio.Lock())
        async with lock:
            await self._run_batch_locked(chat_type, chat_id)

    def preview_batch(
        self, chat_type: ChatType, chat_id: str, *, now: Any = None
    ) -> BatchPreview:
        """只读预演下一次批次：不写库、不调模型、不发消息、不动调度器。

        取批与判定顺序照抄 `_run_batch_locked`，两边的差异只能是「副作用」，
        不能是「看到的东西不一样」。
        """
        moment = now or now_shanghai()
        latest = self.store.recent_messages(chat_type, chat_id, 1)
        batch = (
            self.store.pending_batch(chat_type, chat_id, latest[0].message_id)
            if latest
            else []
        )

        addressed_ids = tuple(m.message_id for m in batch if self._is_addressed(m))
        addressed = bool(addressed_ids)
        if addressed_ids:
            current_id: str | None = addressed_ids[-1]
        elif batch:
            current_id = batch[-1].message_id
        else:
            current_id = None

        decision = self.budget_guard.evaluate(
            chat_type,
            chat_id,
            addressed=addressed,
            purpose="reply" if addressed else "ambient",
            now=moment,
        )
        report = self.budget_guard.report(chat_type, chat_id, now=moment)
        quiet_blocked = not addressed and self.behavior.chat_intensity == "quiet"
        prompt = ""
        truncated = False
        opportunity_hold = False
        if batch:
            ids = [m.message_id for m in batch]
            omitted = self.store.count_undelivered_before(chat_type, chat_id, ids[0])
            opportunity_hold = self._opportunity_hold(
                batch,
                addressed=addressed,
                now_ts=int(moment.timestamp()),
                backlog=omitted,
            )
            prompt = self._build_prompt(
                chat_type,
                chat_id,
                batch,
                continuation=self._continuation_for(chat_type, chat_id),
                omitted=omitted,
                # 预览不能推进跨群查证缓存的使用计数。
                fact_hint=self._shared_fact_hint(
                    chat_type, chat_id, batch, bump=False
                ),
                # 预览必须和真实批次看到同一刻，否则「此刻」「时机」会漂。
                now=moment,
            )
            if len(prompt) > PREVIEW_PROMPT_MAX_CHARS:
                prompt = prompt[:PREVIEW_PROMPT_MAX_CHARS] + PREVIEW_TRUNCATED_MARK
                truncated = True

        would_call = (
            bool(batch)
            and self._budget_allows(decision, addressed)
            and not quiet_blocked
            and not opportunity_hold
        )
        tokens = estimate_tokens(prompt)
        window: dict[str, Any] = (
            self.scheduler.window_snapshot(chat_type, chat_id)
            if self.scheduler is not None
            else {"queued": False}
        )
        return BatchPreview(
            chat_type=chat_type,
            chat_id=chat_id,
            generated_at=moment.isoformat(timespec="seconds"),
            messages=tuple(batch),
            addressed=addressed,
            addressed_message_ids=addressed_ids,
            current_message_id=current_id,
            budget=decision,
            budget_report=report,
            would_call_model=would_call,
            prompt=prompt,
            prompt_truncated=truncated,
            token_estimate=tokens,
            cost_estimate=estimate_cost(tokens, 0, self.settings.budget),
            next_send_at=window.get("due_at"),
            window=window,
        )

    def _opportunity_hold(
        self, batch: list[Message], *, addressed: bool, now_ts: int, backlog: int
    ) -> bool:
        """机会检测（规则版）：纯热聊/纯斗图没有钩子时先攒着不投。

        只认 P0 的负分信号 `hot`（60 秒内 ≥5 条且无问句）；单条短句仍照旧送审，
        避免把「帮我看看」这类短请求误吞。待投队列快到 `pending_batch` 的 20 条
        上限时也不再攒，否则热聊会把队列越撑越大。
        """
        if not self.settings.persona.opportunity_guard:
            return False
        if len(batch) + backlog >= PENDING_BATCH_LIMIT:
            return False
        signals = analyze(batch, now_ts=now_ts, addressed=addressed)
        return (
            signals.hot
            and not signals.question
            and not signals.help
            and signals.score <= OPPORTUNITY_HOLD_SCORE
        )

    async def _run_batch_locked(self, chat_type: ChatType, chat_id: str) -> None:
        latest = self.store.recent_messages(chat_type, chat_id, 1)
        if not latest:
            return
        batch = self.store.pending_batch(chat_type, chat_id, latest[0].message_id)
        if not batch:
            return

        if batch[-1].recalled_at:
            # 触发这一轮的最后一条被撤回：整批不回复，但游标要推进，避免反复重试。
            self.store.mark_delivered(
                chat_type, chat_id, *[m.message_id for m in batch]
            )
            log.info("批次取消（末条已撤回）%s/%s", chat_id, batch[-1].message_id)
            return

        ids = [m.message_id for m in batch]
        # 积压超过窗口时更早的消息不进 prompt：给模型留一句断层提示，别当成连续对话。
        backlog = self.store.count_undelivered_before(chat_type, chat_id, ids[0])
        if backlog:
            log.info(
                "批次积压 %s/%s backlog=%d window=%d",
                chat_id,
                ids[0],
                backlog,
                len(ids),
            )
        addressed_ids = [
            m.message_id for m in batch if self._is_addressed(m)
        ]
        addressed = bool(addressed_ids)
        current_id = addressed_ids[-1] if addressed_ids else ids[-1]
        key = (chat_type, chat_id)

        # 切到安静强度前已经排队的批次：非点名也不送模型。不 mark_skipped，
        # 消息留在待投队列里，等下一次点名再一起看。
        if not addressed and self.behavior.chat_intensity == "quiet":
            log.debug("批次跳过（安静强度）%s/%s", chat_id, current_id)
            return

        # 机会检测（规则版）：纯热聊/纯斗图先攒着不投。
        # 关键是不 mark_skipped：游标不动，下次被点名或有问句时连这批一起进 prompt。
        if self._opportunity_hold(
            batch,
            addressed=addressed,
            now_ts=int(now_shanghai().timestamp()),
            backlog=backlog,
        ):
            log.debug(
                "批次攒着不投（纯热聊）%s/%s ids=%d", chat_id, current_id, len(batch)
            )
            return

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

        continuation = self._continuation_for(chat_type, chat_id)
        fact_hint = self._shared_fact_hint(chat_type, chat_id, batch)
        prompt = self._build_prompt(
            chat_type,
            chat_id,
            batch,
            continuation=continuation,
            omitted=backlog,
            fact_hint=fact_hint,
        )

        log.info(
            "批次送审 %s/%s ids=%d addressed=%s",
            chat_type.value,
            chat_id,
            len(batch),
            addressed,
        )
        tool_use = ToolUse()
        try:
            reply = await self._call_brain(
                chat_type, chat_id, batch, prompt, on_tool=tool_use.record
            )
        except BrainError as exc:
            # 失败也占一次调用额度；游标随 mark_skipped 推进，不重投。
            # 不再往群里发「断线了」这类失败提示：用户明确不要，失败只留日志。
            self.store.record_usage(chat_type, chat_id, "error", 0, 0)
            self.store.mark_skipped(chat_type, chat_id, ids, SKIP_ERROR)
            log.warning("批次调用失败 %s/%s: %s", chat_id, current_id, exc)
            self._check_workspace(chat_type, chat_id)
            return

        sent = await self._send_batch_reply(batch, reply, chat_type, chat_id)
        if not sent:
            self.router.note_no_reply(chat_type, chat_id)
        self._remember_shared_fact(chat_type, chat_id, batch, reply, tool_use)

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
        if continuation:
            # 已消费：同一进程内不再从库里重复注入（内存里的到这一步也已弹出）。
            self._continuation_used.add(key)
        log.debug("本轮工具调用 %s/%s: %s", chat_id, current_id, tool_use.names)
        self._check_workspace(chat_type, chat_id)

    def _shared_fact_hint(
        self,
        chat_type: ChatType,
        chat_id: str,
        messages: list[Message],
        *,
        bump: bool = True,
    ) -> str | None:
        """命中跨群查证缓存时给 prompt 加一段参考。

        只在群聊里查：私聊的内容本来就不该进共享缓存（写入侧也拦）。
        `bump=False` 供调试器预览：只读，不推进使用计数。
        """
        if chat_type is not ChatType.GROUP:
            return None
        question = self._shared_fact_question(messages)
        if question is None:
            return None
        fact = shared_facts.pick_best(self.store.shared_facts(), question)
        if fact is None:
            return None
        if bump:
            self.store.bump_shared_fact(fact.id)
            log.info("命中跨群查证缓存 %s/%s fact=%s", chat_type, chat_id, fact.id)
        return (
            f"{SHARED_FACT_HEADER}\n"
            f"问：{fact.question_text}\n"
            f"答：{fact.answer_text}\n"
            f"{SHARED_FACT_HINT}"
        )

    def _remember_shared_fact(
        self,
        chat_type: ChatType,
        chat_id: str,
        messages: list[Message],
        reply: BrainReply,
        tool_use: ToolUse,
    ) -> None:
        """本轮查证成功（调过工具且有正式回复）时写入跨群缓存。

        只记群聊、只记带提问的消息；隐私过滤在 `shared_facts.extract` 里，
        存储层还会再拦一次，防止以后别处调用绕过。
        """
        if chat_type is not ChatType.GROUP or not tool_use.used or not reply.speak:
            return
        question = self._shared_fact_question(messages)
        if question is None:
            return
        nickname = next(
            (
                message.nickname
                for message in reversed(messages)
                if not message.is_bot and message.content.strip()
            ),
            "",
        )
        fact_id = self.store.add_shared_fact(
            question, reply.reply, chat_type, chat_id, nickname
        )
        if fact_id:
            log.info(
                "写入跨群查证缓存 %s/%s fact=%s", chat_type, chat_id, fact_id
            )

    @staticmethod
    def _shared_fact_question(messages: list[Message]) -> str | None:
        """批次里最后一条「像提问」的人类消息；没有就返回 None。"""
        for message in reversed(messages):
            if message.is_bot or message.recalled_at:
                continue
            text = message.content.strip()
            if text and shared_facts.looks_like_question(text):
                return text
        return None

    async def _maybe_emoji_reaction(
        self, chat_type: ChatType, chat_id: str, message_id: str | None
    ) -> None:
        """mind.emoji_reactions 开启时，给刚发出的 QQ 消息贴一个表情回应。

        只有适配器真的实现 ``set_msg_emoji_like`` 时才尝试；任何失败只记
        debug，不影响已经完成的发送。
        """
        if not message_id:
            return
        if not bool(getattr(self.settings.mind, "emoji_reactions", False)):
            return
        adapter = self._adapter_for(chat_id)
        react = cast(
            Callable[..., Awaitable[bool]] | None,
            getattr(adapter, "set_msg_emoji_like", None),
        )
        if not callable(react):
            return
        emoji = str(getattr(self.settings.mind, "emoji_reaction_id", "128") or "128")
        try:
            ok = await react(chat_type, chat_id, str(message_id), emoji)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 表情回应是加法项
            log.debug("表情回应异常 %s/%s: %s", chat_id, message_id, exc)
            return
        if not ok:
            log.debug("表情回应未生效 %s/%s", chat_id, message_id)

    async def _send_chunk(
        self,
        chat_type: ChatType,
        chat_id: str,
        chunk: str,
        *,
        quote_id: str | None = None,
        attachments: tuple[Attachment, ...] = (),
        at_qq_ids: tuple[str, ...] = (),
        at_names: tuple[str, ...] = (),
    ) -> str | None:
        """发一片回复；有附件/@/引用时升级为消息段，失败退回纯文本。

        引用、@、附件都只影响「这一条怎么发」，不能让回复本身发不出去：
        适配器没有对应能力（微信桥没有消息段）、NapCat 拒收（老版本/消息已
        撤回）或本地文件上传失败时一律降级为纯文本，且不重试。
        """
        adapter = self._adapter_for(chat_id)

        # 合并转发：OneBot 独立接口；适配器不支持/调用失败时降级成一行文本提示，
        # 不把异常抛回上层（转发始终是加法项）。
        forward_id: str | None = None
        forwards = [
            item
            for item in attachments
            if str(item.kind or "").strip().lower() == "forward" and item.nodes
        ]
        if forwards:
            forwarder = cast(
                Callable[..., Awaitable[str | None]] | None,
                getattr(adapter, "send_forward", None),
            )
            for item in forwards:
                if not callable(forwarder):
                    chunk += f"\n[合并转发 {len(item.nodes)} 条]"
                    continue
                try:
                    forward_id = await forwarder(
                        chat_type, chat_id, _forward_message_nodes(item)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 转发是加法项
                    log.warning("合并转发失败，降级为文本 %s: %s", chat_id, exc)
                    chunk += f"\n[合并转发 {len(item.nodes)} 条]"
            attachments = tuple(
                item
                for item in attachments
                if str(item.kind or "").strip().lower() != "forward"
            )

        # 文件类附件走独立上传接口；上传失败只记日志，文本照发。
        uploader = cast(
            Callable[..., Awaitable[None]] | None,
            getattr(adapter, "upload_file", None),
        )
        if callable(uploader):
            for item in attachments:
                if item.kind != "file" or not item.path:
                    continue
                try:
                    await uploader(chat_type, chat_id, item.path, name=item.name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 附件是加分项
                    log.warning("附件上传失败（继续发文本）%s: %s", chat_id, exc)

        media = tuple(item for item in attachments if item.kind != "file")

        # 没有消息段能力的通道（微信桥）：逐条走自己的媒体接口，再发正文。
        # 失败只记日志，正文照发——附件是加分项。
        if media and not callable(getattr(adapter, "send_segments", None)):
            push_media = cast(
                Callable[..., Awaitable[Any]] | None,
                getattr(adapter, "send_media", None),
            )
            if callable(push_media):
                for item in media:
                    if not item.path:
                        continue
                    try:
                        await push_media(
                            chat_type, chat_id, item.kind, item.path, item.name
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - 附件是加分项
                        log.info("媒体发送失败（继续发文本）%s: %s", chat_id, exc)
                media = ()

        if quote_id or at_qq_ids or media:
            sender = cast(
                Callable[..., Awaitable[str | None]] | None,
                getattr(adapter, "send_segments", None),
            )
            if callable(sender):
                try:
                    return await sender(
                        chat_type,
                        chat_id,
                        build_onebot_segments(
                            chunk,
                            quote_id=quote_id,
                            at_qq_ids=at_qq_ids,
                            attachments=media,
                        ),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 消息段是加分项
                    log.info("消息段发送失败，退回纯文本 %s: %s", chat_id, exc)

        if at_names:
            at_sender = cast(
                Callable[..., Awaitable[str | None]] | None,
                getattr(adapter, "send_at", None),
            )
            if callable(at_sender):
                try:
                    return await at_sender(chat_type, chat_id, chunk, list(at_names))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - @ 是加分项
                    log.info("带 @ 发送失败，退回纯文本 %s: %s", chat_id, exc)

        # 只有转发、没有正文/附件时，直接返回转发消息 id，避免再发一条空文本。
        if not chunk and not at_names and not attachments and not quote_id:
            return forward_id
        return await adapter.send(chat_type, chat_id, chunk)

    def _safe_reply_attachments(
        self, reply: BrainReply, chat_id: str
    ) -> tuple[Attachment, ...]:
        """解析模型附件；解析器自身出错也只丢附件，不影响文本。"""
        try:
            return tuple(resolve_attachments(reply, self.settings))
        except Exception as exc:  # noqa: BLE001 - 文本优先
            log.warning("附件解析失败，本条只发文本 %s: %s", chat_id, exc)
            return ()

    @staticmethod
    def _at_qq_ids(
        names: tuple[str, ...], participants: list[Message]
    ) -> tuple[str, ...]:
        """名字 → 本批次第一个同昵称成员的 user_id；去重、封顶 3、跳过 bot。"""
        ids: list[str] = []
        seen: set[str] = set()
        for name in names:
            for message in participants:
                if message.is_bot:
                    continue
                if (message.nickname or "").strip() != name:
                    continue
                user_id = str(message.user_id or "").strip()
                # OneBot 的 at 段要裸号；仓库内部 id 带 "qq:" 前缀。
                user_id = user_id.removeprefix(QQ_PREFIX)
                if user_id and user_id not in seen:
                    seen.add(user_id)
                    ids.append(user_id)
                break
            if len(ids) >= 3:
                break
        return tuple(ids)

    def _attachment_note(self, item: Attachment) -> str:
        """单个附件的 content 注记；路径转成 workspace 相对形式供面板预览。"""
        path = str(item.path or "").strip()
        template = _ATTACHMENT_NOTE_BY_KIND.get((item.kind or "").strip().lower())
        if not path or template is None:
            return ""
        display = path
        if self.settings.workspace.enabled and self.settings.workspace.dir:
            try:
                root = Path(self.settings.workspace.dir).resolve()
                display = (
                    Path(path).resolve(strict=False).relative_to(root).as_posix()
                )
            except (OSError, ValueError):
                display = path
        name = (item.name or Path(path).name).strip() or Path(path).name
        return template.format(path=display, name=name)

    def _annotate_attachments(
        self, chunk: str, attachments: tuple[Attachment, ...]
    ) -> str:
        """把出站附件写成纯文本注记；附件永不进 pending_outbox。"""
        if not attachments:
            return chunk
        notes = [note for item in attachments if (note := self._attachment_note(item))]
        return chunk + "".join(notes)

    def _guard_chunks(
        self, chat_type: ChatType, chat_id: str, chunks: list[str]
    ) -> list[str]:
        """重复守卫 + 内心 OS 抑制：读不到历史就原样放行。"""
        if not chunks:
            return chunks
        try:
            recent = [
                message.content
                for message in self.store.recent_bot_messages_in(
                    chat_type, chat_id, REPEAT_WINDOW
                )
            ]
        except Exception as exc:  # noqa: BLE001 - 守卫是加分项，不能挡正常发送
            log.debug(
                "重复守卫读取历史失败 %s/%s：%s", chat_id, chat_type.value, exc
            )
            return chunks

        # 内心 OS 频率抑制：最近 10 条自己真冒过 OS，才剥这轮的行首 OS。
        # 与 repeat_guard 独立：关掉重复守卫不该把 OS 抑制一起关掉。
        if any(has_leading_os(text) for text in recent[:10]):
            stripped: list[str] = []
            for chunk in chunks:
                cleaned = strip_leading_os(chunk)
                if not cleaned:
                    log.info(
                        "OS 抑制：丢弃纯 OS 分片 %s/%s %r",
                        chat_id,
                        chat_type.value,
                        chunk[:40],
                    )
                    continue
                if cleaned != chunk:
                    log.info(
                        "OS 抑制：剥离行首 OS %s/%s %r -> %r",
                        chat_id,
                        chat_type.value,
                        chunk[:40],
                        cleaned[:40],
                    )
                stripped.append(cleaned)
            chunks = stripped
            if not chunks:
                return chunks

        if not getattr(self.settings.persona, "repeat_guard", True):
            return chunks
        kept: list[str] = []
        for chunk in chunks:
            guarded = polish_chunk(chunk, recent)
            if guarded is None:
                log.info(
                    "重复守卫：丢弃分片 %s/%s %r", chat_id, chat_type.value, chunk[:40]
                )
                continue
            if guarded != chunk:
                log.info(
                    "重复守卫：改写分片 %s/%s %r -> %r",
                    chat_id,
                    chat_type.value,
                    chunk[:40],
                    guarded[:40],
                )
            # 中性短句本身也可能被运营者设成禁句；strict/regenerate 下命中就整片不发。
            # soft 只提示不拦截，保持既有「仍发送」语义。
            if (
                self.behavior.restriction_mode in ("strict", "regenerate")
                and self.behavior.match(guarded) is not None
            ):
                log.warning(
                    "重复守卫改写后仍命中言论限制，丢弃分片 %s/%s %r",
                    chat_id,
                    chat_type.value,
                    guarded[:40],
                )
                continue
            kept.append(guarded)
            # 同一轮后续分片也不能和刚定下的前一片重复。
            recent.append(guarded)
        return kept

    async def _send_batch_reply(
        self, batch: list[Message], reply: BrainReply, chat_type: ChatType, chat_id: str
    ) -> list[tuple[str, str | None]]:
        """把批次回复分片发出并入库；返回 (分片文本, 网关真实 id) 列表。"""
        if reply.read or reply.think:
            log.debug(
                "模型读/想 %s/%s read=%s think=%s",
                chat_type.value,
                chat_id,
                reply.read or "-",
                reply.think or "-",
            )
        if not reply.speak:
            log.info(
                "模型选择不说 %s/%s ids=%d reason=%s",
                chat_type.value,
                chat_id,
                len(batch),
                (reply.reason or "-").strip()[:120],
            )
            return []
        addressed = any(self._is_addressed(m) for m in batch)
        reply_text = await self._apply_restrictions(
            chat_type, chat_id, reply.reply, addressed=addressed
        )
        if reply_text is None:
            return []
        chunks = shape_reply(
            reply_text,
            self.settings.persona.max_reply_chars,
            self.settings.persona.max_reply_chars_technical,
            is_technical=is_technical_question(batch[-1].content),
        )
        # 单条回复够长且有多句时拆成 2-3 条短消息；>500 字的技术长文仍按原分片走。
        if len(chunks) == 1:
            chunks = segment_reply(chunks[0])
        if not chunks:
            return []
        chunks = self._guard_chunks(chat_type, chat_id, chunks)
        if not chunks:
            return []
        # 附件与 @ 只在守卫确认有内容要发之后才解析/上传：文本被整体丢弃时
        # 一个字节都不许发出去。
        attachments = self._safe_reply_attachments(reply, chat_id)
        at_names = tuple(resolve_at_names(reply.at, batch))
        at_qq_ids = self._at_qq_ids(at_names, batch)
        sent: list[tuple[str, str | None]] = []
        defer = 0
        delay = float(getattr(self.settings.persona, "segment_delay_seconds", 0.0))
        # 只引用被点名/被回复的那条，且只挂在第一片上（后面几片是同一轮接话）。
        addressed_ids = [m.message_id for m in batch if self._is_addressed(m)]
        quote_id = (
            addressed_ids[-1]
            if addressed_ids and self.settings.persona.quote_reply
            else None
        )
        for index, chunk in enumerate(chunks):
            if index and delay > 0:
                await self._sleep(delay)
            try:
                message_id = await self._send_chunk(
                    chat_type,
                    chat_id,
                    chunk,
                    quote_id=quote_id if index == 0 else None,
                    attachments=attachments if index == 0 else (),
                    at_qq_ids=at_qq_ids if index == 0 else (),
                    at_names=at_names if index == 0 else (),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络抖动不应拖垮整条链路
                log.warning("发送失败 %s/%s: %s", chat_id, batch[-1].message_id, exc)
                # 失败这条和后面所有分片一起进待发队列：通道恢复后补发，
                # 不让这段回复永久丢失。
                for rest in chunks[index:]:
                    if self.store.enqueue_outbox(
                        chat_type, chat_id, rest, origin_message_id=batch[-1].message_id
                    ):
                        defer += 1
                break
            sent.append((chunk, message_id))
            await self._maybe_emoji_reaction(chat_type, chat_id, message_id)
        if defer:
            log.info("发送失败，待发队列 +%d %s/%s", defer, chat_id, batch[-1].message_id)
        else:
            self.router.note_bot_spoke(chat_type, chat_id, ambient=not addressed)
        # 即使中途失败，已经发出去的分片也要进入历史，避免上下文断裂。
        self._store_bot_chunks(batch[-1], sent, attachments=attachments)
        return sent

    async def _apply_restrictions(
        self, chat_type: ChatType, chat_id: str, text: str, *, addressed: bool
    ) -> str | None:
        """言论限制的最后一道网：按模式决定放行、改写还是丢弃。

        关键词级匹配是兜底，不保证语义审核；真正的约束在注入的提示里。
        regenerate 先让模型改写（每批最多 REGENERATE_ATTEMPTS 次），仍命中
        则删掉含禁句的整句；删不出干净内容就不发。
        """
        violation = self.behavior.match(text)
        if violation is None:
            return text
        mode = self.behavior.restriction_mode
        if mode == "strict":
            log.warning("回复命中言论限制，整条丢弃：%s", violation)
            return None
        if mode != "regenerate":
            log.warning("回复触及言论限制（提示模式，仍发送）：%s", violation)
            return text
        for attempt in range(REGENERATE_ATTEMPTS):
            rewritten = await self._regenerate_reply(
                chat_type, chat_id, text, addressed=addressed
            )
            if not rewritten:
                break
            if self.behavior.match(rewritten) is None:
                log.info(
                    "禁句改写成功（第 %d 次）%s/%s", attempt + 1, chat_id, chat_type.value
                )
                return rewritten
        cleaned = strip_banned_sentences(text, self.behavior.banned_phrases)
        if cleaned and self.behavior.match(cleaned) is None:
            log.info("禁句删句后发送 %s/%s：%r", chat_id, chat_type.value, cleaned[:40])
            return cleaned
        log.warning("禁句改写与删句都失败，本次不发：%s", violation)
        return None

    async def _regenerate_reply(
        self, chat_type: ChatType, chat_id: str, text: str, *, addressed: bool
    ) -> str:
        """让模型把命中禁句的回复改写一版；能力缺失或失败时返回空串。

        走 ``complete_detailed`` 的一次性补全（临时 session），不污染聊天上下文。
        用量记进 reply/ambient，和主回复同口径，避免预算与面板对不上。
        """
        detailed = getattr(self.brain, "complete_detailed", None)
        if not callable(detailed):
            return ""
        # callable() 会把 Any 收窄成「返回 object 的函数」，这里再放宽一次，
        # 否则 wait_for 的参数类型会被误判。
        rewrite: Any = detailed
        system = self._system_prompt(chat_type, [], chat_id)
        banned = "、".join(
            item for item in (*self.behavior.banned_phrases, *self.behavior.banned_topics) if item
        )
        prompt = (
            "把下面这条回复改写一遍，保留原意和语气，但不要出现这些内容："
            f"{banned}。只输出改写后的回复正文，不要解释。\n\n{text}"
        )
        try:
            raw, prompt_tokens, completion_tokens = await asyncio.wait_for(
                rewrite(system, [{"role": "user", "content": prompt}]),
                timeout=REGENERATE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 改写失败就退回确定性删句
            log.warning("禁句改写调用失败 %s/%s：%s", chat_id, chat_type.value, exc)
            return ""
        prompt_tokens, completion_tokens, estimated = resolve_usage(
            f"{system}\n{prompt}", raw or "", prompt_tokens, completion_tokens
        )
        self.store.record_usage(
            chat_type,
            chat_id,
            "reply" if addressed else "ambient",
            prompt_tokens,
            completion_tokens,
            estimated,
        )
        return (raw or "").strip()

    def _chat_budget_decision(
        self, chat_type: ChatType, chat_id: str, *, addressed: bool
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

    def _budget_for(self, chat_type: ChatType, chat_id: str, *, addressed: bool) -> bool:
        """80/20 预算分层：非点名只能用 ambient 额度，点名可用剩下的全部。"""
        return self._budget_allows(
            self._chat_budget_decision(chat_type, chat_id, addressed=addressed),
            addressed,
        )

    def _continuation_for(self, chat_type: ChatType, chat_id: str) -> str | None:
        """本轮要注入的接续语：内存优先，重启后回落到库里的 24h 摘要。

        只为拼 prompt 取值，不在这里删除：失败的那一轮必须能原样重投。
        """
        key = (chat_type, chat_id)
        text = self._continuations.get(key)
        if text:
            return text
        if key in self._continuation_used:
            return None
        return self.store.recent_session_summary(chat_type, chat_id)

    async def _maybe_rotate(self, chat_type: ChatType, chat_id: str) -> None:
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
            # 轮换产出了新接续语，旧的「已消费」标记必须失效，否则这条新语不会注入。
            self._continuation_used.discard((chat_type, chat_id))

    def _check_workspace(self, chat_type: ChatType, chat_id: str) -> None:
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

    def _image_hint(
        self, chat_type: ChatType, chat_id: str, messages: list[Message]
    ) -> str:
        """本轮或最近提到图时，给模型一段「先 read 再看」的硬提示。

        只靠 content 里的「可用 read 查看」不够：模型会跳过图片，直接回
        「图没传到我这儿」。这里把最近一张点名，并要求先读再答；如果图在
        更早的会话里（轮换后不在 batch 中），就从最近消息里补出来。
        """
        paths: list[tuple[Message, str]] = []
        missing = 0
        for message in messages:
            if message.recalled_at:
                continue
            found = _IMAGE_PATH_RE.findall(message.content or "")
            if found:
                for path in found:
                    paths.append((message, path))
            elif "[图片]" in (message.content or ""):
                missing += 1

        if not paths and not missing:
            # 当前 batch 没有图，但有人在问图：把最近发过的图补进来。
            asked = any(
                word in (m.content or "")
                for m in messages
                for word in ("图", "照片", "截图", "看图", "上面", "刚才")
            )
            if not asked:
                return ""
            rows = self.store.recent_messages(chat_type, chat_id, 60)
            seen: set[str] = set()
            for row in rows:
                if row.recalled_at:
                    continue
                for path in _IMAGE_PATH_RE.findall(row.content or ""):
                    if path in seen:
                        continue
                    seen.add(path)
                    paths.append((row, path))
            paths = paths[-3:]
            if not paths:
                return ""

        if not paths:
            return (
                "【图片】本轮有图片没有取到内容（下载失败或无可用来源），"
                "只能确认有图，不要编造图里是什么。"
            )

        shown = paths[-4:]
        parts: list[str] = ["【图片】"]
        parts.append(f"最近发过的图（共 {len(shown)} 张）：")
        for index, (message, path) in enumerate(shown):
            suffix = "（最近一张）" if index == len(shown) - 1 else ""
            parts.append(f"- {path}（来自 {message.nickname}）{suffix}")
        parts.append(
            "涉及图片内容的问题，必须先用 read 打开最近那张再回答；"
            "如果问题明显指更早的图，再读对应那张。"
            "不要凭想象描述，也不要说“看不到”。"
        )
        if shown[-1][1].lower().endswith(".gif"):
            parts.append("最近一张是动图，read 后按首帧理解即可。")
        if missing:
            parts.append(
                f"另有 {missing} 张图片没有取到内容（下载失败或无可用来源），"
                "只能确认有图，不要编造图里是什么。"
            )
        return "".join(parts)

    def _schedule_group_name(self, chat_id: str) -> None:
        """群名缓存过期才起一个后台刷新任务；查不到就一直省着那半句。"""
        cached = self._group_names.get(chat_id)
        if cached and time.monotonic() - cached[1] < GROUP_NAME_TTL_SECONDS:
            return
        task = asyncio.ensure_future(self._refresh_group_name(chat_id))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _refresh_group_name(self, chat_id: str) -> None:
        """向网关要群名；适配器不支持（微信桥）或失败都静默跳过。

        失败也写空名占位：同一条 TTL 内不再重试，否则网关抽风时每条消息都会
        多打一次 `get_group_info`。
        """
        name = await self._fetch_group_name(chat_id)
        self._group_names[chat_id] = (name, time.monotonic())

    async def _fetch_group_name(self, chat_id: str) -> str:
        try:
            adapter = self._adapter_for(chat_id)
        except KeyError:
            return ""
        call = getattr(adapter, "call_action", None)
        if not callable(call):
            return ""
        bare = chat_id.removeprefix(QQ_PREFIX)
        if not bare.isdigit():
            return ""
        try:
            data = await asyncio.wait_for(
                call("get_group_info", {"group_id": int(bare)}),
                GROUP_NAME_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 群名只是锦上添花
            log.debug("群名获取失败 %s: %s", chat_id, exc)
            return ""
        return str(data.get("group_name") or "").strip() if isinstance(data, dict) else ""

    def _scene_hint(
        self,
        chat_type: ChatType,
        chat_id: str,
        messages: list[Message],
        *,
        now: Any = None,
    ) -> str:
        """「此刻」：现在几点、这是哪个群、你多久没开口、刚才是谁在说。

        同一句「在吗」，凌晨和下午该有的反应完全不同。缺哪项就省哪项，
        绝不编造；整段控制在一行内，不抢记忆注入的注意力。
        """
        now = now or now_shanghai()
        parts = [
            (
                f"现在是 {now:%m-%d} {_WEEKDAYS[now.weekday()]} {now:%H:%M}"
                f"（{_period_of_day(now.hour)}）"
            )
        ]
        if chat_type is ChatType.GROUP:
            name = self._group_names.get(chat_id, ("", 0.0))[0]
            if name:
                parts.append(f"本群：{name}")
        try:
            state = self.store.load_chat_state(chat_type, chat_id) or {}
        except Exception:  # noqa: BLE001 - 状态读不到就不提这半句
            state = {}
        last_spoke = state.get("last_bot_spoke")
        if isinstance(last_spoke, (int, float)) and last_spoke > 0:
            delta = int(now.timestamp() - float(last_spoke))
            if delta >= 60:
                parts.append(f"距你上次开口 {_duration_text(delta)}")
            elif delta >= 0:
                parts.append("你刚刚才说过话")
        if messages and not messages[-1].is_bot:
            parts.append(f"刚才是 {messages[-1].nickname} 在说")
        return "【此刻】" + "；".join(parts) + "。"

    def _build_prompt(
        self,
        chat_type: ChatType,
        chat_id: str,
        messages: list[Message],
        *,
        continuation: str | None = None,
        omitted: int = 0,
        fact_hint: str | None = None,
        now: Any = None,
    ) -> str:
        """三种模式共用的 prompt：长期记忆注入 + 时间线（单条也走这里）。"""
        moment = now or now_shanghai()
        injection_cfg = self.injection or InjectionConfig()
        recall_queries = [
            message.content
            for message in messages
            if not message.is_bot and not message.recalled_at and message.content.strip()
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
            today=moment.date().isoformat(),
            recall_queries=recall_queries,
            exclude_ids=exclude_ids,
        )
        if injection_cfg.scene_enabled:
            scene = self._scene_hint(chat_type, chat_id, messages, now=moment)
            if scene:
                injection = f"{scene}\n\n{injection}" if injection.strip() else scene

        framing = self.behavior.framing()
        if framing:
            injection = f"{injection}\n\n{framing}" if injection.strip() else framing

        if fact_hint:
            injection = (
                f"{injection}\n\n{fact_hint}" if injection.strip() else fact_hint
            )

        # 时机提示只给群聊：私聊无所谓「插不插嘴」。
        if chat_type is ChatType.GROUP and messages:
            # 心智层关闭时不查 open_loops、不注入开场负例：默认行为必须与今天一致。
            pending_loop = (
                self._pending_loop_hint(chat_type, chat_id)
                if self.settings.mind.enabled
                else ""
            )
            signals = analyze(
                messages,
                now_ts=int(moment.timestamp()),
                addressed=any(self._is_addressed(m) for m in messages),
                followed_up=any(self._answers_bot_question(m) for m in messages),
                pending_loop=pending_loop,
            )
            hint = signals.hint()
            if hint:
                injection = f"{injection}\n\n{hint}" if injection.strip() else hint

            if self.settings.mind.enabled:
                openings = self._opening_negatives(chat_type, chat_id)
                if openings:
                    negatives = "【别再用这些开头】" + "、".join(openings)
                    injection = (
                        f"{injection}\n\n{negatives}"
                        if injection.strip()
                        else negatives
                    )

        image_hint = self._image_hint(chat_type, chat_id, messages)
        if image_hint:
            injection = f"{injection}\n\n{image_hint}" if injection.strip() else image_hint

        reply_ids = [
            m.reply_to_message_id for m in messages if m.reply_to_message_id
        ]
        reply_names = (
            self.store.message_nicknames(chat_type, chat_id, reply_ids)
            if reply_ids
            else None
        )
        return build_batch_prompt(
            messages,
            injection,
            continuation=continuation,
            omitted=omitted,
            reply_names=reply_names,
        )

    def _pending_loop_hint(self, chat_type: ChatType, chat_id: str) -> str:
        """最近一件「没下文的事」，只截 40 字给提示层用。

        查库失败一律 fail-open：提示是锦上添花，不能因为 read 出错挡住回复。
        """
        try:
            loop = self.store.latest_open_loop(chat_type, chat_id)
        except Exception as exc:  # noqa: BLE001 - 提示层失败不影响主链路
            log.debug("读取未了事失败，跳过提示：%s", exc)
            return ""
        if loop is None:
            return ""
        return loop.content.strip()[:40]

    def _opening_negatives(self, chat_type: ChatType, chat_id: str) -> list[str]:
        """本群最近用过的开场，作为「别再用这些开头」的负例。"""
        try:
            recent = self.store.recent_bot_messages_in(chat_type, chat_id, 10)
        except Exception as exc:  # noqa: BLE001 - 提示层失败不影响主链路
            log.debug("读取最近发言失败，跳过开场负例：%s", exc)
            return []
        return opening_fragments([message.content for message in recent])

    async def _call_brain(
        self,
        chat_type: ChatType,
        chat_id: str,
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
                self._system_prompt(chat_type, messages, chat_id),
                [{"role": "user", "content": prompt}],
            )
        if getattr(self.brain, "accepts_history", False):
            return await responder(
                chat_type,
                chat_id,
                prompt,
                system=self._system_prompt(chat_type, messages, chat_id),
                history=self._history_before(chat_type, chat_id, messages),
                on_tool=on_tool,
            )
        if on_tool is None:
            # 单条路径历来不传 on_tool，旧的 brain/fake 只认三个位置参数。
            try:
                return await responder(chat_type, chat_id, prompt)
            except BrainError as exc:
                return await self._retry_fresh_session(
                    chat_type, chat_id, messages, prompt, exc
                )
        try:
            return await responder(chat_type, chat_id, prompt, on_tool=on_tool)
        except BrainError as exc:
            return await self._retry_fresh_session(
                chat_type, chat_id, messages, prompt, exc, on_tool=on_tool
            )

    async def _retry_fresh_session(
        self,
        chat_type: ChatType,
        chat_id: str,
        messages: list[Message],
        prompt: str,
        exc: BrainError,
        *,
        on_tool: Any = None,
    ) -> BrainReply:
        """额度/限流/旧 session 失败：换新会话重放本地历史再试一次。

        免费额度、账号配额这类失败不会因为重试同一会话就好转；换一个干净的
        session 并补上本地窗口，至少还能把这一轮聊完。opencode 的 session 还会
        绑定创建时的项目目录——仓库被移动后旧 session 会报 Agent not found，
        同样清掉重建即可恢复。普通网络错误不重试，避免一次超时变成两次计费。
        """
        if not (
            is_quota_error(exc)
            or is_rate_limit_error(exc)
            or is_agent_missing_error(exc)
        ):
            raise exc
        fresh = getattr(self.brain, "reply_fresh", None)
        if not callable(fresh):
            raise exc
        log.warning("大脑调用失败，换新会话重放历史再试一次：%s", exc)
        return await fresh(
            chat_type,
            chat_id,
            prompt,
            system=self._system_prompt(chat_type, messages, chat_id),
            history=self._history_before(chat_type, chat_id, messages),
            on_tool=on_tool,
        )

    def _system_prompt(
        self,
        chat_type: ChatType,
        messages: list[Message],
        chat_id: str | None = None,
    ) -> str:
        chat_title = (
            "本群"
            if chat_type is ChatType.GROUP
            else (messages[-1].nickname if messages else "私聊")
        )
        prompt = build_system_prompt(
            self.persona_text,
            chat_title,
            now_shanghai().date().isoformat(),
            self.settings.persona.max_reply_chars,
            self.settings.persona.max_reply_chars_technical,
            self.settings.persona.os_probability,
            tools_enabled=self._tools_enabled(),
        )
        if (
            chat_id is not None
            and chat_type is ChatType.GROUP
            and self.router.is_thread_closed(chat_type, chat_id)
        ):
            prompt += (
                "\n\n这个话题你刚收尾过。除非对方明显在问你、"
                "或者话题被重新提起，否则不要接。"
            )
        return prompt

    def _tools_enabled(self) -> bool:
        """只有 opencode 且工作区开着时，模型才真的有 read/edit/webfetch。"""
        return (
            bool(getattr(self.brain, "supports_tools", False))
            and self.workspace is not None
        )

    def _history_before(
        self, chat_type: ChatType, chat_id: str, messages: list[Message]
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
            # 失败也占一次调用额度，避免异常时无限重试烧钱；静默失败，不发提示。
            self.store.record_usage(msg.chat_type, msg.chat_id, "error", 0, 0)
            log.warning("模型调用失败 %s/%s: %s", msg.chat_id, msg.message_id, exc)
            return

        self.store.record_usage(
            msg.chat_type,
            msg.chat_id,
            "reply",
            reply.prompt_tokens,
            reply.completion_tokens,
            reply.estimated,
        )

        if reply.read or reply.think:
            log.debug(
                "模型读/想 %s/%s read=%s think=%s",
                msg.chat_type.value,
                msg.chat_id,
                reply.read or "-",
                reply.think or "-",
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

        reply_text = await self._apply_restrictions(
            msg.chat_type,
            msg.chat_id,
            reply.reply,
            addressed=self._is_addressed(msg),
        )
        if reply_text is None:
            self.router.note_no_reply(msg.chat_type, msg.chat_id)
            return

        chunks = shape_reply(
            reply_text,
            self.settings.persona.max_reply_chars,
            self.settings.persona.max_reply_chars_technical,
            is_technical=is_technical_question(msg.content),
        )
        if len(chunks) == 1:
            chunks = segment_reply(chunks[0])
        if not chunks:
            self.router.note_no_reply(msg.chat_type, msg.chat_id)
            return
        chunks = self._guard_chunks(msg.chat_type, msg.chat_id, chunks)
        if not chunks:
            self.router.note_no_reply(msg.chat_type, msg.chat_id)
            return
        # 与批次路径同序：先确认有内容，再解析附件/@。
        attachments = self._safe_reply_attachments(reply, msg.chat_id)
        at_names = tuple(resolve_at_names(reply.at, [msg]))
        at_qq_ids = self._at_qq_ids(at_names, [msg])

        sent: list[tuple[str, str | None]] = []
        defer = 0
        delay = float(getattr(self.settings.persona, "segment_delay_seconds", 0.0))
        quote_id = (
            msg.message_id
            if self._is_addressed(msg) and self.settings.persona.quote_reply
            else None
        )
        for index, chunk in enumerate(chunks):
            if index and delay > 0:
                await self._sleep(delay)
            try:
                message_id = await self._send_chunk(
                    msg.chat_type,
                    msg.chat_id,
                    chunk,
                    quote_id=quote_id if index == 0 else None,
                    attachments=attachments if index == 0 else (),
                    at_qq_ids=at_qq_ids if index == 0 else (),
                    at_names=at_names if index == 0 else (),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络抖动不应拖垮整条链路
                log.warning("发送失败 %s/%s: %s", msg.chat_id, msg.message_id, exc)
                for rest in chunks[index:]:
                    if self.store.enqueue_outbox(
                        msg.chat_type, msg.chat_id, rest, origin_message_id=msg.message_id
                    ):
                        defer += 1
                break
            sent.append((chunk, message_id))
            await self._maybe_emoji_reaction(msg.chat_type, msg.chat_id, message_id)
        if defer:
            log.info("发送失败，待发队列 +%d %s/%s", defer, msg.chat_id, msg.message_id)
        else:
            self.router.note_bot_spoke(
                msg.chat_type,
                msg.chat_id,
                ambient=msg.chat_type is ChatType.GROUP
                and not self._is_addressed(msg),
            )
        # 即使中途失败，已经发出去的分片也要进入历史，避免上下文断裂。
        self._store_bot_chunks(msg, sent, attachments=attachments)

    def _store_bot_chunks(
        self,
        msg: Message,
        chunks: list[tuple[str, str | None]],
        *,
        attachments: tuple[Attachment, ...] = (),
    ) -> None:
        """入库 bot 回复：优先用网关返回的真实 message_id，没有才回退合成值。

        附件只以纯文本注记的形式挂在第一片 content 末尾（面板据此预览），
        不新增字段、不写 pending_outbox。
        """
        now_ts = int(now_shanghai().timestamp())
        for index, (chunk, message_id) in enumerate(chunks):
            content = self._annotate_attachments(chunk, attachments) if index == 0 else chunk
            self.store.add_message(
                Message(
                    chat_type=msg.chat_type,
                    chat_id=msg.chat_id,
                    user_id=self._adapter_for(msg.chat_id).bot_id,
                    nickname=self.settings.persona.name,
                    content=content,
                    message_id=message_id or f"{msg.message_id}:bot:{index}",
                    is_bot=True,
                    timestamp=now_ts,
                )
            )

    def _schedule_impressions(self, msg: Message) -> None:
        # 印象是「群友印象」：私聊既不落库也不消耗抽取配额。
        if msg.chat_type != ChatType.GROUP:
            return
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

    async def forget_chat(self, chat_type: ChatType, chat_id: str) -> None:
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
        await self._adapter_for(msg.chat_id).send(msg.chat_type, msg.chat_id, FORGET_REPLY)

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
            await self._adapter_for(msg.chat_id).send(msg.chat_type, msg.chat_id, ADMIN_ONLY_REPLY)
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
        await self._adapter_for(msg.chat_id).send(msg.chat_type, msg.chat_id, text)

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
        await self._adapter_for(msg.chat_id).send(msg.chat_type, msg.chat_id, "\n".join(lines))

    async def _cmd_status(self, msg: Message) -> None:
        connected = bool(
            self.channel_status().get(channel_of(msg.chat_id), {}).get("connected", False)
        )

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
        await self._adapter_for(msg.chat_id).send(msg.chat_type, msg.chat_id, "\n".join(lines))

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
        await self._adapter_for(msg.chat_id).send(msg.chat_type, msg.chat_id, "\n".join(lines))

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
