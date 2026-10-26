from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor.auth import COOKIE_NAME, DashboardAuth
from dafeiyu.supervisor.dashboard import AUTH_KEY, create_app
from dafeiyu.supervisor.logs import LogHub

BOT_URL = "http://127.0.0.1:1"
PASSWORD = "correct-horse-battery"


class FakeSupervisor:
    def __init__(self) -> None:
        self.specs = {"opencode": object(), "bot": object(), "napcat": object()}
        self.calls: list[tuple[str, str]] = []

    def status(self):
        return []

    async def start(self, name):
        self.calls.append(("start", name))

    async def stop(self, name):
        self.calls.append(("stop", name))

    async def restart(self, name):
        self.calls.append(("restart", name))


def make_auth(**kwargs) -> DashboardAuth:
    options = {
        "enabled": True,
        "username": "admin",
        "password": PASSWORD,
        "session_seconds": 3600,
        "secret": b"test-secret-32-bytes-long-000000",
    }
    options.update(kwargs)
    return DashboardAuth(**options)


def make_app(tmp_path: Path, auth: DashboardAuth | None = None):
    supervisor = FakeSupervisor()
    app = create_app(
        supervisor,
        settings=None,
        logs=LogHub(tmp_path / "logs"),
        qr_path=tmp_path / "qrcode.png",
        bot_url=BOT_URL,
        auth=auth or make_auth(),
    )
    return app, supervisor


async def test_login_page_is_public_and_escapes_next(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/login?next=/api/status")
        body = await resp.text()
        assert resp.status == 200
        assert 'name="username"' in body
        assert 'name="password"' in body
        assert 'value="/api/status"' in body

        evil = await (await client.get("/login?next=//evil.example")).text()
        assert 'value="//evil.example"' not in evil
        assert 'value="/"' in evil
    finally:
        await client.close()


async def test_already_logged_in_login_page_redirects_home(tmp_path: Path) -> None:
    auth = make_auth()
    app, _ = make_app(tmp_path, auth=auth)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = auth.issue_cookie()
        resp = await client.get(
            "/login",
            headers={"Cookie": f"{COOKIE_NAME}={token}"},
            allow_redirects=False,
        )
    finally:
        await client.close()
    assert resp.status == 302
    assert resp.headers["Location"] == "/"


async def test_unauthenticated_page_redirects_and_api_returns_401(
    tmp_path: Path,
) -> None:
    app, supervisor = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        page = await client.get("/", allow_redirects=False)
        assert page.status == 302
        assert page.headers["Location"].startswith("/login?next=")

        for path in (
            "/api/health",
            "/api/status",
            "/api/backup",
            "/api/logs/stream?source=bot",
            "/api/auth/session",
        ):
            resp = await client.get(path)
            assert resp.status == 401, path
            assert await resp.json() == {"error": "unauthorized"}, path

        for path in ("/api/control/stop", "/api/auth/logout"):
            resp = await client.post(path, json={"target": "all"})
            assert resp.status == 401, path
        assert supervisor.calls == []

        favicon = await client.get("/favicon.ico")
        assert favicon.status == 204
    finally:
        await client.close()


async def test_create_app_derives_auth_from_settings(tmp_path: Path) -> None:
    dashboard = SimpleNamespace(
        auth_required=True,
        username="fish",
        password=PASSWORD,
        session_hours=2,
    )
    settings = SimpleNamespace(supervisor=SimpleNamespace(dashboard=dashboard))
    app = create_app(
        FakeSupervisor(),
        settings=settings,
        logs=LogHub(tmp_path / "logs"),
        qr_path=tmp_path / "qrcode.png",
        bot_url=BOT_URL,
    )
    auth = app[AUTH_KEY]
    assert auth.enabled is True
    assert auth.username == "fish"
    assert auth.session_seconds == 7200


async def test_login_sets_secure_cookie_and_grants_access(tmp_path: Path) -> None:
    auth = make_auth()
    app, _ = make_app(tmp_path, auth=auth)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": PASSWORD},
        )
        assert resp.status == 204
        cookie = resp.headers["Set-Cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        assert "Max-Age=3600" in cookie

        status = await client.get("/api/status")
        assert status.status == 200
        assert (await status.json())["processes"] == []

        session = await (await client.get("/api/auth/session")).json()
        assert session == {"enabled": True, "authenticated": True, "user": "admin"}

        control = await client.post("/api/control/stop", json={"target": "all"})
        assert control.status == 200
    finally:
        await client.close()


async def test_wrong_password_is_rate_limited(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for _ in range(5):
            resp = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong"},
            )
            assert resp.status == 401
            assert "用户名或密码错误" in (await resp.json())["error"]

        blocked = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": PASSWORD},
        )
        assert blocked.status == 429
    finally:
        await client.close()


async def test_tampered_and_expired_cookies_are_rejected(tmp_path: Path) -> None:
    auth = make_auth()
    app, _ = make_app(tmp_path, auth=auth)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = auth.issue_cookie()
        tampered = token[:-1] + ("x" if token[-1] != "x" else "y")
        resp = await client.get(
            "/api/status", headers={"Cookie": f"{COOKIE_NAME}={tampered}"}
        )
        assert resp.status == 401

        expired = auth.issue_cookie(now=time.time() - 4000)
        resp = await client.get(
            "/api/status", headers={"Cookie": f"{COOKIE_NAME}={expired}"}
        )
        assert resp.status == 401
    finally:
        await client.close()


async def test_logout_clears_cookie(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        login = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": PASSWORD},
        )
        assert login.status == 204
        assert (await client.get("/api/status")).status == 200

        logout = await client.post("/api/auth/logout")
        assert logout.status == 204
        assert "Max-Age=0" in logout.headers["Set-Cookie"]
        assert (await client.get("/api/status")).status == 401
    finally:
        await client.close()


async def test_disabled_auth_keeps_local_access_open(tmp_path: Path) -> None:
    app, _ = make_app(
        tmp_path,
        auth=DashboardAuth(enabled=False, username="", password=""),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/api/status")).status == 200
        login = await client.get("/login", allow_redirects=False)
        assert login.status == 302
        assert login.headers["Location"] == "/"
    finally:
        await client.close()
