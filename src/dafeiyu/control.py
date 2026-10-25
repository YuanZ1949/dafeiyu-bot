from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Callable

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
from .memory.store import MAX_MEMORY_PAGE, Store
from .models import ChatType
from .runtime import RuntimeState, load_runtime, save_runtime
from .timeutil import SHANGHAI
from .workers.failure import QUOTA_KEY, MemoryPaused

log = logging.getLogger(__name__)

CLOSE_DELAY_SECONDS = 30.0
MODEL_PROBE_TIMEOUT = 3.0


class MemoryActions:
    """控制口的手动记忆操作；实现由启动方注入，未注入时三条路由返回 503。"""

    def __init__(
        self,
        summarize: Callable[..., Any],
        extract: Callable[..., Any],
        delete_impression: Callable[[int], bool],
    ) -> None:
        self.summarize = summarize
        self.extract = extract
        self.delete_impression = delete_impression


def _json_ok(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "请求体必须是对象"
    return None


async def _read_json_object(
    request: web.Request,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    try:
        payload = await request.json()
    except ValueError:
        return None, web.json_response({"error": "请求体必须是 JSON"}, status=400)
    error = _json_ok(payload)
    if error is not None:
        return None, web.json_response({"error": error}, status=400)
    return payload, None


def _chat_target(
    payload: dict[str, Any],
) -> tuple[ChatType | None, int | None, web.Response | None]:
    raw_type = str(payload.get("chat_type") or "").strip()
    if raw_type not in ("group", "private"):
        return None, None, web.json_response(
            {"error": "chat_type 必须是 group 或 private"}, status=400
        )
    try:
        chat_id = int(payload.get("chat_id"))
    except (TypeError, ValueError):
        return None, None, web.json_response(
            {"error": "chat_id 必须是整数"}, status=400
        )
    if chat_id <= 0:
        return None, None, web.json_response(
            {"error": "chat_id 必须是正整数"}, status=400
        )
    return ChatType(raw_type), chat_id, None


def _summary_date(payload: dict[str, Any]) -> tuple[str | None, web.Response | None]:
    raw = str(payload.get("date") or "").strip()
    if not raw:
        return (
            datetime.now(SHANGHAI).date() - timedelta(days=1)
        ).isoformat(), None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None, web.json_response(
            {"error": "date 必须是 YYYY-MM-DD"}, status=400
        )
    return raw, None


class SwitchError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _query_int(request: web.Request, name: str, default: int) -> int:
    raw = (request.query.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc


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

    def memory_snapshot(
        self,
        limit: int = 20,
        offset: int = 0,
        chat_type: ChatType | None = None,
        chat_id: int | None = None,
    ) -> dict[str, Any]:
        """dashboard 只读记忆面板的数据源：最近摘要 + 最近印象。"""
        limit = max(1, min(MAX_MEMORY_PAGE, int(limit)))
        offset = max(0, int(offset))
        summaries = [
            {
                "chat_type": s.chat_type.value,
                "chat_id": s.chat_id,
                "date": s.date,
                "content": s.content,
                "updated_at": s.updated_at,
            }
            for s in self.store.recent_summaries(limit, offset, chat_type, chat_id)
        ]
        impressions = [
            {
                "id": i.id,
                "chat_type": i.chat_type.value,
                "chat_id": i.chat_id,
                "subject_user_id": i.subject_user_id,
                "kind": i.kind,
                "content": i.content,
                "confidence": i.confidence,
                "updated_at": i.updated_at,
            }
            for i in self.store.recent_impressions(limit, offset, chat_type, chat_id)
        ]
        # 面板要显示「已加载 n / 共 m」，否则分页器只能靠「这一页满没满」猜。
        summary_total = self.store.count_summaries(chat_type, chat_id)
        impression_total = self.store.count_impressions(chat_type, chat_id)
        # 熔断状态只有库里有真值：进程重启后仍然延续，面板不该比 worker 更乐观。
        quota_until = self.store.load_backoff().get(QUOTA_KEY)
        quota_paused = bool(quota_until and float(quota_until) > time.time())
        return {
            "summaries": summaries,
            "impressions": impressions,
            "summary_total": summary_total,
            "impression_total": impression_total,
            "summary_has_more": offset + len(summaries) < summary_total,
            "impression_has_more": offset + len(impressions) < impression_total,
            # 分页上限的后端真值：面板据此禁用「显示更多」，不再各自写死 200。
            "limit_max": MAX_MEMORY_PAGE,
            # 配额熔断中时面板要说明「为什么记忆不动」，别让人以为坏了。
            "quota": {
                "paused": quota_paused,
                "until": float(quota_until) if quota_paused else None,
            },
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


def create_app(
    control: BrainControl,
    token: str = "",
    memory_actions: MemoryActions | None = None,
) -> web.Application:
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
            limit = _query_int(request, "limit", 20)
            offset = _query_int(request, "offset", 0)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not 1 <= limit <= MAX_MEMORY_PAGE:
            return web.json_response(
                {"error": f"limit 必须在 1..{MAX_MEMORY_PAGE} 之间"}, status=400
            )
        if offset < 0:
            return web.json_response({"error": "offset 不能为负数"}, status=400)

        raw_chat_type = (request.query.get("chat_type") or "").strip()
        chat_type: ChatType | None = None
        if raw_chat_type:
            try:
                chat_type = ChatType(raw_chat_type)
            except ValueError:
                return web.json_response(
                    {"error": "chat_type 必须是 group 或 private"}, status=400
                )

        chat_id: int | None = None
        raw_chat_id = (request.query.get("chat_id") or "").strip()
        if raw_chat_id:
            try:
                chat_id = int(raw_chat_id)
            except ValueError:
                return web.json_response({"error": "chat_id 必须是整数"}, status=400)
            if chat_id <= 0:
                return web.json_response({"error": "chat_id 必须是正整数"}, status=400)

        try:
            return web.json_response(
                control.memory_snapshot(limit, offset, chat_type, chat_id)
            )
        except Exception as exc:  # 读库失败不能把控制口打挂
            log.warning("读取记忆失败：%s", exc)
            return web.json_response(
                {"error": f"读取记忆失败：{exc.__class__.__name__}"}, status=500
            )

    async def post_summary(request: web.Request) -> web.Response:
        if memory_actions is None:
            return web.json_response({"error": "记忆操作未启用"}, status=503)
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        chat_type, chat_id, error = _chat_target(payload)
        if error is not None:
            return error
        date, error = _summary_date(payload)
        if error is not None:
            return error
        try:
            text = await memory_actions.summarize(chat_type, chat_id, date)
        except MemoryPaused as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:  # noqa: BLE001 - 手动触发失败要回给面板
            log.warning("手动生成摘要失败：%s", exc)
            return web.json_response(
                {"error": f"生成摘要失败：{exc.__class__.__name__}"}, status=502
            )
        if text is None:
            return web.json_response({"error": "生成摘要失败，详见日志"}, status=502)
        return web.json_response({"ok": True, "date": date, "summary": text})

    async def post_impressions(request: web.Request) -> web.Response:
        if memory_actions is None:
            return web.json_response({"error": "记忆操作未启用"}, status=503)
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        chat_type, chat_id, error = _chat_target(payload)
        if error is not None:
            return error
        try:
            added = await memory_actions.extract(chat_type, chat_id)
        except MemoryPaused as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:  # noqa: BLE001
            log.warning("手动抽取印象失败：%s", exc)
            return web.json_response(
                {"error": f"抽取印象失败：{exc.__class__.__name__}"}, status=502
            )
        if added is None:
            return web.json_response({"error": "抽取印象失败，详见日志"}, status=502)
        return web.json_response({"ok": True, "added": added})

    async def delete_impression(request: web.Request) -> web.Response:
        if memory_actions is None:
            return web.json_response({"error": "记忆操作未启用"}, status=503)
        raw = request.match_info.get("impression_id", "")
        try:
            impression_id = int(raw)
        except ValueError:
            return web.json_response({"error": "印象 id 必须是整数"}, status=400)
        if impression_id <= 0:
            return web.json_response({"error": "印象 id 必须是正整数"}, status=400)
        try:
            deleted = memory_actions.delete_impression(impression_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除印象失败：%s", exc)
            return web.json_response(
                {"error": f"删除印象失败：{exc.__class__.__name__}"}, status=500
            )
        if not deleted:
            return web.json_response({"error": "印象不存在"}, status=404)
        return web.json_response({"ok": True, "deleted": impression_id})

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/brain", get_brain)
    app.router.add_post("/api/brain", post_brain)
    app.router.add_get("/api/brain/models", models)
    app.router.add_get("/api/memory", memory)
    app.router.add_post("/api/memory/summaries", post_summary)
    app.router.add_post("/api/memory/impressions", post_impressions)
    app.router.add_delete(
        "/api/memory/impressions/{impression_id}", delete_impression
    )
    return app


async def start_control_server(
    settings: Settings,
    store: Store,
    service: Any,
    adapter: Any,
    runtime_path: str = "data/runtime.json",
    memory_actions: MemoryActions | None = None,
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
    app = create_app(control, token, memory_actions)
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
