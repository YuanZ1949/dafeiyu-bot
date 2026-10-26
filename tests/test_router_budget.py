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
