import json

import pytest

from dafeiyu.brain.provider_config import (
    GENERATED_MARK,
    TEMPLATES_DIR,
    build_agent,
    build_config,
    build_memory_agent,
    normalize_base_url,
    render_config,
)
from dafeiyu.config import BrainConfig, LocalModelConfig


def test_free_mode_config_shape():
    cfg = BrainConfig()
    out = build_config("free", cfg)
    assert out["model"] == "opencode/deepseek-v4.1-flash-free"
    assert out["plugins"] == []
    assert "providers" not in out


def test_render_starts_with_generated_mark():
    text = render_config(build_config("free", BrainConfig()))
    assert text.splitlines()[0] == GENERATED_MARK
    assert json.loads("\n".join(text.splitlines()[1:]))["plugins"] == []


def test_key_mode_uses_file_reference():
    cfg = BrainConfig(
        mode="key", key_base_url="https://api.x/v1/", key_model="deepseek-chat"
    )
    out = build_config("key", cfg, key_rel="../../../secrets/provider.key")
    prov = out["providers"]["dafeiyu-key"]
    assert prov["package"] == "@opencode/ai/providers/openai-compatible"
    assert prov["settings"]["baseURL"] == "https://api.x/v1"
    assert prov["settings"]["apiKey"] == "{file:../../../secrets/provider.key}"
    assert "sk-" not in json.dumps(out)


def test_key_mode_defaults_to_documented_key_reference():
    cfg = BrainConfig(
        mode="key", key_base_url="https://api.x/v1", key_model="deepseek-chat"
    )
    out = build_config("key", cfg)
    settings = out["providers"]["dafeiyu-key"]["settings"]
    assert settings["apiKey"] == "{file:../../../secrets/provider.key}"
    assert "sk-" not in json.dumps(out)


def test_local_mode_writes_limit_and_capabilities():
    local = LocalModelConfig(
        base_url="http://127.0.0.1:1234/v1", model="qwen3", context=32768
    )
    out = build_config("local_model", BrainConfig(mode="local_model"), local=local)
    model = out["providers"]["dafeiyu-local"]["models"]["qwen3"]
    assert model["limit"]["context"] == 32768
    assert model["limit"]["output"] == 8192
    assert model["capabilities"]["tools"] is True
    assert "apiKey" not in out["providers"]["dafeiyu-local"]["settings"]


def test_unknown_mode_raises_with_mode_name():
    with pytest.raises(ValueError, match="bogus"):
        build_config("bogus", BrainConfig())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("https://api.x/v1/", "https://api.x/v1"),
        ("https://api.x/v1/  ", "https://api.x/v1"),
        (" https://api.x/v1/v1", "https://api.x/v1"),
        ("https://api.x/v1/v1/", "https://api.x/v1"),
        ("https://api.x/v1///", "https://api.x/v1"),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


AGENT_TEMPLATE = TEMPLATES_DIR / "agent_template.md"
MEMORY_AGENT_TEMPLATE = TEMPLATES_DIR / "memory_agent_template.md"


def _front_matter(text: str) -> str:
    lines = text.splitlines()
    assert lines[0] == "---"
    return "\n".join(lines[1 : lines.index("---", 1)])


def test_chat_agent_gets_steps_for_multi_round_retrieval():
    text = build_agent(
        "free", lean=False, template=AGENT_TEMPLATE.read_text(encoding="utf-8")
    )
    front_matter = _front_matter(text)
    assert "steps: 12" in front_matter
    assert "  - action: websearch" in front_matter
    assert "  - action: webfetch" in front_matter


def test_memory_agent_stays_single_step_without_tools():
    text = build_memory_agent(
        "free", template=MEMORY_AGENT_TEMPLATE.read_text(encoding="utf-8")
    )
    front_matter = _front_matter(text)
    assert "steps: 1" in front_matter
    assert "steps: 12" not in front_matter
    assert "websearch" not in front_matter
    assert "webfetch" not in front_matter


def test_agent_template_documents_retrieval_policy():
    text = AGENT_TEMPLATE.read_text(encoding="utf-8")
    for needle in (
        "必须先 `websearch`",
        "两个独立来源",
        "只有单一来源",
        "查不到",
        "`read`",
        "3-4 次搜索/抓取",
        "webfetch",
    ):
        assert needle in text, needle
