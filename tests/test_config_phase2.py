from pathlib import Path

import pytest

from dafeiyu.config import ConfigError, load_settings

BASE = """\
persona:
  name: 溟月
  aliases: [大肥鱼]
  max_reply_chars: 120
  max_reply_chars_technical: 300
  os_probability: 0.05
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
"""

ENV = "BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n"


def load(tmp_path: Path, extra_yaml: str = "", env: str = ENV):
    config = tmp_path / "config.yaml"
    config.write_text(BASE + extra_yaml, encoding="utf-8")
    env_file = tmp_path / "test.env"
    env_file.write_text(env, encoding="utf-8")
    return load_settings(env_file=env_file, config_file=config)


def test_local_provider_is_token_alias(tmp_path: Path) -> None:
    settings = load(tmp_path, "brain:\n  provider: local\n")
    assert settings.brain.provider == "token"


def test_local_model_provider_does_not_need_deepseek_key(tmp_path: Path) -> None:
    settings = load(
        tmp_path,
        "brain:\n  provider: local_model\n",
        env="BOT_QQ=10001\n",
    )
    assert settings.brain.provider == "local_model"


def test_local_model_defaults_point_at_lm_studio(tmp_path: Path) -> None:
    settings = load(tmp_path, "brain:\n  provider: local_model\n", env="BOT_QQ=10001\n")
    assert settings.local_model.base_url == "http://127.0.0.1:1234/v1"
    assert settings.local_model.model == ""
    assert settings.local_model.api_key == "local"


def test_local_model_section_is_parsed(tmp_path: Path) -> None:
    settings = load(
        tmp_path,
        "brain:\n  provider: local_model\n"
        "local_model:\n"
        "  base_url: http://127.0.0.1:5555/v1\n"
        "  model: qwen2.5-7b\n"
        "  api_key: whatever\n",
    )
    assert settings.local_model.base_url == "http://127.0.0.1:5555/v1"
    assert settings.local_model.model == "qwen2.5-7b"
    assert settings.local_model.api_key == "whatever"


def test_local_model_max_tokens_defaults_and_overrides(tmp_path: Path) -> None:
    default = load(tmp_path, "brain:\n  provider: local_model\n", env="BOT_QQ=10001\n")
    assert default.local_model.max_tokens == 2048
    assert default.local_model.timeout_seconds == 60.0

    custom = load(
        tmp_path,
        "brain:\n  provider: local_model\nlocal_model:\n"
        "  max_tokens: 4096\n  timeout_seconds: 120\n",
        env="BOT_QQ=10001\n",
    )
    assert custom.local_model.max_tokens == 4096
    assert custom.local_model.timeout_seconds == 120.0


def test_local_model_max_tokens_too_small_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="local_model.max_tokens"):
        load(
            tmp_path,
            "brain:\n  provider: local_model\nlocal_model:\n  max_tokens: 128\n",
            env="BOT_QQ=10001\n",
        )


def test_local_model_timeout_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="local_model.timeout_seconds"):
        load(
            tmp_path,
            "brain:\n  provider: local_model\nlocal_model:\n  timeout_seconds: 0\n",
            env="BOT_QQ=10001\n",
        )


def test_local_model_brain_uses_configured_max_tokens(tmp_path: Path) -> None:
    from dafeiyu.brain.factory import build_brain
    from dafeiyu.memory.store import Store

    settings = load(
        tmp_path,
        "brain:\n  provider: local_model\nlocal_model:\n  max_tokens: 3072\n",
        env="BOT_QQ=10001\n",
    )
    store = Store(tmp_path / "mem.db")
    try:
        brain = build_brain(settings, store, "local_model", model="qwen3-8b")
        assert brain.max_tokens == 3072
        assert brain.timeout == 60.0
        assert brain.model == "qwen3-8b"
    finally:
        store.close()


def test_local_model_reasoning_effort_defaults_and_overrides(tmp_path: Path) -> None:
    default = load(tmp_path, "brain:\n  provider: local_model\n", env="BOT_QQ=10001\n")
    assert default.local_model.reasoning_effort == ""

    custom = load(
        tmp_path,
        "brain:\n  provider: local_model\nlocal_model:\n  reasoning_effort: none\n",
        env="BOT_QQ=10001\n",
    )
    assert custom.local_model.reasoning_effort == "none"


def test_local_model_brain_passes_reasoning_effort(tmp_path: Path) -> None:
    from dafeiyu.brain.factory import build_brain
    from dafeiyu.memory.store import Store

    settings = load(
        tmp_path,
        "brain:\n  provider: local_model\nlocal_model:\n  reasoning_effort: none\n",
        env="BOT_QQ=10001\n",
    )
    store = Store(tmp_path / "mem.db")
    try:
        brain = build_brain(settings, store, "local_model", model="qwen3.5-9b")
        assert brain.reasoning_effort == "none"
    finally:
        store.close()


def test_token_brain_does_not_inherit_local_reasoning_effort(tmp_path: Path) -> None:
    from dafeiyu.brain.factory import build_brain
    from dafeiyu.memory.store import Store

    settings = load(
        tmp_path,
        "brain:\n  provider: token\nlocal_model:\n  reasoning_effort: none\n",
        env="BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n",
    )
    store = Store(tmp_path / "mem.db")
    try:
        brain = build_brain(settings, store, "token")
        assert brain.reasoning_effort is None
    finally:
        store.close()


def test_unknown_provider_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="local_model"):
        load(tmp_path, "brain:\n  provider: gpt\n")


def test_supervisor_defaults(tmp_path: Path) -> None:
    settings = load(tmp_path)
    sup = settings.supervisor
    assert sup.dashboard.port == 8787
    assert sup.control_port == 8788
    assert sup.control_host == "127.0.0.1"
    assert sup.dashboard.open_browser is True
    assert sup.restart.backoff_seconds == (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
    assert sup.restart.max_failures == 5


def test_supervisor_section_is_parsed(tmp_path: Path) -> None:
    settings = load(
        tmp_path,
        "supervisor:\n"
        "  dashboard:\n"
        "    port: 9100\n"
        "    open_browser: false\n"
        "  control:\n"
        "    port: 9101\n"
        "  restart:\n"
        "    backoff_seconds: [0.5, 1]\n"
        "    max_failures: 2\n",
    )
    sup = settings.supervisor
    assert sup.dashboard.port == 9100
    assert sup.dashboard.open_browser is False
    assert sup.control_port == 9101
    assert sup.restart.backoff_seconds == (0.5, 1.0)
    assert sup.restart.max_failures == 2


def test_control_port_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DAFEIYU_CONTROL_PORT", "9999")
    settings = load(tmp_path)
    assert settings.supervisor.control_port == 9999


def test_dashboard_and_control_ports_must_differ(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不能相同"):
        load(
            tmp_path,
            "supervisor:\n  dashboard:\n    port: 9000\n  control:\n    port: 9000\n",
        )


def test_supervisor_port_must_not_collide_with_opencode(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="opencode.port"):
        load(
            tmp_path,
            "brain:\n  provider: opencode\n"
            "opencode:\n"
            "  manage_server: false\n"
            "  server_url: http://127.0.0.1:24310\n"
            "  port: 24310\n"
            "supervisor:\n  dashboard:\n    port: 24310\n",
        )


def test_empty_backoff_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="非空列表"):
        load(tmp_path, "supervisor:\n  restart:\n    backoff_seconds: []\n")


def test_nonpositive_backoff_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="大于 0"):
        load(tmp_path, "supervisor:\n  restart:\n    backoff_seconds: [1, 0]\n")


def test_max_failures_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_failures"):
        load(tmp_path, "supervisor:\n  restart:\n    max_failures: 0\n")


def test_open_browser_must_be_bool(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="open_browser"):
        load(tmp_path, "supervisor:\n  dashboard:\n    open_browser: yes please\n")

def test_gateway_defaults_to_qq(tmp_path: Path) -> None:
    assert load(tmp_path).gateway.provider == "qq"


def test_gateway_wechat_without_enabled_degrades_to_qq(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="dafeiyu.config"):
        settings = load(tmp_path, "gateway:\n  provider: wechat\n")
    assert settings.gateway.provider == "qq"
    assert settings.wechat.enabled is False
    assert "降级为 qq" in caplog.text


def test_gateway_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="只能是 qq、wechat 或 both"):
        load(tmp_path, "gateway:\n  provider: telegram\n")


WECHAT_READY = """\
gateway:
  provider: both
  wechat:
    enabled: true
    account_nickname: 小鲸鱼
    group_allowlist: ["小鲸鱼测试群", "123456"]
"""


def test_wechat_block_defaults(tmp_path: Path) -> None:
    cfg = load(tmp_path).wechat
    assert cfg.enabled is False
    assert cfg.bridge_host == "127.0.0.1"
    assert cfg.bridge_port == 8790
    assert cfg.bridge_token == ""
    assert cfg.account_nickname == ""
    assert cfg.group_allowlist == frozenset()
    assert cfg.max_groups == 3
    assert cfg.poll_seconds == 8.0
    assert cfg.send_min_interval_seconds == 20.0
    assert cfg.send_max_per_hour == 20
    assert cfg.send_max_per_day == 60
    assert cfg.quiet_hours == ("23:30", "08:00")
    assert cfg.typing_delay_seconds == (0.6, 1.2)
    assert cfg.ack_requires_observed is True
    assert cfg.echo_window_seconds == 120.0
    assert cfg.keepalive is False
    assert cfg.require_foreground_idle is True
    assert cfg.kill_switch_file == "data/wechat.stop"
    assert cfg.version_min == "4.1.6"
    assert cfg.language == "zh-CN"
    assert cfg.allow_test_inject is False
    # 配置里没有会话别名：别名由桥本地铸造。
    assert not hasattr(cfg, "account_alias")


def test_wechat_ready_config_loads(tmp_path: Path) -> None:
    settings = load(tmp_path, WECHAT_READY)
    assert settings.gateway.provider == "both"
    assert settings.wechat.enabled is True
    assert settings.wechat.account_nickname == "小鲸鱼"
    assert settings.wechat.group_allowlist == frozenset(
        {"小鲸鱼测试群", "123456"}
    )


def test_wechat_allowlist_is_display_names_not_channel_ids(
    tmp_path: Path,
) -> None:
    """允许名单是 `open_chat` 的群显示名：不补 `qq:`、不 casefold。

    别名由桥首次轮询时随机铸造（`uuid4().hex[:12]`），无法预先写进配置；
    若这里按跨通道 id 解析，桥启动后拿 `qq:123456`/`wxg:abc` 当群名会永远
    打不开会话。
    """
    settings = load(
        tmp_path,
        "gateway:\n"
        "  wechat:\n"
        "    enabled: true\n"
        "    account_nickname: 小鲸鱼\n"
        '    group_allowlist: ["123456", "WeChat Group"]\n',
    )
    assert settings.wechat.group_allowlist == frozenset({"123456", "WeChat Group"})


def test_wechat_allowlist_rejects_empty_entries(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="group_allowlist"):
        load(
            tmp_path,
            "gateway:\n"
            "  wechat:\n"
            "    enabled: true\n"
            '    group_allowlist: ["  "]\n',
        )


def test_wechat_empty_allowlist_only_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="dafeiyu.config"):
        settings = load(
            tmp_path,
            "gateway:\n"
            "  provider: wechat\n"
            "  wechat:\n"
            "    enabled: true\n"
            "    account_nickname: 小鲸鱼\n",
        )
    assert settings.wechat.enabled is True
    assert settings.wechat.group_allowlist == frozenset()
    assert settings.gateway.provider == "wechat"
    assert "group_allowlist 为空" in caplog.text


def test_wechat_empty_nickname_only_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="dafeiyu.config"):
        settings = load(
            tmp_path,
            "gateway:\n"
            "  wechat:\n"
            "    enabled: true\n"
            '    group_allowlist: ["小鲸鱼测试群"]\n',
        )
    assert settings.wechat.enabled is True
    assert settings.wechat.account_nickname == ""
    assert "account_nickname 为空" in caplog.text


def test_wechat_token_never_in_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAFEIYU_WECHAT_BRIDGE_TOKEN", "sentinel-token-9f3a")
    settings = load(tmp_path, WECHAT_READY)
    assert settings.wechat.bridge_token == "sentinel-token-9f3a"
    assert "sentinel-token-9f3a" not in repr(settings)
    assert "sentinel-token-9f3a" not in repr(settings.wechat)
    assert "sentinel-token-9f3a" not in str(settings.wechat)


def test_group_ids_normalize_to_channel_prefixes(tmp_path: Path) -> None:
    settings = load(
        tmp_path,
        "groups:\n"
        '  whitelist: [123456, "qq:777", "WXG:AbC"]\n'
        "  blacklist: [42]\n"
        "  admins: [42]\n",
    )
    assert settings.groups.whitelist == frozenset({"qq:123456", "qq:777", "wxg:abc"})
    assert settings.groups.blacklist == frozenset({"qq:42"})
    assert settings.groups.admins == frozenset({"qq:42"})


def test_group_id_without_prefix_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="qq:/wxg: 前缀"):
        load(tmp_path, 'groups:\n  whitelist: ["not-an-id"]\n')


def test_example_config_defaults_to_free_opencode():
    text = Path("config.example.yaml").read_text(encoding="utf-8")
    assert "provider: opencode" in text
    assert "mode: free" in text
