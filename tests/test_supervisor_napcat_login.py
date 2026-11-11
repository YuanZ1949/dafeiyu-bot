"""NapCat 探测在「二次 401 / 非 dict 响应」时必须降级，不能带崩 /api/status。"""

from __future__ import annotations

import hashlib
from typing import Self

from aiohttp import web
from aiohttp.test_utils import TestServer

from dafeiyu.supervisor.napcat_login import NapCatLogin, WebUiAuth

WEBUI_TOKEN = "unit-test-token"
CREDENTIAL = "cred-1"


class _StubNapCat:
    """最小 WebUI：登录永远成功，CheckLoginStatus 按 mode 返回异常形状。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.login_calls = 0
        self.status_calls = 0
        self._server: TestServer | None = None

    async def __aenter__(self) -> Self:
        app = web.Application()
        app.router.add_post("/api/auth/login", self._login)
        app.router.add_post("/api/QQLogin/CheckLoginStatus", self._status)
        self._server = TestServer(app)
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            await self._server.close()

    @property
    def auth(self) -> WebUiAuth:
        assert self._server is not None
        return WebUiAuth(host="127.0.0.1", port=self._server.port, token=WEBUI_TOKEN)

    async def _login(self, request: web.Request) -> web.Response:
        self.login_calls += 1
        payload = await request.json()
        expected = hashlib.sha256((WEBUI_TOKEN + ".napcat").encode()).hexdigest()
        if payload.get("hash") != expected:
            return web.json_response({"code": -1, "message": "Unauthorized"})
        return web.json_response({"code": 0, "data": {"Credential": CREDENTIAL}})

    async def _status(self, request: web.Request) -> web.Response:
        self.status_calls += 1
        if self.mode == "unauthorized":
            return web.Response(status=401)
        return web.json_response([1, 2, 3])


async def test_second_unauthorized_returns_unreachable() -> None:
    """WebUI 重启后口令被改：重登仍 401，state() 应降级并缓存，而不是抛出。"""
    async with _StubNapCat("unauthorized") as stub:
        probe = NapCatLogin(stub.auth, ttl=60)
        try:
            state = await probe.state()
            # 第二次走缓存：失败结果也要缓存，避免每 0.x 秒重复打 NapCat。
            cached = await probe.state()
        finally:
            await probe.close()

    assert state["configured"] is True
    assert state["reachable"] is False
    assert state["logged_in"] is False
    assert state["error"] == "NapCat 认证失败（口令被拒）"
    assert cached["error"] == state["error"]
    assert stub.login_calls == 2
    assert stub.status_calls == 2


async def test_non_dict_response_returns_unreachable() -> None:
    """CheckLoginStatus 返回 JSON 数组时 _post 不能 AttributeError。"""
    async with _StubNapCat("non-object") as stub:
        probe = NapCatLogin(stub.auth, ttl=0)
        try:
            state = await probe.state()
        finally:
            await probe.close()

    assert state["configured"] is True
    assert state["reachable"] is False
    assert state["logged_in"] is False
    assert "NapCat 返回非对象" in state["error"]
    assert stub.status_calls == 1
