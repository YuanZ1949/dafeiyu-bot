from __future__ import annotations

from dafeiyu.brain.provider_config import AGENT_MARK, build_agent, write_generated

MIN_TEMPLATE = "# 测试人设\n\n你是一条鱼。\n"


def test_build_agent_pins_model_for_mode():
    text = build_agent("key", lean=False, template=MIN_TEMPLATE)
    assert text.splitlines()[0] == AGENT_MARK
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


def test_write_generated_backs_up_manual_file(tmp_path):
    p = tmp_path / "deepseek-qq.md"
    p.write_text("手写的", encoding="utf-8")
    assert write_generated(p, AGENT_MARK + "\n新内容") is True
    assert list(tmp_path.glob("deepseek-qq.md.bak.*"))
    assert p.read_text(encoding="utf-8").startswith(AGENT_MARK)


def test_write_generated_is_idempotent(tmp_path):
    p = tmp_path / "deepseek-qq.md"
    write_generated(p, AGENT_MARK + "\nA")
    assert write_generated(p, AGENT_MARK + "\nA") is False
    assert not list(tmp_path.glob("*.bak.*"))


def test_build_agent_lean_drops_tools_but_keeps_denies():
    text = build_agent("free", lean=True, template=MIN_TEMPLATE)
    assert "data/bot-workspace/**" not in text
    assert "webfetch" not in text
    assert "本轮没有工具" in text
    assert 'action: "*"' in text and "effect: deny" in text
    assert ".env" in text and "data/secrets" in text
