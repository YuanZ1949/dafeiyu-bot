"""Task 13.1：bot 专用 opencode 配置目录（隔离全局插件 / MCP / 外来 agent）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from test_config_agents import BASE, load, patch

from dafeiyu.brain import provider_config, runtime_config
from dafeiyu.brain.opencode_server import OpenCodeServer
from dafeiyu.brain.runtime_config import (
    DEFAULT_OPENCODE_MODEL,
    MINIMAL_CONFIG,
    ensure_isolated_config,
    server_env,
)
from dafeiyu.config import ConfigError


def _load_config(text: str) -> dict:
    """生成物首行是标记（JSONC 注释），解析前先摘掉。"""
    lines = text.splitlines()
    assert lines[0] == provider_config.GENERATED_MARK
    return json.loads("\n".join(lines[1:]))


# ---- 配置目录本体 ---------------------------------------------------------


def test_ensure_creates_minimal_config(tmp_path: Path) -> None:
    home = tmp_path / "runtime" / "config"

    root = ensure_isolated_config(home)

    assert root == home.resolve()
    cfg = home / "opencode" / "opencode.jsonc"
    assert cfg.is_file()
    data = _load_config(cfg.read_text(encoding="utf-8"))
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["model"] == DEFAULT_OPENCODE_MODEL
    # 显式空列表：隔离目录里不该再加载任何插件
    assert data["plugins"] == []


def test_ensure_is_idempotent_for_generated_file(tmp_path: Path) -> None:
    home = tmp_path / "config"
    ensure_isolated_config(home)
    cfg = home / "opencode" / "opencode.jsonc"
    before = cfg.read_text(encoding="utf-8")

    ensure_isolated_config(home)

    assert cfg.read_text(encoding="utf-8") == before
    assert not list(cfg.parent.glob("opencode.jsonc.bak.*"))
    assert runtime_config.LAST_BACKUP is None


def test_generated_file_is_left_alone(tmp_path: Path) -> None:
    p = provider_config.config_path(str(tmp_path))
    p.parent.mkdir(parents=True)
    generated = "// generated-by: dafeiyu\n{}\n"
    p.write_text(generated, encoding="utf-8")

    ensure_isolated_config(str(tmp_path))

    # 整份字节不变：只断言首行标记的话，「用兜底配置覆盖生成物」也会绿。
    assert p.read_text(encoding="utf-8") == generated
    assert runtime_config.LAST_BACKUP is None


def test_old_minimal_config_is_left_alone(tmp_path: Path) -> None:
    """升级前的兜底配置（无标记、raw json.dumps）不该被当成手改件备份。

    原文写死成升级前的字面量（不是用当前 MINIMAL_CONFIG 拼）：将来改默认值却
    忘了兼容旧文件时，这里会红，而不是每台机器静默多出一份备份。
    """
    p = provider_config.config_path(str(tmp_path))
    p.parent.mkdir(parents=True)
    legacy = (
        "{\n"
        '  "$schema": "https://opencode.ai/config.json",\n'
        '  "model": "opencode/deepseek-v4.1-flash-free",\n'
        '  "plugins": []\n'
        "}\n\n"
    )
    p.write_text(legacy, encoding="utf-8")

    ensure_isolated_config(str(tmp_path))

    assert p.read_text(encoding="utf-8") == legacy
    assert not list(p.parent.glob("opencode.jsonc.bak.*"))
    assert runtime_config.LAST_BACKUP is None


def test_undecodable_file_is_backed_up(tmp_path: Path) -> None:
    """GBK 等非 UTF-8 编辑器写出的配置：不抛异常，按手改过备份再写兜底。"""
    p = provider_config.config_path(str(tmp_path))
    p.parent.mkdir(parents=True)
    raw = '{"model": "自定义模型"}'.encode("gbk")
    p.write_bytes(raw)

    root = ensure_isolated_config(str(tmp_path))

    assert root == tmp_path.resolve()
    backups = list(p.parent.glob("opencode.jsonc.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == raw
    assert runtime_config.LAST_BACKUP == backups[0]
    assert p.read_text(encoding="utf-8") == provider_config.render_config(
        MINIMAL_CONFIG
    )


def test_default_model_is_single_sourced() -> None:
    assert DEFAULT_OPENCODE_MODEL is provider_config.FREE_MODEL
    assert MINIMAL_CONFIG["model"] == provider_config.FREE_MODEL


def test_manual_file_is_backed_up(tmp_path: Path) -> None:
    p = provider_config.config_path(str(tmp_path))
    p.parent.mkdir(parents=True)
    p.write_text('{"model":"x"}', encoding="utf-8")

    ensure_isolated_config(str(tmp_path))

    assert list(p.parent.glob("opencode.jsonc.bak.*"))


def test_manual_file_backup_is_exposed(tmp_path: Path) -> None:
    p = provider_config.config_path(str(tmp_path))
    p.parent.mkdir(parents=True)
    p.write_text('{"model":"x"}', encoding="utf-8")

    root = ensure_isolated_config(str(tmp_path))

    # 返回值仍是配置根目录（server_env 依赖它拼 XDG_CONFIG_HOME）。
    assert root == tmp_path.resolve()
    backup = runtime_config.LAST_BACKUP
    assert backup is not None
    assert backup.parent == p.parent
    assert backup.name.startswith("opencode.jsonc.bak.")
    assert backup.read_text(encoding="utf-8") == '{"model":"x"}'
    assert p.read_text(encoding="utf-8").startswith(provider_config.GENERATED_MARK)


def test_ensure_resolves_relative_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    root = ensure_isolated_config("data/opencode-runtime/config")

    assert root == (tmp_path / "data" / "opencode-runtime" / "config").resolve()
    assert (root / "opencode" / "opencode.jsonc").is_file()


# ---- 环境变量 -------------------------------------------------------------


def test_server_env_empty_means_inherit(monkeypatch: pytest.MonkeyPatch) -> None:
    # 不读真实凭据：空 config_home 会直接返回 None，但仍按约定关掉来源。
    monkeypatch.setattr(
        "dafeiyu.brain.runtime_config._global_auth_file", lambda: None
    )
    assert server_env("") is None
    assert server_env("   ") is None


def test_server_env_points_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 凭据复制另有测试；这里不碰用户主目录里的 auth.json。
    monkeypatch.setattr(
        "dafeiyu.brain.runtime_config._global_auth_file", lambda: None
    )
    env = server_env(tmp_path / "cfg")

    root = (tmp_path / "cfg").resolve()
    assert env == {
        "XDG_CONFIG_HOME": str(root),
        # 实测：OPENCODE_CONFIG 的优先级高于 XDG_CONFIG_HOME，只改 XDG 的话
        # 全局 opencode.jsonc 仍会被读进来（全局插件照常加载）。
        "OPENCODE_CONFIG": str(root / "opencode" / "opencode.jsonc"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
    }
    assert (root / "opencode" / "opencode.jsonc").is_file()


def test_server_env_wraps_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_path: object) -> Path:
        raise OSError("拒绝访问")

    monkeypatch.setattr(
        "dafeiyu.brain.runtime_config._global_auth_file", lambda: None
    )
    monkeypatch.setattr("dafeiyu.brain.runtime_config.ensure_isolated_config", boom)

    with pytest.raises(ConfigError, match="opencode.config_home"):
        server_env(tmp_path / "cfg")


def test_server_build_env_carries_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "dafeiyu.brain.runtime_config._global_auth_file", lambda: None
    )
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


# ---- 凭据复制 ----


def test_server_env_seeds_isolated_auth_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "global" / "opencode" / "auth.json"
    source.parent.mkdir(parents=True)
    raw = b'{"opencode": {"type": "api", "key": "test-key"}}'
    source.write_bytes(raw)
    monkeypatch.setattr(
        "dafeiyu.brain.runtime_config._global_auth_file", lambda: source
    )

    server_env(tmp_path / "cfg")

    target = (tmp_path / "cfg").resolve() / "data" / "opencode" / "auth.json"
    assert target.read_bytes() == raw


def test_server_env_does_not_overwrite_isolated_auth_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "global" / "opencode" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"opencode": {"key": "old"}}')
    refreshed = b'{"opencode": {"key": "refreshed"}}'
    target = (tmp_path / "cfg").resolve() / "data" / "opencode" / "auth.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(refreshed)
    monkeypatch.setattr(
        "dafeiyu.brain.runtime_config._global_auth_file", lambda: source
    )

    server_env(tmp_path / "cfg")

    # 隔离目录里的凭据可能已被刷新，绝不能被全局旧文件覆盖。
    assert target.read_bytes() == refreshed


def test_server_env_without_global_auth_file_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "dafeiyu.brain.runtime_config._global_auth_file", lambda: None
    )

    env = server_env(tmp_path / "cfg")

    assert env is not None
    target = (tmp_path / "cfg").resolve() / "data" / "opencode" / "auth.json"
    assert not target.exists()


def test_server_env_warns_when_auth_copy_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "global" / "opencode" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"{}")

    def boom(_source: object, _target: object) -> None:
        raise OSError("拒绝访问")

    monkeypatch.setattr(
        "dafeiyu.brain.runtime_config._global_auth_file", lambda: source
    )
    monkeypatch.setattr("dafeiyu.brain.runtime_config.shutil.copy2", boom)

    with caplog.at_level("WARNING", logger="dafeiyu.brain.runtime_config"):
        env = server_env(tmp_path / "cfg")

    # 复制失败不阻断启动：env 照常返回，只留一条 warning。
    root = (tmp_path / "cfg").resolve()
    assert env == {
        "XDG_CONFIG_HOME": str(root),
        "OPENCODE_CONFIG": str(root / "opencode" / "opencode.jsonc"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
    }
    warnings = [
        record
        for record in caplog.records
        if record.name == "dafeiyu.brain.runtime_config"
        and record.levelno == logging.WARNING
    ]
    assert warnings
    assert "凭据复制失败" in warnings[-1].getMessage()


# ---- 配置解析 -------------------------------------------------------------


def test_config_home_parsed_from_yaml(tmp_path: Path) -> None:
    assert load(tmp_path).opencode.config_home == ""

    text = patch(
        BASE,
        "logging:\n",
        "opencode:\n  config_home: data/opencode-runtime/config\nlogging:\n",
    )
    assert load(tmp_path, text).opencode.config_home == "data/opencode-runtime/config"
