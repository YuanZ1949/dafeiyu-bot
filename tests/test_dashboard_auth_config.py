from __future__ import annotations

from pathlib import Path

import pytest
from conftest import TEST_CONFIG_YAML

from dafeiyu.config import ConfigError, load_settings


def load(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    auth: str = "auto",
    password: str = "",
    user: str = "admin",
    session_hours: int | None = None,
):
    config = tmp_path / "config.yaml"
    config.write_text(
        TEST_CONFIG_YAML
        + "supervisor:\n"
        + "  dashboard:\n"
        + f"    host: {host}\n"
        + f"    auth: {auth}\n",
        encoding="utf-8",
    )
    lines = [
        "BOT_QQ=10001",
        "DEEPSEEK_API_KEY=sk-test",
        f"DAFEIYU_DASHBOARD_USER={user}",
        f"DAFEIYU_DASHBOARD_PASSWORD={password}",
    ]
    if session_hours is not None:
        lines.append(f"DAFEIYU_DASHBOARD_SESSION_HOURS={session_hours}")
    env = tmp_path / "test.env"
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_settings(env_file=env, config_file=config)


def test_loopback_auto_needs_no_password(tmp_path: Path) -> None:
    settings = load(tmp_path)

    assert settings.supervisor.dashboard.auth == "auto"
    assert settings.supervisor.dashboard.auth_required is False
    assert settings.supervisor.dashboard.password == ""


def test_non_loopback_auto_allows_missing_password_for_bootstrap(tmp_path: Path) -> None:
    """非回环 + 空密码不再报错：启动期用 data/dashboard.password 生成。"""
    settings = load(tmp_path, host="0.0.0.0")

    assert settings.supervisor.dashboard.password == ""
    assert settings.supervisor.dashboard.auth_required is True


def test_non_loopback_auto_requires_a_long_password(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="12"):
        load(tmp_path, host="0.0.0.0", password="short")


def test_non_loopback_auto_accepts_a_long_password(tmp_path: Path) -> None:
    settings = load(
        tmp_path,
        host="0.0.0.0",
        password="correct-horse-battery",
        user="fish",
        session_hours=24,
    )

    dashboard = settings.supervisor.dashboard
    assert dashboard.auth_required is True
    assert dashboard.username == "fish"
    assert dashboard.password == "correct-horse-battery"
    assert dashboard.session_hours == 24
    assert "correct-horse-battery" not in repr(dashboard)


def test_auth_off_is_rejected_on_non_loopback(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="loopback"):
        load(tmp_path, host="0.0.0.0", auth="off", password="correct-horse-battery")


def test_auth_off_is_allowed_on_loopback(tmp_path: Path) -> None:
    settings = load(tmp_path, auth="off")

    assert settings.supervisor.dashboard.auth_required is False


def test_auth_on_requires_auth_even_on_loopback(tmp_path: Path) -> None:
    # 空密码不再报错（启动期生成），但鉴权必须仍然开启。
    settings = load(tmp_path, auth="on")
    assert settings.supervisor.dashboard.auth_required is True

    settings = load(tmp_path, auth="on", password="correct-horse-battery")
    assert settings.supervisor.dashboard.auth_required is True


def test_invalid_auth_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="supervisor.dashboard.auth"):
        load(tmp_path, auth="maybe")


def test_invalid_session_hours_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="SESSION_HOURS"):
        load(
            tmp_path,
            password="correct-horse-battery",
            session_hours=0,
        )
