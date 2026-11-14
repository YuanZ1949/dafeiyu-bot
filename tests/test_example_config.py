"""config.example.yaml 模板回归：模板必须能被 load_settings() 加载，且微信默认关闭。"""

import dataclasses
from pathlib import Path

import pytest
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


# ------------------------------------ chat-driven-behaviour-fixes todo 0: 配置键脚手架
def test_example_config_link_defaults(tmp_path, monkeypatch):
    settings = _load_example(tmp_path, monkeypatch)

    assert settings.link.enabled is True
    assert settings.link.browser.mode == "minimized"
    assert settings.link.browser.enabled is True
    assert settings.link.timeout_seconds == 12.0
    assert settings.link.total_budget_seconds == 20.0
    assert settings.link.host_failure_ttl_seconds == 604800
    assert settings.link.cache_ttl_seconds == 86400
    assert settings.link.cache_max_entries == 500
    assert settings.link.cache_max_bytes == 67108864


def test_example_config_new_behaviour_keys(tmp_path, monkeypatch):
    settings = _load_example(tmp_path, monkeypatch)

    assert settings.mirror.bubble_enabled is True
    assert settings.mirror.bubble_corner_chars == "╭╮╰╯"
    assert settings.mirror.bubble_max_content_width == 30
    assert settings.mind.skip_known_silent is True
    assert settings.mind.skip_silent_cooldown_seconds == 90
    assert settings.injection.lessons_chars == 300


@pytest.mark.parametrize(
    "fragment",
    [
        "link:",
        "browser:",
        "mode: minimized",
        "binary:",
        "idle_close_seconds:",
        "max_targets:",
        "profile_dir:",
        "max_bytes:",
        "max_chars:",
        "cache_dir:",
        "cache_ttl_seconds:",
        "cache_max_entries:",
        "cache_max_bytes:",
        "user_agent:",
        "total_budget_seconds:",
        "host_failure_ttl_seconds:",
        "bubble_enabled:",
        "bubble_corner_chars:",
        "bubble_max_content_width:",
        "skip_known_silent:",
        "skip_silent_cooldown_seconds:",
        "lessons_chars:",
    ],
)
def test_example_config_contains_new_keys(fragment: str) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert fragment in text
