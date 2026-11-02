from __future__ import annotations

from pathlib import Path

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor.audit import AuditLog
from dafeiyu.supervisor.auth import DashboardAuth
from dafeiyu.supervisor.backup import BackupManifest
from dafeiyu.supervisor.dashboard import create_app
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.ops import OpsServices

PASSWORD = "correct-horse"


class FakeSupervisor:
    def __init__(self) -> None:
        self.specs = {"bot": object(), "opencode": object()}
        self.calls: list[tuple[str, str]] = []

    def status(self) -> list[dict]:
        return [{"name": name, "status": "running"} for name in self.specs]

    async def start(self, name: str) -> None:
        self.calls.append(("start", name))

    async def stop(self, name: str) -> None:
        self.calls.append(("stop", name))

    async def restart(self, name: str) -> None:
        self.calls.append(("restart", name))


class FakeHealth:
    async def snapshot(self) -> dict:
        return {
            "ok": True,
            "generated_at": "2026-10-26T12:00:00+08:00",
            "processes": {"bot": {"status": "running"}},
            "gateway": {"connected": True},
        }


class FakeAlerts:
    def __init__(self) -> None:
        self.tested = 0

    def summary(self) -> dict:
        return {"firing": 1, "resolved_24h": 2}

    def events(self, include_resolved_hours: int = 24) -> list[dict]:
        return [{"rule": "bot_down", "status": "firing"}]

    async def send_test(self) -> dict:
        self.tested += 1
        return {"sent": 1, "failed": 0}


class FakeBackups:
    def __init__(self) -> None:
        self.items: list[BackupManifest] = []
        self.counter = 0

    def status(self) -> dict:
        return {"last_success_at": None, "last_error_at": None, "count": len(self.items)}

    def list(self) -> list[BackupManifest]:
        return list(self.items)

    async def create(self, reason: str = "manual") -> BackupManifest:
        self.counter += 1
        manifest = BackupManifest(
            id=f"b{self.counter}",
            filename=f"backup-{self.counter}.db",
            created_at="2026-10-26T12:00:00+08:00",
            reason=reason,
            size_bytes=1024,
            sha256="0" * 64,
            schema_version=1,
            integrity_ok=True,
            app_version="0.1.0",
        )
        self.items.append(manifest)
        return manifest

    def delete(self, backup_id: str) -> None:
        from dafeiyu.supervisor.backup import BackupError

        match = next((item for item in self.items if item.id == backup_id), None)
        if match is None:
            raise BackupError("备份不存在")
        self.items.remove(match)


class FakeRestore:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.busy = False

    async def restore(self, path: Path, *, actor: str, ip: str = "", request_id: str = "") -> dict:
        self.calls.append(
            {
                "path": Path(path),
                "name": Path(path).name,
                "actor": actor,
                "ip": ip,
            }
        )
        return {"ok": True, "duration_seconds": 0.1}


def make_auth(enabled: bool = True) -> DashboardAuth:
    return DashboardAuth(
        enabled=enabled,
        username="admin",
        password=PASSWORD,
        session_seconds=3600,
        secret=b"test-secret-32-bytes-long-000000",
    )


def make_app(
    tmp_path: Path,
    *,
    ops: OpsServices | None = None,
    auth: DashboardAuth | None = None,
    bot_url: str = "http://127.0.0.1:1",
):
    supervisor = FakeSupervisor()
    audit = AuditLog(tmp_path / "audit.jsonl")
    ops = ops or OpsServices(
        health=FakeHealth(),
        alerts=FakeAlerts(),
        backups=FakeBackups(),
        restore=FakeRestore(),
        audit=audit,
    )
    if ops.audit is None:
        ops.audit = audit
    app = create_app(
        supervisor,
        settings=None,
        logs=LogHub(tmp_path / "logs"),
        qr_path=tmp_path / "qrcode.png",
        bot_url=bot_url,
        auth=auth or make_auth(False),
        ops=ops,
    )
    return app, supervisor, ops, audit


async def login(client: TestClient) -> None:
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    assert resp.status == 204


async def test_ops_routes_require_auth(tmp_path: Path) -> None:
    app, _supervisor, _ops, _audit = make_app(tmp_path, auth=make_auth(True))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for path in (
            "/api/health/detail",
            "/api/backups",
            "/api/alerts",
            "/api/audit",
            "/api/budget?chat_type=group&chat_id=1",
        ):
            resp = await client.get(path)
            assert resp.status == 401, path
        assert (await client.post("/api/alerts/test")).status == 401
    finally:
        await client.close()


async def test_ops_routes_503_without_services(tmp_path: Path) -> None:
    supervisor = FakeSupervisor()
    app = create_app(
        supervisor,
        settings=None,
        logs=LogHub(tmp_path / "logs"),
        qr_path=tmp_path / "qrcode.png",
        bot_url="http://127.0.0.1:1",
        auth=make_auth(False),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for path in ("/api/health/detail", "/api/backups", "/api/audit"):
            resp = await client.get(path)
            assert resp.status == 503, path
            assert (await resp.json())["error"] == "运维服务未启用"
        # /api/alerts 特例：未启用时回 200 + enabled:false，避免面板轮询刷 console 网络错误
        resp = await client.get("/api/alerts")
        assert resp.status == 200
        data = await resp.json()
        assert data["enabled"] is False
        assert data["events"] == []
        assert data["summary"] == {"firing": 0, "resolved_24h": 0}
    finally:
        await client.close()


async def test_health_detail_combines_snapshot_and_alerts(tmp_path: Path) -> None:
    app, _supervisor, _ops, _audit = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/health/detail")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["gateway"]["connected"] is True
        assert data["alerts"] == {"firing": 1, "resolved_24h": 2}
    finally:
        await client.close()


async def test_alerts_endpoints(tmp_path: Path) -> None:
    app, _supervisor, ops, _audit = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        listed = await client.get("/api/alerts")
        assert listed.status == 200
        data = await listed.json()
        assert data["enabled"] is True
        assert data["events"][0]["rule"] == "bot_down"
        assert data["summary"]["firing"] == 1

        tested = await client.post("/api/alerts/test")
        assert tested.status == 200
        assert await tested.json() == {"ok": True, "sent": 1, "failed": 0}
        assert ops.alerts.tested == 1
    finally:
        await client.close()


async def test_backup_create_list_delete_and_audit(tmp_path: Path) -> None:
    app, _supervisor, ops, audit = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        created = await client.post("/api/backups", json={"reason": "manual"})
        assert created.status == 200
        backup = (await created.json())["backup"]
        assert backup["reason"] == "manual"

        listed = await client.get("/api/backups")
        assert listed.status == 200
        data = await listed.json()
        assert len(data["backups"]) == 1
        assert data["status"]["count"] == 1

        deleted = await client.request(
            "DELETE", f"/api/backups/{backup['id']}", json={"confirm": "DELETE"}
        )
        assert deleted.status == 200
        assert ops.backups.items == []

        actions = {row["action"] for row in audit.query(20)}
        assert {"backup.create", "backup.delete"} <= actions
    finally:
        await client.close()


async def test_backup_invalid_reason_and_missing_id(tmp_path: Path) -> None:
    app, _supervisor, _ops, _audit = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/backups", json={"reason": "weird"})
        assert resp.status == 400

        resp = await client.request("DELETE", "/api/backups/nope")
        assert resp.status == 404
    finally:
        await client.close()


async def test_restore_requires_confirm_and_file(tmp_path: Path) -> None:
    app, _supervisor, _ops, _audit = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/backups/restore", headers={"Content-Type": "multipart/form-data"}
        )
        assert resp.status == 400
    finally:
        await client.close()


async def test_restore_with_file_and_confirm(tmp_path: Path) -> None:
    app, _supervisor, ops, _audit = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        form = FormData()
        form.add_field("file", b"SQLite format 3\x00rest", filename="upload.db")
        form.add_field("confirm", "RESTORE")
        resp = await client.post("/api/backups/restore", data=form)
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
        assert len(ops.restore.calls) == 1
        assert ops.restore.calls[0]["actor"].startswith("dashboard:")
        assert not ops.restore.calls[0]["path"].exists()

        form = FormData()
        form.add_field("file", b"data", filename="upload.db")
        form.add_field("confirm", "WRONG")
        resp = await client.post("/api/backups/restore", data=form)
        assert resp.status == 400
        assert len(ops.restore.calls) == 1
    finally:
        await client.close()


async def test_audit_endpoint_and_process_control(tmp_path: Path) -> None:
    app, supervisor, _ops, audit = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/control/start", json={"target": "bot"})
        assert resp.status == 200
        assert supervisor.calls == [("start", "bot")]

        rows = await (await client.get("/api/audit")).json()
        actions = [row["action"] for row in rows["items"]]
        assert "process.start" in actions

        filtered = await client.get("/api/audit?limit=1")
        assert len((await filtered.json())["items"]) == 1
    finally:
        await client.close()


async def test_login_and_logout_are_audited(tmp_path: Path) -> None:
    app, _supervisor, _ops, audit = make_app(tmp_path, auth=make_auth(True))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong"}
        )
        await login(client)
        await client.post("/api/auth/logout")

        actions = [row["action"] for row in audit.query(20)]
        assert "auth.login" in actions
        assert "auth.logout" in actions
        failures = [row for row in audit.query(20) if row["action"] == "auth.login"]
        assert any(row["result"] == "error" for row in failures)
        assert any(row["result"] == "ok" for row in failures)
    finally:
        await client.close()


async def test_budget_proxies_bot_payload_after_login(tmp_path: Path) -> None:
    payload = {
        "chat_type": "group",
        "chat_id": 1,
        "per_chat": {
            "calls": 2,
            "tokens": 900,
            "daily_calls": 200,
            "daily_tokens": 300000,
        },
        "global_daily": {
            "calls": 5,
            "tokens": 1200,
            "limit_calls": 0,
            "limit_tokens": 0,
        },
        "monthly": {
            "calls": 9,
            "tokens": 4000,
            "limit_calls": 0,
            "limit_tokens": 0,
        },
        "cost": {
            "cost": 0.01,
            "cost_limit": 0.0,
            "currency": "CNY",
            "estimated": True,
        },
        "warnings": [{"scope": "per_chat", "ratio": 0.5, "calls": 100, "limit": 200}],
    }
    seen: dict[str, str] = {}

    async def budget_endpoint(request: web.Request) -> web.Response:
        seen.update(dict(request.query))
        return web.json_response(payload)

    bot_app = web.Application()
    bot_app.router.add_get("/api/budget", budget_endpoint)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _supervisor, _ops, _audit = make_app(
            tmp_path, auth=make_auth(True), bot_url=url
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await login(client)
            resp = await client.get("/api/budget?chat_type=group&chat_id=1")
            data = await resp.json()
        finally:
            await client.close()
    finally:
        await bot_client.close()

    assert resp.status == 200
    assert seen == {"chat_type": "group", "chat_id": "1"}
    assert data == payload
    assert {"per_chat", "global_daily", "monthly", "cost", "warnings"} <= set(data)


async def _start_user_delete_bot(handler):
    bot_app = web.Application()
    bot_app.router.add_delete(
        "/api/memory/{chat_type}/{chat_id}/user/{user_id}", handler
    )
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    return bot_client, url


async def test_user_delete_missing_confirm_does_not_call_bot(tmp_path: Path) -> None:
    calls: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        calls.append(dict(request.match_info))
        return web.json_response({"ok": True, "deleted_messages": 0})

    bot_client, url = await _start_user_delete_bot(handler)
    try:
        app, _supervisor, _ops, audit = make_app(tmp_path, bot_url=url)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            no_body = await client.delete("/api/memory/group/100/user/7")
            empty_body = await client.delete(
                "/api/memory/group/100/user/7", json={}
            )
        finally:
            await client.close()
    finally:
        await bot_client.close()

    assert no_body.status == 400
    assert empty_body.status == 400
    assert calls == []
    assert all(row["action"] != "memory.user_delete" for row in audit.query(20))


async def test_user_delete_wrong_confirm_does_not_call_bot(tmp_path: Path) -> None:
    calls: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        calls.append(dict(request.match_info))
        return web.json_response({"ok": True, "deleted_messages": 1})

    bot_client, url = await _start_user_delete_bot(handler)
    try:
        app, _supervisor, _ops, audit = make_app(tmp_path, bot_url=url)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.delete(
                "/api/memory/group/100/user/7", json={"confirm": "delete_user"}
            )
            data = await resp.json()
        finally:
            await client.close()
    finally:
        await bot_client.close()

    assert resp.status == 400
    assert "DELETE_USER" in data["error"]
    assert calls == []
    assert all(row["action"] != "memory.user_delete" for row in audit.query(20))


async def test_user_delete_forwards_confirmation_and_writes_audit(
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["method"] = request.method
        seen["path"] = request.path
        seen["body"] = await request.json()
        # 即使 bot 多返回内容，审计也不该抄进去。
        return web.json_response(
            {
                "ok": True,
                "deleted_messages": 3,
                "content": "不能进审计的消息正文",
                "nickname": "不能进审计的昵称",
            }
        )

    bot_client, url = await _start_user_delete_bot(handler)
    try:
        app, _supervisor, _ops, audit = make_app(tmp_path, bot_url=url)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.delete(
                "/api/memory/group/100/user/7", json={"confirm": "DELETE_USER"}
            )
            data = await resp.json()
        finally:
            await client.close()
    finally:
        await bot_client.close()

    assert resp.status == 200
    assert data["ok"] is True
    assert data["deleted_messages"] == 3
    assert seen == {
        "method": "DELETE",
        "path": "/api/memory/group/100/user/7",
        "body": {"confirm": "DELETE_USER"},
    }

    rows = [row for row in audit.query(20) if row["action"] == "memory.user_delete"]
    assert len(rows) == 1
    assert rows[0]["target"] == "group/100/user/7"
    assert rows[0]["detail"] == {
        "deleted_messages": 3,
        "chat_type": "group",
        "chat_id": "100",
        "user_id": "7",
    }
    raw_audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "不能进审计的消息正文" not in raw_audit
    assert "不能进审计的昵称" not in raw_audit


async def test_user_delete_passes_bot_error_without_success_audit(
    tmp_path: Path,
) -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"error": "删除失败：数据库忙"}, status=409)

    bot_client, url = await _start_user_delete_bot(handler)
    try:
        app, _supervisor, _ops, audit = make_app(tmp_path, bot_url=url)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.delete(
                "/api/memory/group/100/user/7", json={"confirm": "DELETE_USER"}
            )
            data = await resp.json()
        finally:
            await client.close()
    finally:
        await bot_client.close()

    assert resp.status == 409
    assert data == {"error": "删除失败：数据库忙"}
    assert all(row["action"] != "memory.user_delete" for row in audit.query(20))


async def test_user_delete_rejects_invalid_target_before_calling_bot(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        calls.append(dict(request.match_info))
        return web.json_response({"ok": True, "deleted_messages": 0})

    bot_client, url = await _start_user_delete_bot(handler)
    try:
        app, _supervisor, _ops, _audit = make_app(tmp_path, bot_url=url)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            results = [
                await client.delete(
                    "/api/memory/channel/100/user/7",
                    json={"confirm": "DELETE_USER"},
                ),
                await client.delete(
                    "/api/memory/group/0/user/7",
                    json={"confirm": "DELETE_USER"},
                ),
                await client.delete(
                    "/api/memory/group/100/user/0",
                    json={"confirm": "DELETE_USER"},
                ),
                await client.delete(
                    "/api/memory/group/100/user/-1",
                    json={"confirm": "DELETE_USER"},
                ),
            ]
        finally:
            await client.close()
    finally:
        await bot_client.close()

    assert [resp.status for resp in results] == [400, 400, 400, 400]
    assert calls == []


async def test_budget_passes_bot_error_status(tmp_path: Path) -> None:
    async def budget_endpoint(_: web.Request) -> web.Response:
        return web.json_response({"error": "chat_type 必须是 group 或 private"}, status=400)

    bot_app = web.Application()
    bot_app.router.add_get("/api/budget", budget_endpoint)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _supervisor, _ops, _audit = make_app(tmp_path, bot_url=url)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/budget?chat_type=channel&chat_id=1")
            data = await resp.json()
        finally:
            await client.close()
    finally:
        await bot_client.close()

    assert resp.status == 400
    assert "chat_type" in data["error"]
