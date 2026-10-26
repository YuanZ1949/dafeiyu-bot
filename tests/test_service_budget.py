from __future__ import annotations

from test_service import build, make_msg

from dafeiyu.budget import BudgetAction, BudgetDecision
from dafeiyu.models import ChatType


def decision(action: BudgetAction, reason: str = "allowed") -> BudgetDecision:
    scope = "per_chat" if action is BudgetAction.DENY else "none"
    return BudgetDecision(action, reason, scope)


def test_service_shares_router_guard(tmp_path) -> None:
    service, *_ = build(tmp_path)
    assert service.budget_guard is service.router.budget_guard


def test_budget_for_deny_blocks_addressed_and_ambient(tmp_path, monkeypatch) -> None:
    service, *_ = build(tmp_path)
    seen: list[tuple[bool, str]] = []

    def deny(chat_type, chat_id, *, addressed, purpose, now=None):
        seen.append((addressed, purpose))
        return decision(BudgetAction.DENY, "budget_per_chat")

    monkeypatch.setattr(service.router.budget_guard, "evaluate", deny)

    assert service._budget_for(ChatType.GROUP, 1001, addressed=False) is False
    assert service._budget_for(ChatType.GROUP, 1001, addressed=True) is False
    assert seen == [(False, "ambient"), (True, "reply")]


def test_budget_for_degrade_only_blocks_ambient(tmp_path, monkeypatch) -> None:
    service, *_ = build(tmp_path)
    monkeypatch.setattr(
        service.router.budget_guard,
        "evaluate",
        lambda *a, **k: decision(BudgetAction.DEGRADE, "budget_global_daily"),
    )

    assert service._budget_for(ChatType.GROUP, 1001, addressed=False) is False
    assert service._budget_for(ChatType.GROUP, 1001, addressed=True) is True


def test_budget_for_allow_passes(tmp_path, monkeypatch) -> None:
    service, *_ = build(tmp_path)
    monkeypatch.setattr(
        service.router.budget_guard,
        "evaluate",
        lambda *a, **k: decision(BudgetAction.ALLOW),
    )

    assert service._budget_for(ChatType.GROUP, 1001, addressed=False) is True


def test_private_budget_treats_message_as_addressed(tmp_path, monkeypatch) -> None:
    service, *_ = build(tmp_path)
    seen: list[tuple[bool, str]] = []

    def result(action: BudgetAction):
        def evaluate(chat_type, chat_id, *, addressed, purpose, now=None):
            seen.append((addressed, purpose))
            return decision(action)

        return evaluate

    monkeypatch.setattr(
        service.router.budget_guard, "evaluate", result(BudgetAction.ALLOW)
    )
    assert service._private_budget_ok(make_msg(chat_type=ChatType.PRIVATE)) is True

    monkeypatch.setattr(
        service.router.budget_guard, "evaluate", result(BudgetAction.DEGRADE)
    )
    assert service._private_budget_ok(make_msg(chat_type=ChatType.PRIVATE)) is True

    monkeypatch.setattr(
        service.router.budget_guard, "evaluate", result(BudgetAction.DENY)
    )
    assert service._private_budget_ok(make_msg(chat_type=ChatType.PRIVATE)) is False
    assert seen == [(True, "reply")] * 3
