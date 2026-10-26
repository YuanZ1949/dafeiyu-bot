from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp
from aiohttp import web

from ..config import Settings
from .auth import COOKIE_NAME, DashboardAuth, safe_next_path
from .backup import REASONS, BackupError
from .control_token import ControlToken
from .logs import SOURCES, LogHub
from .manager import Supervisor
from .napcat_login import NapCatLogin
from .ops import OpsServices
from .restore import RestoreError

log = logging.getLogger(__name__)

QR_STALE_SECONDS = 130
BOT_TIMEOUT = 2.0
BOT_SWITCH_TIMEOUT = 20.0
# 删远端 session 要等 OpenCode 收尾，换脑/导出/整库备份更慢，不能沿用 2 秒。
BOT_DELETE_TIMEOUT = 20.0
BOT_BACKUP_TIMEOUT = 30.0
MAX_AUDIT_LIMIT = 200
DEFAULT_MAX_UPLOAD_MB = 512
DASHBOARD_DIR = Path(__file__).with_name("dashboard")


@web.middleware
async def _same_origin(request: web.Request, handler):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        host = request.headers.get("Host")
        if origin and host and origin.split("//", 1)[-1] != host:
            return web.json_response({"error": "forbidden"}, status=403)
    return await handler(request)


AUTH_KEY: web.AppKey[DashboardAuth] = web.AppKey("dashboard_auth", DashboardAuth)


def _wants_html(request: web.Request) -> bool:
    if request.path == "/" or request.path.endswith(".html"):
        return True
    if request.path.startswith("/api/"):
        return False
    return "text/html" in request.headers.get("Accept", "")


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    auth = request.app.get(AUTH_KEY)
    if auth is None or not auth.enabled or auth.is_public(request.path):
        return await handler(request)
    if auth.verify_cookie(request.cookies.get(COOKIE_NAME, "")):
        return await handler(request)
    if _wants_html(request):
        next_path = request.rel_url.raw_path
        if request.rel_url.raw_query_string:
            next_path = f"{next_path}?{request.rel_url.raw_query_string}"
        raise web.HTTPFound(f"/login?next={quote(next_path, safe='')}")
    return web.json_response({"error": "unauthorized"}, status=401)


def _auth_from_settings(settings: Settings | None) -> DashboardAuth:
    if settings is None:
        return DashboardAuth(enabled=False, username="", password="")
    dashboard = settings.supervisor.dashboard
    return DashboardAuth(
        enabled=dashboard.auth_required,
        username=dashboard.username,
        password=dashboard.password,
        session_seconds=dashboard.session_hours * 3600,
    )


def _is_https(request: web.Request) -> bool:
    if request.secure:
        return True
    forwarded = request.headers.get("X-Forwarded-Proto", "")
    return forwarded.split(",", 1)[0].strip().lower() == "https"


def create_app(
    supervisor: Supervisor,
    settings: Settings | None,
    logs: LogHub,
    qr_path: Path,
    bot_url: str,
    token: str | Callable[[], str] = "",
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
    napcat_login: NapCatLogin | None = None,
    auth: DashboardAuth | None = None,
    ops: OpsServices | None = None,
    control_token: ControlToken | None = None,
) -> web.Application:
    app = web.Application(middlewares=[_same_origin, _auth_middleware])
    auth_config = auth or _auth_from_settings(settings)
    app[AUTH_KEY] = auth_config
    closing = asyncio.Event()
    # 每个 app 独立一把锁，避免跨事件循环复用；只保护 token 轮换关键区。
    rotate_lock = asyncio.Lock()

    async def _mark_closing(_: web.Application) -> None:
        closing.set()

    app.on_shutdown.append(_mark_closing)

    def audit_actor(request: web.Request, username: str = "") -> str:
        user = username or (
            auth_config.username if auth_config.enabled else "local"
        )
        return f"dashboard:{user}@{request.remote or 'unknown'}"

    def write_audit(
        request: web.Request,
        action: str,
        *,
        actor: str = "",
        target: str = "",
        result: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> None:
        if ops is None or ops.audit is None:
            return
        ops.audit.write(
            action,
            actor=actor or audit_actor(request),
            target=target,
            result=result,
            detail=detail,
            ip=request.remote or "",
        )

    def ops_unavailable() -> web.Response:
        return web.json_response({"error": "运维服务未启用"}, status=503)

    token_provider: Callable[[], str] = (
        token if callable(token) else (lambda: token)
    )

    def control_headers() -> dict[str, str]:
        value = token_provider()
        return {"X-Control-Token": value} if value else {}

    # 复用同一个 aiohttp 会话：httpx 会为每个 Client 构 SSLContext，
    # 在 Windows 上会阻塞事件循环数秒，绝不能每次请求新建。
    session: dict[str, aiohttp.ClientSession] = {}

    def bot_session() -> aiohttp.ClientSession:
        if "client" not in session:
            # token 每次请求动态取（见 control_headers），这里不能写死默认 header，
            # 否则轮换后旧 token 会一直跟着复用会话。
            session["client"] = aiohttp.ClientSession(
                base_url=bot_url,
                timeout=aiohttp.ClientTimeout(total=BOT_TIMEOUT),
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
            async with bot_session().request(
                method, path, headers=control_headers(), **kwargs
            ) as resp:
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

    async def bot_raw(
        method: str, path: str, timeout: float | None = None
    ) -> tuple[int, bytes, dict[str, str]]:
        """导出/备份类接口要原样转发 body 与下载头，不能先解析成 JSON。"""
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        try:
            async with bot_session().request(
                method, path, headers=control_headers(), **kwargs
            ) as resp:
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

    async def favicon(_: web.Request) -> web.Response:
        return web.Response(status=204)

    async def login_page(request: web.Request) -> web.Response:
        if not auth_config.enabled:
            raise web.HTTPFound("/")
        if auth_config.verify_cookie(request.cookies.get(COOKIE_NAME, "")):
            raise web.HTTPFound("/")
        next_path = safe_next_path(request.query.get("next", ""))
        body = (DASHBOARD_DIR / "login.html").read_text(encoding="utf-8")
        body = body.replace("__NEXT__", html_lib.escape(next_path, quote=True))
        return web.Response(
            text=body,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def login_submit(request: web.Request) -> web.Response:
        if not auth_config.enabled:
            return web.json_response({"error": "登录未启用"}, status=400)
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        ip = request.remote or "unknown"
        if auth_config.is_rate_limited(ip):
            write_audit(
                request,
                "auth.login",
                result="error",
                detail={"reason": "rate_limited"},
            )
            return web.json_response(
                {"error": "尝试次数过多，请 5 分钟后再试"}, status=429
            )
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not auth_config.verify_credentials(username, password):
            auth_config.record_failure(ip)
            write_audit(
                request,
                "auth.login",
                actor=audit_actor(request, username or "anonymous"),
                result="error",
                detail={"reason": "bad_credentials", "username": username[:64]},
            )
            return web.json_response({"error": "用户名或密码错误"}, status=401)
        auth_config.clear_failures(ip)
        write_audit(
            request,
            "auth.login",
            actor=audit_actor(request, username),
            target=username,
        )
        response = web.Response(
            status=204, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            COOKIE_NAME,
            auth_config.issue_cookie(),
            max_age=auth_config.session_seconds,
            httponly=True,
            samesite="Strict",
            secure=_is_https(request),
            path="/",
        )
        return response

    async def logout(request: web.Request) -> web.Response:
        write_audit(request, "auth.logout")
        response = web.Response(
            status=204, headers={"Cache-Control": "no-store"}
        )
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def session_info(_: web.Request) -> web.Response:
        if not auth_config.enabled:
            return web.json_response(
                {"enabled": False, "authenticated": False, "user": ""}
            )
        return web.json_response(
            {
                "enabled": True,
                "authenticated": True,
                "user": auth_config.username,
            }
        )

    async def index(_: web.Request) -> web.Response:
        return web.FileResponse(DASHBOARD_DIR / "index.html")

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def health_detail(_: web.Request) -> web.Response:
        if ops is None:
            return ops_unavailable()
        try:
            snapshot = await ops.health.snapshot()
        except Exception as exc:  # noqa: BLE001 - 探活失败回错误，不打挂 dashboard
            log.warning("读取健康快照失败：%s", exc)
            return web.json_response(
                {"error": f"读取健康快照失败：{exc.__class__.__name__}"}, status=500
            )
        if not isinstance(snapshot, dict):
            snapshot = {"ok": False, "error": "健康快照格式异常"}
        alerts = (
            ops.alerts.summary()
            if ops.alerts is not None
            else {"firing": 0, "resolved_24h": 0}
        )
        return web.json_response({**snapshot, "alerts": alerts})

    async def alerts(request: web.Request) -> web.Response:
        if ops is None or ops.alerts is None:
            return ops_unavailable()
        try:
            return web.json_response(
                {
                    "events": ops.alerts.events(),
                    "summary": ops.alerts.summary(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("读取告警失败：%s", exc)
            return web.json_response(
                {"error": f"读取告警失败：{exc.__class__.__name__}"}, status=500
            )

    async def alerts_test(_: web.Request) -> web.Response:
        if ops is None or ops.alerts is None:
            return ops_unavailable()
        try:
            result = await ops.alerts.send_test()
        except Exception as exc:  # noqa: BLE001
            log.warning("测试告警失败：%s", exc)
            return web.json_response(
                {"error": f"测试告警失败：{exc.__class__.__name__}"}, status=500
            )
        return web.json_response({"ok": True, **result})

    async def backups(request: web.Request) -> web.Response:
        if ops is None or ops.backups is None:
            return ops_unavailable()
        if request.method == "GET":
            try:
                items = [asdict(item) for item in ops.backups.list()]
                return web.json_response(
                    {"backups": items, "status": ops.backups.status()}
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("读取备份列表失败：%s", exc)
                return web.json_response(
                    {"error": f"读取备份失败：{exc.__class__.__name__}"}, status=500
                )

        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        reason = str(payload.get("reason") or "manual")
        if reason not in REASONS:
            return web.json_response(
                {"error": f"reason 只能是 {'、'.join(REASONS)}"}, status=400
            )
        try:
            manifest = await ops.backups.create(reason)
        except BackupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            log.warning("创建备份失败：%s", exc)
            return web.json_response(
                {"error": f"创建备份失败：{exc.__class__.__name__}"}, status=500
            )
        write_audit(
            request,
            "backup.create",
            target=manifest.id,
            detail={"reason": manifest.reason, "filename": manifest.filename},
        )
        return web.json_response({"ok": True, "backup": asdict(manifest)})

    async def delete_backup(request: web.Request) -> web.Response:
        if ops is None or ops.backups is None:
            return ops_unavailable()
        payload = await _json_body(request)
        if payload is not None:
            confirm = str(payload.get("confirm") or "")
            if confirm and confirm != "DELETE":
                return web.json_response(
                    {"error": "confirm 必须是 DELETE"}, status=400
                )
        backup_id = request.match_info.get("backup_id", "")
        try:
            ops.backups.delete(backup_id)
        except BackupError as exc:
            message = str(exc)
            status = 404 if "不存在" in message else 400
            return web.json_response({"error": message}, status=status)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除备份失败：%s", exc)
            return web.json_response(
                {"error": f"删除备份失败：{exc.__class__.__name__}"}, status=500
            )
        write_audit(request, "backup.delete", target=backup_id)
        return web.json_response({"ok": True, "deleted": backup_id})

    async def restore_backup(request: web.Request) -> web.Response:
        if ops is None or ops.restore is None:
            return ops_unavailable()
        max_upload_mb = (
            settings.backup.max_upload_mb if settings is not None else DEFAULT_MAX_UPLOAD_MB
        )
        max_bytes = max(1, int(max_upload_mb)) * 1024 * 1024
        upload_path: Path | None = None
        confirm = ""
        too_large = False
        try:
            try:
                reader = await request.multipart()
            except Exception:  # noqa: BLE001 - 非 multipart 请求按 400 处理
                return web.json_response({"error": "请求必须是 multipart 表单"}, status=400)
            async for part in reader:
                if part.name == "file":
                    fd, tmp = tempfile.mkstemp(prefix="dafeiyu-restore-", suffix=".db")
                    upload_path = Path(tmp)
                    with os.fdopen(fd, "wb") as fh:
                        while True:
                            chunk = await part.read_chunk()
                            if not chunk:
                                break
                            if fh.tell() + len(chunk) > max_bytes:
                                too_large = True
                                break
                            fh.write(chunk)
                    if too_large:
                        break
                elif part.name == "confirm":
                    confirm = (await part.text()).strip()
        finally:
            if too_large and upload_path is not None:
                upload_path.unlink(missing_ok=True)
                upload_path = None
        if too_large:
            return web.json_response(
                {"error": f"上传文件不能超过 {max_upload_mb} MB"}, status=413
            )
        if upload_path is None:
            return web.json_response({"error": "缺少备份文件"}, status=400)
        if confirm != "RESTORE":
            upload_path.unlink(missing_ok=True)
            return web.json_response(
                {"error": "confirm 必须是 RESTORE"}, status=400
            )
        try:
            result = await ops.restore.restore(
                upload_path,
                actor=audit_actor(request),
                ip=request.remote or "",
                request_id=request.headers.get("X-Request-Id", ""),
            )
        except RestoreError as exc:
            return web.json_response(
                {"error": str(exc), "stage": exc.stage}, status=exc.status
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("恢复数据库失败：%s", exc)
            return web.json_response(
                {"error": f"恢复失败：{exc.__class__.__name__}"}, status=500
            )
        finally:
            upload_path.unlink(missing_ok=True)
        return web.json_response({"ok": True, "result": result})

    async def audit_log(request: web.Request) -> web.Response:
        if ops is None or ops.audit is None:
            return ops_unavailable()
        try:
            limit = max(
                1, min(MAX_AUDIT_LIMIT, int(request.query.get("limit", "50")))
            )
        except ValueError:
            return web.json_response({"error": "limit 必须是整数"}, status=400)
        before = (request.query.get("before") or "").strip() or None
        try:
            return web.json_response(
                {"items": ops.audit.query(limit=limit, before=before)}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("读取审计失败：%s", exc)
            return web.json_response(
                {"error": f"读取审计失败：{exc.__class__.__name__}"}, status=500
            )

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
        last_event_id = request.headers.get("Last-Event-ID", "").strip()
        try:
            after_seq = int(last_event_id) if last_event_id else None
        except ValueError:
            after_seq = None
        if after_seq is not None and after_seq > logs.last_seq(source):
            # 服务重启后序号会从 1 重新开始；旧页面带着更大的 Last-Event-ID
            # 重连时不能继续等更大的序号，否则会永远收不到新日志。
            after_seq = None

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        queue = logs.subscribe(source)
        initial = (
            logs.tail_events(source, tail)
            if after_seq is None
            else logs.events_after(source, after_seq)
        )
        last_sent = after_seq or 0
        idle = 0
        try:
            for seq, line in initial:
                await response.write(_sse(line, seq))
                last_sent = seq
            while not closing.is_set():
                try:
                    seq, line = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    idle += 1
                    if idle >= 15:
                        await response.write(b": ping\n\n")
                        idle = 0
                    continue
                if seq <= last_sent:
                    continue
                idle = 0
                await response.write(_sse(line, seq))
                last_sent = seq
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            logs.unsubscribe(source, queue)
        return response

    async def logs_download(request: web.Request) -> web.StreamResponse:
        source = request.query.get("source", "bot")
        if source not in SOURCES:
            return web.json_response({"error": f"未知日志来源：{source}"}, status=400)
        path = logs.log_path(source)
        if not path.is_file():
            return web.json_response({"error": "日志文件不存在"}, status=404)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{source}.log"',
                "Cache-Control": "no-store",
            },
        )

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

    async def budget(request: web.Request) -> web.Response:
        return await proxy_get(request, "/api/budget")

    async def memory_search(request: web.Request) -> web.Response:
        return await proxy_get(request, "/api/memory/search")

    async def sessions(request: web.Request) -> web.Response:
        return await proxy_get(request, "/api/sessions")

    async def debug_trigger(request: web.Request) -> web.Response:
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        code, data = await bot_call("POST", "/api/debug/trigger", payload)
        return web.json_response(data, status=code)

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

    def _memory_user_target(
        request: web.Request,
    ) -> tuple[str, str, str, web.Response | None]:
        chat_type, chat_id, error = _session_target(request)
        if error is not None:
            return "", "", "", error
        user_id = request.match_info.get("user_id", "")
        if not user_id.isdigit() or int(user_id) <= 0:
            return "", "", "", web.json_response(
                {"error": "user_id 必须是正整数"}, status=400
            )
        return chat_type, chat_id, user_id, None

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

    async def session_timeline(request: web.Request) -> web.Response:
        chat_type, chat_id, error = _session_target(request)
        if error is not None:
            return error
        return await proxy_get(
            request, f"/api/sessions/{chat_type}/{chat_id}/timeline"
        )

    async def session_delete(request: web.Request) -> web.Response:
        chat_type, chat_id, error = _session_target(request)
        if error is not None:
            return error
        code, data = await bot_call(
            "DELETE",
            f"/api/sessions/{chat_type}/{chat_id}",
            timeout=BOT_DELETE_TIMEOUT,
        )
        return web.json_response(data, status=code)

    async def memory_export(request: web.Request) -> web.Response:
        path = "/api/memory/export"
        if request.query_string:
            path = f"{path}?{request.query_string}"
        status, body, headers = await bot_raw(
            "GET", path, timeout=BOT_BACKUP_TIMEOUT
        )
        return web.Response(body=body, status=status, headers=headers)

    async def backup(_: web.Request) -> web.Response:
        status, body, headers = await bot_raw(
            "GET", "/api/backup", timeout=BOT_BACKUP_TIMEOUT
        )
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

    async def memory_impression_update(request: web.Request) -> web.Response:
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        impression_id = request.match_info["impression_id"]
        code, data = await bot_call(
            "PATCH", f"/api/memory/impressions/{impression_id}", payload
        )
        return web.json_response(data, status=code)

    async def memory_impression_merge(request: web.Request) -> web.Response:
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        code, data = await bot_call(
            "POST", "/api/memory/impressions/merge", payload
        )
        return web.json_response(data, status=code)

    async def memory_impression_delete(request: web.Request) -> web.Response:
        impression_id = request.match_info["impression_id"]
        code, data = await bot_call(
            "DELETE", f"/api/memory/impressions/{impression_id}"
        )
        return web.json_response(data, status=code)

    async def memory_user_delete(request: web.Request) -> web.Response:
        chat_type, chat_id, user_id, error = _memory_user_target(request)
        if error is not None:
            return error
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        if str(payload.get("confirm") or "") != "DELETE_USER":
            return web.json_response(
                {"error": "confirm 必须是 DELETE_USER"}, status=400
            )
        code, data = await bot_call(
            "DELETE",
            f"/api/memory/{chat_type}/{chat_id}/user/{user_id}",
            {"confirm": "DELETE_USER"},
            timeout=BOT_DELETE_TIMEOUT,
        )
        if not 200 <= code < 300:
            return web.json_response(data, status=code)
        raw_deleted = data.get("deleted_messages") if isinstance(data, dict) else 0
        deleted = (
            int(raw_deleted)
            if isinstance(raw_deleted, int) and not isinstance(raw_deleted, bool)
            else 0
        )
        write_audit(
            request,
            "memory.user_delete",
            target=f"{chat_type}/{chat_id}/user/{user_id}",
            detail={
                "deleted_messages": deleted,
                "chat_type": chat_type,
                "chat_id": int(chat_id),
                "user_id": int(user_id),
            },
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
        for name in names:
            failure = next(
                (item for item in errors if item.startswith(f"{name}:")), ""
            )
            write_audit(
                request,
                f"process.{action}",
                target=name,
                result="error" if failure else "ok",
                detail={"error": failure} if failure else None,
            )
        return web.json_response(result, status=207 if errors else 200)

    async def rotate_control_token(request: web.Request) -> web.Response:
        if control_token is None:
            return ops_unavailable()
        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        if str(payload.get("confirm") or "") != "ROTATE":
            return web.json_response({"error": "confirm 必须是 ROTATE"}, status=400)

        spec = supervisor.specs.get("bot")
        env = getattr(spec, "env", None)
        if not isinstance(env, dict):
            return web.json_response(
                {"error": "bot 进程未配置控制口令，无法轮换"}, status=500
            )

        async with rotate_lock:
            old = control_token.value
            old_env = env.get("DAFEIYU_CONTROL_TOKEN", old)
            health = ops.health if ops is not None else None
            old_health = health.token if health is not None else ""

            def rollback() -> None:
                # 即使 old 为空也要尝试回写，避免 holder / 磁盘 / env / health 分裂。
                try:
                    control_token.set(old)
                except Exception as rollback_exc:  # noqa: BLE001
                    log.critical("control token 回滚写盘失败：%s", rollback_exc)
                env["DAFEIYU_CONTROL_TOKEN"] = old_env
                if health is not None:
                    health.token = old_health

            restart_attempted = False
            try:
                new = control_token.rotate()
                env["DAFEIYU_CONTROL_TOKEN"] = new
                if health is not None:
                    health.token = new
                # 只有 bot 用新环境重启；NapCat / OpenCode 不动，且只调用一次。
                restart_attempted = True
                await supervisor.restart("bot")
            except asyncio.CancelledError:
                # 取消也必须留审计并回滚，但不能吞掉取消信号。
                rollback()
                write_audit(
                    request,
                    "token.rotate",
                    target="control",
                    result="error",
                    detail={"restarted": False},
                )
                raise
            except Exception as exc:  # noqa: BLE001 - 失败要回滚并返回 500
                rollback()
                log.critical("control token 轮换失败：%s", exc)
                if restart_attempted:
                    # restart 是 stop+start；start 失败时 bot 会停在停止状态。
                    # 旧口令已回滚，尽力用旧环境把 bot 拉回来。
                    try:
                        await supervisor.start("bot")
                    except Exception as recovery_exc:  # noqa: BLE001
                        log.critical("control token 回滚后 bot 恢复失败：%s", recovery_exc)
                write_audit(
                    request,
                    "token.rotate",
                    target="control",
                    result="error",
                    detail={"restarted": False},
                )
                return web.json_response(
                    {"error": f"轮换失败：{exc}"}, status=500
                )

            write_audit(
                request,
                "token.rotate",
                target="control",
                detail={"restarted": True},
            )
            return web.json_response({"ok": True, "restarted": True})

    async def shutdown(request: web.Request) -> web.Response:
        if on_shutdown is None:
            return web.json_response({"error": "不支持退出"}, status=400)
        write_audit(request, "shutdown", target="all")
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, lambda: asyncio.create_task(on_shutdown()))
        return web.json_response({"ok": True, "message": "正在关闭所有进程"})

    app.router.add_get("/", index)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/login", login_page)
    app.router.add_post("/api/auth/login", login_submit)
    app.router.add_post("/api/auth/logout", logout)
    app.router.add_get("/api/auth/session", session_info)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/health/detail", health_detail)
    app.router.add_get("/api/alerts", alerts)
    app.router.add_post("/api/alerts/test", alerts_test)
    app.router.add_get("/api/backups", backups)
    app.router.add_post("/api/backups", backups)
    app.router.add_post("/api/backups/restore", restore_backup)
    app.router.add_delete("/api/backups/{backup_id}", delete_backup)
    app.router.add_get("/api/audit", audit_log)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/logs/stream", logs_stream)
    app.router.add_get("/api/logs/download", logs_download)
    app.router.add_get("/api/qr/image", qr_image)
    app.router.add_get("/api/brain", get_brain)
    app.router.add_post("/api/brain", post_brain)
    app.router.add_get("/api/brain/models", models)
    app.router.add_get("/api/memory", memory)
    app.router.add_get("/api/usage", usage)
    app.router.add_get("/api/budget", budget)
    app.router.add_get("/api/memory/search", memory_search)
    app.router.add_get("/api/memory/export", memory_export)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_post("/api/debug/trigger", debug_trigger)
    app.router.add_get(
        "/api/sessions/{chat_type}/{chat_id}/timeline", session_timeline
    )
    app.router.add_post("/api/sessions/{chat_type}/{chat_id}/{action}", session_action)
    app.router.add_delete("/api/sessions/{chat_type}/{chat_id}", session_delete)
    app.router.add_get("/api/config", config)
    app.router.add_get("/api/backup", backup)
    app.router.add_post("/api/memory/summaries", memory_summary)
    app.router.add_post("/api/memory/impressions", memory_impressions)
    app.router.add_patch(
        "/api/memory/impressions/{impression_id}", memory_impression_update
    )
    app.router.add_post("/api/memory/impressions/merge", memory_impression_merge)
    app.router.add_delete(
        "/api/memory/impressions/{impression_id}", memory_impression_delete
    )
    app.router.add_delete(
        "/api/memory/{chat_type}/{chat_id}/user/{user_id}",
        memory_user_delete,
    )
    app.router.add_post("/api/control/{action}", control)
    app.router.add_post("/api/control-token/rotate", rotate_control_token)
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


def _sse(line: str, event_id: int | None = None) -> bytes:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}data: {json.dumps(line, ensure_ascii=False)}\n\n".encode("utf-8")


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
    token: str | Callable[[], str] = "",
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
    napcat_login: NapCatLogin | None = None,
    ops: OpsServices | None = None,
    control_token: ControlToken | None = None,
) -> web.AppRunner:
    app = create_app(
        supervisor,
        settings,
        logs,
        qr_path,
        bot_url,
        token,
        on_shutdown,
        napcat_login,
        ops=ops,
        control_token=control_token,
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
