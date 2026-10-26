from __future__ import annotations

import pytest

from dafeiyu.commands import CommandRegistry
from dafeiyu.models import ChatType


@pytest.fixture
def registry() -> CommandRegistry:
    return CommandRegistry()


def test_match_aliases_and_args(registry: CommandRegistry) -> None:
    matched = registry.match("/帮助 额外")
    assert matched is not None
    spec, args = matched
    assert spec.name == "help"
    assert args == "额外"

    stats = registry.match("/stats")
    assert stats is not None and stats[0].name == "stats"

    alias = registry.match("/统计")
    assert alias is not None and alias[0].name == "stats"


def test_match_joins_multiple_args(registry: CommandRegistry) -> None:
    matched = registry.match("/help 一 二 三")
    assert matched is not None
    assert matched[1] == "一 二 三"


def test_unknown_and_wrong_scope(registry: CommandRegistry) -> None:
    assert registry.match("/nope") is None
    # shutup 只在群里有效，私聊应当落到正常路径。
    assert registry.match("/shutup", ChatType.PRIVATE) is None
    assert registry.match("/闭嘴", ChatType.PRIVATE) is None
    assert registry.match("/shutup", ChatType.GROUP) is not None


@pytest.mark.parametrize("text", ["", "   ", "/", "没有斜杠", "@溟月 在吗"])
def test_non_commands_return_none(registry: CommandRegistry, text: str) -> None:
    assert registry.match(text) is None


def test_case_insensitive_command_and_alias(registry: CommandRegistry) -> None:
    upper = registry.match("/STATS")
    assert upper is not None and upper[0].name == "stats"

    mixed = registry.match("/ShutUp", ChatType.GROUP)
    assert mixed is not None and mixed[0].name == "shutup"

    upper_alias = registry.match("/帮助")
    assert upper_alias is not None and upper_alias[0].name == "help"


def test_is_known_accepts_with_or_without_slash_and_checks_scope(
    registry: CommandRegistry,
) -> None:
    assert registry.is_known("/stats", ChatType.GROUP)
    assert registry.is_known("stats", ChatType.PRIVATE)
    assert registry.is_known("统计", ChatType.PRIVATE)
    assert registry.is_known("/帮助", ChatType.GROUP)
    assert not registry.is_known("shutup", ChatType.PRIVATE)
    assert registry.is_known("shutup", ChatType.GROUP)
    assert not registry.is_known("/nope", ChatType.GROUP)


def test_help_text_hides_admin_commands_for_non_admin(
    registry: CommandRegistry,
) -> None:
    text = registry.help_text(ChatType.GROUP, is_admin=False)
    assert "/help" in text
    assert "/stats" in text
    assert "/budget" not in text
    assert "/status" not in text
    assert "/forget" not in text
    assert "/闭嘴" not in text
    assert "管理员：" not in text


def test_help_text_for_group_admin_lists_admin_commands(
    registry: CommandRegistry,
) -> None:
    text = registry.help_text(ChatType.GROUP, is_admin=True)
    assert "/help" in text
    assert "/stats" in text
    assert "/budget" in text
    assert "/status" in text
    assert "/forget" in text
    assert "管理员：" in text


def test_help_text_private_non_admin_keeps_forget(
    registry: CommandRegistry,
) -> None:
    text = registry.help_text(ChatType.PRIVATE, is_admin=False)
    assert "/forget" in text
    assert "/budget" not in text
    assert "/status" not in text


def test_help_text_private_admin_lists_budget_and_forget(
    registry: CommandRegistry,
) -> None:
    text = registry.help_text(ChatType.PRIVATE, is_admin=True)
    assert "/budget" in text
    assert "/status" in text
    assert "/forget" in text
