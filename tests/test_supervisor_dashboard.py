from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor.dashboard import create_app
from dafeiyu.supervisor.logs import LogHub

BOT_URL = "http://127.0.0.1:1"


class FakeSupervisor:
    def __init__(self) -> None:
        self.specs = {"opencode": object(), "bot": object(), "napcat": object()}
        self.calls: list[tuple[str, str]] = []
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


@asynccontextmanager
async def running(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


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

    app, supervisor, _ = make_app(tmp_path, supervisor=Failing())
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
