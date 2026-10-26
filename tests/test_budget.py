"""BudgetGuard：per-chat/全局日/月度/费用判定与预警阈值。"""

from __future__ import annotations

from datetime import datetime

import pytest

from dafeiyu.budget import (
    BudgetAction,
    BudgetGuard,
    BudgetReport,
    estimate_cost,
)
from dafeiyu.config import BudgetConfig
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType
from dafeiyu.timeutil import SHANGHAI

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=SHANGHAI)
TODAY = "2026-10-26"


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    yield s
    s.close()


def usage(
    store: Store,
    *,
    date: str = TODAY,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: int = 1,
    purpose: str = "reply",
    prompt: int = 0,
    completion: int = 0,
    estimated: int = 0,
) -> None:
    """绕过 record_usage 直接写指定日期的 usage 行。"""
    store._conn.execute(
        "INSERT INTO usage "
        "(chat_type, chat_id, date, purpose, prompt_tokens, completion_tokens, estimated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_type.value, chat_id, date, purpose, prompt, completion, estimated),
    )
    store._conn.commit()


def make_config(**overrides) -> BudgetConfig:
    values = {"daily_calls": 100, "daily_tokens": 100_000}
    values.update(overrides)
    return BudgetConfig(**values)


def make_guard(store: Store, **overrides) -> BudgetGuard:
    return BudgetGuard(store, make_config(**overrides))


@pytest.fixture
def guard(store):
    # 默认只有 1 次额度，方便把“达到 per-chat 上限”写成一条数据。
    return make_guard(store, daily_calls=1, daily_tokens=1000)


def test_per_chat_limit_denies(guard, store):
    usage(store, date=TODAY, chat_id=1, prompt=10, completion=10)
    d = guard.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.DENY and d.scope == "per_chat"


def test_per_chat_limit_is_hard_for_addressed(store):
    g = make_guard(store, daily_calls=10, daily_tokens=1000)
    for _ in range(10):
        usage(store, date=TODAY, purpose="reply", prompt=1)
    assert (
        g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW).action
        is BudgetAction.DENY
    )


def test_ambient_ratio_boundary(store):
    g = make_guard(store, daily_calls=10, daily_tokens=1000, ambient_ratio=0.8)
    for _ in range(8):
        usage(store, date=TODAY, chat_id=1, purpose="ambient", prompt=1)
    assert (
        g.evaluate(ChatType.GROUP, 1, addressed=False, purpose="ambient", now=NOW).action
        is BudgetAction.DEGRADE
    )
    assert (
        g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW).action
        is BudgetAction.ALLOW
    )


@pytest.mark.parametrize(
    "on_exceed,action",
    [
        ("degrade", BudgetAction.DEGRADE),
        ("stop", BudgetAction.DENY),
        ("notify", BudgetAction.ALLOW),
    ],
)
def test_global_daily_on_exceed(store, on_exceed, action):
    g = make_guard(
        store,
        daily_calls=1000,
        daily_tokens=1_000_000,
        global_daily_calls=10,
        on_exceed=on_exceed,
    )
    for _ in range(10):
        usage(store, date=TODAY, chat_id=1, prompt=1)
    d = g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is action
    assert d.scope == "global_daily"
    if on_exceed == "notify":
        assert d.warnings.count("global_daily:1.0") == 1


@pytest.mark.parametrize(
    "on_exceed,action",
    [
        ("degrade", BudgetAction.DEGRADE),
        ("stop", BudgetAction.DENY),
        ("notify", BudgetAction.ALLOW),
    ],
)
def test_monthly_on_exceed(store, on_exceed, action):
    g = make_guard(
        store,
        daily_calls=1000,
        daily_tokens=1_000_000,
        monthly_calls=10,
        on_exceed=on_exceed,
    )
    for _ in range(10):
        usage(store, date=TODAY, chat_id=1, prompt=1)
    d = g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is action
    assert d.scope == "monthly"
    if on_exceed == "notify":
        assert d.warnings.count("monthly:1.0") == 1


def test_monthly_cost_is_hard_stop_even_notify(store):
    g = make_guard(
        store,
        on_exceed="notify",
        monthly_cost_limit=1.0,
        price_input_per_million=1000.0,
    )
    usage(store, date=TODAY, prompt=2000)
    d = g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.DENY and d.scope == "cost"


def test_warnings_do_not_change_action(store):
    g = make_guard(
        store, daily_calls=10, daily_tokens=1000, warning_ratios=(0.5, 0.8)
    )
    for _ in range(5):
        usage(store, date=TODAY, prompt=1)
    d = g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.ALLOW
    assert d.warnings == ("per_chat:0.5",)


def test_warnings_follow_scope_order(store):
    g = make_guard(
        store,
        daily_calls=10,
        daily_tokens=1000,
        global_daily_calls=10,
        monthly_calls=10,
        warning_ratios=(0.5,),
    )
    for _ in range(5):
        usage(store, date=TODAY, prompt=1)
    d = g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.warnings == ("per_chat:0.5", "global_daily:0.5", "monthly:0.5")


def test_cost_warning_does_not_change_action(store):
    g = make_guard(
        store,
        daily_tokens=10_000_000,
        monthly_cost_limit=10.0,
        price_input_per_million=10.0,
        warning_ratios=(0.5,),
    )
    usage(store, date=TODAY, prompt=500_000)
    d = g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.ALLOW
    assert d.warnings == ("cost:0.5",)


def test_storage_error_degrades(monkeypatch, guard):
    monkeypatch.setattr(
        guard.store,
        "usage_between",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")),
    )
    d = guard.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.DEGRADE
    assert d.scope == "storage_error" and d.warnings == ("budget_storage_error",)
    assert d.reason == "budget_storage_error"


@pytest.mark.parametrize("method", ["usage_global_today", "usage_month"])
def test_storage_error_covers_all_reads(monkeypatch, guard, method):
    monkeypatch.setattr(
        guard.store,
        method,
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")),
    )
    d = guard.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.DEGRADE
    assert d.scope == "storage_error"


def test_other_purpose_allows_without_query(monkeypatch, guard):
    monkeypatch.setattr(
        guard.store,
        "usage_between",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该查库")),
    )
    d = guard.evaluate(ChatType.GROUP, 1, addressed=True, purpose="summary", now=NOW)
    assert d.action is BudgetAction.ALLOW
    assert d.scope == "none" and d.warnings == ()


def test_non_budget_purposes_are_ignored(store):
    g = make_guard(store, daily_calls=10, daily_tokens=1000)
    usage(store, date=TODAY, purpose="summary", prompt=5000, completion=5000)
    usage(store, date=TODAY, purpose="report", prompt=5000, completion=5000)
    assert (
        g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW).action
        is BudgetAction.ALLOW
    )


def test_zero_limits_skipped(store):
    g = make_guard(
        store,
        daily_calls=1_000_000,
        daily_tokens=1_000_000_000,
        global_daily_calls=0,
        global_daily_tokens=0,
        monthly_calls=0,
        monthly_tokens=0,
        monthly_cost_limit=0.0,
        warning_ratios=(0.5, 0.8),
    )
    for _ in range(1000):
        usage(store, date=TODAY, prompt=1000)
    d = g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.ALLOW
    assert d.warnings == ()


def test_estimate_cost_uses_per_million_prices():
    config = make_config(price_input_per_million=2.0, price_output_per_million=4.0)
    assert estimate_cost(1_000_000, 500_000, config) == pytest.approx(4.0)
    assert estimate_cost(0, 0, config) == 0.0


def test_report_cost(store):
    g = make_guard(
        store,
        daily_calls=10,
        daily_tokens=10_000_000,
        monthly_cost_limit=10.0,
        price_input_per_million=2.0,
        price_output_per_million=4.0,
        warning_ratios=(0.5,),
    )
    usage(store, date=TODAY, prompt=1_000_000, completion=500_000, estimated=1)
    report = g.report(ChatType.GROUP, 1, now=NOW)

    assert isinstance(report, BudgetReport)
    assert report.cost == {
        "cost": pytest.approx(4.0),
        "cost_limit": 10.0,
        "currency": "CNY",
        "estimated": True,
    }
    assert report.per_chat["calls"] == 1
    assert report.per_chat["tokens"] == 1_500_000
    assert report.global_daily["calls"] == 1
    assert report.monthly["tokens"] == 1_500_000
    assert report.warnings == ()


def test_report_shape_and_warnings(store):
    g = make_guard(
        store,
        daily_calls=10,
        daily_tokens=1000,
        global_daily_calls=100,
        monthly_calls=100,
        warning_ratios=(0.5, 0.8),
    )
    for _ in range(5):
        usage(store, date=TODAY, prompt=1)
    report = g.report(ChatType.GROUP, 1, now=NOW)

    assert report.per_chat == {
        "calls": 5,
        "tokens": 5,
        "daily_calls": 10,
        "daily_tokens": 1000,
    }
    assert report.global_daily == {
        "calls": 5,
        "tokens": 5,
        "limit_calls": 100,
        "limit_tokens": 0,
    }
    assert report.monthly == {
        "calls": 5,
        "tokens": 5,
        "limit_calls": 100,
        "limit_tokens": 0,
    }
    assert report.cost == {
        "cost": 0.0,
        "cost_limit": 0.0,
        "currency": "CNY",
        "estimated": False,
    }
    assert report.warnings == (
        {"scope": "per_chat", "ratio": 0.5, "calls": 5, "limit": 10},
    )


def test_report_is_json_ready(store):
    import json

    g = make_guard(store, daily_calls=10, daily_tokens=1000)
    usage(store, date=TODAY, prompt=1)
    report = g.report(ChatType.GROUP, 1, now=NOW)
    payload = {
        "per_chat": report.per_chat,
        "global_daily": report.global_daily,
        "monthly": report.monthly,
        "cost": report.cost,
        "warnings": list(report.warnings),
    }
    assert json.loads(json.dumps(payload))["warnings"] == []
