from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

import aiohttp
from aiohttp import web

from .brain.factory import (
    build_brain,
    current_provider,
    local_model_name,
    mode_status,
    normalize_provider,
)
from .config import ConfigError, Settings
from .memory.store import Store
from .runtime import RuntimeState, load_runtime, save_runtime

log = logging.getLogger(__name__)

CLOSE_DELAY_SECONDS = 30.0
MODEL_PROBE_TIMEOUT = 3.0


class SwitchError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class BrainControl:
    """运行时换脑的唯一写入点。"""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        service: Any,
        adapter: Any,
        runtime_path: str = "data/runtime.json",
    ) -> None:
        self.settings = settings
        self.store = store
        self.service = service
        self.adapter = adapter
        self.runtime_path = runtime_path
        self.runtime: RuntimeState = load_runtime(runtime_path)
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        provider = current_provider(self.settings, self.runtime)
        if provider == "local_model":
            model = local_model_name(self.settings, self.runtime)
        elif provider == "token":
            model = self.settings.model
        else:
            model = ""
        return {
            "provider": provider,
            "model": model,
            "modes": mode_status(self.settings, self.runtime),
            "gateway_connected": bool(getattr(self.adapter, "connected", False)),
        }

    async def switch(self, provider: str, model: str = "") -> dict[str, Any]:
        target = normalize_provider(provider)
        model = (model or "").strip()
        async with self._lock:
            before = current_provider(self.settings, self.runtime)
            before_model = (
                local_model_name(self.settings, self.runtime)
                if before == "local_model"
                else ""
            )
            if target == before and (target != "local_model" or (model or before_model) == before_model):
                return self.snapshot()

            try:
                brain = build_brain(self.settings, self.store, target, model)
            except ConfigError as exc:
                raise SwitchError(409, str(exc)) from exc
            except Exception as exc:  # 构造期的其它失败同样不该打断现有大脑
                log.exception("构建 %s brain 失败", target)
                raise SwitchError(500, f"构建 {target} 失败：{exc}") from exc

            previous = self.service.set_brain(brain)
            self.runtime = self.runtime.with_provider(target, model)
            try:
                save_runtime(self.runtime, self.runtime_path)
            except OSError as exc:
                log.warning("runtime.json 写入失败：%s", exc)
            asyncio.create_task(self._close_later(previous))
            log.info("大脑已切换：%s -> %s", before, target)
            return self.snapshot()

    async def _close_later(self, brain: Any) -> None:
        await asyncio.sleep(CLOSE_DELAY_SECONDS)
        closer = getattr(brain, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:
            log.debug("关闭旧 brain 失败", exc_info=True)

    def memory_snapshot(self, limit: int = 20) -> dict[str, Any]:
        """dashboard 只读记忆面板的数据源：最近摘要 + 最近印象。"""
        limit = max(1, min(200, int(limit)))
        return {
            "summaries": [
                {
                    "chat_type": s.chat_type.value,
                    "chat_id": s.chat_id,
                    "date": s.date,
                    "content": s.content,
                    "updated_at": s.updated_at,
                }
                for s in self.store.recent_summaries(limit)
            ],
            "impressions": [
                {
                    "chat_type": i.chat_type.value,
                    "chat_id": i.chat_id,
                    "subject_user_id": i.subject_user_id,
                    "kind": i.kind,
                    "content": i.content,
                    "confidence": i.confidence,
                    "updated_at": i.updated_at,
                }
                for i in self.store.recent_impressions(limit)
            ],
        }

    async def list_models(self) -> list[str]:
        base = self.settings.local_model.base_url.rstrip("/")
        if not base:
            raise SwitchError(409, "未配置 local_model.base_url")
        url = f"{base}/models"
        timeout = aiohttp.ClientTimeout(total=MODEL_PROBE_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.get(url) as resp:
                    status = resp.status
                    payload = await resp.json(content_type=None) if status < 400 else None
        except asyncio.TimeoutError as exc:
            raise SwitchError(
                504, f"LM Studio 在 {MODEL_PROBE_TIMEOUT:.0f}s 内没有响应"
            ) from exc
        except aiohttp.ClientError as exc:
            raise SwitchError(
                502, f"连不上 LM Studio（{base}）：{exc.__class__.__name__}"
            ) from exc
        except ValueError as exc:
            raise SwitchError(502, "LM Studio 返回格式异常") from exc
        if status >= 400:
            raise SwitchError(502, f"LM Studio 返回 HTTP {status}")
        try:
            items = (payload or {}).get("data") or []
            return [str(item["id"]) for item in items if item.get("id")]
        except (KeyError, TypeError, AttributeError) as exc:
            raise SwitchError(502, "LM Studio 返回格式异常") from exc


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path == "/api/health":
        return await handler(request)
    token = request.app.get(TOKEN_KEY, "")
    if token and request.headers.get("X-Control-Token", "") != token:
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


TOKEN_KEY: web.AppKey[str] = web.AppKey("control_token", str)


def create_app(control: BrainControl, token: str = "") -> web.Application:
    app = web.Application(middlewares=[_auth_middleware])
    app[TOKEN_KEY] = token

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def status(_: web.Request) -> web.Response:
        return web.json_response(control.snapshot())

    async def get_brain(_: web.Request) -> web.Response:
        return web.json_response(control.snapshot())

    async def post_brain(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"error": "请求体必须是 JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "请求体必须是对象"}, status=400)
        provider = str(payload.get("provider") or "")
        model = str(payload.get("model") or "")
        if normalize_provider(provider) not in ("opencode", "token", "local_model"):
            return web.json_response({"error": f"未知模式：{provider}"}, status=400)
        try:
            return web.json_response(await control.switch(provider, model))
        except SwitchError as exc:
            return web.json_response({"error": exc.reason}, status=exc.status)

    async def models(_: web.Request) -> web.Response:
        try:
            return web.json_response({"models": await control.list_models()})
        except SwitchError as exc:
            return web.json_response({"error": exc.reason}, status=exc.status)

    async def memory(request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            return web.json_response({"error": "limit 必须是整数"}, status=400)
        try:
            return web.json_response(control.memory_snapshot(limit))
        except Exception as exc:  # 读库失败不能把控制口打挂
            log.warning("读取记忆失败：%s", exc)
            return web.json_response(
                {"error": f"读取记忆失败：{exc.__class__.__name__}"}, status=500
            )

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/brain", get_brain)
    app.router.add_post("/api/brain", post_brain)
    app.router.add_get("/api/brain/models", models)
    app.router.add_get("/api/memory", memory)
    return app


async def start_control_server(
    settings: Settings,
    store: Store,
    service: Any,
    adapter: Any,
    runtime_path: str = "data/runtime.json",
) -> web.AppRunner | None:
    """按环境变量启动控制口；返回 Runner 供退出时 cleanup。"""
    raw_port = os.environ.get("DAFEIYU_CONTROL_PORT", "").strip()
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError("DAFEIYU_CONTROL_PORT 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("DAFEIYU_CONTROL_PORT 必须在 1-65535 之间")
    host = os.environ.get("DAFEIYU_CONTROL_HOST", "127.0.0.1").strip() or "127.0.0.1"
    token = os.environ.get("DAFEIYU_CONTROL_TOKEN", "").strip()

    control = BrainControl(settings, store, service, adapter, runtime_path)
    app = create_app(control, token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except OSError as exc:
        with contextlib.suppress(Exception):
            await runner.cleanup()
        raise ConfigError(f"控制口 {host}:{port} 无法监听：{exc}") from exc
    log.info("控制口已监听 http://%s:%s", host, port)
    return runner
