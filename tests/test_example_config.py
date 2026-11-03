"""config.example.yaml 模板回归：模板必须能被 load_settings() 加载，且微信默认关闭。"""

import dataclasses
from pathlib import Path

import yaml

from dafeiyu.config import WeChatConfig, load_settings

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


def _load_example(tmp_path: Path, monkeypatch):
    # chdir 到空目录：data/settings.local.yaml / runtime.json 覆盖层不会从真实仓库泄漏进来。
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_QQ=10001\n", encoding="utf-8")
    return load_settings(env_file=env_file, config_file=tmp_path / "config.yaml")


def test_example_config_loads_and_wechat_disabled(tmp_path, monkeypatch):
    settings = _load_example(tmp_path, monkeypatch)

    assert settings.gateway.provider == "qq"
    assert settings.wechat.enabled is False
    assert settings.wechat.bridge_port == 8790
    assert settings.wechat.version_min == "4.1.6"
    assert settings.wechat.language == "zh-CN"
    assert settings.wechat.kill_switch_file == "data/wechat.stop"


def test_example_config_wechat_block_matches_defaults():
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    wechat_raw = raw["gateway"]["wechat"]

    field_names = {f.name for f in dataclasses.fields(WeChatConfig)}
    assert set(wechat_raw) == field_names
    assert wechat_raw["enabled"] is False
    assert wechat_raw["bridge_token"] == ""
