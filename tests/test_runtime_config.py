"""Task 13.1：bot 专用 opencode 配置目录（隔离全局插件 / MCP / 外来 agent）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dafeiyu.brain.opencode_server import OpenCodeServer
from dafeiyu.brain.runtime_config import (
    DEFAULT_OPENCODE_MODEL,
    ensure_isolated_config,
    server_env,
)
from dafeiyu.config import ConfigError
from test_config_agents import BASE, load, patch


# ---- 配置目录本体 ---------------------------------------------------------


def test_ensure_creates_minimal_config(tmp_path: Path) -> None:
    home = tmp_path / "runtime" / "config"

    root = ensure_isolated_config(home)

    assert root == home.resolve()
    cfg = home / "opencode" / "opencode.jsonc"
    assert cfg.is_file()
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["model"] == DEFAULT_OPENCODE_MODEL
    # 显式空列表：隔离目录里不该再加载任何插件
    assert data["plugins"] == []


def test_ensure_is_idempotent_and_keeps_hand_edits(tmp_path: Path) -> None:
    home = tmp_path / "config"
    ensure_isolated_config(home)
    cfg = home / "opencode" / "opencode.jsonc"
    cfg.write_text('{"model": "custom/model"}', encoding="utf-8")

    ensure_isolated_config(home)

    assert cfg.read_text(encoding="utf-8") == '{"model": "custom/model"}'


def test_ensure_resolves_relative_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    root = ensure_isolated_config("data/opencode-runtime/config")

    assert root == (tmp_path / "data" / "opencode-runtime" / "config").resolve()
    assert (root / "opencode" / "opencode.jsonc").is_file()


# ---- 环境变量 -------------------------------------------------------------


def test_server_env_empty_means_inherit() -> None:
    assert server_env("") is None
    assert server_env("   ") is None


def test_server_env_points_xdg_config_home(tmp_path: Path) -> None:
    env = server_env(tmp_path / "cfg")

    root = (tmp_path / "cfg").resolve()
    assert env == {
        "XDG_CONFIG_HOME": str(root),
        # 实测：OPENCODE_CONFIG 的优先级高于 XDG_CONFIG_HOME，只改 XDG 的话
        # 全局 opencode.jsonc 仍会被读进来（全局插件照常加载）。
        "OPENCODE_CONFIG": str(root / "opencode" / "opencode.jsonc"),
    }
    assert (root / "opencode" / "opencode.jsonc").is_file()


def test_server_env_wraps_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_path: object) -> Path:
        raise OSError("拒绝访问")

    monkeypatch.setattr("dafeiyu.brain.runtime_config.ensure_isolated_config", boom)

    with pytest.raises(ConfigError, match="opencode.config_home"):
        server_env(tmp_path / "cfg")


def test_server_build_env_carries_xdg_config_home(tmp_path: Path) -> None:
    env = server_env(tmp_path / "cfg") or {}
    server = OpenCodeServer(
        "opencode.exe",
        str(tmp_path),
        "data/bot.db",
        env=env,
    )

    built = server._build_env()
    assert built["XDG_CONFIG_HOME"] == env["XDG_CONFIG_HOME"]
    assert built["OPENCODE_CONFIG"] == env["OPENCODE_CONFIG"]


# ---- 配置解析 -------------------------------------------------------------


def test_config_home_parsed_from_yaml(tmp_path: Path) -> None:
    assert load(tmp_path).opencode.config_home == ""

    text = patch(
        BASE,
        "logging:\n",
        "opencode:\n  config_home: data/opencode-runtime/config\nlogging:\n",
    )
    assert load(tmp_path, text).opencode.config_home == "data/opencode-runtime/config"
