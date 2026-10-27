from __future__ import annotations

import os
import subprocess
import sys
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor import stop as stop_mod
from dafeiyu.supervisor.auth import COOKIE_NAME


def _fake_dashboard(*, login_status: int, require_cookie: bool):
    state = {"login": 0, "shutdown": 0}

    async def login(request: web.Request) -> web.Response:
        state["login"] += 1
        if login_status != 204:
            return web.json_response({"error": "登录未启用"}, status=login_status)
        response = web.Response(status=204)
        response.set_cookie(COOKIE_NAME, "session-token", path="/")
        return response

    async def shutdown(request: web.Request) -> web.Response:
        if require_cookie and request.cookies.get(COOKIE_NAME) != "session-token":
            return web.json_response({"error": "unauthorized"}, status=401)
        state["shutdown"] += 1
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/api/auth/login", login)
    app.router.add_post("/api/shutdown", shutdown)
    return app, state


async def _run_shutdown(login_status: int, require_cookie: bool):
    app, state = _fake_dashboard(
        login_status=login_status, require_cookie=require_cookie
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ok = await stop_mod.request_shutdown(
            "127.0.0.1",
            client.server.port,
            username="admin",
            password="x" * 12,
        )
    finally:
        await client.close()
    return ok, state


async def test_request_shutdown_logs_in_then_posts_with_cookie():
    ok, state = await _run_shutdown(login_status=204, require_cookie=True)

    assert ok is True
    assert state == {"login": 1, "shutdown": 1}


async def test_request_shutdown_skips_login_when_auth_disabled():
    ok, state = await _run_shutdown(login_status=400, require_cookie=False)

    assert ok is True
    assert state == {"login": 1, "shutdown": 1}


async def test_request_shutdown_returns_false_on_bad_credentials():
    ok, state = await _run_shutdown(login_status=401, require_cookie=False)

    assert ok is False
    assert state == {"login": 1, "shutdown": 0}


def test_read_pid_missing_and_garbage(tmp_path):
    path = tmp_path / "supervisor.pid"
    assert stop_mod.read_pid(path) is None

    path.write_text("not-a-pid", encoding="utf-8")
    assert stop_mod.read_pid(path) is None

    path.write_text(" 4242 \n", encoding="utf-8")
    assert stop_mod.read_pid(path) == 4242


def test_pid_alive_for_self_and_unknown_pid():
    assert stop_mod.pid_alive(os.getpid()) is True
    assert stop_mod.pid_alive(999_999) is False


async def test_stop_reports_not_running_and_cleans_stale_pid(tmp_path):
    path = tmp_path / "supervisor.pid"
    path.write_text("999999", encoding="utf-8")

    result = await stop_mod.stop_supervisor(port=8787, pid_path=path)

    assert result.status == "not_running"
    assert result.stale is True
    assert not path.exists()


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )


def _wait_dead(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    try:
        proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=timeout)


async def test_stop_prefers_graceful_shutdown(tmp_path):
    proc = _spawn_sleeper()
    path = tmp_path / "supervisor.pid"
    path.write_text(str(proc.pid), encoding="utf-8")

    async def fake_shutdown(host, port, *, username, password):
        proc.terminate()
        return True

    try:
        result = await stop_mod.stop_supervisor(
            port=8787, pid_path=path, shutdown_fn=fake_shutdown
        )
    finally:
        _wait_dead(proc)

    assert result.status == "stopped"
    assert result.method == "shutdown"
    assert result.pid == proc.pid
    assert not path.exists()


async def test_stop_forces_when_shutdown_unavailable(tmp_path):
    proc = _spawn_sleeper()
    path = tmp_path / "supervisor.pid"
    path.write_text(str(proc.pid), encoding="utf-8")

    async def no_shutdown(host, port, *, username, password):
        return False

    started = time.monotonic()
    try:
        result = await stop_mod.stop_supervisor(
            port=8787, pid_path=path, shutdown_fn=no_shutdown, timeout=10.0
        )
    finally:
        _wait_dead(proc)

    assert result.status == "stopped"
    assert result.method == "forced"
    assert time.monotonic() - started < 15
    assert not path.exists()
