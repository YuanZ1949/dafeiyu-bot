from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_service import FakeAdapter, FakeBrain, make_settings

from dafeiyu.budget import BudgetAction, BudgetDecision
from dafeiyu.config import BudgetConfig
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service

SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)


class BatchBrain(FakeBrain):
    async def reply(self, chat_type, chat_id, prompt, on_tool=None):
        raise AssertionError("explain_trigger 不应调用模型")


def make_msg(
    content: str = "随便聊聊",
    *,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = "qq:1001",
    user_id: str = "qq:2002",
    at: bool = False,
    reply: bool = False,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname="调试用户",
        content=content,
        message_id="debug-m1",
        segments=[],
        at_bot=at,
        reply_to_bot=reply,
        timestamp=int(NOON.timestamp()),
    )


def make_service(
    tmp_path: Path,
    *,
    mode: str = "rules",
    brain=None,
) -> tuple[Service, Store, Router]:
    settings = make_settings()
    settings = replace(
        settings,
        triggers=replace(settings.triggers, mode=mode),
    )
    store = Store(tmp_path / "trigger.db")
    store.init_schema()
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(
        settings,
        store,
        brain or FakeBrain(),
        {"qq": FakeAdapter()},
        router,
        "人设",
    )
    return service, store, router


def test_explain_trigger_rules_hard_is_model(tmp_path: Path) -> None:
    service, store, router = make_service(tmp_path)

    result = service.explain_trigger(make_msg("大肥鱼在吗", at=True), now=NOON)

    assert result["verdict"] == "model"
    assert result["mode"] == "rules"
    assert result["configured_mode"] == "rules"
    assert result["reason"] == "hard"
    assert result["hard_source"] == "at"
    assert result["checks"]
    assert service.scheduler is None
    assert store.count_messages(ChatType.GROUP, "qq:1001") == 0
    assert router._states == {}


def test_explain_trigger_model_group_enters_batch_without_scheduler(tmp_path: Path) -> None:
    service, store, router = make_service(
        tmp_path, mode="model", brain=BatchBrain()
    )

    result = service.explain_trigger(make_msg("随便聊聊"), now=NOON)

    assert result["verdict"] == "batch"
    assert result["mode"] == "model"
    assert result["reason"] == "batch"
    assert result["checks"]
    assert service.scheduler is None
    assert store.count_messages(ChatType.GROUP, "qq:1001") == 0
    assert router._states == {}


def test_explain_trigger_model_group_reports_deferred_budget(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path, mode="model", brain=BatchBrain())

    result = service.explain_trigger(make_msg("随便聊聊"), now=NOON)

    assert result["metrics"]["budget_ok"] is True
    budget = next(check for check in result["checks"] if check["key"] == "budget")
    assert budget["status"] == "info"
    assert "批次投递前" in result["checks"][-2]["detail"]


def test_explain_trigger_model_mute_blocks_even_addressed(tmp_path: Path) -> None:
    service, store, router = make_service(
        tmp_path, mode="model", brain=BatchBrain()
    )
    router.quiet(ChatType.GROUP, "qq:1001", 30)
    before = copy.deepcopy(router._states)

    result = service.explain_trigger(make_msg("在吗", at=True), now=NOON)

    assert result["verdict"] == "skip"
    assert result["reason"] == "muted"
    assert result["hard_source"] == "at"
    assert router._states == before
    assert store.count_messages(ChatType.GROUP, "qq:1001") == 0


def test_explain_trigger_private_budget_blocks(tmp_path: Path) -> None:
    service, store, router = make_service(tmp_path, mode="rules")
    budget = BudgetConfig(1, service.settings.budget.daily_tokens)
    service.settings = replace(service.settings, budget=budget)
    router.settings = service.settings
    service.budget_guard.config = budget
    store.record_usage(ChatType.PRIVATE, "qq:2002", "reply", 1, 1)

    result = service.explain_trigger(
        make_msg("你好", chat_type=ChatType.PRIVATE, chat_id="qq:2002"),
    )

    assert result["verdict"] == "skip"
    assert result["reason"] == "budget"
    assert result["reason_text"] == "本会话每日预算已用完"
    assert result["mode"] == "rules"
    assert router._states == {}


def test_explain_trigger_private_budget_guard_deny(tmp_path: Path, monkeypatch) -> None:
    service, _, _ = make_service(tmp_path, mode="rules")

    def deny(chat_type, chat_id, *, addressed, purpose, now=None):
        return BudgetDecision(BudgetAction.DENY, "budget_global_daily", "global_daily")

    monkeypatch.setattr(service.budget_guard, "evaluate", deny)
    result = service.explain_trigger(
        make_msg("你好", chat_type=ChatType.PRIVATE, chat_id="qq:2002")
    )

    assert result["verdict"] == "skip"
    assert result["reason"] == "budget"
    assert result["reason_text"] == "全局日预算已用完"
    assert result["metrics"]["budget_action"] == "deny"
    assert result["metrics"]["budget_scope"] == "global_daily"
    budget_check = next(c for c in result["checks"] if c["key"] == "budget")
    assert budget_check["status"] == "blocked"


def test_explain_trigger_private_budget_guard_degrade(tmp_path: Path, monkeypatch) -> None:
    service, _, _ = make_service(tmp_path, mode="rules")
    monkeypatch.setattr(
        service.budget_guard,
        "evaluate",
        lambda *a, **k: BudgetDecision(
            BudgetAction.DEGRADE, "budget_global_daily", "global_daily"
        ),
    )

    result = service.explain_trigger(
        make_msg("你好", chat_type=ChatType.PRIVATE, chat_id="qq:2002")
    )

    assert result["verdict"] == "model"
    assert result["reason"] == "private"
    assert result["metrics"]["budget_action"] == "degrade"
    budget_check = next(c for c in result["checks"] if c["key"] == "budget")
    assert budget_check["status"] == "pass"


def test_explain_trigger_model_budget_guard_deny(tmp_path: Path, monkeypatch) -> None:
    service, _, _ = make_service(tmp_path, mode="model", brain=BatchBrain())
    monkeypatch.setattr(
        service.budget_guard,
        "evaluate",
        lambda *a, **k: BudgetDecision(
            BudgetAction.DENY, "budget_per_chat", "per_chat"
        ),
    )

    result = service.explain_trigger(make_msg("随便聊聊"), now=NOON)

    assert result["verdict"] == "skip"
    assert result["reason"] == "budget"
    assert result["reason_text"] == "本会话每日预算已用完"
    assert result["metrics"]["budget_action"] == "deny"
    assert result["metrics"]["budget_scope"] == "per_chat"
    budget_check = next(c for c in result["checks"] if c["key"] == "budget")
    assert budget_check["status"] == "blocked"


def test_explain_trigger_model_cost_scope_text(tmp_path: Path, monkeypatch) -> None:
    service, _, _ = make_service(tmp_path, mode="model", brain=BatchBrain())
    monkeypatch.setattr(
        service.budget_guard,
        "evaluate",
        lambda *a, **k: BudgetDecision(BudgetAction.DENY, "budget_cost", "cost"),
    )

    result = service.explain_trigger(make_msg("随便聊聊"), now=NOON)

    assert result["reason"] == "budget"
    assert result["reason_text"] == "月度费用已达上限"
    assert result["metrics"]["budget_scope"] == "cost"


def test_explain_trigger_model_budget_guard_degrade_ambient(
    tmp_path: Path, monkeypatch
) -> None:
    service, _, _ = make_service(tmp_path, mode="model", brain=BatchBrain())
    monkeypatch.setattr(
        service.budget_guard,
        "evaluate",
        lambda *a, **k: BudgetDecision(
            BudgetAction.DEGRADE, "budget_global_daily", "global_daily"
        ),
    )

    result = service.explain_trigger(make_msg("随便聊聊"), now=NOON)

    assert result["verdict"] == "skip"
    assert result["reason"] == "budget"
    assert result["reason_text"] == "全局日预算已用完"
    assert result["metrics"]["budget_scope"] == "global_daily"
    budget_check = next(c for c in result["checks"] if c["key"] == "budget")
    assert budget_check["status"] == "blocked"


def test_explain_trigger_model_budget_guard_degrade_hard_still_batch(
    tmp_path: Path, monkeypatch
) -> None:
    service, _, _ = make_service(tmp_path, mode="model", brain=BatchBrain())
    monkeypatch.setattr(
        service.budget_guard,
        "evaluate",
        lambda *a, **k: BudgetDecision(
            BudgetAction.DEGRADE, "budget_global_daily", "global_daily"
        ),
    )

    result = service.explain_trigger(make_msg("在吗", at=True), now=NOON)

    assert result["verdict"] == "batch"
    assert result["reason"] == "batch"
    assert result["metrics"]["budget_action"] == "degrade"
    budget_check = next(c for c in result["checks"] if c["key"] == "budget")
    assert budget_check["status"] == "info"


def test_explain_trigger_known_command_is_not_executed(tmp_path: Path) -> None:
    service, store, router = make_service(tmp_path)

    result = service.explain_trigger(make_msg("/闭嘴"), now=NOON)

    assert result["verdict"] == "command"
    assert result["reason"] == "command"
    assert router._states == {}
    assert store.count_messages(ChatType.GROUP, "qq:1001") == 0


def test_explain_trigger_unknown_slash_falls_through_to_rules(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)

    result = service.explain_trigger(make_msg("/unknown"), now=NOON, seed=1)

    assert result["verdict"] != "command"
    assert result["reason"] == "sample_skip"
