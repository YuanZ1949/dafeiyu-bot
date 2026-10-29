from __future__ import annotations

from pathlib import Path

import yaml

from dafeiyu.brain.provider_config import (
    AGENT_MARK,
    AGENT_MARK_SCAN_LINES,
    build_agent,
    write_generated,
)

MIN_TEMPLATE = "# 测试人设\n\n你是一条鱼。\n"


def _split_front_matter(text: str) -> tuple[dict, str]:
    """按 opencode 的读法切前置区：第 1 行必须是 `---`，到下一个 `---` 为止。"""
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end])), "\n".join(lines[end + 1 :])


def test_build_agent_pins_model_for_mode():
    text = build_agent("key", lean=False, template=MIN_TEMPLATE)
    lines = text.splitlines()
    assert text.startswith("---\n")
    close = lines.index("---", 1)
    # 标记只能落在闭合 `---` 之后：写在首行会让整个前置区失效。
    assert lines[close + 1] == AGENT_MARK
    assert AGENT_MARK in lines[:AGENT_MARK_SCAN_LINES]
    assert "model: dafeiyu-key/" in text
    assert "model: opencode/deepseek-v4.1-flash-free" not in text


def test_build_agent_denies_shell_and_secrets():
    text = build_agent("free", lean=False, template=MIN_TEMPLATE)
    assert 'action: "*"' in text and "effect: deny" in text
    for secret in (
        ".env",
        "data/secrets",
        "data/dashboard.password",
        "data/supervisor.token",
    ):
        assert secret in text
    assert 'read: "*"' not in text


def test_build_agent_front_matter_parses_as_yaml():
    text = build_agent("free", lean=False, template=MIN_TEMPLATE)
    front_matter, body = _split_front_matter(text)
    assert front_matter["model"] == "opencode/deepseek-v4.1-flash-free"
    assert front_matter["mode"] == "primary"
    assert front_matter["steps"] == 4
    assert front_matter["permissions"][0] == {
        "action": "*",
        "resource": "*",
        "effect": "deny",
    }
    body_lines = body.splitlines()
    assert body_lines[0] == AGENT_MARK
    assert body_lines[1] == ""
    assert body_lines[2] == "# 测试人设"


def test_write_generated_backs_up_manual_file(tmp_path):
    p = tmp_path / "deepseek-qq.md"
    p.write_text("手写的", encoding="utf-8")
    generated = build_agent("free", lean=False, template=MIN_TEMPLATE)
    assert write_generated(p, generated) is True
    backups = list(tmp_path.glob("deepseek-qq.md.bak.*"))
    assert backups
    assert backups[0].read_text(encoding="utf-8") == "手写的"
    assert p.read_text(encoding="utf-8") == generated


def test_write_generated_is_idempotent(tmp_path):
    p = tmp_path / "deepseek-qq.md"
    generated = build_agent("free", lean=False, template=MIN_TEMPLATE)
    write_generated(p, generated)
    assert write_generated(p, generated) is False
    assert not list(tmp_path.glob("*.bak.*"))


def test_write_generated_overwrites_generated_file_without_backup(tmp_path):
    p = tmp_path / "deepseek-qq.md"
    first = build_agent("free", lean=False, template=MIN_TEMPLATE)
    write_generated(p, first)
    assert write_generated(p, first.replace("你是一条鱼", "你是一条鲸")) is False
    assert p.read_text(encoding="utf-8").endswith("你是一条鲸。\n")
    assert not list(tmp_path.glob("*.bak.*"))


def test_build_agent_lean_drops_tools_but_keeps_denies():
    text = build_agent("free", lean=True, template=MIN_TEMPLATE)
    assert "data/bot-workspace/**" not in text
    assert "webfetch" not in text
    assert "本轮没有工具" in text
    assert text.splitlines()[-1].startswith("（本轮没有工具")
    assert 'action: "*"' in text and "effect: deny" in text
    assert ".env" in text and "data/secrets" in text


CHAT_AGENT_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "dafeiyu"
    / "brain"
    / "agent_template.md"
)
REPO_CHAT_AGENT = (
    Path(__file__).resolve().parents[1] / ".opencode" / "agents" / "deepseek-qq.md"
)


def test_repo_chat_agent_is_up_to_date() -> None:
    """仓库里那份必须等于生成结果：手改会静默丢掉密钥 deny 段之类的更新。"""
    generated = build_agent(
        "free", lean=False, template=CHAT_AGENT_TEMPLATE.read_text(encoding="utf-8")
    )
    assert REPO_CHAT_AGENT.read_text(encoding="utf-8") == generated
