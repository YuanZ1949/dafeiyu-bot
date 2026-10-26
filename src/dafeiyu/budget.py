"""预算守卫：按会话日额度、全局日额度、自然月额度与月度费用分级放行。

预算只统计 ``reply`` / ``ambient`` 两种用途；摘要、印象、批评测等用途不占用
对话预算。所有“今天/本月”均按上海时区计算，月度是自然月。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from dafeiyu.config import BudgetConfig
from dafeiyu.memory.store import _BUDGET_PURPOSES, Store, UsageTotals
from dafeiyu.models import ChatType
from dafeiyu.timeutil import SHANGHAI, now_shanghai

log = logging.getLogger(__name__)

__all__ = [
    "BudgetAction",
    "BudgetDecision",
    "BudgetGuard",
    "BudgetReport",
    "estimate_cost",
]

# 复用 store 的预算口径，避免两处各写一份字面量。
_BUDGET_PURPOSE_SET = frozenset(_BUDGET_PURPOSES)
_ACTION_RANK = {"allow": 0, "degrade": 1, "deny": 2}
_SCOPE_ORDER = {"per_chat": 0, "global_daily": 1, "monthly": 2, "cost": 3}


class BudgetAction(str, Enum):
    ALLOW = "allow"
    DEGRADE = "degrade"
    DENY = "deny"


@dataclass(frozen=True)
class BudgetDecision:
    action: BudgetAction
    reason: str
    scope: str
    warnings: tuple[str, ...] = ()
    remaining_calls: int | None = None
    remaining_tokens: int | None = None
    remaining_cost: float | None = None


@dataclass(frozen=True)
class BudgetReport:
    per_chat: dict[str, Any]
    global_daily: dict[str, Any]
    monthly: dict[str, Any]
    cost: dict[str, Any]
    warnings: tuple[dict[str, Any], ...]


def estimate_cost(
    prompt_tokens: int, completion_tokens: int, config: BudgetConfig
) -> float:
    """按百万 token 单价估算费用；单价为 0 时该项自然为 0。"""
    return (
        prompt_tokens / 1_000_000 * config.price_input_per_million
        + completion_tokens / 1_000_000 * config.price_output_per_million
    )


class BudgetGuard:
    def __init__(self, store: Store, config: BudgetConfig) -> None:
        self.store = store
        self.config = config

    # ---- 对外接口 -------------------------------------------------
    def evaluate(
        self,
        chat_type: ChatType,
        chat_id: int,
        *,
        addressed: bool,
        purpose: str,
        now: datetime | None = None,
    ) -> BudgetDecision:
        """按固定顺序判断：per-chat 日 → 全局日 → 月度 → 费用 → 预警。

        其它用途（摘要/印象/批评测）不进入预算，直接放行且不查库。
        """
        if purpose not in _BUDGET_PURPOSE_SET:
            return BudgetDecision(BudgetAction.ALLOW, "allowed", "none")

        try:
            moment = _shanghai_moment(now)
            day = moment.date().isoformat()
            month = f"{moment.year:04d}-{moment.month:02d}"
            per_chat = self._chat_usage(chat_type, chat_id, day)
            # 非点名消息才需要 ambient 子额度；点名的总量检查已包含两种用途。
            ambient = None if addressed else self._ambient_usage(chat_type, chat_id, day)
            global_daily = self.store.usage_global_today(day)
            monthly = self.store.usage_month(month)
            cost = estimate_cost(
                monthly.prompt_tokens, monthly.completion_tokens, self.config
            )
        except Exception as exc:  # 读库失败不能拦死点名回复，但也不能放开环境发言。
            log.warning("预算检查读库失败，降级处理: %s", type(exc).__name__)
            return BudgetDecision(
                BudgetAction.DEGRADE,
                "budget_storage_error",
                "storage_error",
                ("budget_storage_error",),
            )

        action, reason, scope, extra_warnings = self._decide(
            addressed, per_chat, ambient, global_daily, monthly, cost
        )
        warnings, _ = self._warning_pairs(
            per_chat,
            global_daily,
            monthly,
            cost,
            extra_warnings=extra_warnings,
        )
        remaining_calls, remaining_tokens, remaining_cost = self._remaining(
            action,
            addressed,
            scope,
            per_chat,
            ambient,
            global_daily,
            monthly,
            cost,
        )
        return BudgetDecision(
            action,
            reason,
            scope,
            warnings,
            remaining_calls,
            remaining_tokens,
            remaining_cost,
        )

    def report(
        self,
        chat_type: ChatType,
        chat_id: int,
        *,
        now: datetime | None = None,
    ) -> BudgetReport:
        """一次读取会话日/全局日/月度用量，生成 JSON 友好的预算报告。"""
        moment = _shanghai_moment(now)
        day = moment.date().isoformat()
        month = f"{moment.year:04d}-{moment.month:02d}"
        per_chat = self._chat_usage(chat_type, chat_id, day)
        global_daily = self.store.usage_global_today(day)
        monthly = self.store.usage_month(month)
        cost = estimate_cost(
            monthly.prompt_tokens, monthly.completion_tokens, self.config
        )
        _, details = self._warning_pairs(
            per_chat,
            global_daily,
            monthly,
            cost,
            extra_warnings=(),
        )
        return BudgetReport(
            per_chat={
                "calls": per_chat.calls,
                "tokens": per_chat.tokens,
                "daily_calls": self.config.daily_calls,
                "daily_tokens": self.config.daily_tokens,
            },
            global_daily={
                "calls": global_daily.calls,
                "tokens": global_daily.tokens,
                "limit_calls": self.config.global_daily_calls,
                "limit_tokens": self.config.global_daily_tokens,
            },
            monthly={
                "calls": monthly.calls,
                "tokens": monthly.tokens,
                "limit_calls": self.config.monthly_calls,
                "limit_tokens": self.config.monthly_tokens,
            },
            cost={
                "cost": cost,
                "cost_limit": self.config.monthly_cost_limit,
                "currency": self.config.currency,
                "estimated": monthly.estimated,
            },
            warnings=details,
        )

    # ---- 内部实现 -------------------------------------------------
    def _chat_usage(
        self, chat_type: ChatType, chat_id: int, day: str
    ) -> UsageTotals:
        return self.store.usage_between(
            day,
            day,
            purposes=_BUDGET_PURPOSES,
            chat_type=chat_type,
            chat_id=chat_id,
        )

    def _ambient_usage(
        self, chat_type: ChatType, chat_id: int, day: str
    ) -> UsageTotals:
        return self.store.usage_between(
            day,
            day,
            purposes=("ambient",),
            chat_type=chat_type,
            chat_id=chat_id,
        )

    def _decide(
        self,
        addressed: bool,
        per_chat: UsageTotals,
        ambient: UsageTotals | None,
        global_daily: UsageTotals,
        monthly: UsageTotals,
        cost: float,
    ) -> tuple[BudgetAction, str, str, tuple[tuple[str, str], ...]]:
        """先定 action；同等级保留更靠前的 scope，更严格的会覆盖较宽松的。"""
        config = self.config
        action = BudgetAction.ALLOW
        reason = "allowed"
        scope = "none"
        extra: list[tuple[str, str]] = []

        def consider(new_action: BudgetAction, new_reason: str, new_scope: str) -> None:
            nonlocal action, reason, scope
            if _ACTION_RANK[new_action.value] > _ACTION_RANK[action.value]:
                action, reason, scope = new_action, new_reason, new_scope
            elif (
                new_action is BudgetAction.ALLOW
                and action is BudgetAction.ALLOW
                and scope == "none"
            ):
                # notify 只告警，但仍记录命中的 scope，方便 explain/日志定位。
                reason, scope = new_reason, new_scope

        # 1. per-chat 日额度：点名也不能突破。
        if (
            per_chat.calls >= config.daily_calls
            or per_chat.tokens >= config.daily_tokens
        ):
            consider(BudgetAction.DENY, "budget_per_chat", "per_chat")
        # 非点名只能使用 ambient_ratio 那部分额度，用满即降级（点名仍可继续）。
        elif ambient is not None:
            ambient_calls = config.daily_calls * config.ambient_ratio
            ambient_tokens = config.daily_tokens * config.ambient_ratio
            if ambient.calls >= ambient_calls or ambient.tokens >= ambient_tokens:
                consider(BudgetAction.DEGRADE, "budget_ambient_ratio", "per_chat")

        # 2. 全局日额度；on_exceed 只在这里与月度生效。
        if self._exceeded(
            global_daily, config.global_daily_calls, config.global_daily_tokens
        ):
            if config.on_exceed == "stop":
                consider(BudgetAction.DENY, "budget_global_daily", "global_daily")
            elif config.on_exceed == "degrade":
                consider(BudgetAction.DEGRADE, "budget_global_daily", "global_daily")
            else:  # notify：只告警，不改变放行结果。
                consider(BudgetAction.ALLOW, "budget_global_daily", "global_daily")
                extra.append(("global_daily", "1.0"))

        # 3. 月度 calls/tokens，规则同全局日。
        if self._exceeded(monthly, config.monthly_calls, config.monthly_tokens):
            if config.on_exceed == "stop":
                consider(BudgetAction.DENY, "budget_monthly", "monthly")
            elif config.on_exceed == "degrade":
                consider(BudgetAction.DEGRADE, "budget_monthly", "monthly")
            else:
                consider(BudgetAction.ALLOW, "budget_monthly", "monthly")
                extra.append(("monthly", "1.0"))

        # 4. 月度费用：到达上限即硬停，不受 on_exceed 影响。
        if config.monthly_cost_limit > 0 and cost >= config.monthly_cost_limit:
            consider(BudgetAction.DENY, "budget_cost", "cost")

        return action, reason, scope, tuple(extra)

    @staticmethod
    def _exceeded(usage: UsageTotals, limit_calls: int, limit_tokens: int) -> bool:
        """0 表示未启用；任一启用项达到上限即算超限。"""
        over_calls = limit_calls > 0 and usage.calls >= limit_calls
        over_tokens = limit_tokens > 0 and usage.tokens >= limit_tokens
        return over_calls or over_tokens

    def _warning_pairs(
        self,
        per_chat: UsageTotals,
        global_daily: UsageTotals,
        monthly: UsageTotals,
        cost: float,
        *,
        extra_warnings: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
        """预警只提示“用量达到配置比例”，从不改变 action。

        返回 ``(字符串列表, 结构化明细)``；同一 scope 的同一比例只出现一次，
        on_exceed=notify 追加的 1.0 也去重。
        """
        config = self.config
        records: list[tuple[int, float, str, dict[str, Any]]] = []
        seen: set[str] = set()

        def add(
            scope: str,
            used: float,
            limit: float,
            tokens: float,
            token_limit: float,
        ) -> None:
            if limit <= 0 and token_limit <= 0:
                return
            for ratio in config.warning_ratios:
                hit_calls = limit > 0 and used >= ratio * limit
                hit_tokens = token_limit > 0 and tokens >= ratio * token_limit
                if not hit_calls and not hit_tokens:
                    continue
                if hit_calls:
                    value, cap = used, limit
                else:  # 只有 token 触发时，calls 字段承载 token 用量。
                    value, cap = tokens, token_limit
                key = f"{scope}:{ratio}"
                if key in seen:
                    continue
                seen.add(key)
                records.append(
                    (
                        _SCOPE_ORDER[scope],
                        ratio,
                        key,
                        {"scope": scope, "ratio": ratio, "calls": value, "limit": cap},
                    )
                )

        # 顺序与判定一致：per-chat → 全局日 → 月度 → 费用。
        add(
            "per_chat",
            per_chat.calls,
            config.daily_calls,
            per_chat.tokens,
            config.daily_tokens,
        )
        add(
            "global_daily",
            global_daily.calls,
            config.global_daily_calls,
            global_daily.tokens,
            config.global_daily_tokens,
        )
        add(
            "monthly",
            monthly.calls,
            config.monthly_calls,
            monthly.tokens,
            config.monthly_tokens,
        )
        if config.monthly_cost_limit > 0:
            add(
                "cost",
                cost,  # 费用用同一对 calls/limit 字段承载。
                config.monthly_cost_limit,
                0,
                0,
            )

        for scope, ratio in extra_warnings:
            key = f"{scope}:{ratio}"
            if key in seen:
                continue
            seen.add(key)
            if scope == "global_daily":
                used, limit = _exceeded_metric(
                    global_daily, config.global_daily_calls, config.global_daily_tokens
                )
            else:
                used, limit = _exceeded_metric(
                    monthly, config.monthly_calls, config.monthly_tokens
                )
            records.append(
                (
                    _SCOPE_ORDER[scope],
                    float(ratio),
                    key,
                    {
                        "scope": scope,
                        "ratio": float(ratio),
                        "calls": used,
                        "limit": limit,
                    },
                )
            )

        records.sort(key=lambda item: (item[0], item[1]))
        strings = tuple(record[2] for record in records)
        details = tuple(record[3] for record in records)
        return strings, details

    def _remaining(
        self,
        action: BudgetAction,
        addressed: bool,
        scope: str,
        per_chat: UsageTotals,
        ambient: UsageTotals | None,
        global_daily: UsageTotals,
        monthly: UsageTotals,
        cost: float,
    ) -> tuple[int | None, int | None, float | None]:
        config = self.config
        if scope == "global_daily":
            calls = _remaining_count(config.global_daily_calls, global_daily.calls)
            tokens = _remaining_count(config.global_daily_tokens, global_daily.tokens)
        elif scope == "monthly":
            calls = _remaining_count(config.monthly_calls, monthly.calls)
            tokens = _remaining_count(config.monthly_tokens, monthly.tokens)
        elif scope == "cost":
            calls = _remaining_count(config.monthly_calls, monthly.calls)
            tokens = _remaining_count(config.monthly_tokens, monthly.tokens)
        elif action is BudgetAction.DENY:
            calls, tokens = 0, 0
        elif not addressed and ambient is not None:
            calls = _remaining_count(
                int(config.daily_calls * config.ambient_ratio), ambient.calls
            )
            tokens = _remaining_count(
                int(config.daily_tokens * config.ambient_ratio), ambient.tokens
            )
        else:
            calls = _remaining_count(config.daily_calls, per_chat.calls)
            tokens = _remaining_count(config.daily_tokens, per_chat.tokens)

        if config.monthly_cost_limit > 0:
            cost_left: float | None = max(0.0, config.monthly_cost_limit - cost)
        else:
            cost_left = None
        return calls, tokens, cost_left


def _remaining_count(limit: int, used: int) -> int | None:
    if limit <= 0:
        return None
    return max(0, int(limit) - int(used))


def _exceeded_metric(
    usage: UsageTotals, limit_calls: int, limit_tokens: int
) -> tuple[int, int]:
    """取真正触发超限的那一维（calls 或 tokens），供 1.0 预警明细使用。"""
    if limit_calls > 0 and usage.calls >= limit_calls:
        return usage.calls, limit_calls
    return usage.tokens, limit_tokens


def _shanghai_moment(now: datetime | None) -> datetime:
    """统一按上海时区取日期/月份；naive 时间视为已经是上海本地时间。"""
    if now is None:
        return now_shanghai()
    if now.tzinfo is None:
        return now
    return now.astimezone(SHANGHAI)
