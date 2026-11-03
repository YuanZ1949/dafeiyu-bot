from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor import control_token
from dafeiyu.supervisor.audit import AuditLog
from dafeiyu.supervisor.auth import DashboardAuth
from dafeiyu.supervisor.control_token import ControlToken
from dafeiyu.supervisor.dashboard import create_app
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.manager import ProcSpec
from dafeiyu.supervisor.ops import OpsServices

BOT_URL = "http://127.0.0.1:1"
PASSWORD = "correct-horse"


class FakeSupervisor:
    def __init__(self) -> None:
        self.specs = {"opencode": object(), "bot": object(), "napcat": object()}
        self.calls: list[tuple[str, str]] = []
        self.restarted: list[str] = []
        self._status = [
            {"name": "opencode", "status": "running", "pid": 11, "port": 24310},
            {"name": "bot", "status": "stopped", "pid": None, "port": 8788},
            {"name": "napcat", "status": "external", "pid": 22, "port": 8080},
        ]

    def status(self):
        return self._status

    async def start(self, name):
        self.calls.append(("start", name))

    async def stop(self, name):
        self.calls.append(("stop", name))

    async def restart(self, name):
        self.calls.append(("restart", name))
        self.restarted.append(name)


@asynccontextmanager
async def running(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def read_sse_event(response) -> list[str]:
    lines: list[str] = []
    while True:
        raw = await asyncio.wait_for(response.content.readline(), timeout=2.0)
        assert raw, "SSE 连接提前结束"
        text = raw.decode("utf-8").rstrip("\r\n")
        if not text:
            return lines
        lines.append(text)


def make_app(tmp_path: Path, supervisor=None, qr: Path | None = None, on_shutdown=None, bot_url: str = BOT_URL):
    supervisor = supervisor or FakeSupervisor()
    hub = LogHub(tmp_path / "logs")
    app = create_app(
        supervisor,
        settings=None,
        logs=hub,
        qr_path=qr or tmp_path / "qrcode.png",
        bot_url=bot_url,
        token="",
        on_shutdown=on_shutdown,
    )
    return app, supervisor, hub


async def test_index_serves_dashboard(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.get("/")
        text = await resp.text()
    assert resp.status == 200
    assert "大肥鱼控制台" in text
    assert "/api/control/" in text
    assert "memory-summaries" in text
    assert "群印象" in text
    assert "btn-more-memory" in text
    assert "memory-chat-filter" in text
    assert "MEMORY_PAGE_MAX" in text
    assert "timeline-drawer" in text
    assert "查看时间线" in text
    assert "btn-timeline-earlier" in text
    assert "timeline-metrics" in text
    assert "openTimeline" in text
    assert "定位" in text
    assert "log-level" in text
    assert "log-filter" in text
    assert "log-lines" in text
    assert "btn-log-pause" in text
    assert "btn-log-clear" in text
    assert "btn-log-download" in text
    assert "LOG_MAX_LINES" in text
    assert "trigger-debug" in text
    assert "debug-chat-type" in text
    assert "debug-chat-id" in text
    assert "debug-user-id" in text
    assert "debug-nickname" in text
    assert "debug-content" in text
    assert "debug-at-bot" in text
    assert "debug-reply-bot" in text
    assert "btn-debug-trigger" in text
    assert "debug-result" in text
    assert "debug-checks" in text
    assert "debug-metrics" in text
    assert "btn-logout" in text
    assert "loadAuthSession" in text
    assert "redirectToLogin" in text


async def test_index_has_budget_card_and_loader(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.get("/")
        text = await resp.text()

    assert resp.status == 200
    for anchor in (
        "budget-panel",
        "budget-global",
        "budget-monthly",
        "budget-cost",
        "budget-warnings",
        "budget-chat-type",
        "budget-chat-id",
        "btn-budget-refresh",
    ):
        assert f'id="{anchor}"' in text, anchor
    assert "/api/budget" in text
    assert "loadBudget" in text
    assert "budgetFillClass" in text
    assert "Math.min(100" in text
    assert "${usedText} / ${emptyText}" in text
    assert "resetBudget(" in text
    assert "budgetCount(global.calls)" in text
    assert "fmtTokens(global.tokens)" in text
    assert "escapeHtml(" in text
    assert "/^[A-Z]{3,8}$/" in text
    assert '$("btn-budget-refresh").addEventListener("click", loadBudget)' in text
    assert '$("budget-chat-type").addEventListener("change", loadBudget)' in text
    assert '$("budget-chat-id").addEventListener("change", loadBudget)' in text
    assert "估算" in text
    assert "未启用" in text
    assert "未设上限" in text


async def test_status_reports_processes_and_offline_brain(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/status")
        data = await resp.json()

    assert resp.status == 200
    assert [p["name"] for p in data["processes"]] == ["opencode", "bot", "napcat"]
    assert data["brain"]["available"] is False
    assert data["qr"]["exists"] is False


async def test_status_includes_qr_meta(tmp_path: Path) -> None:
    qr = tmp_path / "qrcode.png"
    qr.write_bytes(b"png")
    app, _, _ = make_app(tmp_path, qr=qr)
    async with running(app) as client:
        data = await (await client.get("/api/status")).json()
    assert data["qr"]["exists"] is True
    assert data["qr"]["stale"] is False


async def test_qr_image_returns_204_when_missing(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/qr/image")
    assert resp.status == 204


async def test_qr_image_serves_png(tmp_path: Path) -> None:
    qr = tmp_path / "qrcode.png"
    qr.write_bytes(b"\x89PNG")
    app, _, _ = make_app(tmp_path, qr=qr)
    async with running(app) as client:
        resp = await client.get("/api/qr/image")
        body = await resp.read()
    assert resp.status == 200
    assert body == b"\x89PNG"


async def test_control_start_all(tmp_path: Path) -> None:
    app, supervisor, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.post("/api/control/start", json={"target": "all"})
        data = await resp.json()

    assert resp.status == 200
    assert data["ok"] is True
    assert supervisor.calls == [
        ("start", "opencode"),
        ("start", "bot"),
        ("start", "napcat"),
    ]


async def test_control_single_target(tmp_path: Path) -> None:
    app, supervisor, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.post("/api/control/restart", json={"target": "bot"})
    assert resp.status == 200
    assert supervisor.calls == [("restart", "bot")]


async def test_control_reports_partial_failure(tmp_path: Path) -> None:
    class Failing(FakeSupervisor):
        async def start(self, name):
            await super().start(name)
            if name == "bot":
                raise RuntimeError("端口被占用")

    app, _supervisor, _ = make_app(tmp_path, supervisor=Failing())
    async with running(app) as client:
        resp = await client.post("/api/control/start", json={"target": "all"})
        data = await resp.json()

    assert resp.status == 207
    assert data["ok"] is False
    assert any("bot" in item for item in data["errors"])


async def test_control_rejects_unknown_target(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.post("/api/control/start", json={"target": "mysql"})
    assert resp.status == 400


async def test_control_rejects_unknown_action(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.post("/api/control/explode", json={"target": "all"})
    assert resp.status == 400


async def test_cross_origin_post_is_rejected(tmp_path: Path) -> None:
    app, supervisor, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.post(
            "/api/control/stop",
            json={"target": "all"},
            headers={"Origin": "http://evil.example"},
        )
    assert resp.status == 403
    assert supervisor.calls == []


async def test_cross_origin_patch_is_rejected(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.patch(
            "/api/memory/impressions/1",
            json={"content": "x"},
            headers={"Origin": "http://evil.example"},
        )
    assert resp.status == 403


async def test_logs_stream_rejects_unknown_source(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/logs/stream?source=mysql")
    assert resp.status == 400


async def test_logs_stream_ids_and_reconnect_resume(tmp_path: Path) -> None:
    app, _, hub = make_app(tmp_path)
    hub.write("bot", "one\ntwo")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        first = await client.get("/api/logs/stream?source=bot&tail=2")
        first_events = [await read_sse_event(first) for _ in range(2)]
        first_ids = [int(event[0].removeprefix("id: ")) for event in first_events]
        assert first_ids == [1, 2]
        assert "one" in first_events[0][1]
        assert "two" in first_events[1][1]
        first.close()

        hub.write("bot", "three\nfour")
        second = await client.get(
            "/api/logs/stream?source=bot&tail=200",
            headers={"Last-Event-ID": str(first_ids[-1])},
        )
        resumed = [await read_sse_event(second) for _ in range(2)]
        resumed_ids = [int(event[0].removeprefix("id: ")) for event in resumed]
        assert resumed_ids == [3, 4]
        assert "three" in resumed[0][1]
        assert "four" in resumed[1][1]
        assert all("one" not in event[1] for event in resumed)

        hub.write("bot", "five")
        live = await read_sse_event(second)
        assert live[0] == "id: 5"
        assert "five" in live[1]
        second.close()
    finally:
        await client.close()


async def test_logs_stream_resets_stale_last_event_id(tmp_path: Path) -> None:
    app, _, hub = make_app(tmp_path)
    hub.write("bot", "one\ntwo")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get(
            "/api/logs/stream?source=bot&tail=2",
            headers={"Last-Event-ID": "999"},
        )
        events = [await read_sse_event(resp) for _ in range(2)]
        assert [int(event[0].removeprefix("id: ")) for event in events] == [1, 2]

        hub.write("bot", "three")
        live = await read_sse_event(resp)
        assert live[0] == "id: 3"
        assert "three" in live[1]
        resp.close()
    finally:
        await client.close()


async def test_logs_download_streams_without_read_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    app, _, hub = make_app(tmp_path)
    hub.write("bot", "streamed-line")

    def fail_read_bytes(self):
        raise AssertionError("下载不应把整个日志文件读入内存")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    async with running(app) as client:
        resp = await client.get("/api/logs/download?source=bot")
        body = await resp.text()

    assert resp.status == 200
    assert "streamed-line" in body


async def test_logs_download_returns_current_file_as_attachment(
    tmp_path: Path,
) -> None:
    app, _, hub = make_app(tmp_path)
    hub.write("bot", "第一行\n第二行")
    async with running(app) as client:
        resp = await client.get("/api/logs/download?source=bot")
        body = await resp.text()

    assert resp.status == 200
    assert resp.content_type == "text/plain"
    assert resp.charset == "utf-8"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert 'filename="bot.log"' in resp.headers["Content-Disposition"]
    assert "第一行" in body
    assert "第二行" in body


async def test_logs_download_rejects_unknown_source_and_missing_file(
    tmp_path: Path,
) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        unknown = await client.get("/api/logs/download?source=mysql")
        traversal = await client.get("/api/logs/download?source=../bot")
        missing = await client.get("/api/logs/download?source=bot")

    assert unknown.status == 400
    assert traversal.status == 400
    assert missing.status == 404


async def test_shutdown_invokes_callback(tmp_path: Path) -> None:
    called = asyncio.Event()

    async def on_shutdown() -> None:
        called.set()

    app, _, _ = make_app(tmp_path, on_shutdown=on_shutdown)
    async with running(app) as client:
        resp = await client.post("/api/shutdown", json={})
        assert resp.status == 200
        await asyncio.wait_for(called.wait(), timeout=2.0)


async def test_shutdown_without_callback_is_rejected(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.post("/api/shutdown", json={})
    assert resp.status == 400

async def test_open_log_stream_does_not_block_cleanup(tmp_path: Path) -> None:
    """SSE 长连接不能挡住 runner.cleanup()，否则退出会卡满 shutdown_timeout。"""
    app, _, _ = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/logs/stream?source=bot&tail=5")
        assert resp.status == 200
    finally:
        await asyncio.wait_for(client.close(), timeout=5)

async def test_brain_switch_survives_slow_brain_build(tmp_path: Path) -> None:
    """换脑要构造新 brain，耗时远超 BOT_TIMEOUT；dashboard 不能误报 504。"""

    async def slow_brain(_: web.Request) -> web.Response:
        await asyncio.sleep(3)
        return web.json_response({"provider": "token", "model": "fake"})

    bot_app = web.Application()
    bot_app.router.add_post("/api/brain", slow_brain)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _, _ = make_app(tmp_path, bot_url=url)
        async with running(app) as client:
            resp = await client.post("/api/brain", json={"provider": "token"})
            assert resp.status == 200
            assert (await resp.json())["provider"] == "token"
    finally:
        await bot_client.close()

async def test_memory_proxies_bot_payload(tmp_path: Path) -> None:
    async def memory_endpoint(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "summaries": [
                    {
                        "chat_type": "group",
                        "chat_id": 100,
                        "date": "2026-10-24",
                        "content": "昨天聊了钓鱼",
                        "updated_at": "2026-10-25 01:00:00",
                    }
                ],
                "impressions": [],
            }
        )

    bot_app = web.Application()
    bot_app.router.add_get("/api/memory", memory_endpoint)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _, _ = make_app(tmp_path, bot_url=url)
        async with running(app) as client:
            resp = await client.get("/api/memory?limit=5")
            data = await resp.json()
        assert resp.status == 200
        assert data["summaries"][0]["content"] == "昨天聊了钓鱼"
        assert data["impressions"] == []
    finally:
        await bot_client.close()


async def test_memory_proxy_forwards_pagination_and_filter(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    async def memory_endpoint(request: web.Request) -> web.Response:
        seen.update(dict(request.query))
        return web.json_response({"summaries": [], "impressions": []})

    bot_app = web.Application()
    bot_app.router.add_get("/api/memory", memory_endpoint)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _, _ = make_app(tmp_path, bot_url=url)
        async with running(app) as client:
            resp = await client.get(
                "/api/memory?limit=5&offset=10&chat_type=group&chat_id=20001"
            )
        assert resp.status == 200
    finally:
        await bot_client.close()

    assert seen == {
        "limit": "5",
        "offset": "10",
        "chat_type": "group",
        "chat_id": "20001",
    }


async def test_memory_reports_bot_offline(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/memory")
        data = await resp.json()

    assert resp.status in (502, 504)  # Windows 上连 127.0.0.1:1 可能超时而不是立刻拒绝
    assert "bot" in data["error"]


class RotateSupervisor(FakeSupervisor):
    def __init__(self, token: str = "old-token") -> None:
        super().__init__()
        self.specs = {
            "opencode": object(),
            "bot": ProcSpec(
                name="bot",
                command=[],
                env={"DAFEIYU_CONTROL_TOKEN": token},
            ),
            "napcat": object(),
        }


class FakeHealth:
    def __init__(self, token: str = "") -> None:
        self.token = token

    async def snapshot(self) -> dict:
        return {"ok": True, "token": self.token}


def make_auth(enabled: bool = True) -> DashboardAuth:
    return DashboardAuth(
        enabled=enabled,
        username="admin",
        password=PASSWORD,
        session_seconds=3600,
        secret=b"test-secret-32-bytes-long-000000",
    )


async def login(client: TestClient) -> None:
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    assert resp.status == 204


def make_rotate_app(
    tmp_path: Path,
    monkeypatch,
    *,
    auth: DashboardAuth | None = None,
    token: str = "old-token",
    supervisor=None,
    bot_url: str = BOT_URL,
):
    monkeypatch.setattr(control_token, "harden_token_file", lambda _path: None)
    supervisor = supervisor or RotateSupervisor(token)
    audit = AuditLog(tmp_path / "audit.jsonl")
    health = FakeHealth(token)
    ops = OpsServices(health=health, audit=audit)
    holder = ControlToken(tmp_path / "control.token")
    holder.set(token)
    app = create_app(
        supervisor,
        settings=None,
        logs=LogHub(tmp_path / "logs"),
        qr_path=tmp_path / "qrcode.png",
        bot_url=bot_url,
        token=lambda: holder.value,
        auth=auth or make_auth(False),
        ops=ops,
        control_token=holder,
    )
    return app, supervisor, ops, audit, holder


async def test_rotate_token_requires_login(tmp_path: Path, monkeypatch) -> None:
    app, supervisor, _ops, _audit, holder = make_rotate_app(
        tmp_path, monkeypatch, auth=make_auth(True)
    )
    async with running(app) as client:
        resp = await client.post(
            "/api/control-token/rotate", json={"confirm": "ROTATE"}
        )

    assert resp.status == 401
    assert supervisor.calls == []
    assert holder.value == "old-token"


async def test_rotate_token_rejects_wrong_confirm(tmp_path: Path, monkeypatch) -> None:
    app, supervisor, ops, audit, holder = make_rotate_app(
        tmp_path, monkeypatch, auth=make_auth(True)
    )
    async with running(app) as client:
        await login(client)
        resp = await client.post(
            "/api/control-token/rotate", json={"confirm": "rotate"}
        )
        data = await resp.json()

    assert resp.status == 400
    assert "ROTATE" in data["error"]
    assert supervisor.calls == []
    assert holder.value == "old-token"
    assert ops.health.token == "old-token"
    assert all(row["action"] != "token.rotate" for row in audit.query(20))


async def test_rotate_token_updates_env_health_and_restarts(
    tmp_path: Path, monkeypatch
) -> None:
    app, supervisor, ops, audit, holder = make_rotate_app(
        tmp_path, monkeypatch, auth=make_auth(True)
    )
    async with running(app) as client:
        await login(client)
        resp = await client.post(
            "/api/control-token/rotate", json={"confirm": "ROTATE"}
        )
        data = await resp.json()

    assert resp.status == 200
    assert data == {"ok": True, "restarted": True}
    new = holder.value
    assert new and new != "old-token"
    assert supervisor.specs["bot"].env["DAFEIYU_CONTROL_TOKEN"] == new
    assert ops.health.token == new
    assert supervisor.restarted == ["bot"]
    assert supervisor.calls == [("restart", "bot")]
    assert (tmp_path / "control.token").read_text(encoding="utf-8") == new

    rows = [row for row in audit.query(20) if row["action"] == "token.rotate"]
    assert len(rows) == 1
    assert rows[0]["target"] == "control"
    assert rows[0]["detail"] == {"restarted": True}
    assert new not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


async def test_rotate_token_switches_dashboard_bot_header(
    tmp_path: Path, monkeypatch
) -> None:
    supervisor = RotateSupervisor()
    seen: list[str] = []

    async def brain_endpoint(request: web.Request) -> web.Response:
        expected = supervisor.specs["bot"].env["DAFEIYU_CONTROL_TOKEN"]
        token = request.headers.get("X-Control-Token", "")
        seen.append(token)
        if token != expected:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"ok": True})

    bot_app = web.Application()
    bot_app.router.add_get("/api/brain", brain_endpoint)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _supervisor, _ops, _audit, holder = make_rotate_app(
            tmp_path,
            monkeypatch,
            supervisor=supervisor,
            auth=make_auth(True),
            bot_url=url,
        )
        async with running(app) as client:
            await login(client)
            before = await client.get("/api/brain")
            assert before.status == 200
            old = holder.value

            rotated = await client.post(
                "/api/control-token/rotate", json={"confirm": "ROTATE"}
            )
            assert rotated.status == 200
            new = holder.value
            assert new != old

            after = await client.get("/api/brain")
            assert after.status == 200

            direct_old = await bot_client.get(
                "/api/brain", headers={"X-Control-Token": old}
            )
            direct_new = await bot_client.get(
                "/api/brain", headers={"X-Control-Token": new}
            )
    finally:
        await bot_client.close()

    assert direct_old.status == 401
    assert direct_new.status == 200
    assert seen[:2] == [old, new]


async def test_rotate_token_restart_failure_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    class FailingRestartSupervisor(RotateSupervisor):
        async def restart(self, name):
            self.restarted.append(name)
            raise RuntimeError("重启失败")

    supervisor = FailingRestartSupervisor()

    async def brain_endpoint(request: web.Request) -> web.Response:
        if request.headers.get("X-Control-Token") != "old-token":
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"ok": True})

    bot_app = web.Application()
    bot_app.router.add_get("/api/brain", brain_endpoint)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _supervisor, ops, audit, holder = make_rotate_app(
            tmp_path,
            monkeypatch,
            supervisor=supervisor,
            auth=make_auth(True),
            bot_url=url,
        )
        old = holder.value
        async with running(app) as client:
            await login(client)
            resp = await client.post(
                "/api/control-token/rotate", json={"confirm": "ROTATE"}
            )
            data = await resp.json()
            still_old = await client.get("/api/brain")
    finally:
        await bot_client.close()

    assert resp.status == 500
    assert "重启失败" in data["error"]
    assert holder.value == old
    assert (tmp_path / "control.token").read_text(encoding="utf-8") == old
    assert supervisor.specs["bot"].env["DAFEIYU_CONTROL_TOKEN"] == old
    assert ops.health.token == old
    assert supervisor.restarted == ["bot"]
    assert ("start", "bot") in supervisor.calls
    assert still_old.status == 200

    rows = [row for row in audit.query(20) if row["action"] == "token.rotate"]
    assert len(rows) == 1
    assert rows[0]["result"] == "error"
    assert rows[0]["detail"] == {"restarted": False}


async def test_index_has_security_rotate_controls(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        text = await (await client.get("/")).text()

    for anchor in (
        "btn-rotate-token",
        "rotate-token-confirm",
        "rotate-token-status",
    ):
        assert f'id="{anchor}"' in text, anchor
    assert "/api/control-token/rotate" in text
    assert "rotateControlToken" in text
    assert '$("btn-rotate-token").addEventListener("click", rotateControlToken)' in text
    assert "已轮换，bot 已重启" in text
    assert "轮换" in text and "重启" in text


async def test_rotate_token_write_failure_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    app, supervisor, ops, audit, holder = make_rotate_app(
        tmp_path, monkeypatch, auth=make_auth(True)
    )
    old = holder.value

    def failing_rotate() -> str:
        raise OSError("磁盘已满")

    monkeypatch.setattr(holder, "rotate", failing_rotate)

    async with running(app) as client:
        await login(client)
        resp = await client.post(
            "/api/control-token/rotate", json={"confirm": "ROTATE"}
        )
        data = await resp.json()

    assert resp.status == 500
    assert "磁盘已满" in data["error"]
    assert supervisor.restarted == []
    assert holder.value == old
    assert (tmp_path / "control.token").read_text(encoding="utf-8") == old
    assert supervisor.specs["bot"].env["DAFEIYU_CONTROL_TOKEN"] == old
    assert ops.health.token == old

    rows = [row for row in audit.query(20) if row["action"] == "token.rotate"]
    assert len(rows) == 1
    assert rows[0]["result"] == "error"
    assert rows[0]["detail"] == {"restarted": False}
    assert old not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


async def test_rotate_token_cancellation_is_audited(
    tmp_path: Path, monkeypatch
) -> None:
    class CancelledRestartSupervisor(RotateSupervisor):
        async def restart(self, name):
            self.restarted.append(name)
            raise asyncio.CancelledError()

    supervisor = CancelledRestartSupervisor()
    app, _supervisor, ops, audit, holder = make_rotate_app(
        tmp_path, monkeypatch, auth=make_auth(True), supervisor=supervisor
    )
    old = holder.value

    with pytest.raises((asyncio.CancelledError, aiohttp.ClientError)):
        async with running(app) as client:
            await login(client)
            await client.post(
                "/api/control-token/rotate", json={"confirm": "ROTATE"}
            )

    assert supervisor.restarted == ["bot"]
    assert holder.value == old
    assert supervisor.specs["bot"].env["DAFEIYU_CONTROL_TOKEN"] == old
    assert ops.health.token == old

    rows = [row for row in audit.query(20) if row["action"] == "token.rotate"]
    assert len(rows) == 1
    assert rows[0]["result"] == "error"
    assert rows[0]["detail"] == {"restarted": False}


async def test_rotate_token_serializes_concurrent_requests(
    tmp_path: Path, monkeypatch
) -> None:
    class SlowRestartSupervisor(RotateSupervisor):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def restart(self, name):
            self.restarted.append(name)
            self.entered.set()
            await self.release.wait()

    supervisor = SlowRestartSupervisor()
    app, _supervisor, _ops, _audit, holder = make_rotate_app(
        tmp_path, monkeypatch, auth=make_auth(True), supervisor=supervisor
    )

    async with running(app) as client:
        await login(client)
        first = asyncio.create_task(
            client.post("/api/control-token/rotate", json={"confirm": "ROTATE"})
        )
        await asyncio.wait_for(supervisor.entered.wait(), timeout=2.0)
        second = asyncio.create_task(
            client.post("/api/control-token/rotate", json={"confirm": "ROTATE"})
        )
        await asyncio.sleep(0.1)
        assert supervisor.restarted == ["bot"]  # 第二个请求被锁挡住，尚未重启
        supervisor.release.set()
        first_resp, second_resp = await asyncio.gather(first, second)

    assert first_resp.status == 200
    assert second_resp.status == 200
    assert supervisor.restarted == ["bot", "bot"]
    assert holder.value


async def test_index_has_per_user_delete_controls(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.get("/")
        text = await resp.text()

    assert resp.status == 200
    for anchor in (
        "delete-user-form",
        "delete-user-chat-type",
        "delete-user-id",
        "delete-user-confirm",
        "btn-delete-user",
    ):
        assert f'id="{anchor}"' in text, anchor
    assert "只删除该用户" in text
    assert "摘要与印象等聚合数据可能仍保留痕迹" in text
    assert "/forget" in text
    assert '$("delete-user-chat-type").value' in text
    assert "deleteUserMessages" in text
    assert 'method: "DELETE"' in text
    assert 'confirm: "DELETE_USER"' in text
    assert "/api/memory/${chatType}/${chatId}/user/${userId}" in text
    assert '$("delete-user-form").addEventListener("submit", deleteUserMessages)' in text


async def test_index_audit_rows_render_expandable_details(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.get("/")
        text = await resp.text()

    assert resp.status == 200
    assert 'createElement("details")' in text
    assert '<pre class="audit-detail">' in text
    assert "JSON.stringify(row.detail, null, 2)" in text
    assert "暂无审计记录" in text
    # 审计内容一律走 textContent，避免 detail 里的 HTML 被当标签执行。
    assert "summary.textContent" in text
    assert "pre.textContent" in text


async def test_index_has_channel_controls_and_string_ids(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async with running(app) as client:
        resp = await client.get("/")
        text = await resp.text()

    assert resp.status == 200
    assert 'id="btn-wechat-stop"' in text
    assert 'id="btn-wechat-resume"' in text
    assert 'data-channel="wechat"' in text
    assert 'id="health-channels"' in text
    assert 'Number($("debug-chat-id").value)' not in text
    assert 'Number($("debug-user-id").value)' not in text
    assert (
        re.search(
            r'id="(debug-chat-id|debug-user-id|memory-chat-id|delete-user-chat-id|delete-user-id|budget-chat-id|search-chat-id)"[^>]*type="number"',
            text,
        )
        is None
    )


async def test_channel_stop_resume_proxy(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    async def wechat_stop(request: web.Request) -> web.Response:
        seen.append((request.method, request.path))
        return web.json_response({"ok": True, "event": "stop"})

    async def wechat_resume(request: web.Request) -> web.Response:
        seen.append((request.method, request.path))
        return web.json_response({"error": "急停中，无法恢复"}, status=409)

    bot_app = web.Application()
    bot_app.router.add_post("/api/channels/wechat/stop", wechat_stop)
    bot_app.router.add_post("/api/channels/wechat/resume", wechat_resume)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _, _ = make_app(tmp_path, bot_url=url)
        async with running(app) as client:
            stop_resp = await client.post("/api/channels/wechat/stop", json={})
            stop_data = await stop_resp.json()
            resume_resp = await client.post("/api/channels/wechat/resume", json={})
            resume_data = await resume_resp.json()
    finally:
        await bot_client.close()

    assert stop_resp.status == 200
    assert stop_data == {"ok": True, "event": "stop"}
    assert resume_resp.status == 409
    assert resume_data == {"error": "急停中，无法恢复"}
    assert seen == [
        ("POST", "/api/channels/wechat/stop"),
        ("POST", "/api/channels/wechat/resume"),
    ]


async def test_session_timeline_rejects_traversal_without_calling_bot(
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, str]] = []

    async def timeline_endpoint(request: web.Request) -> web.Response:
        seen.append((request.method, request.path))
        return web.json_response(
            {"messages": [], "chat": {"chat_id": request.match_info["chat_id"]}}
        )

    bot_app = web.Application()
    bot_app.router.add_get(
        "/api/sessions/{chat_type}/{chat_id}/timeline", timeline_endpoint
    )
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _, _ = make_app(tmp_path, bot_url=url)
        async with running(app) as client:
            bad = await client.get("/api/sessions/group/%2e%2e%5cetc/timeline")
            invalid = await client.get("/api/sessions/group/bad%20id/timeline")
            invalid_data = await invalid.json()
            good = await client.get("/api/sessions/group/wxg:abc/timeline")
            good_data = await good.json()
    finally:
        await bot_client.close()

    assert bad.status == 400
    assert invalid.status == 400
    assert "格式非法" in invalid_data["error"]
    assert good.status == 200
    assert good_data["chat"]["chat_id"] == "wxg:abc"
    assert seen == [("GET", "/api/sessions/group/wxg:abc/timeline")]
