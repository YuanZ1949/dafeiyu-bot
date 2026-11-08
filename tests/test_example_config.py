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


def test_example_config_images_max_edge(tmp_path, monkeypatch):
    settings = _load_example(tmp_path, monkeypatch)

    assert settings.images.max_edge == 1280


def test_example_config_media_and_observer_default_off(tmp_path, monkeypatch):
    """新增的媒体/观察者开关在模板里必须显式关闭（软回滚前提）。"""
    settings = _load_example(tmp_path, monkeypatch)

    assert settings.wechat.media.enabled is False
    assert settings.wechat.media.max_bytes == 10 * 1024 * 1024
    assert settings.wechat.media.storage_root == ""
    assert settings.mind.observer_enabled is False


def test_example_config_media_block_optional(tmp_path, monkeypatch):
    """删掉 gateway.wechat.media 段后仍按代码默认值加载（老配置不炸）。"""
    monkeypatch.chdir(tmp_path)
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["gateway"]["wechat"].pop("media")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_QQ=10001\n", encoding="utf-8")

    settings = load_settings(env_file=env_file, config_file=config)

    assert settings.wechat.media.enabled is False
    assert settings.wechat.media.max_bytes == 10 * 1024 * 1024
