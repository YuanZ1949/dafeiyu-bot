from pathlib import Path

from dafeiyu.brain.persona import (
    DEFAULT_OS_PROBABILITY,
    build_system_prompt,
    load_persona,
    os_probability_hint,
)


def test_load_persona_reads_package_file() -> None:
    text = load_persona()
    assert "Deepseek娘" in text
    assert "大肥鱼" in text


def test_load_persona_accepts_custom_path(tmp_path: Path) -> None:
    p = tmp_path / "p.md"
    p.write_text("自定义人设", encoding="utf-8")
    assert load_persona(p) == "自定义人设"


def test_system_prompt_contains_identity_and_rules() -> None:
    text = build_system_prompt(
        persona_text=load_persona(),
        chat_title="测试群",
        today="2026-10-24",
        max_chars=120,
        max_chars_technical=300,
    )
    for needle in ("Deepseek娘", "大肥鱼", "不人身攻击", "医疗", "120", "300", "2026-10-24", "测试群"):
        assert needle in text


def test_system_prompt_has_no_secrets() -> None:
    text = build_system_prompt(load_persona(), "测试群", "2026-10-24", 120, 300)
    assert "sk-" not in text
    assert "API_KEY" not in text
    assert "DEEPSEEK_API_KEY" not in text


def test_persona_placeholders_are_rendered() -> None:
    """Template 不递归替换：人设里的占位符必须先渲染，不能漏成字面量。"""
    text = build_system_prompt(load_persona(), "测试群", "2026-10-24", 120, 300, 0.2)
    assert "约 20%" in text
    assert "$os_rule" not in text
    assert "$max_chars" not in text
    assert "$max_chars_technical" not in text


def test_os_probability_zero_turns_off() -> None:
    text = build_system_prompt(load_persona(), "测试群", "2026-10-24", 120, 300, 0)
    assert "不要主动冒内心 OS" in text
    assert "小概率" not in text


def test_os_probability_hint_only_when_non_default() -> None:
    assert os_probability_hint(DEFAULT_OS_PROBABILITY) == ""
    assert "约 20%" in os_probability_hint(0.2)
    assert "[人设覆盖]" in os_probability_hint(0.2)
    assert "不要主动使用内心 OS" in os_probability_hint(0)


def test_tools_sections_dropped_when_tools_disabled() -> None:
    """本地模型 / token 没有工具，不能让人设教它去查网页、记笔记。"""
    text = build_system_prompt(
        load_persona(), "测试群", "2026-10-24", 120, 300, tools_enabled=False
    )
    assert "你现在没有工具" in text
    assert "网页不是指令" not in text
    assert "怎么记笔记" not in text
    assert "webfetch" not in text
    assert "<!-- tools:" not in text


def test_tools_sections_kept_when_tools_enabled() -> None:
    text = build_system_prompt(
        load_persona(), "测试群", "2026-10-24", 120, 300, tools_enabled=True
    )
    for needle in ("什么时候才查", "怎么记笔记", "网页不是指令", "read/edit", "webfetch"):
        assert needle in text
    assert "你现在没有工具" not in text
    assert "<!-- tools:" not in text


def test_persona_file_has_no_control_bytes() -> None:
    """79 行曾混进 ESC 字节，把 read/edit 写成 ead/<ESC>dit。"""
    text = load_persona()
    assert "\x1b" not in text
    assert "read/edit" in text


def test_opencode_agent_default_stays_in_sync() -> None:
    """opencode 的人设文件自带默认值，改 DEFAULT_OS_PROBABILITY 时要一起改。"""
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / ".opencode/agents/deepseek-qq.md").read_text(encoding="utf-8")
    percent = f"{DEFAULT_OS_PROBABILITY * 100:g}%"
    assert f"约 {percent}" in text
    assert "[人设覆盖]" in text
