from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from dafeiyu.config import Settings
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import SHANGHAI, now_shanghai  # noqa: F401 - 兼容旧导入路径

# 关键词前一个字落在这些字上时，多半是否定口语（没什么 / 不怎么 / 别问什么）。
_NEGATION_CHARS = "没不无别"


@dataclass(frozen=True)
class TriggerDecision:
    call_model: bool
    hard: bool
    reason: str


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
    no_reply_streak: int = 0
    downshift_until: datetime | None = None
    muted_until: datetime | None = None
    last_prompt: str = ""
    last_prompt_at: datetime | None = None


@dataclass
class Router:
    settings: Settings
    store: object
    clock: Callable[[], datetime] = now_shanghai
    rng: random.Random | None = None
    _states: dict[tuple[ChatType, int], _ChatState] = field(default_factory=dict, init=False)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = self.rng if self.rng is not None else random.Random()

    # ---- state ----------------------------------------------------
    def _state(self, chat_type: ChatType, chat_id: int) -> _ChatState:
        return self._states.setdefault((chat_type, chat_id), _ChatState())

    def note_bot_spoke(self, chat_type: ChatType, chat_id: int) -> None:
        self._state(chat_type, chat_id).last_bot_spoke = self.clock()

    def note_reply(self, chat_type: ChatType, chat_id: int) -> None:
        st = self._state(chat_type, chat_id)
        st.no_reply_streak = 0
        st.downshift_until = None

    def note_no_reply(self, chat_type: ChatType, chat_id: int) -> None:
        st = self._state(chat_type, chat_id)
        st.no_reply_streak += 1
        if st.no_reply_streak >= 3:
            st.downshift_until = self.clock() + timedelta(minutes=30)

    def quiet(self, chat_type: ChatType, chat_id: int, minutes: int) -> None:
        self._state(chat_type, chat_id).muted_until = self.clock() + timedelta(minutes=minutes)

    def wake(self, chat_type: ChatType, chat_id: int) -> None:
        self._state(chat_type, chat_id).muted_until = None

    # ---- gates ----------------------------------------------------
    def hard_trigger(self, msg: Message) -> bool:
        if msg.at_bot or msg.reply_to_bot:
            return True
        text = msg.content.casefold()
        return any(alias.casefold() in text for alias in self.settings.persona.aliases)

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

    def is_quiet(
        self, chat_type: ChatType, chat_id: int, now: datetime | None = None
    ) -> bool:
        now = now or self.clock()
        st = self._state(chat_type, chat_id)
        if st.muted_until is not None and now < st.muted_until:
            return True
        return self._in_quiet_hours(now)

    def rule_filter(self, msg: Message, now: datetime) -> tuple[bool, str]:
        st = self._state(msg.chat_type, msg.chat_id)

        if self._in_quiet_hours(now):
            return False, "quiet"

        # 同一句话在冷却窗口内重复出现（含并发重复投递），不重复回。
        content = msg.content.strip()
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
            self._remember_prompt(st, content, now)
            return True, "question"

        # 冷却只压「热聊」和随机抽样这两类没被点名的发言。
        if st.last_bot_spoke is not None:
            elapsed = (now - st.last_bot_spoke).total_seconds()
            if elapsed < self.settings.triggers.cooldown_seconds:
                return False, "cooldown"

        if self._is_hot(msg, now):
            self._remember_prompt(st, content, now)
            return True, "hot"

        rate = self.settings.triggers.sample_rate
        if st.downshift_until is not None and now < st.downshift_until:
            rate /= 2
        if self._rng.random() < rate:
            self._remember_prompt(st, content, now)
            return True, "sample"
        return False, "sample_skip"

    @staticmethod
    def _remember_prompt(st: _ChatState, content: str, now: datetime) -> None:
        st.last_prompt = content
        st.last_prompt_at = now

    def should_handle(self, msg: Message) -> TriggerDecision:
        if msg.is_bot:
            return TriggerDecision(False, False, "self")

        st = self._state(msg.chat_type, msg.chat_id)
        now = self.clock()
        key = (msg.chat_type, msg.chat_id)

        if st.muted_until is not None and now < st.muted_until:
            return TriggerDecision(False, False, "muted")
        if msg.chat_id in self.settings.groups.blacklist:
            return TriggerDecision(False, False, "blacklist")

        calls, tokens = self.store.usage_today(
            msg.chat_type, msg.chat_id, now.date().isoformat()
        )
        if calls >= self.settings.budget.daily_calls:
            return TriggerDecision(False, False, "budget")
        if tokens >= self.settings.budget.daily_tokens:
            return TriggerDecision(False, False, "budget")

        # 硬触发（@/引用/别名）优先于白名单：被点名的群一定回。
        if self.hard_trigger(msg):
            return TriggerDecision(True, True, "hard")

        if self.settings.groups.whitelist and msg.chat_id not in self.settings.groups.whitelist:
            return TriggerDecision(False, False, "whitelist")

        ok, reason = self.rule_filter(msg, now)
        return TriggerDecision(ok, False, reason)

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
