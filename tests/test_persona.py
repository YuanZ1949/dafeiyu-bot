from pathlib import Path

from dafeiyu.brain.persona import build_system_prompt, load_persona


def test_load_persona_reads_package_file() -> None:
    text = load_persona()
    assert "溟月" in text
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
    for needle in ("溟月", "大肥鱼", "不人身攻击", "医疗", "120", "300", "2026-10-24", "测试群"):
        assert needle in text


def test_system_prompt_has_no_secrets() -> None:
    text = build_system_prompt(load_persona(), "测试群", "2026-10-24", 120, 300)
    assert "sk-" not in text
    assert "API_KEY" not in text
    assert "DEEPSEEK_API_KEY" not in text
