from __future__ import annotations

from pathlib import Path

from conftest import TEST_CONFIG_YAML

from dafeiyu.config import DashboardConfig, load_settings
from dafeiyu.supervisor.lan_access import dashboard_urls, ensure_dashboard_password

LAN_HOST = "0.0.0.0"


def load(tmp_path: Path, *, dashboard_yaml: str = "", password: str | None = None):
    config = tmp_path / "config.yaml"
    config.write_text(TEST_CONFIG_YAML + dashboard_yaml, encoding="utf-8")
    lines = ["BOT_QQ=10001", "DEEPSEEK_API_KEY=sk-test"]
    if password is not None:
        lines.append(f"DAFEIYU_DASHBOARD_PASSWORD={password}")
    env = tmp_path / "test.env"
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_settings(env_file=env, config_file=config)


DASHBOARD_BLOCK = "supervisor:\n  dashboard:\n    host: {host}\n"


def test_dashboard_config_defaults_to_lan() -> None:
    dashboard = DashboardConfig()

    assert dashboard.host == LAN_HOST
    assert dashboard.auth_required is True


def test_config_without_supervisor_section_defaults_to_lan(tmp_path: Path) -> None:
    settings = load(tmp_path)

    dashboard = settings.supervisor.dashboard
    assert dashboard.host == LAN_HOST
    assert dashboard.auth_required is True
    # 未配置密码不是错误：启动期会生成并落盘。
    assert dashboard.password == ""


def test_config_without_supervisor_section_needs_no_password(tmp_path: Path) -> None:
    """默认值变更后，全新部署不带密码也必须能加载配置。"""
    settings = load(tmp_path, password="")

    assert settings.supervisor.dashboard.host == LAN_HOST


def test_explicit_loopback_still_opts_out(tmp_path: Path) -> None:
    settings = load(tmp_path, dashboard_yaml=DASHBOARD_BLOCK.format(host="127.0.0.1"))

    dashboard = settings.supervisor.dashboard
    assert dashboard.host == "127.0.0.1"
    assert dashboard.auth_required is False


def test_ensure_password_generates_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "data" / "dashboard.password"

    first = ensure_dashboard_password(DashboardConfig(host=LAN_HOST), path=path)
    second = ensure_dashboard_password(DashboardConfig(host=LAN_HOST), path=path)

    assert len(first) >= 24
    assert path.read_text(encoding="utf-8").strip() == first
    # 第二次启动复用同一口令，不能每次重启都换密码。
    assert second == first


def test_ensure_password_reuses_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "dashboard.password"
    path.write_text("existing-secret-value\n", encoding="utf-8")

    assert ensure_dashboard_password(DashboardConfig(host=LAN_HOST), path=path) == (
        "existing-secret-value"
    )


def test_ensure_password_prefers_env_value(tmp_path: Path) -> None:
    path = tmp_path / "dashboard.password"

    assert (
        ensure_dashboard_password(
            DashboardConfig(host=LAN_HOST, password="from-env-123456"), path=path
        )
        == "from-env-123456"
    )
    assert not path.exists()


def test_ensure_password_skipped_when_auth_not_required(tmp_path: Path) -> None:
    path = tmp_path / "dashboard.password"

    assert ensure_dashboard_password(DashboardConfig(host="127.0.0.1"), path=path) == ""
    assert not path.exists()


def test_ensure_password_survives_unwritable_path(tmp_path: Path) -> None:
    path = tmp_path / "occupied"
    path.mkdir()  # 目标是个目录，os.replace 必然失败

    password = ensure_dashboard_password(DashboardConfig(host=LAN_HOST), path=path)

    # 写不了盘也要让本次进程可用，不能阻断启动。
    assert len(password) >= 24


def test_dashboard_urls_report_loopback_and_lan() -> None:
    urls = dashboard_urls(LAN_HOST, 8787, lan_ip="192.168.1.5")

    assert "http://127.0.0.1:8787/" in urls
    assert "http://192.168.1.5:8787/" in urls


def test_dashboard_urls_loopback_only_on_loopback_bind() -> None:
    urls = dashboard_urls("127.0.0.1", 8787, lan_ip="192.168.1.5")

    assert urls == ["http://127.0.0.1:8787/"]


def test_dashboard_urls_skip_lan_when_ip_unknown() -> None:
    urls = dashboard_urls(LAN_HOST, 8787, lan_ip="")

    assert urls == ["http://127.0.0.1:8787/"]
