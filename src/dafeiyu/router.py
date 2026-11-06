from __future__ import annotations

import copy
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from dafeiyu.budget import BudgetAction, BudgetGuard
from dafeiyu.config import Settings
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import SHANGHAI, now_shanghai  # noqa: F401 - 兼容旧导入路径

log = logging.getLogger(__name__)

# 关键词前一个字落在这些字上时，多半是否定口语（没什么 / 不怎么 / 别问什么）。
_NEGATION_CHARS = "没不无别"

# 别名判定：「称呼」是明确在叫 bot，必须回；「提及」只是聊到它，只给提示。
# 称呼形态：文本开头就是别名、别名后跟标点/称呼后缀、文本以别名结尾、或 @别名。
_ALIAS_ADDRESS_SUFFIXES = (
    "在吗",
    "在么",
    "在不在",
    "呢",
    "啊",
    "呀",
    "吧",
    "嘛",
    "哦",
    "喔",
    "喂",
    "你好",
    "您好",
    "帮我",
    "帮忙",
    "看看",
    "出来",
    "过来",
    "醒醒",
    "起床",
    "说话",
    "回答",
    "解释",
    "讲讲",
    "说说",
)
_ALIAS_BOUNDARY = frozenset(
    " \t\r\n@，。！？、,.!?;:：；~～…—()（）[]【】<>《》\"'“”‘’"
)


def _alias_is_vocative(text: str, alias: str) -> bool:
    """判断别名在文本里是「在叫它」还是「只是提到它」。"""
    needle = alias.casefold()
    if not needle:
        return False
    lowered = text.casefold()
    start = lowered.find(needle)
    while start != -1:
        end = start + len(needle)
        previous = lowered[start - 1] if start else ""
        following = lowered[end : end + 1]
        if start == 0 or previous == "@":
            return True
        if end == len(lowered):
            return True
        if previous in _ALIAS_BOUNDARY and (
            not following or following in _ALIAS_BOUNDARY
        ):
            return True
        tail = lowered[end : end + 6]
        if previous in _ALIAS_BOUNDARY and any(
            tail.startswith(suffix) for suffix in _ALIAS_ADDRESS_SUFFIXES
        ):
            return True
        start = lowered.find(needle, start + 1)
    return False


@dataclass(frozen=True)
class TriggerDecision:
    call_model: bool
    hard: bool
    reason: str

    @property
    def handle(self) -> bool:
        """兼容调试输出：是否放行。"""
        return self.call_model


@dataclass(frozen=True)
class Command:
    name: str
    args: list[str]


def parse_command(text: str) -> Command | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split()
    if not parts:
        return None
    return Command(parts[0], parts[1:])


@dataclass
class _ChatState:
    last_bot_spoke: datetime | None = None
    last_user_message: datetime | None = None
    no_reply_streak: int = 0
    downshift_until: datetime | None = None
    muted_until: datetime | None = None
    last_prompt: str = ""
    last_prompt_at: datetime | None = None
    # 连续 ambient 发言计数；被点名时清零。
    ambient_streak: int = 0
    # 话题收尾窗口：非空且未过期时，不再主动接话。
    thread_closed_until: datetime | None = None
    last_closed_at: datetime | None = None


_CHECK_LABELS = {
    "self": "自身消息",
    "muted": "静音",
    "blacklist": "群黑名单",
    "budget": "每日预算",
    "budget_global": "全局日预算",
    "budget_monthly": "月度预算",
    "budget_cost": "月度费用",
    "hard": "硬触发",
    "whitelist": "群白名单",
    "quiet": "夜间静默",
    "duplicate": "重复消息",
    "question": "提问",
    "cooldown": "冷却",
    "closed": "话题收尾",
    "hot": "热聊",
    "sample": "随机抽样",
}


def _limit_detail(
    used_calls: int,
    limit_calls: int,
    used_tokens: int,
    limit_tokens: int,
) -> str:
    """按已启用的维度展示；全部为 0 时明确说“未设上限”。"""
    if limit_calls <= 0 and limit_tokens <= 0:
        return "未设上限"
    parts: list[str] = []
    if limit_calls > 0:
        parts.append(f"{used_calls}/{limit_calls} 次")
    if limit_tokens > 0:
        parts.append(f"{used_tokens}/{limit_tokens} token")
    return "，".join(parts)


def _check_detail(
    key: str,
    msg: Message,
    st: _ChatState,
    metrics: dict[str, Any],
    source: str | None,
    status: str,
) -> str:
    if status == "skipped":
        return "前序判定已结束，未执行"
    if key == "self":
        return "机器人自己的消息" if msg.is_bot else "普通用户消息"
    if key == "muted":
        left = metrics["muted_remaining_seconds"]
        return f"剩余 {int(left)} 秒" if left > 0 else "未静音"
    if key == "blacklist":
        return "命中黑名单" if status == "blocked" else "不在黑名单"
    if key == "budget":
        if metrics.get("budget_scope") == "storage_error":
            return "预算检查读库失败，按降级处理"
        return _limit_detail(
            metrics["usage_calls"],
            metrics["daily_calls"],
            metrics["usage_tokens"],
            metrics["daily_tokens"],
        )
    if key == "budget_global":
        return _limit_detail(
            metrics["global_calls"],
            metrics["global_limit_calls"],
            metrics["global_tokens"],
            metrics["global_limit_tokens"],
        )
    if key == "budget_monthly":
        return _limit_detail(
            metrics["monthly_calls"],
            metrics["monthly_limit_calls"],
            metrics["monthly_tokens"],
            metrics["monthly_limit_tokens"],
        )
    if key == "budget_cost":
        limit = metrics["monthly_cost_limit"]
        currency = metrics["currency"]
        used = metrics["monthly_cost"]
        if limit <= 0:
            return f"{used:.2f} {currency}，未设上限"
        return f"{used:.2f}/{limit:.2f} {currency}"
    if key == "hard":
        labels = {"at": "@ 机器人", "reply": "引用回复", "alias": "正文别名"}
        return labels.get(source or "", "未点名")
    if key == "whitelist":
        if metrics.get("whitelist_empty"):
            return "白名单为空"
        return "群在白名单" if status != "blocked" else "群不在白名单"
    if key == "quiet":
        start, end = metrics["quiet_hours"]
        return f"静默时段 {start}–{end}" if metrics["in_quiet_hours"] else "不在静默时段"
    if key == "duplicate":
        return "与上次内容重复" if status == "blocked" else "无重复"
    if key == "question":
        return "命中提问规则" if status == "pass" else "不是提问"
    if key == "cooldown":
        left = metrics["cooldown_remaining_seconds"]
        return f"剩余 {int(left)} 秒" if left > 0 else "不在冷却"
    if key == "closed":
        left = metrics["closed_remaining_seconds"]
        if left <= 0:
            return "话题进行中"
        rate = metrics.get("effective_sample_rate", 0.0)
        if status == "pass":
            return f"话题已收尾，按 {rate:.0%} 抽样放行"
        return f"话题已收尾，剩余 {int(left)} 秒，抽样未命中"
    if key == "hot":
        return "60 秒内消息充足" if status == "pass" else "热度不足"
    if key == "sample":
        rate = metrics["effective_sample_rate"]
        if status == "pass":
            return f"抽样率 {rate:.0%}，命中"
        if status == "blocked":
            return f"抽样率 {rate:.0%}，未命中"
        return f"抽样率 {rate:.0%}，未执行"
    return ""


@dataclass
class Router:
    settings: Settings
    store: object
    clock: Callable[[], datetime] = now_shanghai
    rng: random.Random | None = None
    _states: dict[tuple[ChatType, int], _ChatState] = field(default_factory=dict, init=False)
    _loaded: set[tuple[ChatType, int]] = field(default_factory=set, init=False, repr=False)
    _persist_enabled: bool = field(default=True, init=False, repr=False)
    _rng: random.Random = field(init=False, repr=False)
    budget_guard: BudgetGuard = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = self.rng if self.rng is not None else random.Random()
        self.budget_guard = BudgetGuard(self.store, self.settings.budget)

    # ---- state ----------------------------------------------------
    def _state(self, chat_type: ChatType, chat_id: str) -> _ChatState:
        key = (chat_type, chat_id)
        st = self._states.get(key)
        if st is not None:
            return st
        st = _ChatState()
        if key not in self._loaded:
            self._loaded.add(key)
            self._hydrate(st, chat_type, chat_id)
        self._states[key] = st
        return st

    def _hydrate(self, st: _ChatState, chat_type: ChatType, chat_id: str) -> None:
        """首次访问时从库里恢复；读不到就按空状态起步。"""
        loader = getattr(self.store, "load_chat_state", None)
        if loader is None:
            return
        try:
            row = loader(chat_type, chat_id)
        except Exception:
            log.warning("读取会话状态失败：%s/%s", chat_type.value, chat_id, exc_info=True)
            return
        if not row:
            return
        st.last_bot_spoke = self._from_epoch(row.get("last_bot_spoke"))
        st.last_user_message = self._from_epoch(row.get("last_user_message"))
        st.muted_until = self._from_epoch(row.get("muted_until"))
        st.ambient_streak = int(row.get("ambient_streak") or 0)
        st.no_reply_streak = int(row.get("no_reply_streak") or 0)
        st.downshift_until = self._from_epoch(row.get("downshift_until"))
        st.last_prompt = str(row.get("last_prompt") or "")
        st.last_prompt_at = self._from_epoch(row.get("last_prompt_at"))
        st.thread_closed_until = self._from_epoch(row.get("thread_closed_until"))
        st.last_closed_at = self._from_epoch(row.get("last_closed_at"))

    def _persist(self, chat_type: ChatType, chat_id: str, st: _ChatState) -> None:
        """把会跨重启保留的字段写回；失败只记日志，不影响当前消息。"""
        if not self._persist_enabled:
            return
        saver = getattr(self.store, "save_chat_state", None)
        if saver is None:
            return
        try:
            saver(
                chat_type,
                chat_id,
                {
                    "last_bot_spoke": self._epoch(st.last_bot_spoke),
                    "last_user_message": self._epoch(st.last_user_message),
                    "muted_until": self._epoch(st.muted_until),
                    "ambient_streak": st.ambient_streak,
                    "no_reply_streak": st.no_reply_streak,
                    "downshift_until": self._epoch(st.downshift_until),
                    "last_prompt": st.last_prompt,
                    "last_prompt_at": self._epoch(st.last_prompt_at),
                    "thread_closed_until": self._epoch(st.thread_closed_until),
                    "last_closed_at": self._epoch(st.last_closed_at),
                },
            )
        except Exception:
            log.warning("保存会话状态失败：%s/%s", chat_type.value, chat_id, exc_info=True)

    @staticmethod
    def _epoch(value: datetime | None) -> float | None:
        return None if value is None else value.timestamp()

    def _from_epoch(self, value: object) -> datetime | None:
        if value is None:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=self.clock().tzinfo)
        except (TypeError, ValueError, OSError):
            return None

    def note_bot_spoke(
        self, chat_type: ChatType, chat_id: str, *, ambient: bool = False
    ) -> None:
        st = self._state(chat_type, chat_id)
        st.last_bot_spoke = self.clock()
        st.ambient_streak = st.ambient_streak + 1 if ambient else 0
        self._persist(chat_type, chat_id, st)

    def note_user_message(
        self, chat_type: ChatType, chat_id: str, *, addressed: bool = False
    ) -> None:
        """记录用户消息；被点名等于话题重新开始，清掉收尾与降频状态。"""
        st = self._state(chat_type, chat_id)
        st.last_user_message = self.clock()
        was_closed = st.thread_closed_until is not None
        if addressed:
            st.ambient_streak = 0
            st.thread_closed_until = None
            st.no_reply_streak = 0
            st.downshift_until = None
        # 普通 ambient 消息不落库，避免每条群消息都多一次写；真正改变
        # 接话状态的两类（点名重开、关窗期有人接话）才持久化。
        if addressed or was_closed:
            self._persist(chat_type, chat_id, st)

    def ambient_turns(self, chat_type: ChatType, chat_id: str) -> int:
        return self._state(chat_type, chat_id).ambient_streak

    def is_thread_closed(
        self, chat_type: ChatType, chat_id: str, now: datetime | None = None
    ) -> bool:
        """话题是否已经收尾：连续 ambient 发言超限，或发言后无人回应。"""
        now = now or self.clock()
        st = self._state(chat_type, chat_id)
        closed_seconds = getattr(self.settings.triggers, "thread_closed_seconds", 600)
        if st.thread_closed_until is not None:
            if now < st.thread_closed_until:
                return True
            st.thread_closed_until = None
            st.ambient_streak = 0
            self._persist(chat_type, chat_id, st)

        max_turns = getattr(self.settings.triggers, "ambient_max_turns", 2)
        if st.ambient_streak >= max_turns:
            st.thread_closed_until = now + timedelta(seconds=closed_seconds)
            st.last_closed_at = now
            self._persist(chat_type, chat_id, st)
            return True

        if st.last_bot_spoke is None:
            return False
        # 上一次收尾之前的发言不再重复触发收尾，否则窗口一过期就立刻又关上。
        if st.last_closed_at is not None and st.last_bot_spoke <= st.last_closed_at:
            return False
        idle_seconds = getattr(self.settings.triggers, "thread_idle_seconds", 300)
        if (now - st.last_bot_spoke).total_seconds() <= idle_seconds:
            return False
        # 发言之后有人接过话，说明话题还在继续。
        if st.last_user_message is not None and st.last_user_message > st.last_bot_spoke:
            return False
        st.thread_closed_until = now + timedelta(seconds=closed_seconds)
        st.last_closed_at = now
        self._persist(chat_type, chat_id, st)
        return True

    def closed_sample_rate(self) -> float:
        return float(getattr(self.settings.triggers, "closed_sample_rate", 0.05))

    def closed_sample(
        self, chat_type: ChatType, chat_id: str, now: datetime | None = None
    ) -> bool:
        """话题已收尾时，非点名消息是否还要送模型；0 表示彻底静默。"""
        if not self.is_thread_closed(chat_type, chat_id, now):
            return False
        rate = self.closed_sample_rate()
        return rate > 0 and self._rng.random() < rate

    def note_reply(self, chat_type: ChatType, chat_id: str) -> None:
        st = self._state(chat_type, chat_id)
        st.no_reply_streak = 0
        st.downshift_until = None
        self._persist(chat_type, chat_id, st)

    def note_no_reply(self, chat_type: ChatType, chat_id: str) -> None:
        st = self._state(chat_type, chat_id)
        st.no_reply_streak += 1
        if st.no_reply_streak >= 3:
            st.downshift_until = self.clock() + timedelta(minutes=30)
        self._persist(chat_type, chat_id, st)

    def quiet(self, chat_type: ChatType, chat_id: str, minutes: int) -> None:
        st = self._state(chat_type, chat_id)
        st.muted_until = self.clock() + timedelta(minutes=minutes)
        self._persist(chat_type, chat_id, st)

    def wake(self, chat_type: ChatType, chat_id: str) -> None:
        st = self._state(chat_type, chat_id)
        st.muted_until = None
        self._persist(chat_type, chat_id, st)

    def clone(
        self, *, now: datetime | None = None, seed: int | None = None
    ) -> Router:
        """复制一份路由状态用于只读推演，绝不改真实状态和真实 RNG。"""
        probe = Router(
            settings=self.settings,
            store=self.store,
            clock=(lambda: now) if now is not None else self.clock,
            rng=random.Random(seed) if seed is not None else random.Random(),
        )
        probe._states = copy.deepcopy(self._states)
        probe._loaded = set(self._loaded)
        probe._persist_enabled = False
        return probe

    # ---- gates ----------------------------------------------------
    def addressed_source(self, msg: Message) -> str | None:
        """返回点名来源：at / reply / alias；未点名为 None。

        正文里的别名只在「称呼」形态下算点名（开头、后跟标点/称呼后缀、
        结尾或 @别名）；「提到你」不算，避免把「我刚才看到大肥鱼…」当成叫她。
        """
        if msg.at_bot:
            return "at"
        if msg.reply_to_bot:
            return "reply"
        text = msg.content.casefold()
        for alias in self.settings.persona.aliases:
            if alias and alias.casefold() in text and _alias_is_vocative(
                msg.content, alias
            ):
                return "alias"
        return None

    def alias_mention(self, msg: Message) -> bool:
        """正文提到别名但不是在称呼：只给模型一个弱提示，不强制回复。"""
        text = msg.content.casefold()
        if not text:
            return False
        for alias in self.settings.persona.aliases:
            if not alias or alias.casefold() not in text:
                continue
            return not _alias_is_vocative(msg.content, alias)
        return False

    def is_addressed(self, msg: Message) -> bool:
        """被点名：@、引用回复、或正文里出现别名。批次模式用它决定防抖计时。"""
        return self.addressed_source(msg) is not None

    def hard_trigger(self, msg: Message) -> bool:
        return self.is_addressed(msg)

    def _in_quiet_hours(self, now: datetime) -> bool:
        start_s, end_s = self.settings.triggers.quiet_hours
        start = self._minutes(start_s)
        end = self._minutes(end_s)
        cur = now.hour * 60 + now.minute
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end

    @staticmethod
    def _minutes(hhmm: str) -> int:
        hours, minutes = hhmm.split(":")
        return int(hours) * 60 + int(minutes)

    def is_muted(self, chat_type: ChatType, chat_id: str, now: datetime | None = None) -> bool:
        """只判 /闭嘴 的静音，不含夜间静音时段。"""
        now = now or self.clock()
        st = self._state(chat_type, chat_id)
        return st.muted_until is not None and now < st.muted_until

    def is_quiet(
        self, chat_type: ChatType, chat_id: str, now: datetime | None = None
    ) -> bool:
        now = now or self.clock()
        return self.is_muted(chat_type, chat_id, now) or self._in_quiet_hours(now)

    def is_downshifted(
        self, chat_type: ChatType, chat_id: str, now: datetime | None = None
    ) -> bool:
        """连续不说话后的降频窗口；批次模式与 rules 模式共用同一份状态。"""
        now = now or self.clock()
        st = self._state(chat_type, chat_id)
        return st.downshift_until is not None and now < st.downshift_until

    def rule_filter(self, msg: Message, now: datetime) -> tuple[bool, str]:
        st = self._state(msg.chat_type, msg.chat_id)

        if self._in_quiet_hours(now):
            return False, "quiet"

        content = msg.content.strip()

        # 话题已经收尾：不再硬拦，改成低概率放行让模型自己判断；
        # 0 表示彻底静默，被点名仍走硬触发分支，不受这里影响。
        if self.is_thread_closed(msg.chat_type, msg.chat_id, now):
            rate = self.closed_sample_rate()
            if rate > 0 and self._rng.random() < rate:
                self._remember_prompt(msg.chat_type, msg.chat_id, st, content, now)
                return True, "closed"
            return False, "closed"

        # 同一句话在冷却窗口内重复出现（含并发重复投递），不重复回。
        if (
            content
            and content == st.last_prompt
            and st.last_prompt_at is not None
            and (now - st.last_prompt_at).total_seconds()
            < self.settings.triggers.cooldown_seconds
        ):
            return False, "duplicate"

        # 明确提问不受冷却约束：追问被静默丢掉，比多回一句的代价高得多。
        if self._is_question(msg.content):
            self._remember_prompt(msg.chat_type, msg.chat_id, st, content, now)
            return True, "question"

        # 冷却只压「热聊」和随机抽样这两类没被点名的发言。
        if st.last_bot_spoke is not None:
            elapsed = (now - st.last_bot_spoke).total_seconds()
            if elapsed < self.settings.triggers.cooldown_seconds:
                return False, "cooldown"

        if self._is_hot(msg, now):
            self._remember_prompt(msg.chat_type, msg.chat_id, st, content, now)
            return True, "hot"

        rate = self.settings.triggers.sample_rate
        if st.downshift_until is not None and now < st.downshift_until:
            rate /= 2
        if self._rng.random() < rate:
            self._remember_prompt(msg.chat_type, msg.chat_id, st, content, now)
            return True, "sample"
        return False, "sample_skip"

    def _remember_prompt(
        self,
        chat_type: ChatType,
        chat_id: str,
        st: _ChatState,
        content: str,
        now: datetime,
    ) -> None:
        """记下「这次接的是哪句话」用于防重复；顺手落库，重启后仍能判重。"""
        st.last_prompt = content
        st.last_prompt_at = now
        self._persist(chat_type, chat_id, st)

    def should_handle(self, msg: Message) -> TriggerDecision:
        if msg.is_bot:
            return TriggerDecision(False, False, "self")

        st = self._state(msg.chat_type, msg.chat_id)
        now = self.clock()

        if st.muted_until is not None and now < st.muted_until:
            return TriggerDecision(False, False, "muted")
        if msg.chat_id in self.settings.groups.blacklist:
            return TriggerDecision(False, False, "blacklist")

        hard = self.hard_trigger(msg)
        budget = self.budget_guard.evaluate(
            msg.chat_type,
            msg.chat_id,
            addressed=hard,
            purpose="reply" if hard else "ambient",
            now=now,
        )
        if budget.action is BudgetAction.DENY or (
            budget.action is BudgetAction.DEGRADE and not hard
        ):
            return TriggerDecision(False, False, "budget")
        # 硬触发（@/引用/别名）优先于白名单：被点名的群一定回。
        if hard:
            return TriggerDecision(True, True, "hard")

        if self.settings.groups.whitelist and msg.chat_id not in self.settings.groups.whitelist:
            return TriggerDecision(False, False, "whitelist")

        ok, reason = self.rule_filter(msg, now)
        return TriggerDecision(ok, False, reason)

    def explain(
        self, msg: Message, *, now: datetime | None = None, seed: int | None = None
    ) -> dict[str, Any]:
        """只读推演 rules 模式的一条消息，返回判定、有序检查和可观察指标。"""
        now = now or self.clock()
        probe = self.clone(now=now, seed=seed)
        if msg.reply_to_bot:
            # 真实 handle() 在进 gate 前会先 note_reply，推演必须镜像这一步。
            probe.note_reply(msg.chat_type, msg.chat_id)

        decision = probe.should_handle(msg)
        source = probe.addressed_source(msg)
        st = probe._state(msg.chat_type, msg.chat_id)
        calls, tokens = self.store.usage_today(
            msg.chat_type, msg.chat_id, now.date().isoformat()
        )
        cooldown = self.settings.triggers.cooldown_seconds
        muted_left = 0.0
        if st.muted_until is not None:
            muted_left = max(0.0, (st.muted_until - now).total_seconds())
        cooldown_left = 0.0
        if st.last_bot_spoke is not None:
            cooldown_left = max(0.0, cooldown - (now - st.last_bot_spoke).total_seconds())
        downshift_left = 0.0
        if st.downshift_until is not None:
            downshift_left = max(0.0, (st.downshift_until - now).total_seconds())
        closed_left = 0.0
        if st.thread_closed_until is not None:
            closed_left = max(0.0, (st.thread_closed_until - now).total_seconds())
        base_rate = self.settings.triggers.sample_rate
        if closed_left > 0:
            effective_rate = self.closed_sample_rate()
        elif downshift_left > 0:
            effective_rate = base_rate / 2
        else:
            effective_rate = base_rate
        metrics: dict[str, Any] = {
            "usage_calls": calls,
            "usage_tokens": tokens,
            "daily_calls": self.settings.budget.daily_calls,
            "daily_tokens": self.settings.budget.daily_tokens,
            "muted_remaining_seconds": round(muted_left, 3),
            "cooldown_remaining_seconds": round(cooldown_left, 3),
            "downshift_remaining_seconds": round(downshift_left, 3),
            "closed_remaining_seconds": round(closed_left, 3),
            "last_prompt_age_seconds": (
                round((now - st.last_prompt_at).total_seconds(), 3)
                if st.last_prompt_at is not None
                else None
            ),
            "no_reply_streak": st.no_reply_streak,
            "quiet_hours": list(self.settings.triggers.quiet_hours),
            "in_quiet_hours": self._in_quiet_hours(now),
            "sample_rate": base_rate,
            "effective_sample_rate": effective_rate,
            "whitelist_empty": not bool(self.settings.groups.whitelist),
        }

        hard = probe.hard_trigger(msg)
        budget_decision = probe.budget_guard.evaluate(
            msg.chat_type,
            msg.chat_id,
            addressed=hard,
            purpose="reply" if hard else "ambient",
            now=now,
        )
        budget_cfg = self.settings.budget
        metrics.update(
            {
                "global_calls": 0,
                "global_tokens": 0,
                "global_limit_calls": budget_cfg.global_daily_calls,
                "global_limit_tokens": budget_cfg.global_daily_tokens,
                "monthly_calls": 0,
                "monthly_tokens": 0,
                "monthly_limit_calls": budget_cfg.monthly_calls,
                "monthly_limit_tokens": budget_cfg.monthly_tokens,
                "monthly_cost": 0.0,
                "monthly_cost_limit": budget_cfg.monthly_cost_limit,
                "currency": budget_cfg.currency,
                "budget_action": budget_decision.action.value,
                "budget_scope": budget_decision.scope,
                "warnings": list(budget_decision.warnings),
            }
        )
        try:
            report = probe.budget_guard.report(
                msg.chat_type, msg.chat_id, now=now
            )
        except Exception:  # noqa: BLE001 - explain 不能因读库失败而中断
            report = None
        if report is not None:
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

        keys = [
            "self",
            "muted",
            "blacklist",
            "budget",
            "budget_global",
            "budget_monthly",
            "budget_cost",
            "hard",
            "whitelist",
            "quiet",
            "duplicate",
            "question",
            "cooldown",
            "closed",
            "hot",
            "sample",
        ]
        stop_key = "sample" if decision.reason == "sample_skip" else decision.reason
        if stop_key == "budget":
            # 归因到真正触发预算的层级，而不是一律怪每日预算。
            stop_key = {
                "per_chat": "budget",
                "global_daily": "budget_global",
                "monthly": "budget_monthly",
                "cost": "budget_cost",
            }.get(metrics.get("budget_scope", ""), "budget")
        stop_index = keys.index(stop_key) if stop_key in keys else len(keys)
        positive = {"hard", "question", "hot", "sample"}
        checks: list[dict[str, str]] = []
        for index, key in enumerate(keys):
            if index < stop_index:
                status = "info" if key == "hard" and source is None else "pass"
            elif index == stop_index:
                status = "pass" if stop_key in positive else "blocked"
            else:
                status = "skipped"
            checks.append(
                {
                    "key": key,
                    "label": _CHECK_LABELS[key],
                    "status": status,
                    "detail": _check_detail(key, msg, st, metrics, source, status),
                }
            )

        return {
            "decision": {
                "call_model": decision.call_model,
                "hard": decision.hard,
                "reason": decision.reason,
            },
            "addressed": source is not None,
            "hard_source": source,
            "checks": checks,
            "metrics": metrics,
        }

    # ---- helpers --------------------------------------------------
    def _is_question(self, text: str) -> bool:
        """问号结尾直接算提问；关键词命中时排除「没什么/不怎么」这类否定口语。"""
        stripped = text.strip()
        if not stripped:
            return False
        if stripped.endswith(("?", "？")):
            return True

        for word in self.settings.triggers.keywords:
            if not word:
                continue
            start = 0
            while (index := stripped.find(word, start)) != -1:
                if index == 0 or stripped[index - 1] not in _NEGATION_CHARS:
                    return True
                start = index + 1
        return False

    def _is_hot(self, msg: Message, now: datetime) -> bool:
        cutoff = now.timestamp() - 60
        recent = self.store.recent_messages(msg.chat_type, msg.chat_id, 30)
        return sum(1 for m in recent if m.timestamp >= cutoff) >= 5
