from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from dafeiyu.brain.provider_config import (
    AGENT_MARK,
    AGENT_MARK_SCAN_LINES,
    agent_path,
    build_agent,
    ensure_agent_files,
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
    assert front_matter["steps"] == 12
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
    text = REPO_CHAT_AGENT.read_text(encoding="utf-8")
    # 本机切到 key/local 档后，supervisor 会按当前档位重写这两份 agent 文件，
    # 仓库基线（free 档）自然对不上；free 档下仍然逐字节校验。
    if "model: opencode/" not in text:
        pytest.skip("本地仓库当前非 free 档：agent 文件是运行时生成物")
    generated = build_agent(
        "free", lean=False, template=CHAT_AGENT_TEMPLATE.read_text(encoding="utf-8")
    )
    assert text == generated


# ---- T5：链接读取协议与批次讨论措辞（渲染到临时 root，不碰仓库生成物） -------


def _agent_settings(tmp_path: Path):
    from dafeiyu.config import load_settings

    config = tmp_path / "config.yaml"
    # load_settings 要求的段与 test_config_agents.BASE 相同：缺一段就抛 ConfigError。
    config.write_text(
        "persona:\n"
        "  name: 大肥鱼\n"
        "  aliases: [肥鱼]\n"
        "  os_probability: 0.05\n"
        "  max_reply_chars: 120\n"
        "  max_reply_chars_technical: 300\n"
        "  reply_hard_part: 500\n"
        "  reply_hard_total: 2000\n"
        "triggers:\n"
        "  cooldown_seconds: 60\n"
        "  sample_rate: 0.05\n"
        "  quiet_hours: [\"01:00\", \"07:00\"]\n"
        "  keywords: [怎么]\n"
        "groups:\n"
        "  whitelist: []\n"
        "  blacklist: []\n"
        "budget:\n"
        "  daily_calls: 200\n"
        "  daily_tokens: 300000\n"
        "memory:\n"
        "  window_size: 20\n"
        "logging:\n"
        "  level: INFO\n",
        encoding="utf-8",
    )
    env_file = tmp_path / "test.env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n", encoding="utf-8")
    return load_settings(env_file=env_file, config_file=config)


def test_ensure_agent_files_renders_link_protocol(tmp_path: Path) -> None:
    """渲染出的 agent 文件必须同时带上四处新措辞；只对临时 root 调用，绝不碰仓库根。"""
    settings = _agent_settings(tmp_path)
    ensure_agent_files(settings, root=tmp_path)
    text = agent_path(tmp_path).read_text(encoding="utf-8")
    for needle in ("最多直抓一次", "websearch", "不要再", "把这一批当成一段讨论"):
        assert needle in text, needle


def test_ensure_agent_files_has_no_unresolved_placeholders(tmp_path: Path) -> None:
    settings = _agent_settings(tmp_path)
    ensure_agent_files(settings, root=tmp_path)
    text = agent_path(tmp_path).read_text(encoding="utf-8")
    assert re.search(r"\$[a-z_]+", text) is None


def test_ensure_agent_files_renders_reply_hard_part_from_config(tmp_path: Path) -> None:
    """reply_hard_part 必须仍按配置渲染为数字：证明本次改动没碰长度配置。"""
    settings = _agent_settings(tmp_path)
    ensure_agent_files(settings, root=tmp_path)
    text = agent_path(tmp_path).read_text(encoding="utf-8")
    assert f"单条尽量不超过 {settings.persona.reply_hard_part} 字" in text
