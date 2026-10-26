from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.config import load_settings
from dafeiyu.supervisor.alerts import AlertManager, QQAdminChannel, WebhookChannel
from dafeiyu.supervisor.backup import BackupError
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.ops import OpsServices
from dafeiyu.timeutil import SHANGHAI
from conftest import TEST_CONFIG_YAML

BASE = TEST_CONFIG_YAML + """\
alerts:
  enabled: false
  check_seconds: 15
  repeat_seconds: 600
  webhook:
    url: ""
    format: generic
  qq_admin: 0
backup:
  enabled: true
  dir: data/backups
  daily_keep: 7
  weekly_keep: 4
  schedule_hour: 4
  max_total_gb: 20
  max_upload_mb: 512
audit:
  path: data/audit.jsonl
  max_bytes: 10485760
  backups: 3
"""


class FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def status(self) -> list[dict]:
        return [
            {"name": name, "status": "running", "restart_count": 0}
            for name in ("bot", "opencode", "napcat")
        ]

    async def start(self, name: str) -> None:
        self.calls.append(("start", name))

    async def stop(self, name: str) -> None:
        self.calls.append(("stop", name))


def make_settings(
    tmp_path: Path,
    *,
    alerts_enabled: bool = False,
    webhook: str = "",
    qq_admin: int = 0,
):
    text = BASE
    if alerts_enabled:
        text = text.replace("alerts:\n  enabled: false", "alerts:\n  enabled: true")
    if webhook:
        text = text.replace('    url: ""', f'    url: "{webhook}"')
    if qq_admin:
        text = text.replace("  qq_admin: 0", f"  qq_admin: {qq_admin}")
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    return load_settings(env_file=env, config_file=config)


def _isolate_repo(monkeypatch, tmp_path: Path):
    import dafeiyu.supervisor.main as main_module

    monkeypatch.setattr(main_module, "REPO_ROOT", tmp_path)
    return main_module


def _ops_dir(tmp_path: Path, name: str = "logs") -> LogHub:
    return LogHub(tmp_path / name)


def test_build_ops_uses_configured_paths(tmp_path: Path, monkeypatch) -> None:
    main_module = _isolate_repo(monkeypatch, tmp_path)
    settings = make_settings(tmp_path)

    ops = main_module.build_ops(
        settings,
        FakeSupervisor(),
        _ops_dir(tmp_path),
        "http://127.0.0.1:1",
        "t",
    )

    assert ops.backups is not None
    assert ops.backups.backup_dir == tmp_path / settings.backup.dir
    assert ops.audit is not None
    assert ops.audit.path == tmp_path / settings.audit.path
    assert ops.restore is not None
    assert ops.restore.db_path == tmp_path / settings.db_path
    assert ops.health is not None
    assert ops.alerts is None


def test_build_ops_wires_alert_channels_when_enabled(tmp_path: Path, monkeypatch) -> None:
    main_module = _isolate_repo(monkeypatch, tmp_path)
    settings = make_settings(
        tmp_path, alerts_enabled=True, webhook="https://example.invalid/hook", qq_admin=42
    )

    ops = main_module.build_ops(
        settings,
        FakeSupervisor(),
        _ops_dir(tmp_path),
        "http://127.0.0.1:1",
        "t",
    )

    assert isinstance(ops.alerts, AlertManager)
    assert ops.alerts.state_path == tmp_path / "data" / "alert-state.json"
    assert len(ops.alerts.channels) == 2
    assert isinstance(ops.alerts.channels[0], QQAdminChannel)
    assert isinstance(ops.alerts.channels[1], WebhookChannel)


def test_build_ops_without_channels_still_evaluates(tmp_path: Path, monkeypatch) -> None:
    main_module = _isolate_repo(monkeypatch, tmp_path)
    settings = make_settings(tmp_path, alerts_enabled=True)

    ops = main_module.build_ops(
        settings,
        FakeSupervisor(),
        _ops_dir(tmp_path),
        "http://127.0.0.1:1",
        "t",
    )

    assert isinstance(ops.alerts, AlertManager)
    assert ops.alerts.channels == []


class FakeHealth:
    def __init__(self) -> None:
        self.calls = 0

    async def snapshot(self) -> dict:
        self.calls += 1
        return {"ok": True}


class FakeAlerts:
    def __init__(self, stop: asyncio.Event, fail_first: bool = False) -> None:
        self.stop = stop
        self.fail_first = fail_first
        self.calls = 0

    async def evaluate(self, snapshot: dict) -> list:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("boom")
        self.stop.set()
        return []


async def test_alert_loop_evaluates_then_stops(tmp_path: Path) -> None:
    from dafeiyu.supervisor.main import alert_loop

    settings = make_settings(tmp_path, alerts_enabled=True)
    stop = asyncio.Event()
    health = FakeHealth()
    alerts = FakeAlerts(stop)
    ops = OpsServices(health=health, alerts=alerts)  # type: ignore[arg-type]

    await alert_loop(ops, settings, stop, interval=0.01)

    assert health.calls == 1
    assert alerts.calls == 1


async def test_alert_loop_swallows_rule_errors(tmp_path: Path) -> None:
    from dafeiyu.supervisor.main import alert_loop

    settings = make_settings(tmp_path, alerts_enabled=True)
    stop = asyncio.Event()
    alerts = FakeAlerts(stop, fail_first=True)
    ops = OpsServices(health=FakeHealth(), alerts=alerts)  # type: ignore[arg-type]

    await asyncio.wait_for(alert_loop(ops, settings, stop, interval=0.01), timeout=1)

    assert alerts.calls == 2


class FakeBackups:
    def __init__(
        self,
        stop: asyncio.Event,
        *,
        last_date: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.stop = stop
        self.last_date = last_date
        self.error = error
        self.reasons: list[str] = []

    def last_scheduled_date(self) -> str | None:
        return self.last_date

    async def create(self, reason: str = "manual"):
        self.reasons.append(reason)
        self.stop.set()
        if self.error is not None:
            raise self.error
        return object()


def test_backup_due_only_once_per_day() -> None:
    from dafeiyu.supervisor.main import backup_due

    now = datetime(2026, 10, 26, 4, 5, tzinfo=SHANGHAI)
    assert backup_due(now, 4, None) is True
    assert backup_due(now, 4, "2026-10-26") is False
    assert backup_due(now, 3, None) is False


async def test_backup_loop_creates_scheduled_backup(tmp_path: Path) -> None:
    from dafeiyu.supervisor.main import backup_loop

    settings = make_settings(tmp_path)
    stop = asyncio.Event()
    backups = FakeBackups(stop)
    ops = OpsServices(health=FakeHealth(), backups=backups)  # type: ignore[arg-type]
    fixed = datetime(2026, 10, 26, 4, 5, tzinfo=SHANGHAI)

    await backup_loop(ops, settings, stop, interval=0.01, clock=lambda: fixed)

    assert backups.reasons == ["scheduled"]


async def test_backup_loop_skips_when_today_already_done(tmp_path: Path) -> None:
    from dafeiyu.supervisor.main import backup_loop

    settings = make_settings(tmp_path)
    stop = asyncio.Event()
    backups = FakeBackups(stop, last_date="2026-10-26")
    ops = OpsServices(health=FakeHealth(), backups=backups)  # type: ignore[arg-type]
    fixed = datetime(2026, 10, 26, 4, 5, tzinfo=SHANGHAI)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        if calls >= 2:
            stop.set()
        return fixed

    await asyncio.wait_for(
        backup_loop(ops, settings, stop, interval=0.01, clock=clock), timeout=1
    )

    assert backups.reasons == []


async def test_backup_loop_keeps_running_after_failure(tmp_path: Path) -> None:
    from dafeiyu.supervisor.main import backup_loop

    settings = make_settings(tmp_path)
    stop = asyncio.Event()
    backups = FakeBackups(stop, error=BackupError("disk full"))
    ops = OpsServices(health=FakeHealth(), backups=backups)  # type: ignore[arg-type]
    fixed = datetime(2026, 10, 26, 4, 5, tzinfo=SHANGHAI)

    await asyncio.wait_for(
        backup_loop(ops, settings, stop, interval=0.01, clock=lambda: fixed), timeout=1
    )

    assert backups.reasons == ["scheduled"]


def bot_app(token: str, ok: bool) -> web.Application:
    app = web.Application()

    async def detail(request: web.Request) -> web.Response:
        if token and request.headers.get("X-Control-Token") != token:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"ok": ok, "generated_at": "2026-10-26T12:00:00+08:00"})

    app.router.add_get("/api/health/detail", detail)
    return app


async def test_ready_check_uses_authenticated_health_detail(
    tmp_path: Path, monkeypatch
) -> None:
    main_module = _isolate_repo(monkeypatch, tmp_path)
    settings = make_settings(tmp_path)
    client = TestClient(TestServer(bot_app("t", True)))
    await client.start_server()
    try:
        url = str(client.make_url("")).rstrip("/")
        ops = main_module.build_ops(
            settings, FakeSupervisor(), _ops_dir(tmp_path), url, "t"
        )
        assert ops.restore is not None
        assert await ops.restore.ready_check() is True  # type: ignore[misc]

        wrong = main_module.build_ops(
            settings, FakeSupervisor(), _ops_dir(tmp_path, "logs2"), url, "wrong"
        )
        assert wrong.restore is not None
        assert await wrong.restore.ready_check() is False  # type: ignore[misc]
    finally:
        await client.close()


async def test_ready_check_rejects_unhealthy_bot(tmp_path: Path, monkeypatch) -> None:
    main_module = _isolate_repo(monkeypatch, tmp_path)
    settings = make_settings(tmp_path)
    client = TestClient(TestServer(bot_app("t", False)))
    await client.start_server()
    try:
        ops = main_module.build_ops(
            settings,
            FakeSupervisor(),
            _ops_dir(tmp_path),
            str(client.make_url("")).rstrip("/"),
            "t",
        )
        assert ops.restore is not None
        assert await ops.restore.ready_check() is False  # type: ignore[misc]
    finally:
        await client.close()
