from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

from ..config import Settings
from .logs import SOURCES, LogHub
from .manager import Supervisor
from .napcat_login import NapCatLogin

log = logging.getLogger(__name__)

QR_STALE_SECONDS = 130
BOT_TIMEOUT = 2.0
BOT_SWITCH_TIMEOUT = 20.0
DASHBOARD_DIR = Path(__file__).with_name("dashboard")


@web.middleware
async def _same_origin(request: web.Request, handler):
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin")
        host = request.headers.get("Host")
        if origin and host and origin.split("//", 1)[-1] != host:
            return web.json_response({"error": "forbidden"}, status=403)
    return await handler(request)


def create_app(
    supervisor: Supervisor,
    settings: Settings,
    logs: LogHub,
    qr_path: Path,
    bot_url: str,
    token: str = "",
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
    napcat_login: NapCatLogin | None = None,
) -> web.Application:
    app = web.Application(middlewares=[_same_origin])
    closing = asyncio.Event()

    async def _mark_closing(_: web.Application) -> None:
        closing.set()

    app.on_shutdown.append(_mark_closing)

    # 复用同一个 aiohttp 会话：httpx 会为每个 Client 构 SSLContext，
    # 在 Windows 上会阻塞事件循环数秒，绝不能每次请求新建。
    session: dict[str, aiohttp.ClientSession] = {}

    def bot_session() -> aiohttp.ClientSession:
        if "client" not in session:
            session["client"] = aiohttp.ClientSession(
                base_url=bot_url,
                timeout=aiohttp.ClientTimeout(total=BOT_TIMEOUT),
                headers={"X-Control-Token": token} if token else {},
            )
        return session["client"]

    async def close_session(_: web.Application | None = None) -> None:
        client = session.pop("client", None)
        if client is not None and not client.closed:
            await client.close()
        if napcat_login is not None:
            await napcat_login.close()

    app.on_cleanup.append(close_session)

    async def bot_call(
        method: str, path: str, payload: Any = None, timeout: float | None = None
    ) -> tuple[int, Any]:
        kwargs: dict[str, Any] = {"json": payload}
        if timeout is not None:
            # 换脑要构造新 brain（httpx client 初始化在 Windows 上不是零成本），
            # 用 BOT_TIMEOUT 会误报超时，实际却切换成功了。
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        try:
            async with bot_session().request(method, path, **kwargs) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = {"error": "bot 返回非 JSON"}
                return status, data
        except asyncio.TimeoutError:
            return 504, {"error": "bot 响应超时"}
        except aiohttp.ClientError as exc:
            return 502, {"error": f"bot 不可达：{exc.__class__.__name__}"}

    async def bot_raw(method: str, path: str) -> tuple[int, bytes, dict[str, str]]:
        """导出类接口要原样转发 body 与下载头，不能先解析成 JSON。"""
        try:
            async with bot_session().request(method, path) as resp:
                body = await resp.read()
                headers = {}
                for name in ("Content-Type", "Content-Disposition"):
                    value = resp.headers.get(name)
                    if value:
                        headers[name] = value
                return resp.status, body, headers
        except asyncio.TimeoutError:
            return 504, b'{"error":"bot timeout"}', {
                "Content-Type": "application/json"
            }
        except aiohttp.ClientError as exc:
            message = f'{{"error":"bot 不可达：{exc.__class__.__name__}"}}'
            return 502, message.encode("utf-8"), {"Content-Type": "application/json"}

    async def index(_: web.Request) -> web.Response:
        return web.FileResponse(DASHBOARD_DIR / "index.html")

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def status(_: web.Request) -> web.Response:
        brain_task = asyncio.create_task(bot_call("GET", "/api/brain"))
        login_task = (
            asyncio.create_task(napcat_login.state())
            if napcat_login is not None
            else None
        )
        code, brain = await brain_task
        if code != 200 or not isinstance(brain, dict):
            brain = {"available": False, "provider": "", "model": "", "modes": []}
        else:
            brain = {**brain, "available": True}
        return web.json_response(
            {
                "processes": supervisor.status(),
                "brain": brain,
                "qr": _qr_meta(qr_path),
                "napcat": await login_task if login_task is not None else None,
            }
        )

    async def qr_image(_: web.Request) -> web.StreamResponse:
        if not qr_path.exists():
            return web.Response(status=204)
        return web.FileResponse(
            qr_path, headers={"Cache-Control": "no-store"}
        )

    async def logs_stream(request: web.Request) -> web.StreamResponse:
        source = request.query.get("source", "bot")
        if source not in SOURCES:
            return web.json_response({"error": f"未知日志源：{source}"}, status=400)
        try:
            tail = max(1, min(2000, int(request.query.get("tail", "200"))))
        except ValueError:
            tail = 200

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        for line in logs.tail(source, tail):
            await response.write(_sse(line))
        queue = logs.subscribe(source)
        idle = 0
        try:
            while not closing.is_set():
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    idle += 1
                    if idle >= 15:
                        await response.write(b": ping\n\n")
                        idle = 0
                    continue
                idle = 0
                await response.write(_sse(line))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            logs.unsubscribe(source, queue)
        return response

    async def get_brain(_: web.Request) -> web.Response:
        code, data = await bot_call("GET", "/api/brain")
        return web.json_response(data, status=code)

    async def post_brain(request: web.Request) -> web.Response:
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        code, data = await bot_call("POST", "/api/brain", payload, BOT_SWITCH_TIMEOUT)
        return web.json_response(data, status=code)

    async def models(_: web.Request) -> web.Response:
        code, data = await bot_call("GET", "/api/brain/models", None, BOT_SWITCH_TIMEOUT)
        return web.json_response(data, status=code)

    async def memory(request: web.Request) -> web.Response:
        path = "/api/memory"
        if request.query_string:
            path = f"{path}?{request.query_string}"
        code, data = await bot_call("GET", path)
        return web.json_response(data, status=code)

    async def proxy_get(request: web.Request, path: str) -> web.Response:
        if request.query_string:
            path = f"{path}?{request.query_string}"
        code, data = await bot_call("GET", path)
        return web.json_response(data, status=code)

    async def usage(request: web.Request) -> web.Response:
        return await proxy_get(request, "/api/usage")

    async def memory_search(request: web.Request) -> web.Response:
        return await proxy_get(request, "/api/memory/search")

    async def sessions(request: web.Request) -> web.Response:
        return await proxy_get(request, "/api/sessions")

    async def config(request: web.Request) -> web.Response:
        return await proxy_get(request, "/api/config")

    def _session_target(
        request: web.Request,
    ) -> tuple[str, str, web.Response | None]:
        chat_type = request.match_info.get("chat_type", "")
        chat_id = request.match_info.get("chat_id", "")
        if chat_type not in ("group", "private"):
            return "", "", web.json_response(
                {"error": "chat_type 必须是 group 或 private"}, status=400
            )
        if not chat_id.isdigit() or int(chat_id) <= 0:
            return "", "", web.json_response(
                {"error": "chat_id 必须是正整数"}, status=400
            )
        return chat_type, chat_id, None

    async def session_action(request: web.Request) -> web.Response:
        chat_type, chat_id, error = _session_target(request)
        if error is not None:
            return error
        action = request.match_info.get("action", "")
        if action not in ("new", "forget"):
            return web.json_response({"error": "未知会话操作"}, status=400)
        code, data = await bot_call(
            "POST", f"/api/sessions/{chat_type}/{chat_id}/{action}"
        )
        return web.json_response(data, status=code)

    async def session_delete(request: web.Request) -> web.Response:
        chat_type, chat_id, error = _session_target(request)
        if error is not None:
            return error
        code, data = await bot_call("DELETE", f"/api/sessions/{chat_type}/{chat_id}")
        return web.json_response(data, status=code)

    async def memory_export(request: web.Request) -> web.Response:
        path = "/api/memory/export"
        if request.query_string:
            path = f"{path}?{request.query_string}"
        status, body, headers = await bot_raw("GET", path)
        return web.Response(body=body, status=status, headers=headers)


    async def memory_summary(request: web.Request) -> web.Response:
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        code, data = await bot_call("POST", "/api/memory/summaries", payload)
        return web.json_response(data, status=code)

    async def memory_impressions(request: web.Request) -> web.Response:
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        code, data = await bot_call("POST", "/api/memory/impressions", payload)
        return web.json_response(data, status=code)

    async def memory_impression_delete(request: web.Request) -> web.Response:
        impression_id = request.match_info["impression_id"]
        code, data = await bot_call(
            "DELETE", f"/api/memory/impressions/{impression_id}"
        )
        return web.json_response(data, status=code)

    async def control(request: web.Request) -> web.Response:
        action = request.match_info["action"]
        if action not in ("start", "stop", "restart"):
            return web.json_response({"error": f"未知操作：{action}"}, status=400)
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        target = str(payload.get("target") or "all")
        names = list(supervisor.specs) if target == "all" else [target]
        if any(name not in supervisor.specs for name in names):
            return web.json_response({"error": f"未知进程：{target}"}, status=400)

        errors: list[str] = []
        for name in names:
            try:
                if action == "start":
                    await supervisor.start(name)
                elif action == "stop":
                    await supervisor.stop(name)
                else:
                    await supervisor.restart(name)
            except Exception as exc:  # 单个失败不影响其它进程
                errors.append(f"{name}: {exc}")
        result: dict[str, Any] = {
            "ok": not errors,
            "processes": supervisor.status(),
        }
        if errors:
            result["errors"] = errors
        return web.json_response(result, status=207 if errors else 200)

    async def shutdown(_: web.Request) -> web.Response:
        if on_shutdown is None:
            return web.json_response({"error": "不支持退出"}, status=400)
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, lambda: asyncio.create_task(on_shutdown()))
        return web.json_response({"ok": True, "message": "正在关闭所有进程"})

    app.router.add_get("/", index)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/logs/stream", logs_stream)
    app.router.add_get("/api/qr/image", qr_image)
    app.router.add_get("/api/brain", get_brain)
    app.router.add_post("/api/brain", post_brain)
    app.router.add_get("/api/brain/models", models)
    app.router.add_get("/api/memory", memory)
    app.router.add_get("/api/usage", usage)
    app.router.add_get("/api/memory/search", memory_search)
    app.router.add_get("/api/memory/export", memory_export)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_post("/api/sessions/{chat_type}/{chat_id}/{action}", session_action)
    app.router.add_delete("/api/sessions/{chat_type}/{chat_id}", session_delete)
    app.router.add_get("/api/config", config)
    app.router.add_post("/api/memory/summaries", memory_summary)
    app.router.add_post("/api/memory/impressions", memory_impressions)
    app.router.add_delete(
        "/api/memory/impressions/{impression_id}", memory_impression_delete
    )
    app.router.add_post("/api/control/{action}", control)
    app.router.add_post("/api/shutdown", shutdown)
    return app


def _qr_meta(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"exists": False, "age_seconds": None, "stale": True}
    import time

    age = time.time() - stat.st_mtime
    return {
        "exists": True,
        "age_seconds": round(age, 1),
        "stale": age > QR_STALE_SECONDS,
        "size": stat.st_size,
    }


def _sse(line: str) -> bytes:
    return f"data: {json.dumps(line, ensure_ascii=False)}\n\n".encode("utf-8")


async def _json_body(request: web.Request) -> dict | None:
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


async def serve(
    supervisor: Supervisor,
    settings: Settings,
    logs: LogHub,
    qr_path: Path,
    bot_url: str,
    token: str = "",
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
    napcat_login: NapCatLogin | None = None,
) -> web.AppRunner:
    app = create_app(
        supervisor, settings, logs, qr_path, bot_url, token, on_shutdown, napcat_login
    )
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=5.0)
    await runner.setup()
    site = web.TCPSite(
        runner, settings.supervisor.dashboard.host, settings.supervisor.dashboard.port
    )
    await site.start()
    log.info(
        "dashboard 已监听 http://%s:%s",
        settings.supervisor.dashboard.host,
        settings.supervisor.dashboard.port,
    )
    return runner
