from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[3]
WEBUI_CONFIG = REPO_ROOT / "vendor" / "napcat" / "napcat" / "config" / "webui.json"

# 扫码登录过程中 NapCat 依次报的阶段，映射成人话给 dashboard 用。
# 实机观察到的序列：waiting_qrcode → qrcode_scanned → ready（isLogin 同时变 true）。
# waiting_confirm / logging_in / online 是从 WebUI 前端反推出来的兼容值，一并留着。
PHASE_LABELS = {
    "": "",
    "waiting_qrcode": "等待扫码",
    "qrcode_scanned": "已扫码，等待手机确认",
    "waiting_confirm": "已扫码，等待手机确认",
    "logging_in": "正在登录",
    "login_failed": "登录失败",
    "ready": "已登录",
    "online": "已登录",
}


@dataclass(frozen=True)
class WebUiAuth:
    """NapCat WebUI 的本地访问参数。"""

    host: str
    port: int
    token: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_webui_auth(path: Path = WEBUI_CONFIG) -> WebUiAuth | None:
    """从 NapCat 的 webui.json 读取 host/port/token。

    读不到或字段不合法时返回 None——登录状态只是 dashboard 的附加信息，
    不能因为它坏掉就让整个控制台起不来。
    """
    import json

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    host = str(raw.get("host") or "").strip()
    token = str(raw.get("token") or "").strip()
    try:
        port = int(raw.get("port") or 0)
    except (TypeError, ValueError):
        return None
    if not token or not 0 < port < 65536:
        return None
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return WebUiAuth(host=host, port=port, token=token)


def _shape(raw: Any) -> dict[str, Any]:
    """把 NapCat 的 CheckLoginStatus 返回值压成 dashboard 需要的形状。"""
    if not isinstance(raw, dict):
        return {
            "reachable": True,
            "logged_in": False,
            "offline": False,
            "phase": "",
            "phase_label": "",
            "qq": "",
            "nickname": "",
            "error": "NapCat 返回了非预期数据",
        }
    qq = str(raw.get("uin") or raw.get("qq") or raw.get("uid") or "")
    nickname = str(raw.get("nick") or raw.get("nickname") or raw.get("nickName") or "")
    phase = str(raw.get("loginPhase") or "")
    return {
        "reachable": True,
        "logged_in": bool(raw.get("isLogin")),
        # 半离线：进程活着、网关连着、isLogin 仍为 true，但账号实际 offline，
        # 消息只进历史不推送。只看 isLogin 会把这种情况报成「已登录」。
        "offline": bool(raw.get("isOffline")),
        "phase": phase,
        "phase_label": PHASE_LABELS.get(phase, phase),
        "qq": qq,
        "nickname": nickname,
        "error": str(raw.get("loginError") or ""),
    }


def _unreachable(error: str) -> dict[str, Any]:
    return {
        "reachable": False,
        "logged_in": False,
        "offline": False,
        "phase": "",
        "phase_label": "",
        "qq": "",
        "nickname": "",
        "error": error,
    }


class NapCatLogin:
    """轮询 NapCat WebUI 的扫码登录状态。

    dashboard 每 1-2 秒拉一次 /api/status，这里用 TTL 缓存把真实探测频率
    压到 TTL 一次；探测失败只降级成 reachable=false，绝不让控制台报错。
    """

    def __init__(
        self,
        auth: WebUiAuth | None,
        *,
        ttl: float = 3.0,
        timeout: float = 2.0,
    ) -> None:
        self._auth = auth
        self._ttl = ttl
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._credential: str | None = None
        self._cached: dict[str, Any] | None = None
        self._fetched_at = 0.0

    @property
    def configured(self) -> bool:
        return self._auth is not None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            assert self._auth is not None
            self._session = aiohttp.ClientSession(
                base_url=self._auth.base_url,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._credential = None

    async def state(self) -> dict[str, Any]:
        if self._auth is None:
            return {**_unreachable("未找到 NapCat webui.json"), "configured": False}
        now = time.monotonic()
        if self._cached is not None and now - self._fetched_at < self._ttl:
            return self._cached
        try:
            value = await self._fetch()
        except TimeoutError:
            value = _unreachable("NapCat WebUI 响应超时")
        except aiohttp.ClientError as exc:
            value = _unreachable(f"NapCat WebUI 不可达：{exc.__class__.__name__}")
        except _Unauthorized:
            # 重登后仍被拒（口令/账号被改）：降级而不是让 /api/status 500。
            value = _unreachable("NapCat 认证失败（口令被拒）")
        except (ValueError, KeyError) as exc:
            value = _unreachable(f"NapCat WebUI 返回异常：{exc}")
        except Exception as exc:  # noqa: BLE001 - 任何探测异常都不该带崩面板
            value = _unreachable(f"NapCat WebUI 返回异常：{exc.__class__.__name__}")
        value["configured"] = True
        self._cached = value
        self._fetched_at = now
        return value

    async def _fetch(self) -> dict[str, Any]:
        if self._credential is None:
            await self._login()
        try:
            payload = await self._post("/QQLogin/CheckLoginStatus", {})
        except _Unauthorized:
            # WebUI 重启后旧 credential 失效，重新登录一次再问。
            await self._login()
            payload = await self._post("/QQLogin/CheckLoginStatus", {})
        state = _shape(payload)
        if state["logged_in"] and not state["qq"]:
            # CheckLoginStatus 里没有账号信息，已登录时再问一次 GetQQLoginInfo
            # （实机返回 {"uid","uin","nick","online"}）。拿不到就算了，不影响登录态。
            state.update(await self._account())
        return state

    async def _account(self) -> dict[str, str]:
        try:
            info = await self._post("/QQLogin/GetQQLoginInfo", {})
        except (TimeoutError, _Unauthorized, ValueError, aiohttp.ClientError):
            return {}
        if not isinstance(info, dict):
            return {}
        qq = str(info.get("uin") or info.get("uid") or "")
        nick = str(info.get("nick") or info.get("nickname") or info.get("nickName") or "")
        return {"qq": qq, "nickname": nick}

    async def _login(self) -> None:
        assert self._auth is not None
        digest = hashlib.sha256((self._auth.token + ".napcat").encode()).hexdigest()
        response = await self._request(
            "/auth/login", {"hash": digest, "totpCode": ""}, credential=None
        )
        data = response.get("data") if isinstance(response, dict) else None
        credential = data.get("Credential") if isinstance(data, dict) else None
        if not credential:
            raise ValueError("WebUI 登录未返回 Credential")
        self._credential = str(credential)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        response = await self._request(path, payload, credential=self._credential)
        if not isinstance(response, dict):
            raise ValueError("NapCat 返回非对象")
        if response.get("code") not in (0, "0", None):
            message = str(response.get("message") or "")
            if message == "Unauthorized":
                raise _Unauthorized()
            raise ValueError(message or "未知错误")
        return response.get("data")

    async def _request(
        self, path: str, payload: dict[str, Any], *, credential: str | None
    ) -> dict[str, Any]:
        headers = {}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        # aiohttp 的 base_url 只拼在主机后，路径必须自己带上 /api 前缀：
        # 相对路径 + 以 / 开头的 path 会丢掉 base_url 里的路径部分。
        async with self._client().post("/api" + path, json=payload, headers=headers) as resp:
            if resp.status in (401, 403):
                raise _Unauthorized()
            return await resp.json(content_type=None)


class _Unauthorized(Exception):
    pass
