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


def test_recall_defaults_off_and_parses_switch(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    default = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert default.injection.recall_enabled is False
    assert default.injection.recall_topk == 3
    assert default.injection.recall_chars == 300

    custom = BASE_YAML.replace(
        "  extract_every: 20",
        "  extract_every: 20\n  recall_enabled: true\n  recall_topk: 5\n  recall_chars: 120",
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, custom))
    assert s.injection.recall_enabled is True
    assert s.injection.recall_topk == 5
    assert s.injection.recall_chars == 120


def test_recall_rejects_non_positive_numbers(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    bad = BASE_YAML.replace(
        "  extract_every: 20", "  extract_every: 20\n  recall_topk: 0"
    )
    with pytest.raises(ConfigError, match="recall_topk"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, bad))


def test_segment_delay_defaults_and_parses(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    default = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert default.persona.segment_delay_seconds == 0.8

    custom = BASE_YAML.replace(
        "  os_probability: 0.05",
        "  os_probability: 0.05\n  segment_delay_seconds: 1.5",
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, custom))
    assert s.persona.segment_delay_seconds == 1.5


def test_segment_delay_rejects_negative(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    bad = BASE_YAML.replace(
        "  os_probability: 0.05",
        "  os_probability: 0.05\n  segment_delay_seconds: -1",
    )
    with pytest.raises(ConfigError, match="segment_delay_seconds"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, bad))


def test_thread_state_defaults_and_parses(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    default = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert default.triggers.ambient_max_turns == 2
    assert default.triggers.thread_idle_seconds == 300
    assert default.triggers.thread_closed_seconds == 600

    custom = BASE_YAML.replace(
        "sample_rate: 0.05",
        "sample_rate: 0.05\n"
        "  ambient_max_turns: 3\n"
        "  thread_idle_seconds: 120\n"
        "  thread_closed_seconds: 900",
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, custom))
    assert s.triggers.ambient_max_turns == 3
    assert s.triggers.thread_idle_seconds == 120
    assert s.triggers.thread_closed_seconds == 900


def test_thread_state_rejects_negative(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    bad = BASE_YAML.replace(
        "sample_rate: 0.05",
        "sample_rate: 0.05\n  thread_idle_seconds: -1",
    )
    with pytest.raises(ConfigError, match="thread_idle_seconds"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, bad))


def test_closed_sample_rate_defaults_and_parses(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    default = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert default.triggers.closed_sample_rate == 0.05

    custom = BASE_YAML.replace(
        "sample_rate: 0.05",
        "sample_rate: 0.05\n  closed_sample_rate: 0.2",
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, custom))
    assert s.triggers.closed_sample_rate == 0.2

    silent = BASE_YAML.replace(
        "sample_rate: 0.05",
        "sample_rate: 0.05\n  closed_sample_rate: 0",
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, silent))
    assert s.triggers.closed_sample_rate == 0


def test_closed_sample_rate_rejects_out_of_range(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    for value in ("1.5", "-0.1"):
        bad = BASE_YAML.replace(
            "sample_rate: 0.05",
            f"sample_rate: 0.05\n  closed_sample_rate: {value}",
        )
        with pytest.raises(ConfigError, match="closed_sample_rate"):
            load_settings(env_file=env, config_file=write_yaml(tmp_path, bad))


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
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  manage_server: true\ntriggers:",
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert s.brain.provider == "opencode"
    assert s.api_key == ""
    assert s.opencode.agent == "deepseek-qq"
    assert s.opencode.bin == ""
    assert s.opencode.project_dir == ""
    assert s.opencode.timeout_seconds == 90.0
    assert s.opencode.manage_server is True
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


def test_opencode_requires_server_url_when_not_managing(tmp_path: Path) -> None:
    """manage_server=false 且不写 server_url 会静默回落到个人 OpenCode 服务，
    机器人会话写进个人库 —— 必须在启动前拦住。"""
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  manage_server: false\n  server_url: \"\"\n"
        "triggers:",
    )
    with pytest.raises(ConfigError, match="server_url"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_opencode_accepts_explicit_server_url_when_not_managing(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  manage_server: false\n"
        "  server_url: http://127.0.0.1:25000\n"
        "triggers:",
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert s.opencode.manage_server is False
    assert s.opencode.server_url == "http://127.0.0.1:25000"


def test_opencode_port_must_be_valid(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  manage_server: true\n  port: 0\ntriggers:",
    )
    with pytest.raises(ConfigError, match="opencode.port"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_opencode_startup_timeout_must_be_positive(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  manage_server: true\n"
        "  startup_timeout_seconds: 0\ntriggers:",
    )
    with pytest.raises(ConfigError, match="startup_timeout_seconds"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


# ---------------------------------------------------------- todo 11: wechat media
def test_wechat_media_defaults_off(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path))

    assert settings.wechat.media.enabled is False
    assert settings.wechat.media.max_bytes == 10 * 1024 * 1024
    assert settings.wechat.media.storage_root == ""


def test_wechat_media_section_parsed(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + (
        "gateway:\n  wechat:\n    media:\n      enabled: true\n"
        "      max_bytes: 2048\n      storage_root: D:/wx\n"
    )
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))

    assert settings.wechat.media.enabled is True
    assert settings.wechat.media.max_bytes == 2048
    assert settings.wechat.media.storage_root == "D:/wx"


def test_wechat_media_non_mapping_rejected(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + "gateway:\n  wechat:\n    media: []\n"

    with pytest.raises(ConfigError, match="gateway.wechat.media"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_wechat_media_max_bytes_must_be_positive(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + "gateway:\n  wechat:\n    media:\n      max_bytes: 0\n"

    with pytest.raises(ConfigError, match="gateway.wechat.media.max_bytes"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


# ---------------------------------------------- 启动/面板：机器人微信账号（wxid）
def test_wechat_account_defaults_unbound(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path))

    # 空 = 不绑定：只在登录了一个微信时由 helper 自动识别。
    assert settings.wechat.account == ""


def test_wechat_account_is_trimmed(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + 'gateway:\n  wechat:\n    account: "  wxid_516rht3txzox22  "\n'
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))

    assert settings.wechat.account == "wxid_516rht3txzox22"


@pytest.mark.parametrize(
    "account",
    ["filehelper", "gh_abcdef", "12345@chatroom", "wxid 123", "wxid_abc/def"],
)
def test_wechat_account_rejects_non_personal_ids(
    tmp_path: Path, account: str
) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + f'gateway:\n  wechat:\n    account: "{account}"\n'

    with pytest.raises(ConfigError, match="gateway.wechat.account"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_wechat_account_rejects_non_string(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + "gateway:\n  wechat:\n    account: 12345\n"

    with pytest.raises(ConfigError, match="gateway.wechat.account 必须是字符串"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))



def test_wechat_quiet_hours_default_independent_of_triggers(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert settings.triggers.quiet_hours == ("01:00", "07:00")
    # 发送端默认起止相同 = 不静默；triggers.quiet_hours 只控制「要不要回复」，
    # 不再作为发送端默认值。
    assert settings.wechat.quiet_hours == ("00:00", "00:00")


def test_wechat_quiet_hours_explicit_wins(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + 'gateway:\n  wechat:\n    quiet_hours: ["22:00", "06:00"]\n'
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert settings.wechat.quiet_hours == ("22:00", "06:00")
    assert settings.triggers.quiet_hours == ("01:00", "07:00")


# ------------------------------------------ P1-5: 私聊白名单 / 黑名单（config 侧）
def test_parse_wechat_peer_ids_normalizes_to_casefolded_wxg() -> None:
    from dafeiyu.config import parse_wechat_peer_ids

    assert parse_wechat_peer_ids(None, "f") == frozenset()
    assert parse_wechat_peer_ids([], "f") == frozenset()
    assert parse_wechat_peer_ids(["wxg:a1", "WXG:A1"], "f") == frozenset({"wxg:a1"})


def test_wechat_private_allow_deny_defaults_empty(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path))

    assert settings.wechat.private_allow == frozenset()
    assert settings.wechat.private_deny == frozenset()


def test_wechat_private_allow_deny_parsed(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + (
        "gateway:\n"
        "  wechat:\n"
        "    private_allow: [wxg:AbC, WXG:def, wxg:abc]\n"
        '    private_deny: ["wxg:spam"]\n'
    )
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))

    assert settings.wechat.private_allow == frozenset({"wxg:abc", "wxg:def"})
    assert settings.wechat.private_deny == frozenset({"wxg:spam"})


@pytest.mark.parametrize(
    ("section", "match"),
    [
        ("    private_allow: 123\n", "private_allow"),
        ("    private_allow: [123]\n", "private_allow"),
        ("    private_allow: [\"123\"]\n", "wxg: 前缀"),
        ("    private_allow: [\"qq:123\"]\n", "wxg: 前缀"),
        ("    private_allow: [\"wxg:\"]\n", "缺少冒号后的"),
        ("    private_deny: [\"wx:123\"]\n", "private_deny"),
    ],
)
def test_wechat_private_lists_reject_bad_values(
    tmp_path: Path, section: str, match: str
) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + "gateway:\n  wechat:\n" + section

    with pytest.raises(ConfigError, match=match):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_persona_hard_limits_default_and_parse(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    default = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert default.persona.reply_hard_total == 2000
    assert default.persona.reply_hard_part == 500

    yaml = BASE_YAML.replace(
        "  os_probability: 0.05\n",
        "  reply_hard_total: 1600\n  reply_hard_part: 400\n  os_probability: 0.05\n",
    )
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert settings.persona.reply_hard_total == 1600
    assert settings.persona.reply_hard_part == 400


@pytest.mark.parametrize(
    "extra",
    [
        "  reply_hard_total: 100\n  reply_hard_part: 500\n",
        "  reply_hard_total: 2000\n  reply_hard_part: 0\n",
    ],
)
def test_persona_hard_limits_reject_bad_values(tmp_path: Path, extra: str) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML.replace("  os_probability: 0.05\n", extra + "  os_probability: 0.05\n")
    with pytest.raises(ConfigError, match="reply_hard"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_wechat_self_nicknames_default_empty_and_dedup(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    default = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert default.wechat.self_nicknames == ()

    yaml = BASE_YAML + (
        "gateway:\n"
        "  wechat:\n"
        "    self_nicknames: [小菜鱼, 小菜鱼, '  ']\n"
    )
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert settings.wechat.self_nicknames == ("小菜鱼",)


def test_wechat_self_nicknames_rejects_non_list(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + "gateway:\n  wechat:\n    self_nicknames: 小菜鱼\n"
    with pytest.raises(ConfigError, match="self_nicknames"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


# ------------------------------------ chat-driven-behaviour-fixes todo 0: 配置键脚手架
def test_link_defaults_without_section(tmp_path: Path) -> None:
    """完全不带 ``link:`` 段的配置：全默认值，老配置无感。"""
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert s.link.enabled is True
    assert s.link.timeout_seconds == 12.0
    assert s.link.max_bytes == 2_000_000
    assert s.link.max_chars == 6000
    assert s.link.cache_dir == "data/link-cache"
    assert s.link.cache_ttl_seconds == 86400
    assert s.link.cache_max_entries == 500
    assert s.link.cache_max_bytes == 67108864
    assert s.link.user_agent == ""
    assert s.link.total_budget_seconds == 20.0
    assert s.link.host_failure_ttl_seconds == 604800
    assert s.link.browser.enabled is True
    assert s.link.browser.binary == ""
    assert s.link.browser.mode == "minimized"
    assert s.link.browser.timeout_seconds == 30.0
    assert s.link.browser.idle_close_seconds == 300
    assert s.link.browser.max_targets == 2
    assert s.link.browser.profile_dir == "data/link-browser-profile"


def test_link_section_is_parsed(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + (
        "link:\n"
        "  enabled: true\n"
        "  timeout_seconds: 5.5\n"
        "  max_bytes: 1000\n"
        "  max_chars: 800\n"
        "  cache_dir: data/other-cache\n"
        "  cache_ttl_seconds: 60\n"
        "  cache_max_entries: 10\n"
        "  cache_max_bytes: 2048\n"
        "  user_agent: Mozilla/5.0 test\n"
        "  total_budget_seconds: 9.5\n"
        "  host_failure_ttl_seconds: 3600\n"
        "  browser:\n"
        '    mode: "minimized "\n'  # 带空白：归一化后必须通过
        "    binary: C:/msedge.exe\n"
        "    timeout_seconds: 15\n"
        "    idle_close_seconds: 60\n"
        "    max_targets: 3\n"
        "    profile_dir: data/other-profile\n"
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert s.link.timeout_seconds == 5.5
    assert s.link.max_bytes == 1000
    assert s.link.max_chars == 800
    assert s.link.cache_dir == "data/other-cache"
    assert s.link.cache_ttl_seconds == 60
    assert s.link.cache_max_entries == 10
    assert s.link.cache_max_bytes == 2048
    assert s.link.user_agent == "Mozilla/5.0 test"
    assert s.link.total_budget_seconds == 9.5
    assert s.link.host_failure_ttl_seconds == 3600
    assert s.link.browser.mode == "minimized"
    assert s.link.browser.binary == "C:/msedge.exe"
    assert s.link.browser.timeout_seconds == 15.0
    assert s.link.browser.idle_close_seconds == 60
    assert s.link.browser.max_targets == 3
    assert s.link.browser.profile_dir == "data/other-profile"


@pytest.mark.parametrize("mode", ["headless", "HEADLESS", "auto"])
def test_link_browser_mode_rejects_everything_but_minimized(
    tmp_path: Path, mode: str
) -> None:
    """只接受 minimized：其它值显式报错，绝不静默降级为无头。"""
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + f'link:\n  browser:\n    mode: "{mode}"\n'
    with pytest.raises(ConfigError, match="minimized"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_link_missing_section_and_browser_defaults(tmp_path: Path) -> None:
    """link 段在但 browser 子段缺省：browser 全默认。"""
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + "link:\n  enabled: false\n"
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert s.link.enabled is False
    assert s.link.browser.mode == "minimized"


def test_mirror_bubble_mind_skip_lessons_defaults(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path))
    assert s.mirror.bubble_enabled is True
    assert s.mirror.bubble_corner_chars == "╭╮╰╯"
    assert s.mirror.bubble_max_content_width == 30
    assert s.mind.skip_known_silent is True
    assert s.mind.skip_silent_cooldown_seconds == 90
    assert s.injection.lessons_chars == 300


def test_mirror_bubble_mind_skip_lessons_overrides(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "  extract_every: 20",
        "  extract_every: 20\n  lessons_chars: 250",
    ) + (
        "mirror:\n"
        "  bubble_enabled: false\n"
        '  bubble_corner_chars: "┌┐└┘"\n'
        "  bubble_max_content_width: 40\n"
        "mind:\n"
        "  skip_known_silent: false\n"
        "  skip_silent_cooldown_seconds: 45\n"
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert s.mirror.bubble_enabled is False
    assert s.mirror.bubble_corner_chars == "┌┐└┘"
    assert s.mirror.bubble_max_content_width == 40
    assert s.mind.skip_known_silent is False
    assert s.mind.skip_silent_cooldown_seconds == 45
    assert s.injection.lessons_chars == 250


def test_mirror_bubble_keys_parsed_even_when_mirror_disabled(tmp_path: Path) -> None:
    """镜像整体关闭的早退分支也必须解析 bubble_*，否则设了也读不出来。"""
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    yaml = BASE_YAML + (
        "mirror:\n"
        "  enabled: false\n"
        "  bubble_enabled: false\n"
        "  bubble_max_content_width: 45\n"
    )
    s = load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
    assert s.mirror.enabled is False
    assert s.mirror.bubble_enabled is False
    assert s.mirror.bubble_max_content_width == 45
