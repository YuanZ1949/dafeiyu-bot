from pathlib import Path

import pytest

from dafeiyu.config import ConfigError, load_settings

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
"""


def write_env(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.env"
    p.write_text(content, encoding="utf-8")
    return p


def write_yaml(tmp_path: Path, content: str = BASE_YAML) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_missing_api_key_raises(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path))


def test_missing_bot_qq_raises(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\n")
    with pytest.raises(ConfigError, match="BOT_QQ"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path))


def test_env_overrides_yaml_and_applies_defaults(tmp_path: Path) -> None:
    env = write_env(
        tmp_path,
        "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\nDEEPSEEK_MODEL=deepseek-reasoner\n"
        "DEEPSEEK_REASONING_EFFORT=none\n",
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert s.model == "deepseek-reasoner"
    assert s.reasoning_effort == "none"
    assert s.bot_qq == 10001
    assert s.base_url == "https://api.deepseek.com"
    assert s.db_path == "data/dafeiyu.db"
    assert s.budget.daily_calls == 200
    assert s.memory.window_size == 20
    assert s.persona.max_reply_chars == 120


def test_persona_prompt_file_defaults_empty_and_reads_yaml(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    default = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert default.persona.prompt_file == ""
    assert default.reasoning_effort == ""

    custom = BASE_YAML.replace(
        "  os_probability: 0.05", '  os_probability: 0.05\n  prompt_file: "my_persona.md"'
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, custom))
    assert s.persona.prompt_file == "my_persona.md"


def test_invalid_sample_rate_raises(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    bad = BASE_YAML.replace("sample_rate: 0.05", "sample_rate: 1.5")
    with pytest.raises(ConfigError, match="sample_rate"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, bad))


def test_invalid_quiet_hours_raises(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    bad = BASE_YAML.replace('quiet_hours: ["01:00", "07:00"]', 'quiet_hours: ["01:00"]')
    with pytest.raises(ConfigError, match="quiet_hours"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, bad))


def test_error_message_does_not_leak_key(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-super-secret\nBOT_QQ=10001\n")
    bad = BASE_YAML.replace("sample_rate: 0.05", "sample_rate: -1")
    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=env, config_file=write_yaml(tmp_path, bad))
    assert "sk-super-secret" not in str(exc.value)


def test_opencode_provider_does_not_require_deepseek_key(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:", "brain:\n  provider: opencode\ntriggers:"
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert s.brain.provider == "opencode"
    assert s.api_key == ""
    assert s.opencode.agent == "deepseek-qq"
    assert s.opencode.bin == ""
    assert s.opencode.project_dir == ""
    assert s.opencode.timeout_seconds == 90.0
    assert s.opencode.manage_server is False
    assert s.opencode.server_url == ""
    assert s.opencode.db_path == "data/opencode-bot.db"
    assert s.opencode.host == "127.0.0.1"
    assert s.opencode.port == 24310
    assert s.opencode.startup_timeout_seconds == 30.0


def test_opencode_section_is_parsed(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  bin: C:/tools/opencode.exe\n  agent: fish\n"
        "  project_dir: C:/bot\n  timeout_seconds: 12.5\n"
        "  manage_server: true\n  server_url: \"\"\n"
        "  db_path: data/test-opencode.db\n  host: 127.0.0.1\n  port: 4310\n"
        "  startup_timeout_seconds: 5\n"
        "triggers:",
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert s.opencode.bin == "C:/tools/opencode.exe"
    assert s.opencode.agent == "fish"
    assert s.opencode.project_dir == "C:/bot"
    assert s.opencode.timeout_seconds == 12.5
    assert s.opencode.manage_server is True
    assert s.opencode.server_url == ""
    assert s.opencode.db_path == "data/test-opencode.db"
    assert s.opencode.host == "127.0.0.1"
    assert s.opencode.port == 4310
    assert s.opencode.startup_timeout_seconds == 5.0


def test_invalid_brain_provider_raises(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    bad = BASE_YAML.replace(
        "triggers:", "brain:\n  provider: magic\ntriggers:"
    )
    with pytest.raises(ConfigError, match="brain.provider"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, bad))


def test_opencode_timeout_must_be_positive(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    bad = BASE_YAML.replace(
        "triggers:", "opencode:\n  timeout_seconds: 0\ntriggers:"
    )
    with pytest.raises(ConfigError, match="timeout_seconds"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, bad))


def test_opencode_manage_server_rejects_server_url(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  manage_server: true\n"
        "  server_url: http://127.0.0.1:4310\n"
        "triggers:",
    )
    with pytest.raises(ConfigError, match="server_url"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_opencode_port_must_be_valid(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\nopencode:\n  port: 0\ntriggers:",
    )
    with pytest.raises(ConfigError, match="opencode.port"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_opencode_startup_timeout_must_be_positive(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  startup_timeout_seconds: 0\ntriggers:",
    )
    with pytest.raises(ConfigError, match="startup_timeout_seconds"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
