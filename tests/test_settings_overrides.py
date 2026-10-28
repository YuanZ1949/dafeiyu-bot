"""覆盖层（data/settings.local.yaml）合入 load_settings 的契约。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from dafeiyu import config
from dafeiyu.brain.factory import current_provider
from dafeiyu.runtime import RuntimeState
from dafeiyu.settings_local import write_overrides

BASE_YAML = """\
persona:
  name: 溟月
  aliases: [大肥鱼, 小鲸鱼, 溟月, 鱼鱼, 肥鱼]
  max_reply_chars: 120
  max_reply_chars_technical: 300
  os_probability: 0.05
triggers:
  cooldown_seconds: 60
  sample_rate: 0.05
  quiet_hours: ["01:00", "07:00"]
  keywords: [怎么, 为什么, 为啥]
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

# brief 里的两个测试直接用模块级常量，不接收 fixture 参数，所以这里在 import
# 时把一份完整配置和一个最小 env 落到临时目录。
_FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="dafeiyu-task9-"))
CONFIG_FIXTURE = _FIXTURE_DIR / "config.yaml"
ENV_FIXTURE = _FIXTURE_DIR / "settings-test.env"
CONFIG_FIXTURE.write_text(BASE_YAML, encoding="utf-8")
ENV_FIXTURE.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")


def _override_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "settings.local.yaml"
    monkeypatch.setattr(config, "OVERRIDES_PATH", path)
    return path


def test_override_sets_mode(tmp_path, monkeypatch):
    write_overrides({"brain": {"mode": "key", "key": {"base_url": "https://api.x/v1", "model": "m"}}},
                    tmp_path / "settings.local.yaml")
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    s = config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)
    assert s.brain.mode == "key"
    assert s.brain.key_model == "m"


def test_invalid_mode_in_overrides_raises_configerror(tmp_path, monkeypatch):
    write_overrides({"brain": {"mode": "local"}}, tmp_path / "settings.local.yaml")
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    with pytest.raises(config.ConfigError, match="free"):
        config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)


def test_absent_overlay_keeps_todays_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "missing.yaml")

    s = config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)

    assert s.brain.mode == "free"
    assert s.brain.mode_from_overrides is False
    assert s.brain.key_base_url == ""
    assert s.brain.key_model == ""
    assert s.local_model.context == 0
    assert s.local_model.lean is False


def test_unknown_override_keys_are_ignored(tmp_path, monkeypatch):
    path = _override_path(tmp_path, monkeypatch)
    write_overrides(
        {
            "brain": {
                "provider": "opencode",
                "local_model": {"api_key": "not-accepted"},
                "unknown": {"x": 1},
            },
            "local_model": {"base_url": "http://ignored.example/v1"},
            "extra": {"anything": True},
        },
        path,
    )

    s = config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)

    # 覆盖层只认白名单键：provider 与顶层 local_model 都不参与合并。
    assert s.brain.provider == "token"
    assert s.brain.mode == "free"
    assert s.brain.mode_from_overrides is False
    assert s.local_model.base_url == "http://127.0.0.1:1234/v1"
    assert s.local_model.api_key == "local"


def test_overlay_applies_key_and_local_model(tmp_path, monkeypatch):
    path = _override_path(tmp_path, monkeypatch)
    write_overrides(
        {
            "brain": {
                "mode": "local_model",
                "key": {
                    "base_url": "https://api.example/v1",
                    "model": "deepseek-chat",
                },
                "local_model": {
                    "base_url": "http://127.0.0.1:9999/v1",
                    "model": "qwen3-8b",
                    "context": 32768,
                    "output": 8192,
                    "lean": True,
                },
            }
        },
        path,
    )

    s = config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)

    assert s.brain.mode == "local_model"
    assert s.brain.mode_from_overrides is True
    assert s.brain.key_base_url == "https://api.example/v1"
    assert s.brain.key_model == "deepseek-chat"
    assert s.local_model.base_url == "http://127.0.0.1:9999/v1"
    assert s.local_model.model == "qwen3-8b"
    assert s.local_model.context == 32768
    assert s.local_model.lean is True
    # output 只是覆盖层字段，不是 LocalModelConfig 的字段。
    assert not hasattr(s.local_model, "output")


def test_mode_from_overrides_beats_runtime_provider(tmp_path, monkeypatch):
    path = _override_path(tmp_path, monkeypatch)
    write_overrides(
        {"brain": {"mode": "key", "key": {"base_url": "https://api.x/v1", "model": "m"}}},
        path,
    )
    s = config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)

    assert current_provider(s, RuntimeState(provider="opencode")) == "token"


def test_runtime_provider_still_wins_without_mode_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "missing.yaml")
    s = config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)

    assert current_provider(s, RuntimeState(provider="opencode")) == "opencode"


def test_malformed_overlay_raises_configerror(tmp_path, monkeypatch):
    path = _override_path(tmp_path, monkeypatch)
    path.write_text("brain: [unclosed\n", encoding="utf-8")

    with pytest.raises(config.ConfigError):
        config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)


@pytest.mark.parametrize(
    "overrides",
    [
        {"brain": {"mode": None}},
        {"brain": {"key": {"model": 123}}},
        {"brain": {"key": {"base_url": None}}},
        {"brain": {"local_model": {"model": 7}}},
        {"brain": {"local_model": {"context": True}}},
        {"brain": {"local_model": {"context": -1}}},
        {"brain": {"local_model": {"output": "8192"}}},
        {"brain": {"local_model": {"lean": "yes"}}},
    ],
)
def test_invalid_override_types_raise_configerror(tmp_path, monkeypatch, overrides):
    path = _override_path(tmp_path, monkeypatch)
    write_overrides(overrides, path)

    with pytest.raises(config.ConfigError):
        config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)
