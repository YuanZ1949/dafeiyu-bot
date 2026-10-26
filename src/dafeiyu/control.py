from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import sqlite3
import tempfile
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
from . import __version__
from .config import ConfigError, Settings
from .memory.store import MAX_MEMORY_PAGE, MAX_TIMELINE_PAGE, Store
from .models import ChatType, Message
from .runtime import RuntimeState, load_runtime, save_runtime
from .service import BRAIN_ERROR_COOLDOWN_SECONDS, TOOL_HINT_INTERVAL_SECONDS
from .timeutil import SHANGHAI, now_shanghai
from .workers.failure import (
    QUOTA_KEY,
    QUOTA_PAUSE_SECONDS,
    RATE_LIMIT_BACKOFF_SECONDS,
    RATE_LIMIT_THRESHOLD,
    RATE_LIMIT_WINDOW_SECONDS,
    MemoryPaused,
)

log = logging.getLogger(__name__)

CLOSE_DELAY_SECONDS = 30.0
MODEL_PROBE_TIMEOUT = 3.0
MAX_IMPRESSION_CHARS = 200
MAX_DEBUG_MESSAGE_CHARS = 2000
MAX_DEBUG_NICKNAME_CHARS = 40
IMPRESSION_KINDS = {"nickname", "preference", "fact"}
# 进程启动时刻：健康快照的 uptime 基准。
PROCESS_STARTED_AT = time.monotonic()
# 健康快照里「最近错误」的统计窗口。
ERROR_STATS_WINDOW_SECONDS = 3600
# 运维通知：正文上限与进程内最小间隔。
MAX_NOTIFY_CHARS = 1000
MAX_DEDUPE_KEY_CHARS = 64
NOTIFY_MIN_INTERVAL_SECONDS = 10.0


def _iso_ts(timestamp: Any) -> str | None:
    """秒级时间戳 → 上海时区 ISO；没有时间戳时返回 None。"""
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), SHANGHAI).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OSError):
        return None


def _parse_impression_id(raw: str) -> tuple[int | None, web.Response | None]:
    try:
        impression_id = int(raw)
    except ValueError:
        return None, web.json_response({"error": "印象 id 必须是整数"}, status=400)
    if impression_id <= 0:
        return None, web.json_response({"error": "印象 id 必须是正整数"}, status=400)
    return impression_id, None


def _parse_impression_content(value: Any) -> tuple[str | None, web.Response | None]:
    if not isinstance(value, str):
        return None, web.json_response({"error": "content 必须是字符串"}, status=400)
    content = value.strip()
    if not content:
        return None, web.json_response({"error": "content 不能为空"}, status=400)
    if len(content) > MAX_IMPRESSION_CHARS:
        return None, web.json_response(
            {"error": f"content 不能超过 {MAX_IMPRESSION_CHARS} 字"}, status=400
        )
    return content, None


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


def _query_scope(
    request: web.Request,
) -> tuple[ChatType | None, int | None, web.Response | None]:
    raw_chat_type = (request.query.get("chat_type") or "").strip()
    chat_type: ChatType | None = None
    if raw_chat_type:
        try:
            chat_type = ChatType(raw_chat_type)
        except ValueError:
            return None, None, web.json_response(
                {"error": "chat_type 必须是 group 或 private"}, status=400
            )

    chat_id: int | None = None
    raw_chat_id = (request.query.get("chat_id") or "").strip()
    if raw_chat_id:
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            return None, None, web.json_response(
                {"error": "chat_id 必须是整数"}, status=400
            )
        if chat_id <= 0:
            return None, None, web.json_response(
                {"error": "chat_id 必须是正整数"}, status=400
            )
    if chat_id is not None and chat_type is None:
        return None, None, web.json_response(
            {"error": "指定 chat_id 时必须同时指定 chat_type"}, status=400
        )
    return chat_type, chat_id, None


def _path_target(
    request: web.Request,
) -> tuple[ChatType | None, int | None, web.Response | None]:
    raw_type = request.match_info.get("chat_type", "")
    try:
        chat_type = ChatType(raw_type)
    except ValueError:
        return None, None, web.json_response(
            {"error": "chat_type 必须是 group 或 private"}, status=400
        )
    raw_id = request.match_info.get("chat_id", "")
    try:
        chat_id = int(raw_id)
    except ValueError:
        return None, None, web.json_response(
            {"error": "chat_id 必须是整数"}, status=400
        )
    if chat_id <= 0:
        return None, None, web.json_response(
            {"error": "chat_id 必须是正整数"}, status=400
        )
    return chat_type, chat_id, None


def _fmt_ts(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(int(timestamp), SHANGHAI).strftime("%m-%d %H:%M")


def _message_json(message: Any) -> dict[str, Any]:
    return {
        "chat_type": message.chat_type.value,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "user_id": message.user_id,
        "nickname": message.nickname,
        "content": message.content,
        "is_bot": message.is_bot,
        "at_bot": message.at_bot,
        "reply_to_bot": message.reply_to_bot,
        "timestamp": message.timestamp,
        "at": _fmt_ts(message.timestamp),
    }


def _impression_json(impression: Any) -> dict[str, Any]:
    return {
        "id": impression.id,
        "chat_type": impression.chat_type.value,
        "chat_id": impression.chat_id,
        "subject_user_id": impression.subject_user_id,
        "kind": impression.kind,
        "content": impression.content,
        "confidence": impression.confidence,
        "source_message_id": impression.source_message_id,
        "source_excerpt": impression.source_excerpt,
        "updated_at": impression.updated_at,
    }


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
        self._last_notify_at: float | None = None

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

    def health_snapshot(self) -> dict[str, Any]:
        """给 supervisor 与 dashboard 用的不可变健康快照。

        只含计数与时间戳，不含消息正文、昵称、token、API Key。
        读库失败时 ``ok=False`` 并记录时间，绝不把异常抛给探活方。
        """
        generated_at = now_shanghai().isoformat(timespec="seconds")
        state = self.snapshot()

        memory: dict[str, Any] = {
            "db_path": "",
            "db_bytes": 0,
            "wal_bytes": 0,
            "message_count": 0,
            "schema_version": 0,
        }
        errors: dict[str, Any] = {"recent_count": 0, "last_error_at": None}
        today_calls = 0
        today_tokens = 0
        quota_until: float | None = None
        ok = True
        try:
            stats = self.store.db_stats()
            memory = {
                "db_path": str(stats.get("db_path") or ""),
                "db_bytes": int(stats.get("db_bytes") or 0),
                "wal_bytes": int(stats.get("wal_bytes") or 0),
                "message_count": int(stats.get("message_count") or 0),
                "schema_version": int(stats.get("schema_version") or 0),
            }
            today = self.store.today()
            today_calls, today_tokens = self.store.usage_totals(today, today)
            recent = self.store.error_stats(ERROR_STATS_WINDOW_SECONDS)
            errors = {
                "recent_count": int(recent.get("count") or 0),
                "last_error_at": recent.get("last_at"),
            }
            quota_until = self.store.load_backoff().get(QUOTA_KEY)
        except Exception as exc:  # noqa: BLE001 - 探活不能因读库失败而 500
            ok = False
            errors["last_error_at"] = generated_at
            log.warning("读取健康快照统计失败：%s", exc)

        quota_paused = bool(quota_until and float(quota_until) > time.time())
        scheduler = getattr(self.service, "scheduler", None)

        def _count(name: str) -> int:
            counter = getattr(scheduler, name, None)
            if not callable(counter):
                return 0
            try:
                return int(counter())
            except Exception:  # noqa: BLE001 - 计数失败只降级为 0
                return 0

        last_event_at = _iso_ts(getattr(self.adapter, "last_event_at", None))
        return {
            "ok": ok,
            "generated_at": generated_at,
            "uptime_seconds": round(time.monotonic() - PROCESS_STARTED_AT, 1),
            "version": __version__,
            "gateway": {
                "connected": bool(getattr(self.adapter, "connected", False)),
                "last_event_at": last_event_at,
                "last_disconnect_at": _iso_ts(
                    getattr(self.adapter, "last_disconnect_at", None)
                ),
            },
            "traffic": {
                "last_message_at": last_event_at,
                "last_reply_at": _iso_ts(getattr(self.adapter, "last_send_at", None)),
            },
            "scheduler": {
                "pending_chats": _count("pending_count"),
                "inflight_chats": _count("inflight_count"),
            },
            "budget": {
                "per_chat": {
                    "calls": today_calls,
                    "tokens": today_tokens,
                    "daily_calls": self.settings.budget.daily_calls,
                    "daily_tokens": self.settings.budget.daily_tokens,
                },
                # 分册 B 才落地全局/月度/费用预算，A 先保留占位结构。
                "global_daily": {
                    "calls": 0,
                    "tokens": 0,
                    "limit_calls": 0,
                    "limit_tokens": 0,
                },
                "monthly": {
                    "calls": 0,
                    "tokens": 0,
                    "limit_calls": 0,
                    "limit_tokens": 0,
                    "cost": 0.0,
                    "cost_limit": 0.0,
                },
                "warnings": [],
            },
            "quota": {
                "paused_until": float(quota_until) if quota_paused else None,
                "reason": "额度熔断" if quota_paused else "",
            },
            "brain": {"provider": state["provider"], "model": state["model"]},
            "memory": memory,
            "errors": errors,
        }

    def explain_trigger(
        self, msg: Message, *, seed: int | None = None
    ) -> dict[str, Any]:
        explain = getattr(self.service, "explain_trigger", None)
        if not callable(explain):
            raise NotImplementedError("当前服务不支持触发调试")
        return explain(msg, seed=seed)

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
                "source_message_id": i.source_message_id,
                "source_excerpt": i.source_excerpt,
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

    # ---- dashboard 扩展：用量 / 搜索 / 导出 / 会话 / 配置 ----
    def usage_snapshot(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(90, int(days)))
        end = datetime.now(SHANGHAI).date()
        start = end - timedelta(days=days - 1)
        rows = {
            str(row["date"]): row
            for row in self.store.usage_series(start.isoformat(), end.isoformat())
        }
        series = []
        totals = {"calls": 0, "errors": 0, "tokens": 0}
        for offset in range(days):
            date = (start + timedelta(days=offset)).isoformat()
            row = rows.get(date) or {}
            item = {
                "date": date,
                "calls": int(row.get("calls") or 0),
                "errors": int(row.get("errors") or 0),
                "tokens": int(row.get("tokens") or 0),
            }
            for key in totals:
                totals[key] += item[key]
            series.append(item)
        return {
            "days": days,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "series": series,
            "totals": totals,
            "top": self.store.usage_top(start.isoformat(), 5),
            "recent_errors": self.store.recent_errors(10),
        }

    def search_snapshot(
        self,
        query: str,
        chat_type: ChatType | None = None,
        chat_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        total, hits = self.store.search_message_hits(
            query, chat_type, chat_id, limit, offset
        )
        return {
            "query": query,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {**_message_json(message), "snippet": snippet}
                for message, snippet in hits
            ],
        }

    def timeline_snapshot(
        self,
        chat_type: ChatType,
        chat_id: int,
        limit: int = 50,
        before: int | None = None,
        anchor_message_id: str | None = None,
    ) -> dict[str, Any]:
        """会话时间线：消息分页 + 会话指标 + 摘要/印象/用量，全部只读。"""
        page = self.store.messages_page(
            chat_type,
            chat_id,
            limit=limit,
            before=before,
            anchor_message_id=anchor_message_id,
        )
        today = self.store.today()
        calls, tokens = self.store.usage_today(chat_type, chat_id, today)
        stats = self.store.session_stats(chat_type, chat_id)
        summaries = self.store.recent_summaries(20, 0, chat_type, chat_id)
        impressions = self.store.list_impressions(chat_type, chat_id, topk=50)
        return {
            "chat": {
                "chat_type": chat_type.value,
                "chat_id": chat_id,
                "message_total": self.store.count_messages(chat_type, chat_id),
                "summary_total": self.store.count_summaries(chat_type, chat_id),
                "impression_total": self.store.count_impressions(chat_type, chat_id),
            },
            "messages": [_message_json(message) for message in page.messages],
            "has_more": page.has_more,
            "next_before": page.next_before,
            "anchor_found": page.anchor_found,
            "session_summary": self.store.latest_session_summary(chat_type, chat_id),
            "summaries": [
                {
                    "date": summary.date,
                    "content": summary.content,
                    "updated_at": summary.updated_at,
                }
                for summary in summaries
            ],
            "impressions": [_impression_json(item) for item in impressions],
            "usage": {"date": today, "calls": calls, "tokens": tokens},
            "session": {
                "delivered_count": stats.delivered_count,
                "token_estimate": stats.token_estimate,
                "created_at": stats.created_at,
            },
        }

    def export_snapshot(
        self, chat_type: ChatType, chat_id: int, limit: int = 1000
    ) -> dict[str, Any]:
        total = self.store.count_messages(chat_type, chat_id)
        messages = self.store.recent_messages(chat_type, chat_id, limit)
        summaries = self.store.all_summaries(chat_type, chat_id)
        impressions = self.store.list_impressions(chat_type, chat_id, topk=10000)
        return {
            "chat": {"chat_type": chat_type.value, "chat_id": chat_id},
            "exported_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "message_total": total,
            "truncated": total > len(messages),
            "messages": [_message_json(m) for m in messages],
            "summaries": [
                {
                    "date": summary.date,
                    "content": summary.content,
                    "updated_at": summary.updated_at,
                }
                for summary in summaries
            ],
            "impressions": [
                {
                    "subject_user_id": impression.subject_user_id,
                    "kind": impression.kind,
                    "content": impression.content,
                    "confidence": impression.confidence,
                    "source_message_id": impression.source_message_id,
                    "source_excerpt": impression.source_excerpt,
                    "updated_at": impression.updated_at,
                }
                for impression in impressions
            ],
        }

    @staticmethod
    def export_markdown(bundle: dict[str, Any]) -> str:
        chat = bundle["chat"]
        lines = [
            f"# 大肥鱼记忆导出 · {chat['chat_type']}/{chat['chat_id']}",
            "",
            f"- 导出时间：{bundle['exported_at']}",
            f"- 消息：{len(bundle['messages'])} / {bundle['message_total']} 条"
            + ("（已截断，仅保留最近的消息）" if bundle["truncated"] else ""),
            f"- 按日摘要：{len(bundle['summaries'])} 条",
            f"- 印象：{len(bundle['impressions'])} 条",
            "",
            "## 消息",
            "",
        ]
        for message in bundle["messages"]:
            who = "大肥鱼" if message["is_bot"] else message["nickname"]
            content = str(message["content"]).replace("\r\n", "\n").replace("\n", " / ")
            lines.append(f"- [{message['at']}] {who}: {content}")
        if not bundle["messages"]:
            lines.append("（无）")

        lines.extend(["", "## 按日摘要", ""])
        for summary in bundle["summaries"]:
            lines.append(f"### {summary['date']}")
            lines.append("")
            lines.append(str(summary["content"]))
            lines.append("")
        if not bundle["summaries"]:
            lines.append("（无）")

        lines.extend(["", "## 印象", ""])
        for impression in bundle["impressions"]:
            subject = impression["subject_user_id"] or "-"
            source = impression.get("source_message_id") or "-"
            lines.append(
                f"- [{impression['kind']}] {impression['content']}"
                f"（主体 {subject}，置信度 {impression['confidence']}，来源 {source}）"
            )
        if not bundle["impressions"]:
            lines.append("（无）")
        return "\n".join(lines).rstrip() + "\n"

    def sessions_snapshot(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        # chat_overview 本身不做 SQL 分页（四表并集），这里只截断响应；
        # 前端按 offset 加载更多，避免一次把所有会话塞进 15 秒轮询。
        items = self.store.chat_overview()
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "limit": limit,
            "offset": offset,
        }

    async def session_action(
        self, action: str, chat_type: ChatType, chat_id: int
    ) -> dict[str, Any]:
        if action == "new":
            self.store.reset_chat(chat_type, chat_id)
            return {"ok": True, "action": action}
        if action == "forget":
            self.store.forget_messages(chat_type, chat_id)
            return {"ok": True, "action": action}
        if action != "delete":
            raise ValueError(f"未知会话操作：{action}")

        forget_chat = getattr(self.service, "forget_chat", None)
        if forget_chat is not None:
            result = forget_chat(chat_type, chat_id)
            if inspect.isawaitable(result):
                await result
        else:
            brain = getattr(self.service, "brain", None)
            forget = getattr(brain, "forget", None) if brain is not None else None
            if forget is not None:
                result = forget(chat_type, chat_id)
                if inspect.isawaitable(result):
                    await result
            self.store.forget_chat(chat_type, chat_id)
        return {"ok": True, "action": action}

    def config_snapshot(self) -> dict[str, Any]:
        state = self.snapshot()
        persona = self.settings.persona
        return {
            "brain": {
                "provider": state["provider"],
                "model": state["model"],
                "agent": self.settings.opencode.agent,
                "transport": self.settings.opencode.transport,
            },
            "persona": {
                "name": persona.name,
                "max_reply_chars": persona.max_reply_chars,
                "max_reply_chars_technical": persona.max_reply_chars_technical,
                "os_probability": persona.os_probability,
            },
            "cooldown": {
                "brain_error_seconds": BRAIN_ERROR_COOLDOWN_SECONDS,
                "tool_hint_seconds": TOOL_HINT_INTERVAL_SECONDS,
            },
            "rate_limit": {
                "threshold": RATE_LIMIT_THRESHOLD,
                "window_seconds": RATE_LIMIT_WINDOW_SECONDS,
                "backoff_seconds": RATE_LIMIT_BACKOFF_SECONDS,
                "quota_pause_seconds": QUOTA_PAUSE_SECONDS,
            },
            "budget": {
                "daily_calls": self.settings.budget.daily_calls,
                "daily_tokens": self.settings.budget.daily_tokens,
                "ambient_ratio": self.settings.budget.ambient_ratio,
            },
            "memory": {
                "window_size": self.settings.memory.window_size,
                "retention_days": self.settings.memory.retention_days,
            },
            "gateway": {"provider": self.settings.gateway.provider},
        }


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

    async def health_detail(_: web.Request) -> web.Response:
        return web.json_response(control.health_snapshot())

    async def notify_admin(request: web.Request) -> web.Response:
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            return web.json_response({"error": "text 必须是字符串"}, status=400)
        text = raw_text.strip()
        if not text:
            return web.json_response({"error": "text 不能为空"}, status=400)
        if len(text) > MAX_NOTIFY_CHARS:
            return web.json_response(
                {"error": f"text 不能超过 {MAX_NOTIFY_CHARS} 字"}, status=400
            )
        dedupe_key = payload.get("dedupe_key", "")
        if not isinstance(dedupe_key, str):
            return web.json_response(
                {"error": "dedupe_key 必须是字符串"}, status=400
            )
        if len(dedupe_key) > MAX_DEDUPE_KEY_CHARS:
            return web.json_response(
                {"error": f"dedupe_key 不能超过 {MAX_DEDUPE_KEY_CHARS} 字"}, status=400
            )

        admins = sorted(control.settings.groups.admins)
        if not admins:
            return web.json_response({"error": "未配置管理员"}, status=503)
        if not bool(getattr(control.adapter, "connected", False)):
            return web.json_response({"error": "QQ 网关未连接"}, status=503)
        now = time.monotonic()
        last = control._last_notify_at
        if last is not None and now - last < NOTIFY_MIN_INTERVAL_SECONDS:
            return web.json_response(
                {"error": "通知过于频繁，请稍后再试"}, status=429
            )

        target = admins[0]
        try:
            await control.adapter.send(ChatType.PRIVATE, target, text)
        except Exception as exc:  # noqa: BLE001 - 发送失败回 503，不打断告警循环
            log.warning("管理员通知发送失败：%s", exc)
            return web.json_response({"error": "管理员通知发送失败"}, status=503)
        control._last_notify_at = now
        return web.json_response({"ok": True, "target": target})

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

    async def debug_trigger(request: web.Request) -> web.Response:
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        chat_type, chat_id, error = _chat_target(payload)
        if error is not None:
            return error

        raw_content = payload.get("content")
        if not isinstance(raw_content, str):
            return web.json_response({"error": "content 必须是字符串"}, status=400)
        content = raw_content.strip()
        if not content:
            return web.json_response({"error": "content 不能为空"}, status=400)
        if len(content) > MAX_DEBUG_MESSAGE_CHARS:
            return web.json_response(
                {"error": f"content 不能超过 {MAX_DEBUG_MESSAGE_CHARS} 字"},
                status=400,
            )

        raw_user_id = payload.get("user_id")
        if isinstance(raw_user_id, bool):
            return web.json_response({"error": "user_id 必须是正整数"}, status=400)
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "user_id 必须是正整数"}, status=400)
        if user_id <= 0:
            return web.json_response({"error": "user_id 必须是正整数"}, status=400)

        raw_nickname = payload.get("nickname", "调试用户")
        if not isinstance(raw_nickname, str):
            return web.json_response({"error": "nickname 必须是字符串"}, status=400)
        nickname = raw_nickname.strip() or "调试用户"
        if len(nickname) > MAX_DEBUG_NICKNAME_CHARS:
            return web.json_response(
                {"error": f"nickname 不能超过 {MAX_DEBUG_NICKNAME_CHARS} 字"},
                status=400,
            )

        flags: dict[str, bool] = {}
        for field in ("at_bot", "reply_to_bot"):
            value = payload.get(field, False)
            if not isinstance(value, bool):
                return web.json_response(
                    {"error": f"{field} 必须是布尔值"}, status=400
                )
            flags[field] = value

        seed = payload.get("seed")
        if seed is not None:
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
                return web.json_response(
                    {"error": "seed 必须是非负整数"}, status=400
                )

        msg = Message(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=user_id,
            nickname=nickname,
            content=content,
            message_id=f"debug-{time.time_ns()}",
            segments=[],
            at_bot=flags["at_bot"],
            reply_to_bot=flags["reply_to_bot"],
            timestamp=int(time.time()),
        )
        try:
            return web.json_response(control.explain_trigger(msg, seed=seed))
        except NotImplementedError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        except Exception as exc:  # 调试器读失败不能把控制口打挂
            log.warning("触发调试失败：%s", exc)
            return web.json_response(
                {"error": f"触发调试失败：{exc.__class__.__name__}"}, status=500
            )

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

    async def patch_impression(request: web.Request) -> web.Response:
        impression_id, error = _parse_impression_id(
            request.match_info.get("impression_id", "")
        )
        if error is not None:
            return error
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        content: str | None = None
        confidence: float | None = None
        kind: str | None = None
        if "content" in payload:
            content, error = _parse_impression_content(payload["content"])
            if error is not None:
                return error
        if "confidence" in payload:
            raw = payload["confidence"]
            if isinstance(raw, bool):
                return web.json_response({"error": "confidence 必须是 0..1 的数字"}, status=400)
            try:
                confidence = float(raw)
            except (TypeError, ValueError):
                return web.json_response({"error": "confidence 必须是 0..1 的数字"}, status=400)
            if not 0 <= confidence <= 1:
                return web.json_response({"error": "confidence 必须在 0..1 之间"}, status=400)
        if "kind" in payload:
            raw_kind = payload["kind"]
            if not isinstance(raw_kind, str) or raw_kind not in IMPRESSION_KINDS:
                return web.json_response(
                    {"error": "kind 只能是 nickname、preference 或 fact"}, status=400
                )
            kind = raw_kind
        if content is None and confidence is None and kind is None:
            return web.json_response({"error": "至少提供 content/confidence/kind 之一"}, status=400)
        try:
            impression = control.store.update_impression(
                impression_id, content=content, confidence=confidence, kind=kind
            )
        except sqlite3.IntegrityError:
            return web.json_response({"error": "已存在相同内容/类型/主体的印象"}, status=409)
        if impression is None:
            return web.json_response({"error": "印象不存在"}, status=404)
        return web.json_response({"ok": True, "impression": _impression_json(impression)})

    async def merge_impressions(request: web.Request) -> web.Response:
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        raw_ids = payload.get("ids")
        if not isinstance(raw_ids, list) or len(raw_ids) < 2:
            return web.json_response({"error": "ids 至少需要两条印象"}, status=400)
        ids: list[int] = []
        for raw in raw_ids:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
                return web.json_response({"error": "ids 必须是正整数列表"}, status=400)
            if raw not in ids:
                ids.append(raw)
        if len(ids) < 2:
            return web.json_response({"error": "ids 至少需要两条不同的印象"}, status=400)
        content: str | None = None
        if "content" in payload:
            content, error = _parse_impression_content(payload["content"])
            if error is not None:
                return error
        try:
            impression = control.store.merge_impressions(ids, content)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except sqlite3.IntegrityError:
            return web.json_response({"error": "合并后的内容与已有印象重复"}, status=409)
        if impression is None:
            return web.json_response({"error": "有印象不存在"}, status=404)
        return web.json_response({"ok": True, "impression": _impression_json(impression)})

    async def delete_impression(request: web.Request) -> web.Response:
        if memory_actions is None:
            return web.json_response({"error": "记忆操作未启用"}, status=503)
        impression_id, error = _parse_impression_id(
            request.match_info.get("impression_id", "")
        )
        if error is not None:
            return error
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

    async def usage(request: web.Request) -> web.Response:
        try:
            days = _query_int(request, "days", 7)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not 1 <= days <= 90:
            return web.json_response({"error": "days 必须在 1..90 之间"}, status=400)
        try:
            return web.json_response(control.usage_snapshot(days))
        except Exception as exc:  # noqa: BLE001 - 读库失败不能把控制口打挂
            log.warning("读取用量失败：%s", exc)
            return web.json_response(
                {"error": f"读取用量失败：{exc.__class__.__name__}"}, status=500
            )

    async def search_messages(request: web.Request) -> web.Response:
        query = (request.query.get("q") or "").strip()
        if not query:
            return web.json_response({"error": "q 不能为空"}, status=400)
        if len(query) > 200:
            return web.json_response({"error": "q 不能超过 200 字"}, status=400)
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
        chat_type, chat_id, error = _query_scope(request)
        if error is not None:
            return error
        try:
            return web.json_response(
                control.search_snapshot(query, chat_type, chat_id, limit, offset)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("搜索消息失败：%s", exc)
            return web.json_response(
                {"error": f"搜索失败：{exc.__class__.__name__}"}, status=500
            )

    async def export_memory(request: web.Request) -> web.Response:
        chat_type, chat_id, error = _query_scope(request)
        if error is not None:
            return error
        if chat_type is None or chat_id is None:
            return web.json_response(
                {"error": "导出需要同时指定 chat_type 和 chat_id"}, status=400
            )
        fmt = (request.query.get("format") or "md").strip().lower()
        if fmt not in ("md", "json"):
            return web.json_response({"error": "format 必须是 md 或 json"}, status=400)
        try:
            limit = _query_int(request, "limit", 1000)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not 1 <= limit <= 5000:
            return web.json_response({"error": "limit 必须在 1..5000 之间"}, status=400)
        try:
            bundle = control.export_snapshot(chat_type, chat_id, limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("导出记忆失败：%s", exc)
            return web.json_response(
                {"error": f"导出失败：{exc.__class__.__name__}"}, status=500
            )
        stem = f"dafeiyu-{chat_type.value}-{chat_id}"
        if fmt == "json":
            body = json.dumps(bundle, ensure_ascii=False, indent=2)
            headers = {"Content-Disposition": f'attachment; filename="{stem}.json"'}
            return web.Response(
                body=body.encode("utf-8"),
                content_type="application/json",
                charset="utf-8",
                headers=headers,
            )
        headers = {"Content-Disposition": f'attachment; filename="{stem}.md"'}
        return web.Response(
            body=control.export_markdown(bundle).encode("utf-8"),
            content_type="text/markdown",
            charset="utf-8",
            headers=headers,
        )

    async def backup(_: web.Request) -> web.Response:
        """整库一致性快照。先写临时文件再读入内存，避免把半成品流给浏览器。"""
        stamp = datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S")
        filename = f"dafeiyu-backup-{stamp}.db"
        fd, tmp = tempfile.mkstemp(prefix="dafeiyu-backup-", suffix=".db")
        os.close(fd)
        try:
            control.store.backup_to(tmp)
            with open(tmp, "rb") as fh:
                body = fh.read()
        except Exception as exc:  # noqa: BLE001
            log.warning("备份数据库失败：%s", exc)
            return web.json_response(
                {"error": f"备份失败：{exc.__class__.__name__}"}, status=500
            )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return web.Response(
            body=body,
            content_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def sessions(request: web.Request) -> web.Response:
        try:
            limit = _query_int(request, "limit", 50)
            offset = _query_int(request, "offset", 0)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not 1 <= limit <= 200:
            return web.json_response({"error": "limit 必须在 1..200 之间"}, status=400)
        if offset < 0:
            return web.json_response({"error": "offset 不能为负数"}, status=400)
        try:
            return web.json_response(control.sessions_snapshot(limit, offset))
        except Exception as exc:  # noqa: BLE001
            log.warning("读取会话列表失败：%s", exc)
            return web.json_response(
                {"error": f"读取会话失败：{exc.__class__.__name__}"}, status=500
            )

    async def timeline(request: web.Request) -> web.Response:
        chat_type, chat_id, error = _path_target(request)
        if error is not None:
            return error
        try:
            limit = _query_int(request, "limit", 50)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not 1 <= limit <= MAX_TIMELINE_PAGE:
            return web.json_response(
                {"error": f"limit 必须在 1..{MAX_TIMELINE_PAGE} 之间"}, status=400
            )

        before: int | None = None
        raw_before = (request.query.get("before") or "").strip()
        if raw_before:
            try:
                before = int(raw_before)
            except ValueError:
                return web.json_response({"error": "before 必须是整数"}, status=400)
            if before <= 0:
                return web.json_response({"error": "before 必须是正整数"}, status=400)

        anchor: str | None = None
        if "anchor" in request.query:
            anchor = (request.query.get("anchor") or "").strip()
            if not anchor:
                return web.json_response({"error": "anchor 不能为空"}, status=400)
            if len(anchor) > 200:
                return web.json_response({"error": "anchor 不能超过 200 字"}, status=400)

        if before is not None and anchor is not None:
            return web.json_response(
                {"error": "before 与 anchor 不能同时使用"}, status=400
            )
        try:
            return web.json_response(
                control.timeline_snapshot(
                    chat_type,
                    chat_id,
                    limit=limit,
                    before=before,
                    anchor_message_id=anchor,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("读取会话时间线失败：%s", exc)
            return web.json_response(
                {"error": f"读取时间线失败：{exc.__class__.__name__}"}, status=500
            )

    async def session_action(request: web.Request) -> web.Response:
        chat_type, chat_id, error = _path_target(request)
        if error is not None:
            return error
        action = request.match_info.get("action", "")
        if action not in ("new", "forget"):
            return web.json_response({"error": "未知会话操作"}, status=400)
        try:
            return web.json_response(
                await control.session_action(action, chat_type, chat_id)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("会话操作 %s 失败：%s", action, exc)
            return web.json_response(
                {"error": f"会话操作失败：{exc.__class__.__name__}"}, status=500
            )

    async def delete_session(request: web.Request) -> web.Response:
        chat_type, chat_id, error = _path_target(request)
        if error is not None:
            return error
        try:
            return web.json_response(
                await control.session_action("delete", chat_type, chat_id)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("彻底删除会话失败：%s", exc)
            return web.json_response(
                {"error": f"删除会话失败：{exc.__class__.__name__}"}, status=500
            )

    async def config(_: web.Request) -> web.Response:
        try:
            return web.json_response(control.config_snapshot())
        except Exception as exc:  # noqa: BLE001
            log.warning("读取配置失败：%s", exc)
            return web.json_response(
                {"error": f"读取配置失败：{exc.__class__.__name__}"}, status=500
            )

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/health/detail", health_detail)
    app.router.add_post("/api/notify/admin", notify_admin)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/brain", get_brain)
    app.router.add_post("/api/brain", post_brain)
    app.router.add_get("/api/brain/models", models)
    app.router.add_post("/api/debug/trigger", debug_trigger)
    app.router.add_get("/api/memory", memory)
    app.router.add_post("/api/memory/summaries", post_summary)
    app.router.add_post("/api/memory/impressions", post_impressions)
    app.router.add_patch(
        "/api/memory/impressions/{impression_id}", patch_impression
    )
    app.router.add_post("/api/memory/impressions/merge", merge_impressions)
    app.router.add_delete(
        "/api/memory/impressions/{impression_id}", delete_impression
    )
    app.router.add_get("/api/usage", usage)
    app.router.add_get("/api/memory/search", search_messages)
    app.router.add_get("/api/memory/export", export_memory)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get(
        "/api/sessions/{chat_type}/{chat_id}/timeline", timeline
    )
    app.router.add_post("/api/sessions/{chat_type}/{chat_id}/{action}", session_action)
    app.router.add_delete("/api/sessions/{chat_type}/{chat_id}", delete_session)
    app.router.add_get("/api/config", config)
    app.router.add_get("/api/backup", backup)
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
