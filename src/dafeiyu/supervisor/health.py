from __future__ import annotations

import asyncio
import inspect
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import aiohttp

from dafeiyu.config import Settings
from dafeiyu.timeutil import SHANGHAI

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
BOT_PROBE_TIMEOUT = 2.0


class HealthMonitor:
    """聚合 supervisor 进程、bot 健康、napcat 登录、磁盘与备份状态。

    只读探测，任何单点失败都降级为对应字段的错误标记，不向调用方抛异常。
    """

    def __init__(
        self,
        *,
        supervisor: Any,
        settings: Settings,
        bot_url: str,
        token: str = "",
        backups: Any = None,
        napcat_login: Callable[[], Any] | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
        disk_path: str | Path | None = None,
        timeout: float = BOT_PROBE_TIMEOUT,
    ) -> None:
        self.supervisor = supervisor
        self.settings = settings
        self.bot_url = (bot_url or "").rstrip("/")
        # 轮换 control token 后 dashboard 会直接更新这个属性，下一次探活立即用新值。
        self.token = token
        self.backups = backups
        self.napcat_login = napcat_login
        self._session = session
        self._owns_session = session is None
        self._clock = clock or (lambda: datetime.now(SHANGHAI).timestamp())
        self.disk_path = Path(disk_path or REPO_ROOT)
        self.timeout = timeout

    async def snapshot(self) -> dict[str, Any]:
        processes = self._processes()
        bot = await self._bot_health()
        napcat = await self._napcat_state()
        disk = self._disk()
        backups = self._backup_status()
        ok = all(item.get("status") != "failed" for item in processes.values()) and bool(
            bot.get("ok")
        )
        return {
            "ok": ok,
            "generated_at": _iso(self._clock()),
            "processes": processes,
            "bot": bot,
            "napcat": napcat,
            "disk": disk,
            "backups": backups,
        }

    async def close(self) -> None:
        if not self._owns_session or self._session is None:
            return
        session, self._session = self._session, None
        if not session.closed:
            await session.close()

    # ---- 各数据源 -------------------------------------------------
    def _processes(self) -> dict[str, dict[str, Any]]:
        try:
            states = self.supervisor.status()
        except Exception as exc:  # noqa: BLE001 - 状态读取失败不打断探活
            log.warning("读取进程状态失败：%s", exc)
            return {}
        return {
            str(item.get("name")): item
            for item in states
            if isinstance(item, dict) and item.get("name")
        }

    async def _session_or_create(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def _bot_health(self) -> dict[str, Any]:
        if not self.bot_url:
            return {"ok": False, "error": "未配置 bot 地址"}
        headers = {"X-Control-Token": self.token} if self.token else {}
        try:
            session = await self._session_or_create()
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with session.get(
                f"{self.bot_url}/api/health/detail",
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status >= 400:
                    return {"ok": False, "error": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "探活超时"}
        except Exception as exc:  # noqa: BLE001 - 连接失败只降级
            return {"ok": False, "error": exc.__class__.__name__}
        if not isinstance(data, dict):
            return {"ok": False, "error": "响应不是对象"}
        return data

    async def _napcat_state(self) -> dict[str, Any]:
        if self.napcat_login is None:
            return {"logged_in": None, "qr_age_seconds": None}
        try:
            value = self.napcat_login()
            if inspect.isawaitable(value):
                value = await value
            if not isinstance(value, dict):
                return {"logged_in": False, "qr_age_seconds": None}
            return {
                "logged_in": bool(value.get("logged_in")),
                "qr_age_seconds": value.get("qr_age_seconds"),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 NapCat 登录状态失败：%s", exc)
            return {
                "logged_in": False,
                "qr_age_seconds": None,
                "error": exc.__class__.__name__,
            }

    def _disk(self) -> dict[str, Any]:
        path = str(self.disk_path)
        try:
            usage = shutil.disk_usage(self.disk_path)
        except OSError as exc:
            return {
                "path": path,
                "free_bytes": None,
                "total_bytes": None,
                "free_percent": None,
                "error": exc.__class__.__name__,
            }
        total = int(usage.total)
        free = int(usage.free)
        return {
            "path": path,
            "free_bytes": free,
            "total_bytes": total,
            "free_percent": round(free / total * 100, 1) if total else 0.0,
        }

    def _backup_status(self) -> dict[str, Any]:
        if self.backups is None:
            return {"last_success_at": None, "last_error_at": None, "count": 0}
        try:
            status = self.backups.status()
        except Exception as exc:  # noqa: BLE001
            log.warning("读取备份状态失败：%s", exc)
            return {
                "last_success_at": None,
                "last_error_at": _iso(self._clock()),
                "last_error": exc.__class__.__name__,
                "count": 0,
                "running": False,
            }
        if not isinstance(status, dict):
            return {"last_success_at": None, "last_error_at": None, "count": 0}
        return {
            "last_success_at": status.get("last_success_at"),
            "last_error_at": status.get("last_error_at"),
            "count": int(status.get("count") or 0),
            "running": bool(status.get("running")),
        }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), SHANGHAI).isoformat(timespec="seconds")


__all__ = ["HealthMonitor", "BOT_PROBE_TIMEOUT"]
