from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.config import load_settings
from dafeiyu.supervisor.health import HealthMonitor
from dafeiyu.supervisor.ops import OpsServices
from conftest import TEST_CONFIG_YAML


class FakeSupervisor:
    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self._statuses = statuses or {
            "bot": "running",
            "opencode": "running",
            "napcat": "running",
        }

    def status(self) -> list[dict]:
        return [
            {"name": name, "status": status, "restart_count": 0}
            for name, status in self._statuses.items()
        ]


class FakeBackups:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def status(self) -> dict:
        if self.error is not None:
            raise self.error
        return {"last_success_at": "2026-10-26T04:00:00+08:00", "last_error_at": None, "count": 2}


def make_settings(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(TEST_CONFIG_YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    return load_settings(env_file=env, config_file=config)


def bot_app(token: str = "t") -> web.Application:
    app = web.Application()

    async def detail(request: web.Request) -> web.Response:
        if token and request.headers.get("X-Control-Token") != token:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(
            {
                "ok": True,
                "gateway": {"connected": True},
                "quota": {"paused_until": None},
                "errors": {"recent_count": 0},
            }
        )

    app.router.add_get("/api/health/detail", detail)
    return app


async def test_snapshot_aggregates_and_marks_bot_timeout(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    monitor = HealthMonitor(
        supervisor=FakeSupervisor(),
        settings=settings,
        bot_url="http://127.0.0.1:1",
        token="t",
        backups=FakeBackups(),
        napcat_login=lambda: {"logged_in": True, "qr_age_seconds": None},
    )
    try:
        snap = await monitor.snapshot()
    finally:
        await monitor.close()

    assert snap["processes"]["bot"]["status"] == "running"
    assert snap["bot"]["ok"] is False
    assert snap["ok"] is False
    assert "disk" in snap
    assert snap["disk"]["free_percent"] is not None
    assert snap["backups"]["count"] == 2
    assert snap["napcat"]["logged_in"] is True


async def test_napcat_login_object_state_is_awaited(tmp_path: Path) -> None:
    """生产传的是 NapCatLogin 对象（有 state()），不是函数。"""

    class FakeLogin:
        async def state(self):
            return {"logged_in": True, "qr_age_seconds": 3}

    settings = make_settings(tmp_path)
    monitor = HealthMonitor(
        supervisor=FakeSupervisor(),
        settings=settings,
        bot_url="http://127.0.0.1:1",
        token="t",
        backups=FakeBackups(),
        napcat_login=FakeLogin(),
    )
    try:
        snap = await monitor.snapshot()
    finally:
        await monitor.close()

    assert snap["napcat"]["logged_in"] is True
    assert snap["napcat"]["qr_age_seconds"] == 3


async def test_napcat_offline_is_passed_through(tmp_path: Path) -> None:
    """半离线字段必须透传到 snapshot，否则告警和 dashboard 都看不见。"""

    class FakeLogin:
        async def state(self):
            return {
                "logged_in": True,
                "offline": True,
                "reachable": True,
                "phase_label": "已登录",
                "error": "",
                "qr_age_seconds": None,
            }

    settings = make_settings(tmp_path)
    monitor = HealthMonitor(
        supervisor=FakeSupervisor(),
        settings=settings,
        bot_url="http://127.0.0.1:1",
        token="t",
        backups=FakeBackups(),
        napcat_login=FakeLogin(),
    )
    try:
        snap = await monitor.snapshot()
    finally:
        await monitor.close()

    assert snap["napcat"]["logged_in"] is True
    assert snap["napcat"]["offline"] is True
    assert snap["napcat"]["reachable"] is True


async def test_snapshot_reads_bot_health_over_http(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = TestClient(TestServer(bot_app()))
    await client.start_server()
    monitor = HealthMonitor(
        supervisor=FakeSupervisor(),
        settings=settings,
        bot_url=str(client.make_url("")).rstrip("/"),
        token="t",
        backups=FakeBackups(),
    )
    try:
        snap = await monitor.snapshot()
    finally:
        await monitor.close()
        await client.close()

    assert snap["bot"]["ok"] is True
    assert snap["ok"] is True
    assert snap["processes"]["bot"]["status"] == "running"


async def test_failed_process_makes_overall_unhealthy(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    monitor = HealthMonitor(
        supervisor=FakeSupervisor({"bot": "running", "opencode": "failed"}),
        settings=settings,
        bot_url="",
        backups=FakeBackups(),
    )
    try:
        snap = await monitor.snapshot()
    finally:
        await monitor.close()

    assert snap["processes"]["opencode"]["status"] == "failed"
    assert snap["ok"] is False


async def test_disk_error_is_reported_not_raised(tmp_path: Path, monkeypatch) -> None:
    import dafeiyu.supervisor.health as health_module

    def boom(_path):
        raise OSError("disk gone")

    monkeypatch.setattr(health_module.shutil, "disk_usage", boom)
    settings = make_settings(tmp_path)
    monitor = HealthMonitor(
        supervisor=FakeSupervisor(),
        settings=settings,
        bot_url="",
        backups=FakeBackups(),
    )
    try:
        snap = await monitor.snapshot()
    finally:
        await monitor.close()

    assert snap["disk"]["free_bytes"] is None
    assert snap["disk"]["free_percent"] is None
    assert snap["disk"]["error"] == "OSError"


async def test_backup_status_error_is_reported(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    monitor = HealthMonitor(
        supervisor=FakeSupervisor(),
        settings=settings,
        bot_url="",
        backups=FakeBackups(error=RuntimeError("broken")),
    )
    try:
        snap = await monitor.snapshot()
    finally:
        await monitor.close()

    assert snap["backups"]["last_error_at"]
    assert snap["backups"]["count"] == 0


async def test_napcat_login_error_is_reported(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    def broken():
        raise RuntimeError("webui down")

    monitor = HealthMonitor(
        supervisor=FakeSupervisor(),
        settings=settings,
        bot_url="",
        backups=FakeBackups(),
        napcat_login=broken,
    )
    try:
        snap = await monitor.snapshot()
    finally:
        await monitor.close()

    assert snap["napcat"]["logged_in"] is False


def test_ops_services_dataclass(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    health = HealthMonitor(
        supervisor=FakeSupervisor(),
        settings=settings,
        bot_url="",
        backups=FakeBackups(),
    )
    ops = OpsServices(health=health)
    assert ops.health is health
    assert ops.alerts is None
    assert ops.backups is None
