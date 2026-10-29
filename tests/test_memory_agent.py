"""记忆旁路 agent：档位一致、单步无工具、生成物可覆盖。

这条链路的坑在「聊天和记忆各钉一套模型」：手写的 memory agent 永远写死免费
模型，自备 Key 档下摘要/印象照样撞免费额度，额度一耗尽整条记忆旁路就瘫。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from dafeiyu.brain.provider_config import (
    AGENT_MARK,
    MEMORY_AGENT_NAME,
    build_memory_agent,
    memory_agent_path,
    write_generated,
)

MIN_TEMPLATE = "# 测试记忆器\n\n你只做整理。\n"

AGENT_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "dafeiyu"
    / "brain"
    / "memory_agent_template.md"
)
REPO_MEMORY_AGENT = (
    Path(__file__).resolve().parents[1] / ".opencode" / "agents" / f"{MEMORY_AGENT_NAME}.md"
)


def _front_matter(text: str) -> tuple[dict, list[str]]:
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end])), lines[end + 1 :]


def test_memory_agent_pins_same_model_as_mode() -> None:
    free = build_memory_agent("free", template=MIN_TEMPLATE)
    assert _front_matter(free)[0]["model"] == "opencode/deepseek-v4.1-flash-free"

    key = build_memory_agent("key", template=MIN_TEMPLATE)
    # 没显式给 cfg 时用默认 key_model；重点是它跟聊天 agent 同一个来源，不是免费档。
    assert _front_matter(key)[0]["model"].startswith("dafeiyu-key/")
    assert "flash-free" not in key


def test_memory_agent_is_single_step_and_tool_less() -> None:
    front, _ = _front_matter(build_memory_agent("free", template=MIN_TEMPLATE))
    assert front["steps"] == 1
    assert front["mode"] == "primary"
    assert front["permissions"] == [
        {"action": "*", "resource": "*", "effect": "deny"}
    ]
    # 与聊天 agent 不同：不留任何白名单，连 models.json 都不读。
    assert "allow" not in build_memory_agent("free", template=MIN_TEMPLATE)


def test_memory_agent_mark_sits_after_front_matter() -> None:
    text = build_memory_agent("free", template=MIN_TEMPLATE)
    _, body = _front_matter(text)
    assert body[0] == AGENT_MARK
    assert body[1] == ""
    assert body[2] == "# 测试记忆器"
    assert text.endswith("你只做整理。\n")


def test_memory_agent_path_matches_constant(tmp_path: Path) -> None:
    assert memory_agent_path(tmp_path) == (
        tmp_path / ".opencode" / "agents" / "dafeiyu-memory.md"
    )


def test_repo_memory_agent_is_up_to_date() -> None:
    """仓库里那份必须等于生成结果，否则改模板的人会忘了重新生成。"""
    generated = build_memory_agent(
        "free", template=AGENT_TEMPLATE.read_text(encoding="utf-8")
    )
    assert REPO_MEMORY_AGENT.read_text(encoding="utf-8") == generated


def test_write_generated_backs_up_hand_written_memory_agent(tmp_path: Path) -> None:
    """升级前那份是手写、没有生成标记：第一次覆盖必须留备份。"""
    target = memory_agent_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("手写的记忆 agent", encoding="utf-8")

    generated = build_memory_agent("key", template=MIN_TEMPLATE)
    assert write_generated(target, generated) is True
    backups = list(target.parent.glob("dafeiyu-memory.md.bak.*"))
    assert backups and backups[0].read_text(encoding="utf-8") == "手写的记忆 agent"
    assert write_generated(target, generated) is False
