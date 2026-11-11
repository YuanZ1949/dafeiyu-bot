"""mirror 配置段：默认关闭、启用时的启动校验与能力矩阵。"""

import logging
from pathlib import Path

import pytest

import dafeiyu.config as config_module
from dafeiyu.config import (
    ConfigError,
    MirrorConfig,
    MirrorPairConfig,
    load_settings,
    parse_mirror_nicknames,
)

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

VALID_MIRROR = """\
mirror:
  enabled: true
  pairs:
    - qq: "qq:123456"
      wechat: "wxg:abc123"
"""


def _write(tmp_path: Path, extra: str) -> tuple[Path, Path]:
    env = tmp_path / "test.env"
    env.write_text("DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(BASE_YAML + extra, encoding="utf-8")
    return env, config


def _load(tmp_path: Path, extra: str):
    env, config = _write(tmp_path, extra)
    return load_settings(env_file=env, config_file=config)


def _gateway_yaml(
    mirror_block: str,
    *,
    provider: str = "both",
    wechat_enabled: bool = True,
    interval: int = 0,
    per_hour: int = 0,
    per_day: int = 0,
    quiet: str = '["00:00", "00:00"]',
) -> str:
    return (
        "gateway:\n"
        f"  provider: {provider}\n"
        "  wechat:\n"
        f"    enabled: {'true' if wechat_enabled else 'false'}\n"
        f"    send_min_interval_seconds: {interval}\n"
        f"    send_max_per_hour: {per_hour}\n"
        f"    send_max_per_day: {per_day}\n"
        f"    quiet_hours: {quiet}\n" + mirror_block
    )


def test_mirror_config_defaults_disabled() -> None:
    mirror = MirrorConfig()
    assert mirror.enabled is False
    assert mirror.pairs == ()


def test_missing_mirror_section_defaults_off(tmp_path: Path) -> None:
    settings = _load(tmp_path, "")
    assert settings.mirror.enabled is False
    assert settings.mirror.pairs == ()


def test_disabled_mirror_does_not_validate_pairs(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        "mirror:\n  enabled: false\n  pairs:\n    - qq: not-a-prefix\n",
    )
    assert settings.mirror.enabled is False
    assert settings.mirror.pairs == ()


def test_valid_pair_and_capability_matrix(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO)
    settings = _load(tmp_path, _gateway_yaml(VALID_MIRROR))

    assert settings.mirror.enabled is True
    assert len(settings.mirror.pairs) == 1
    pair = settings.mirror.pairs[0]
    assert pair.qq == "qq:123456"
    assert pair.wechat == "wxg:abc123"

    text = caplog.text
    assert "mirror capability qq->wechat" in text
    assert "mirror capability wechat->qq" in text
    assert "text=on" in text
    # 默认 uia + 未开本地库：媒体能力必须降级，不能因为配置合法就报 on。
    assert "image=off(outbound=uia)" in text
    assert "video=off(outbound=uia,video-as-file)" in text
    assert "image=off(localdb/media)" in text


def test_video_degrades_to_file_when_hook_outbound(tmp_path: Path, caplog) -> None:
    """hook 没有视频原语，但能发文件：能力矩阵要显示 video-as-file，而不是恒 off。"""
    caplog.set_level(logging.INFO)
    extra = (
        "gateway:\n"
        "  provider: both\n"
        "  wechat:\n"
        "    enabled: true\n"
        "    outbound: hook\n"
        "    send_min_interval_seconds: 0\n"
        "    send_max_per_hour: 0\n"
        "    send_max_per_day: 0\n"
        '    quiet_hours: ["00:00", "00:00"]\n' + VALID_MIRROR
    )
    settings = _load(tmp_path, extra)

    assert settings.mirror.enabled is True
    text = caplog.text
    assert "video=on(video-as-file)" in text
    assert "image=on" in text


def test_multiple_pairs_accepted(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO)
    settings = _load(
        tmp_path,
        _gateway_yaml(
            "mirror:\n  enabled: true\n  pairs:\n"
            '    - qq: "qq:123456"\n      wechat: "wxg:abc123"\n'
            '    - qq: "qq:654321"\n      wechat: "wxg:Def456"\n'
        ),
    )

    assert settings.mirror.enabled is True
    assert len(settings.mirror.pairs) == 2
    assert (settings.mirror.pairs[0].qq, settings.mirror.pairs[0].wechat) == (
        "qq:123456",
        "wxg:abc123",
    )
    assert (settings.mirror.pairs[1].qq, settings.mirror.pairs[1].wechat) == (
        "qq:654321",
        "wxg:def456",
    )

    text = caplog.text
    assert "mirror capability qq->wechat" in text
    assert "mirror capability wechat->qq" in text
    assert "text=on" in text


@pytest.mark.parametrize(
    ("mirror_block", "match"),
    [
        ("mirror:\n  enabled: true\n  pairs: []\n", "mirror.pairs"),
        (
            "mirror:\n  enabled: true\n  pairs:\n"
            '    - qq: "qq:1"\n      wechat: "wxg:a"\n'
            '    - qq: "qq:1"\n      wechat: "wxg:b"\n',
            "mirror.pairs",
        ),
        (
            "mirror:\n  enabled: true\n  pairs:\n"
            '    - qq: "qq:1"\n      wechat: "wxg:a"\n'
            '    - qq: "qq:2"\n      wechat: "wxg:a"\n',
            "mirror.pairs",
        ),
        (
            "mirror:\n  enabled: true\n  pairs:\n"
            '    - qq: "123456"\n      wechat: "wxg:a"\n',
            "qq:",
        ),
        (
            "mirror:\n  enabled: true\n  pairs:\n"
            '    - qq: "qq:123456"\n      wechat: "abc123"\n',
            "wxg:",
        ),
        (
            "mirror:\n  enabled: true\n  pairs:\n"
            '    - qq: "qq:123456"\n      wechat: "wxg:a"\n      extra: 1\n',
            "mirror.pairs",
        ),
    ],
)
def test_invalid_pairs(
    tmp_path: Path, mirror_block: str, match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        _load(tmp_path, _gateway_yaml(mirror_block))


def test_invalid_provider_must_be_both(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="gateway.provider"):
        _load(tmp_path, _gateway_yaml(VALID_MIRROR, provider="qq"))


def test_invalid_wechat_disabled_reports_wechat_first(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, _gateway_yaml(VALID_MIRROR, wechat_enabled=False))

    message = str(exc.value)
    assert "gateway.wechat.enabled" in message
    assert "gateway.provider" not in message


@pytest.mark.parametrize(
    ("interval", "per_hour", "per_day", "match"),
    [
        (20, 0, 0, "send_min_interval_seconds"),
        (0, 20, 0, "send_max_per_hour"),
        (0, 0, 60, "send_max_per_day"),
    ],
)
def test_invalid_still_rate_limited(
    tmp_path: Path,
    interval: int,
    per_hour: int,
    per_day: int,
    match: str,
) -> None:
    with pytest.raises(ConfigError, match=match):
        _load(
            tmp_path,
            _gateway_yaml(
                VALID_MIRROR,
                interval=interval,
                per_hour=per_hour,
                per_day=per_day,
            ),
        )


def test_invalid_still_quiet_hours(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="quiet_hours"):
        _load(
            tmp_path,
            _gateway_yaml(VALID_MIRROR, quiet='["23:30", "08:00"]'),
        )


# ------------------------------------------------------------ 昵称映射
def test_nicknames_parsed_and_normalized(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        _gateway_yaml(
            VALID_MIRROR
            + "  nicknames:\n"
            '    "QQ:2002": " 老鱼 "\n'
            '    "wxg:AbC": 小鱼\n'
        ),
    )
    assert settings.mirror.nicknames == (("qq:2002", "老鱼"), ("wxg:abc", "小鱼"))


def test_nicknames_absent_is_empty(tmp_path: Path) -> None:
    settings = _load(tmp_path, _gateway_yaml(VALID_MIRROR))
    assert settings.mirror.nicknames == ()


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (["qq:2002"], "必须是映射"),
        ({"2002": "老鱼"}, "qq: 或 wxg:"),
        ({"qq:": "老鱼"}, "缺少冒号"),
        ({"qq:2002": ""}, "不能为空"),
        ({"qq:2002": "   "}, "不能为空"),
        ({"qq:2002": "鱼" * 33}, "32"),
        ({"qq:2002": 5}, "必须是字符串"),
    ],
)
def test_invalid_nicknames(raw: object, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_mirror_nicknames(raw)


def test_duplicate_nickname_key_after_normalization() -> None:
    with pytest.raises(ConfigError, match="用户重复"):
        parse_mirror_nicknames({"QQ:2002": "老鱼", "qq:2002": "小鱼"})


# ------------------------------------------------------------ 关闭态保留
def test_disabled_mirror_keeps_valid_pairs_for_panel(tmp_path: Path) -> None:
    """关闭只是不转发；面板再打开时不该让用户从头重填。"""
    settings = _load(
        tmp_path,
        _gateway_yaml(
            "mirror:\n  enabled: false\n  pairs:\n"
            '    - qq: "qq:123456"\n      wechat: "wxg:abc123"\n'
            "  nicknames:\n"
            '    "qq:2002": 老鱼\n'
        ),
    )
    assert settings.mirror.enabled is False
    assert settings.mirror.pairs == (
        MirrorPairConfig(qq="qq:123456", wechat="wxg:abc123"),
    )
    assert settings.mirror.nicknames == (("qq:2002", "老鱼"),)


# ------------------------------------------------------------ 覆盖层
def test_override_layer_replaces_pairs_and_nicknames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = tmp_path / "settings.local.yaml"
    overrides.write_text(
        "mirror:\n"
        "  enabled: true\n"
        "  pairs:\n"
        '    - qq: "qq:777"\n'
        '      wechat: "wxg:override"\n'
        "  nicknames:\n"
        '    "qq:777": 老鱼\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", overrides)

    settings = _load(tmp_path, _gateway_yaml(VALID_MIRROR))

    assert settings.mirror.pairs == (
        MirrorPairConfig(qq="qq:777", wechat="wxg:override"),
    )
    assert settings.mirror.nicknames == (("qq:777", "老鱼"),)


def test_override_layer_can_enable_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """面板开启镜像只需写覆盖层：config.yaml 保持关闭，启动后照样生效。"""
    overrides = tmp_path / "settings.local.yaml"
    overrides.write_text(
        "mirror:\n"
        "  enabled: true\n"
        "  pairs:\n"
        '    - qq: "qq:777"\n'
        '      wechat: "wxg:override"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", overrides)

    settings = _load(tmp_path, _gateway_yaml("mirror:\n  enabled: false\n"))

    assert settings.mirror.enabled is True
    assert settings.mirror.pairs == (
        MirrorPairConfig(qq="qq:777", wechat="wxg:override"),
    )


def test_override_layer_enable_still_requires_wechat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = tmp_path / "settings.local.yaml"
    overrides.write_text(
        "mirror:\n  enabled: true\n  pairs:\n"
        '    - qq: "qq:777"\n      wechat: "wxg:override"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", overrides)

    with pytest.raises(ConfigError, match="gateway.wechat.enabled"):
        _load(
            tmp_path,
            _gateway_yaml("mirror:\n  enabled: false\n", wechat_enabled=False),
        )
