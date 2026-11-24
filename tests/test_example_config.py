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


def test_example_config_images_file_download_defaults(tmp_path, monkeypatch):
    """QQ 文件下载默认开启，上限 50 MB（模板里两键与代码默认一致）。"""
    settings = _load_example(tmp_path, monkeypatch)

    assert settings.images.file_download is True
    assert settings.images.file_max_bytes == 50 * 1024 * 1024


def test_images_file_max_bytes_explicit_value_honored(tmp_path, monkeypatch):
    """显式配置 images.file_max_bytes 必须生效（非负校验走 _positive_int）。"""
    monkeypatch.chdir(tmp_path)
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["images"]["file_max_bytes"] = 1024
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_QQ=10001\n", encoding="utf-8")

    settings = load_settings(env_file=env_file, config_file=config)

    assert settings.images.file_max_bytes == 1024
    assert settings.images.file_download is True


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
    assert settings.mirror.bubble_max_content_width == 24
    assert settings.mirror.keep_image_quality is True
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
        "keep_image_quality:",
        "skip_known_silent:",
        "skip_silent_cooldown_seconds:",
        "lessons_chars:",
    ],
)
def test_example_config_contains_new_keys(fragment: str) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert fragment in text


# ------------------------------------ hotchat-lifetime-panel-redesign todo 23: 文档 / 模板 / 代码三方口径
README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_persona_hold_max_seconds_matches_code() -> None:
    """README 写的 persona 字段必须在 config.py 里真实存在（防文档漂移）。

    既有守卫只覆盖 ``gateway.wechat`` 的字段集合，persona 段没有同类断言，
    所以这里单独钉一次「文档里出现的键 = 代码里的字段」。
    """
    from dafeiyu.config import PersonaConfig

    text = README.read_text(encoding="utf-8")
    assert "`persona.hold_max_seconds`" in text
    assert "`persona.opportunity_guard`" in text

    persona_fields = {field.name for field in dataclasses.fields(PersonaConfig)}
    assert "hold_max_seconds" in persona_fields


def test_example_config_persona_hold_max_seconds(tmp_path, monkeypatch):
    """模板里必须有 persona.hold_max_seconds，且 persona 解析能读出这个键。"""
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert raw["persona"]["hold_max_seconds"] == 300

    settings = _load_example(tmp_path, monkeypatch)
    assert settings.persona.hold_max_seconds == 300
