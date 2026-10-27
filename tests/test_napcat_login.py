from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor.dashboard import create_app
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.napcat_login import NapCatLogin, WebUiAuth, load_webui_auth

WEBUI_TOKEN = "unit-test-token"
CREDENTIAL = "cred-1"


class StubNapCat:
    """最小 NapCat WebUI，字段照实机抄。

    实机 CheckLoginStatus 只有 isLogin/loginPhase/coreReady/loginError 这些，
    **不含账号信息**；QQ 号与昵称在已登录之后要另外问 GetQQLoginInfo。
    """

    def __init__(
        self,
        *,
        uin: str = "",
        nickname: str = "",
        phase: str = "waiting_qrcode",
        reject_first_status: bool = False,
        info_available: bool = True,
        offline: bool = False,
    ):
        self.uin = uin
        self.nickname = nickname
        self.phase = phase
        self.logged_in = bool(uin)
        self.reject_first_status = reject_first_status
        self.info_available = info_available
        self.offline = offline
        self.login_calls = 0
        self.status_calls = 0
        self.info_calls = 0
        self._server: TestServer | None = None

    async def __aenter__(self) -> "StubNapCat":
        app = web.Application()
        app.router.add_post("/api/auth/login", self._login)
        app.router.add_post("/api/QQLogin/CheckLoginStatus", self._status)
        app.router.add_post("/api/QQLogin/GetQQLoginInfo", self._info)
        self._server = TestServer(app)
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc) -> None:
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
        if request.headers.get("Authorization") != f"Bearer {CREDENTIAL}":
            return web.Response(status=401)
        if self.reject_first_status and self.status_calls == 1:
            return web.Response(status=401)
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "isLogin": self.logged_in,
                    "isOffline": self.offline,
                    # 实机登录成功后报的是 ready，不是 online。
                    "loginPhase": "ready" if self.logged_in else self.phase,
                    "qrLoginAccepted": False,
                    "coreReady": self.logged_in,
                    "qrcodeurl": "https://txz.qq.com/p?k=stub",
                    "loginError": "",
                },
            }
        )

    async def _info(self, request: web.Request) -> web.Response:
        self.info_calls += 1
        if request.headers.get("Authorization") != f"Bearer {CREDENTIAL}":
            return web.Response(status=401)
        if not self.logged_in or not self.info_available:
            return web.json_response({"code": -1, "message": "QQ Is Not Logined"})
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "uid": "u_stub",
                    "uin": self.uin,
                    "nick": self.nickname,
                    "online": True,
                },
            }
        )


def test_load_webui_auth_maps_wildcard_host(tmp_path: Path) -> None:
    path = tmp_path / "webui.json"
    path.write_text(
        json.dumps({"host": "::", "port": 6099, "token": "abc"}), encoding="utf-8"
    )
    assert load_webui_auth(path) == WebUiAuth(host="127.0.0.1", port=6099, token="abc")


def test_load_webui_auth_rejects_missing_or_incomplete(tmp_path: Path) -> None:
    assert load_webui_auth(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"host": "::", "port": 0, "token": ""}), encoding="utf-8")
    assert load_webui_auth(bad) is None


async def test_state_reports_waiting_for_scan() -> None:
    async with StubNapCat() as stub:
        probe = NapCatLogin(stub.auth)
        try:
            state = await probe.state()
        finally:
            await probe.close()
    assert state["reachable"] is True
    assert state["logged_in"] is False
    assert state["phase"] == "waiting_qrcode"
    assert state["phase_label"] == "等待扫码"


async def test_state_reports_logged_in_qq() -> None:
    """实机路径：CheckLoginStatus 说已登录但没账号信息，得再问 GetQQLoginInfo。"""
    async with StubNapCat(uin="10001", nickname="飞鱼小号") as stub:
        probe = NapCatLogin(stub.auth)
        try:
            state = await probe.state()
        finally:
            await probe.close()
    assert state["logged_in"] is True
    assert state["offline"] is False
    assert state["qq"] == "10001"
    assert state["nickname"] == "飞鱼小号"
    assert state["phase"] == "ready"
    assert state["phase_label"] == "已登录"
    assert stub.info_calls == 1


async def test_state_exposes_half_offline() -> None:
    """isLogin=true 但 isOffline=true：进程和网关都正常，账号却没在推消息。"""
    async with StubNapCat(uin="10001", nickname="：）", offline=True) as stub:
        probe = NapCatLogin(stub.auth)
        try:
            state = await probe.state()
        finally:
            await probe.close()
    assert state["logged_in"] is True
    assert state["offline"] is True


async def test_state_maps_scanned_phase() -> None:
    async with StubNapCat(phase="qrcode_scanned") as stub:
        probe = NapCatLogin(stub.auth)
        try:
            state = await probe.state()
        finally:
            await probe.close()
    assert state["logged_in"] is False
    assert state["phase_label"] == "已扫码，等待手机确认"
    assert stub.info_calls == 0


async def test_state_survives_missing_account_info() -> None:
    """GetQQLoginInfo 报错时不能把登录态一起丢掉。"""
    async with StubNapCat(uin="10001", nickname="飞鱼小号", info_available=False) as stub:
        probe = NapCatLogin(stub.auth)
        try:
            state = await probe.state()
        finally:
            await probe.close()
    assert state["logged_in"] is True
    assert state["qq"] == ""
    assert state["nickname"] == ""


async def test_state_is_cached_within_ttl() -> None:
    async with StubNapCat() as stub:
        probe = NapCatLogin(stub.auth, ttl=60)
        try:
            await probe.state()
            await probe.state()
        finally:
            await probe.close()
    assert stub.status_calls == 1


async def test_state_degrades_when_webui_unreachable() -> None:
    probe = NapCatLogin(WebUiAuth(host="127.0.0.1", port=1, token=WEBUI_TOKEN))
    try:
        state = await probe.state()
    finally:
        await probe.close()
    assert state["configured"] is True
    assert state["reachable"] is False
    assert state["logged_in"] is False


async def test_state_relogins_after_credential_rejected() -> None:
    """WebUI 重启后旧 credential 失效：第一次 401，应自动重登再问一次。"""
    async with StubNapCat(reject_first_status=True) as stub:
        probe = NapCatLogin(stub.auth, ttl=0)
        try:
            state = await probe.state()
        finally:
            await probe.close()
    assert state["reachable"] is True
    assert stub.login_calls == 2
    assert stub.status_calls == 2


class _FakeSupervisor:
    specs = {"opencode": object(), "bot": object(), "napcat": object()}

    def status(self):
        return []


async def test_dashboard_status_exposes_napcat_login(tmp_path: Path) -> None:
    async with StubNapCat() as stub:
        probe = NapCatLogin(stub.auth)
        app = create_app(
            _FakeSupervisor(),
            settings=None,
            logs=LogHub(tmp_path / "logs"),
            qr_path=tmp_path / "qrcode.png",
            bot_url="http://127.0.0.1:1",
            napcat_login=probe,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/status")
            data = await resp.json()
            page = await (await client.get("/")).text()
        finally:
            await client.close()
    assert resp.status == 200
    assert data["napcat"]["reachable"] is True
    assert data["napcat"]["logged_in"] is False
    assert data["napcat"]["phase_label"] == "等待扫码"
    assert "login-state" in page
    assert "登录状态" in page
