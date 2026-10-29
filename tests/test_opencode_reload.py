"""切档自动生效：bot 用共享口令让 supervisor 重启 opencode。

没有这条通道时，切档只改了磁盘配置，运行中的 opencode 仍是旧模型——面板显示
「已切换」，实际没切。测试覆盖：口令放行、错口令拒绝、口令不能开别的接口、
客户端把各类失败收敛成 ``{"ok": False}``。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor.auth import DashboardAuth
from dafeiyu.supervisor.control_token import ControlToken
from dafeiyu.supervisor.dashboard import create_app
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.reload_client import reload_opencode

TOKEN = "ops-token-for-tests"


class FakeState:
    status = "running"

    def as_dict(self) -> dict:
        return {"name": "opencode", "pid": 4242, "status": "running"}


class FakeSupervisor:
    def __init__(self) -> None:
        self.states = {"opencode": FakeState()}
        self.restarted: list[str] = []

    async def restart(self, name: str) -> None:
        self.restarted.append(name)


def make_auth(enabled: bool = True) -> DashboardAuth:
    return DashboardAuth(
        enabled=enabled,
        username="admin",
        password="pw",
        session_seconds=3600,
        secret=b"test-secret-32-bytes-long-000000",
    )


@pytest.fixture
def token_holder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ControlToken:
    # 测试机不跑真实 icacls：这里只关心口令比对。
    import dafeiyu.supervisor.control_token as control_token_mod

    monkeypatch.setattr(control_token_mod, "harden_token_file", lambda path: None)
    holder = ControlToken(tmp_path / "supervisor.token")
    holder.set(TOKEN)
    return holder


def make_app(tmp_path: Path, holder: ControlToken) -> tuple[object, FakeSupervisor]:
    supervisor = FakeSupervisor()
    app = create_app(
        supervisor,
        settings=None,
        logs=LogHub(tmp_path / "logs"),
        qr_path=tmp_path / "qr.png",
        bot_url="http://127.0.0.1:1",
        auth=make_auth(True),
        control_token=holder,
    )
    return app, supervisor


async def test_ops_token_restarts_opencode_without_cookie(
    tmp_path: Path, token_holder: ControlToken
) -> None:
    app, supervisor = make_app(tmp_path, token_holder)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/opencode/reload", headers={"X-Control-Token": TOKEN}
        )
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "pid": 4242}
    finally:
        await client.close()

    assert supervisor.restarted == ["opencode"]


async def test_wrong_ops_token_is_rejected(
    tmp_path: Path, token_holder: ControlToken
) -> None:
    app, supervisor = make_app(tmp_path, token_holder)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/opencode/reload", headers={"X-Control-Token": "not-the-token"}
        )
        assert resp.status == 401
    finally:
        await client.close()

    assert supervisor.restarted == []


async def test_ops_token_does_not_open_other_api(
    tmp_path: Path, token_holder: ControlToken
) -> None:
    app, _supervisor = make_app(tmp_path, token_holder)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get(
            "/api/health/detail", headers={"X-Control-Token": TOKEN}
        )
        assert resp.status == 401
    finally:
        await client.close()


async def test_client_sends_token_and_returns_ack(tmp_path: Path) -> None:
    token_file = tmp_path / "supervisor.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-Control-Token", "")
        return httpx.Response(200, json={"ok": True, "pid": 7})

    result = await reload_opencode(
        8787, token_path=token_file, transport=httpx.MockTransport(handler)
    )

    assert result == {"ok": True, "pid": 7}
    assert seen["url"] == "http://127.0.0.1:8787/api/opencode/reload"
    assert seen["token"] == TOKEN


async def test_client_reports_missing_token(tmp_path: Path) -> None:
    result = await reload_opencode(8787, token_path=tmp_path / "missing.token")

    assert result["ok"] is False
    assert "口令" in str(result["error"])


async def test_client_maps_transport_error(tmp_path: Path) -> None:
    token_file = tmp_path / "supervisor.token"
    token_file.write_text(TOKEN, encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = await reload_opencode(
        8787, token_path=token_file, transport=httpx.MockTransport(handler)
    )

    assert result["ok"] is False
    assert "不可达" in str(result["error"])


async def test_client_maps_non_json_response(tmp_path: Path) -> None:
    token_file = tmp_path / "supervisor.token"
    token_file.write_text(TOKEN, encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="gateway timeout")

    result = await reload_opencode(
        8787, token_path=token_file, transport=httpx.MockTransport(handler)
    )

    assert result["ok"] is False
    assert "504" in str(result["error"])
