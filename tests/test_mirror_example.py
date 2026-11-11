"""config.example.yaml 的 mirror 段回归：示例默认关闭、形状不漂移、群重复报错。"""

import dataclasses
import re
from pathlib import Path

import pytest
import yaml

from dafeiyu.config import ConfigError, WeChatConfig, load_settings

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


def _load_example(tmp_path: Path, monkeypatch):
    # 与 test_example_config.py 同款：chdir 到空目录，覆盖层不会从真实仓库泄漏进来。
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_QQ=10001\n", encoding="utf-8")
    return load_settings(env_file=env_file, config_file=tmp_path / "config.yaml")


def test_mirror_example_default_disabled(tmp_path, monkeypatch):
    """示例镜像默认关闭；关闭不校验但保留可解析的配对，面板可一键重新开启。"""
    settings = _load_example(tmp_path, monkeypatch)

    assert settings.mirror.enabled is False
    assert len(settings.mirror.pairs) == 1
    assert settings.mirror.pairs[0].qq.startswith("qq:")


def test_example_wechat_keys_match_dataclass():
    """gateway.wechat 段的键集合必须与 WeChatConfig 字段逐键一致。"""
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    wechat_raw = raw["gateway"]["wechat"]

    field_names = {f.name for f in dataclasses.fields(WeChatConfig)}
    assert set(wechat_raw) == field_names


def test_example_mirror_block_present_and_disabled():
    """mirror 必须是顶格书写的顶层段，且 enabled 显式为 false。"""
    text = EXAMPLE.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)

    # 顶层键必须顶格（Select-String '^mirror:' 能命中），不能缩进进别的段。
    assert re.search(r"^mirror:", text, re.MULTILINE)
    assert raw["mirror"]["enabled"] is False


def test_mirror_example_duplicate_group_config_error(tmp_path, monkeypatch):
    """开启镜像且同一个群出现在两对里：load_settings 必须直接 ConfigError。

    内联构造 YAML（不手改示例文件）；除 pairs 的群重复外其余前置条件全部
    满足，保证报错来自「群不能重复」这条校验本身。
    """
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        """\
gateway:
  provider: both
  wechat:
    enabled: true
    account_nickname: 小号
    # 已去节流：三项全 0，静默起止相同。
    send_min_interval_seconds: 0.0
    send_max_per_hour: 0
    send_max_per_day: 0
    quiet_hours: ["00:00", "00:00"]
triggers: {}
mirror:
  enabled: true
  pairs:
    - qq: "qq:10001"
      wechat: "wxg:alias_a"
    - qq: "qq:10002"
      wechat: "wxg:alias_a"
""",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_QQ=10001\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="mirror.pairs 中群重复"):
        load_settings(env_file=env_file, config_file=config)
