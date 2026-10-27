from __future__ import annotations

from pathlib import Path

import pytest

from dafeiyu.bootstrap import (
    format_issues,
    has_errors,
    init_config,
    preflight,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


def _write_env(tmp_path: Path, **values: str) -> Path:
    env = tmp_path / ".env"
    env.write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
    return env


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_init_creates_config_from_example(tmp_path):
    target = tmp_path / "config.yaml"

    result = init_config(target, source=EXAMPLE)

    assert result.created is True
    assert target.read_text(encoding="utf-8").startswith("# 大肥鱼")


def test_init_keeps_existing_file_without_force(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("keep me", encoding="utf-8")

    result = init_config(target, source=EXAMPLE)

    assert result.created is False
    assert target.read_text(encoding="utf-8") == "keep me"


def test_init_force_overwrites_existing_file(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("keep me", encoding="utf-8")

    result = init_config(target, source=EXAMPLE, force=True)

    assert result.created is True
    assert "persona:" in target.read_text(encoding="utf-8")


def test_init_missing_example_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        init_config(tmp_path / "config.yaml", source=tmp_path / "nope.yaml")


def test_preflight_without_config_points_to_init(tmp_path):
    issues = preflight(
        tmp_path / "missing.yaml", tmp_path / ".env", root=tmp_path, check_ports=False
    )

    assert [issue.title for issue in issues] == ["缺少配置文件"]
    assert "dafeiyu init" in issues[0].fix
    assert has_errors(issues)


def test_preflight_reports_missing_bot_qq(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = _write_env(tmp_path, DEEPSEEK_API_KEY="sk-test")

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert has_errors(issues)
    assert "BOT_QQ" in issues[0].detail
    assert ".env" in issues[0].fix


def test_preflight_reports_missing_napcat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = _write_env(tmp_path, BOT_QQ="10001", DEEPSEEK_API_KEY="sk-test")

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert [issue.title for issue in issues] == ["NapCat 未安装"]
    assert "setup-napcat.ps1" in issues[0].fix


def test_preflight_passes_when_everything_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = _write_env(tmp_path, BOT_QQ="10001", DEEPSEEK_API_KEY="sk-test")
    napcat = tmp_path / "vendor" / "napcat"
    napcat.mkdir(parents=True)
    (napcat / "node.exe").write_bytes(b"")

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert issues == []
    assert "体检通过" in format_issues(issues)


def test_preflight_port_check_reads_configured_ports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = _write_env(tmp_path, BOT_QQ="10001", DEEPSEEK_API_KEY="sk-test")
    napcat = tmp_path / "vendor" / "napcat"
    napcat.mkdir(parents=True)
    (napcat / "node.exe").write_bytes(b"")

    issues = preflight(config, env, root=tmp_path, check_ports=True)

    assert [issue.title for issue in issues if issue.title != "端口已被占用"] == []
    for issue in issues:
        assert issue.level in {"error", "warn"}
