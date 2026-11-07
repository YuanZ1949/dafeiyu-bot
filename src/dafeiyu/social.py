"""社交动作的确定性反应：戳一戳只从固定短句池里挑一句，不调用模型。

门控分两层：本模块只负责「这个会话现在还能不能发」和「发哪一句」——
冷却与每日上限都按 ``(chat_type, chat_id)`` 记账，日期取注入时钟的上海日期。
静音/聊天强度/预算属于 Service 的职责，在调用 :meth:`PokeReactor.claim`
之前过闸，本模块不重复判断。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .config import SocialConfig
from .models import ChatType
from .timeutil import SHANGHAI

__all__ = ["POKE_REPLIES", "PokeReactor"]

# 固定短句池：>=6 条、每条 <=20 字，按顺序轮转（确定性，便于测试与复现）。
POKE_REPLIES: tuple[str, ...] = (
    "怎么啦，戳我一下？",
    "别戳啦，有话直说～",
    "在的在的，什么事？",
    "戳一戳收到，本鱼游过来了。",
    "再戳一下就要收费了哦。",
    "嗯？我在这里，听着呢。",
    "轻点戳，本鱼会害羞的。",
)


@dataclass
class PokeReactor:
    """按会话记录戳一戳的冷却与当日次数；时钟可注入，测试不依赖真实时间。"""

    config: SocialConfig
    clock: Callable[[], float] = time.time
    # (chat_type, chat_id) -> 上一次回应的时刻（epoch 秒）。
    _last_sent: dict[tuple[ChatType, str], float] = field(
        default_factory=dict, init=False
    )
    # (chat_type, chat_id, 上海日期) -> 当日已回次数。
    _daily: dict[tuple[ChatType, str, str], int] = field(
        default_factory=dict, init=False
    )
    # 短句轮转游标：只按「真的发出去」的次数前进。
    _cursor: int = field(default=0, init=False)

    def may_poke(self, chat_type: ChatType, chat_id: str) -> bool:
        """现在能不能回应：开关、冷却、当日上限逐项判断，不改状态。"""
        if not self.config.poke_enabled:
            return False
        key = self._key(chat_type, chat_id)
        if key is None:
            return False
        now = self.clock()
        count = self._daily.get((key[0], key[1], self._day(now)), 0)
        if count >= self.config.poke_daily_cap:
            return False
        last = self._last_sent.get(key)
        if last is not None and now - last < self.config.poke_cooldown_seconds:
            return False
        return True

    def record_sent(self, chat_type: ChatType, chat_id: str) -> None:
        """记一次已发送：写冷却起点并给当日计数 +1。"""
        key = self._key(chat_type, chat_id)
        if key is None:
            return
        now = self.clock()
        day = self._day(now)
        self._last_sent[key] = now
        counts_key = (key[0], key[1], day)
        self._daily[counts_key] = self._daily.get(counts_key, 0) + 1
        self._prune(key, day)

    def claim(self, chat_type: ChatType, chat_id: str) -> str | None:
        """原子地「问一次 + 记账 + 取句」；不能发就返回 None。

        Service 在过完静音/强度/预算三道门后调用一次即可，中间没有 await，
        因此不会出现两个并发 poke 都通过判定的窗口。
        """
        if not self.may_poke(chat_type, chat_id):
            return None
        self.record_sent(chat_type, chat_id)
        phrase = POKE_REPLIES[self._cursor % len(POKE_REPLIES)]
        self._cursor += 1
        return phrase

    @staticmethod
    def _key(chat_type: ChatType, chat_id: str) -> tuple[ChatType, str] | None:
        """非法会话（未知类型/空 id）一律当作「不能发」，不抛异常。"""
        if not isinstance(chat_type, ChatType):
            return None
        cid = str(chat_id or "").strip()
        if not cid:
            return None
        return (chat_type, cid)

    def _day(self, now: float) -> str:
        return datetime.fromtimestamp(now, SHANGHAI).date().isoformat()

    def _prune(self, key: tuple[ChatType, str], today: str) -> None:
        """只保留当日计数：长跑进程里不会为每个会话留下历史日期的键。"""
        stale = [
            item
            for item in self._daily
            if item[0] == key[0] and item[1] == key[1] and item[2] != today
        ]
        for item in stale:
            del self._daily[item]
