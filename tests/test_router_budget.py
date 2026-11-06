from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import TEST_CONFIG_YAML

from dafeiyu.config import load_settings
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router

SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=SHANGHAI)
TODAY = NOW.date().isoformat()


def make_settings(tmp_path: Path, **budget_overrides):
    config = tmp_path / "config.yaml"
    config.write_text(TEST_CONFIG_YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)
    return replace(settings, budget=replace(settings.budget, **budget_overrides))


def usage(
    store: Store,
    *,
    chat_id: int = 1,
    chat_type: ChatType = ChatType.GROUP,
    purpose: str = "reply",
    calls: int = 1,
    tokens: int = 0,
) -> None:
    for _ in range(calls):
        store.record_usage(chat_type, chat_id, purpose, tokens, 0)


def msg(
    content: str = "在吗",
    *,
    at: bool = False,
    chat_id: int = 1,
    chat_type: ChatType = ChatType.GROUP,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=2,
        nickname="阿伟",
        content=content,
        message_id="m1",
        segments=[],
        at_bot=at,
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "dafeiyu.memory.store.today_shanghai", lambda: NOW.date().isoformat()
    )
    store = Store(tmp_path / "test.db")
    store.init_schema()

    def build(**budget_overrides):
        settings = make_settings(tmp_path, **budget_overrides)
        router = Router(settings, store, clock=lambda: NOW)
        return router

    yield build, store
    store.close()


def test_global_budget_blocks_non_hard(env) -> None:
    build, store = env
    router = build(global_daily_calls=10)
    usage(store, calls=10)

    decision = router.should_handle(msg())

    assert decision.handle is False
    assert decision.reason == "budget"


def test_hard_trigger_survives_degrade(env) -> None:
    build, store = env
    router = build(daily_calls=2, ambient_ratio=0.5)
    usage(store, purpose="ambient", calls=1, tokens=100)

    ambient = router.should_handle(msg())
    hard = router.should_handle(msg(at=True))

    assert ambient.handle is False and ambient.reason == "budget"
    assert hard.handle is True and hard.reason == "hard"


def test_explain_lists_three_new_checks(env) -> None:
    build, _store = env
    router = build()

    data = router.explain(msg())

    labels = [check["label"] for check in data["checks"]]
    for label in ("每日预算", "全局日预算", "月度预算", "月度费用"):
        assert label in labels
    assert labels.index("全局日预算") == labels.index("每日预算") + 1
    assert labels.index("月度预算") == labels.index("全局日预算") + 1
    assert labels.index("月度费用") == labels.index("月度预算") + 1
    assert data["metrics"]["budget_action"] in {"allow", "degrade", "deny"}


def test_explain_blames_per_chat_scope(env) -> None:
    build, store = env
    router = build(daily_calls=2)
    usage(store, calls=2)

    data = router.explain(msg())
    by_key = {check["key"]: check["status"] for check in data["checks"]}

    assert data["metrics"]["budget_scope"] == "per_chat"
    assert by_key["budget"] == "blocked"
    assert by_key["budget_global"] == "skipped"


def test_explain_blames_global_scope(env) -> None:
    build, store = env
    router = build(global_daily_calls=10)
    usage(store, calls=10)

    data = router.explain(msg())
    by_key = {check["key"]: check["status"] for check in data["checks"]}

    assert data["metrics"]["budget_scope"] == "global_daily"
    assert by_key["budget"] == "pass"
    assert by_key["budget_global"] == "blocked"
    assert by_key["budget_monthly"] == "skipped"


def test_explain_blames_monthly_scope(env) -> None:
    build, store = env
    router = build(monthly_calls=10)
    usage(store, calls=10)

    data = router.explain(msg())
    by_key = {check["key"]: check["status"] for check in data["checks"]}

    assert data["metrics"]["budget_scope"] == "monthly"
    assert by_key["budget_global"] == "pass"
    assert by_key["budget_monthly"] == "blocked"
    assert by_key["budget_cost"] == "skipped"


def test_explain_blames_cost_scope(env) -> None:
    build, store = env
    router = build(
        daily_tokens=10_000_000,
        monthly_cost_limit=1.0,
        price_input_per_million=2.0,
    )
    usage(store, calls=1, tokens=1_000_000)

    data = router.explain(msg())
    by_key = {check["key"]: check["status"] for check in data["checks"]}

    assert data["metrics"]["budget_scope"] == "cost"
    assert by_key["budget_monthly"] == "pass"
    assert by_key["budget_cost"] == "blocked"


def test_explain_storage_error_has_clear_detail(env, monkeypatch) -> None:
    build, store = env
    router = build()

    def boom(_date):
        raise RuntimeError("db broken")

    monkeypatch.setattr(store, "usage_global_today", boom)

    data = router.explain(msg())
    by_key = {check["key"]: check for check in data["checks"]}

    assert data["metrics"]["budget_action"] == "degrade"
    assert data["metrics"]["budget_scope"] == "storage_error"
    assert by_key["budget"]["status"] == "blocked"
    assert "读库失败" in by_key["budget"]["detail"]


def test_explain_per_chat_detail_only_counts_reply_and_ambient(env) -> None:
    build, store = env
    router = build()
    usage(store, purpose="summary", calls=5, tokens=900)
    usage(store, purpose="reply", calls=1, tokens=100)

    data = router.explain(msg())

    assert data["metrics"]["usage_calls"] == 1
    assert data["metrics"]["usage_tokens"] == 100
    assert "1/200" in data["checks"][3]["detail"]


def test_explain_disabled_limits_render_unlimited(env) -> None:
    build, _store = env
    router = build()

    data = router.explain(msg())
    by_key = {check["key"]: check for check in data["checks"]}

    assert by_key["budget_global"]["detail"] == "未设上限"
    assert by_key["budget_monthly"]["detail"] == "未设上限"


def test_explain_reports_global_and_monthly_metrics(env) -> None:
    build, store = env
    router = build(
        global_daily_calls=20,
        monthly_calls=100,
        monthly_cost_limit=10.0,
        price_input_per_million=1.0,
        price_output_per_million=1.0,
    )
    usage(store, calls=1, tokens=100)

    data = router.explain(msg())
    metrics = data["metrics"]

    assert metrics["global_calls"] == 1
    assert metrics["global_limit_calls"] == 20
    assert metrics["monthly_calls"] == 1
    assert metrics["monthly_limit_calls"] == 100
    assert metrics["monthly_cost"] == pytest.approx(0.0001)
    assert isinstance(metrics["warnings"], list)
    assert metrics["budget_scope"] in {
        "none",
        "per_chat",
        "global_daily",
        "monthly",
        "cost",
    }


def test_explain_reports_closed_thread_as_observer_only(env) -> None:
    """收尾只剩观测信号：explain 里 closed 不再是 blocked 的判定原因。"""
    build, _store = env
    router = build()
    router.note_bot_spoke(ChatType.GROUP, 1, ambient=True)
    router.clock = lambda: NOW + timedelta(seconds=301)

    data = router.explain(msg(), seed=0)

    assert data["decision"]["reason"] == "sample_skip"
    by_key = {check["key"]: check["status"] for check in data["checks"]}
    assert by_key["closed"] == "pass"
    assert data["metrics"]["closed_remaining_seconds"] > 0
    assert "不再拦截" in by_key_detail(data, "closed")


def by_key_detail(data: dict, key: str) -> str:
    return next(check["detail"] for check in data["checks"] if check["key"] == key)
