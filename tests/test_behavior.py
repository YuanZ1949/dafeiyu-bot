"""聊天强度与言论限制：解析、注入文案、关键词匹配、配置/覆盖层入口。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dafeiyu import config
from dafeiyu.behavior import (
    BehaviorError,
    BehaviorSettings,
    as_payload,
    parse_behavior,
)
from dafeiyu.config import ConfigError, load_settings
from dafeiyu.settings_local import write_overrides

BASE_YAML = """\
persona:
  name: 溟月
  aliases: [大肥鱼]
triggers:
  cooldown_seconds: 60
  sample_rate: 0.05
  quiet_hours: ["01:00", "07:00"]
  keywords: [怎么]
groups:
  whitelist: []
  blacklist: []
budget:
  daily_calls: 200
  daily_tokens: 300000
memory:
  window_size: 20
  impression_topk: 5
  extract_every: 20
logging:
  level: INFO
  dir: logs
brain:
  provider: token
"""


@pytest.fixture(autouse=True)
def _isolate_overrides(tmp_path, monkeypatch):
    """测试机上的 data/settings.local.yaml 会盖掉夹具配置，先指到不存在的路径。"""
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "no-overrides.yaml")


def _load(tmp_path: Path, yaml_text: str = BASE_YAML):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")
    env_path = tmp_path / "test.env"
    env_path.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    return load_settings(env_file=env_path, config_file=config_path)


def test_defaults_are_noop():
    b = BehaviorSettings()
    assert b.framing() == ""
    assert b.match("随便说点什么") is None
    assert as_payload(b)["chat_intensity"] == "normal"


def test_parse_normalizes_and_dedupes():
    b = parse_behavior(
        {
            "chat_intensity": " Active ",
            "restriction_mode": "STRICT",
            "banned_phrases": ["  内部黑话 ", "内部黑话", ""],
            "banned_topics": ["股票\n推荐"],
        }
    )
    assert b.chat_intensity == "active"
    assert b.restriction_mode == "strict"
    assert b.banned_phrases == ("内部黑话",)
    assert b.banned_topics == ("股票 推荐",)


@pytest.mark.parametrize(
    "payload",
    [
        {"chat_intensity": "loud"},
        {"restriction_mode": "block"},
        {"banned_phrases": {"a": 1}},
        {"banned_phrases": [123]},
        {"banned_phrases": ["长" * 60]},
    ],
)
def test_parse_rejects_invalid_values(payload):
    with pytest.raises(BehaviorError):
        parse_behavior(payload)


def test_yaml_bool_off_is_tolerated():
    # YAML 1.1 会把裸 off 解析成 False；手改配置的人很容易踩到。
    assert parse_behavior({"restriction_mode": False}).restriction_mode == "off"


def test_regenerate_mode_parses_and_frames():
    b = parse_behavior(
        {
            "restriction_mode": "regenerate",
            "banned_phrases": ["作为一个AI"],
        }
    )
    assert b.restriction_mode == "regenerate"
    framing = b.framing()
    # 照常注入禁句列表，但不要出现 strict 的整条丢弃文案。
    assert "作为一个AI" in framing
    assert "先尝试改写" in framing
    assert "整条回复会被丢弃" not in framing


def test_regenerate_payload_label():
    b = parse_behavior({"restriction_mode": "regenerate"})
    assert as_payload(b)["restriction_label"] == "先改写（最多 2 次）"


def test_restriction_mode_bogus_rejected_with_field_name():
    with pytest.raises(BehaviorError, match="restriction_mode"):
        parse_behavior({"restriction_mode": "bogus"})


def test_match_is_casefold_substring_and_first_hit_wins():
    b = BehaviorSettings(
        restriction_mode="strict",
        banned_phrases=("Internal",),
        banned_topics=("股票",),
    )
    assert b.match("this is internal stuff") == "禁词「Internal」"
    assert b.match("今天聊聊股票") == "禁话题「股票」"
    assert b.match("今天天气不错") is None


def test_off_mode_never_matches():
    b = BehaviorSettings(restriction_mode="off", banned_phrases=("黑话",))
    assert b.match("黑话") is None


def test_framing_contains_intensity_and_restrictions():
    b = BehaviorSettings(
        chat_intensity="quiet",
        restriction_mode="strict",
        banned_phrases=("黑话",),
        banned_topics=("股票",),
    )
    framing = b.framing()
    assert "聊天强度：安静" in framing
    assert "禁止" in framing or "不要" in framing
    assert "黑话" in framing and "股票" in framing
    assert "整条回复会被丢弃" in framing

    active = BehaviorSettings(chat_intensity="active").framing()
    assert "聊天强度：活跃" in active
    assert "言论限制" not in active


def test_config_parses_behavior_section(tmp_path):
    text = BASE_YAML + """
behavior:
  chat_intensity: quiet
  restriction_mode: soft
  banned_phrases: [内部黑话]
  banned_topics: [股票]
"""
    settings = _load(tmp_path, text)
    assert settings.behavior.chat_intensity == "quiet"
    assert settings.behavior.restriction_mode == "soft"
    assert settings.behavior.banned_phrases == ("内部黑话",)


def test_config_rejects_bad_behavior(tmp_path):
    text = BASE_YAML + """
behavior:
  chat_intensity: loud
"""
    with pytest.raises(ConfigError, match="chat_intensity"):
        _load(tmp_path, text)


def test_overrides_apply_behavior_without_brain_mode(
    tmp_path, monkeypatch
):
    overrides = tmp_path / "settings.local.yaml"
    write_overrides(
        {
            "behavior": {
                "chat_intensity": "active",
                "restriction_mode": "strict",
                "banned_phrases": ["黑话"],
            }
        },
        overrides,
    )
    monkeypatch.setattr(config, "OVERRIDES_PATH", overrides)
    settings = _load(tmp_path)
    assert settings.behavior.chat_intensity == "active"
    assert settings.behavior.restriction_mode == "strict"
    assert settings.behavior.banned_phrases == ("黑话",)
