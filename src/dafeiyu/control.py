from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import mimetypes
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path, PurePosixPath
from stat import S_ISREG
from typing import Any, cast

from aiohttp import web

from . import __version__
from .behavior import BehaviorError, as_payload, parse_behavior
from .brain.factory import (
    build_brain,
    current_provider,
    local_model_name,
    mode_status,
    normalize_provider,
)
from .brain.lmstudio import ensure_server
from .brain.provider_config import (
    SUPPORTED_MODES,
    agent_path,
    build_agent,
    build_config,
    build_memory_agent,
    config_path,
    memory_agent_path,
    render_config,
    write_generated,
)
from .brain.runtime_config import ensure_isolated_config
from .budget import BudgetGuard
from .config import (
    OVERRIDES_PATH,
    ConfigError,
    GroupsConfig,
    MindConfig,
    Settings,
    parse_group_ids,
)
from .gateway.base import GatewayError
from .ids import normalize_chat_id
from .memory.store import MAX_MEMORY_PAGE, MAX_TIMELINE_PAGE, Store
from .model_probe import (
    FREE_PROBE_TTL,
    FreeProbeResult,
    ProbeError,
    normalize_base_url,
    pick_flash_model,
    probe_free_model_detail,
    probe_models,
)
from .models import ChatType, Message, channel_of
from .runtime import RuntimeState, load_runtime, save_runtime
from .secrets_store import (
    INSTALL_ROOT,
    KEY_PATH,
    LOCAL_KEY_PATH,
    read_secret,
    secret_exists,
    write_secret,
)
from .settings_local import OverrideError, read_overrides, write_overrides
from .supervisor.reload_client import reload_opencode
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
# 三档切换的探活上限：云端端点比本机 LM Studio 慢，list_models 的 3s 不够用。
BRAIN_PROBE_TIMEOUT = 8.0
# 免费档懒刷新前先看一眼 opencode 就绪：重启窗口内的失败不能缓存 600s。
FREE_PROBE_READY_TIMEOUT = 1.0
# 布尔型的查询参数：只有明确写了真值的才算，空串/0/false 一律当否。
_TRUE_QUERY_VALUES = {"1", "true", "yes", "on"}


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().casefold() in _TRUE_QUERY_VALUES


# 工作区只读文件端点：只放行这两个子目录、这些扩展名，单文件不超过 20 MiB。
WORKSPACE_FILE_DIRS = ("images", "replies")
WORKSPACE_FILE_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".mp3",
        ".wav",
        ".mp4",
        ".pdf",
        ".txt",
        ".zip",
    }
)
MAX_WORKSPACE_FILE_BYTES = 20 * 1024 * 1024
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
# 人设正文的唯一来源：provider_config 只替换前置区，正文原样搬运。
AGENT_TEMPLATE_PATH = Path(__file__).resolve().parent / "brain" / "agent_template.md"
# 记忆旁路 agent 的正文；与聊天 agent 分开，模型随档位一起生成。
MEMORY_AGENT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "brain" / "memory_agent_template.md"
)
# 用户可见的三档，错误文案与 provider_config 共用同一份定义。
BRAIN_MODE_LABEL = " / ".join(SUPPORTED_MODES)


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
        unblock: Callable[[], None] | None = None,
    ) -> None:
        self.summarize = summarize
        self.extract = extract
        self.delete_impression = delete_impression
        # 可选：把同进程内各 worker 的内存退避一起清掉；未注入时只清库。
        self.unblock = unblock


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
) -> tuple[ChatType | None, str | None, web.Response | None]:
    raw_type = str(payload.get("chat_type") or "").strip()
    if raw_type not in ("group", "private"):
        return None, None, web.json_response(
            {"error": "chat_type 必须是 group 或 private"}, status=400
        )
    try:
        chat_id = normalize_chat_id(payload.get("chat_id"))
    except ValueError as exc:
        return None, None, web.json_response({"error": str(exc)}, status=400)
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


def _mode_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _brain_mode_payload(
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any], bool]:
    """校验并归一面板提交的三档切换请求。

    只认 `free / key / local_model`；`local` 是历史拼法，必须拒绝，否则会被
    provider 层静默解析成 token 档。key/local 只保留本次请求真的给了的字段：
    没给的字段不在覆盖层里写，保留用户上一次的选择。
    """
    raw_mode = payload.get("mode")
    if not isinstance(raw_mode, str):
        raise SwitchError(400, f"mode 必须是字符串（仅支持 {BRAIN_MODE_LABEL}）")
    mode = raw_mode.strip().casefold()
    if mode not in SUPPORTED_MODES:
        # 只回显有限长度：mode 不是密钥字段，但不该把任意 payload 原样吐回。
        raise SwitchError(
            400,
            f"不支持的大脑模式：{raw_mode.strip()[:32]}（仅支持 {BRAIN_MODE_LABEL}）",
        )

    key_raw = payload.get("key")
    if key_raw is None:
        key_raw = {}
    # 显式判类型：`or {}` 会把 []/""/0 这类非法值悄悄当成空对象放过。
    if not isinstance(key_raw, dict):
        raise SwitchError(400, "key 必须是对象")
    local_raw = payload.get("local")
    if local_raw is None:
        local_raw = {}
    if not isinstance(local_raw, dict):
        raise SwitchError(400, "local 必须是对象")

    raw_force = payload.get("force", False)
    if not isinstance(raw_force, bool):
        raise SwitchError(400, "force 必须是布尔值")

    key: dict[str, Any] = {
        "base_url": normalize_base_url(_mode_text(key_raw.get("base_url"))),
        "model": _mode_text(key_raw.get("model")),
        # 只用于写密钥文件/探活，绝不进覆盖层。
        "api_key": _mode_text(key_raw.get("api_key")),
    }
    local: dict[str, Any] = {
        "base_url": normalize_base_url(_mode_text(local_raw.get("base_url"))),
        "model": _mode_text(local_raw.get("model")),
        # 本地服务的令牌只用于探活和密钥文件，不进覆盖层。
        "api_key": _mode_text(local_raw.get("api_key")),
    }
    if "context" in local_raw:
        context = local_raw["context"]
        if isinstance(context, bool) or not isinstance(context, int) or context < 0:
            raise SwitchError(400, "local.context 必须是非负整数")
        local["context"] = context
    if "lean" in local_raw:
        if not isinstance(local_raw["lean"], bool):
            raise SwitchError(400, "local.lean 必须是布尔值")
        local["lean"] = local_raw["lean"]

    if mode == "key":
        if not key["base_url"]:
            raise SwitchError(400, "自备 Key 模式需要填写 base_url")
        # model 允许留空：留空意为「按端点清单自动选 flash」，由 switch_mode
        # 在落盘前解析。本地模型档没有等价约定，仍然必填。
    elif mode == "local_model":
        if not local["base_url"]:
            raise SwitchError(400, "本地模型模式需要填写 base_url")
        if not local["model"]:
            raise SwitchError(400, "本地模型模式需要选择 model")

    return mode, key, local, raw_force


def _merged_overrides(
    existing: dict[str, Any], mode: str, key: dict[str, Any], local: dict[str, Any]
) -> dict[str, Any]:
    """把本次选择并进已有覆盖层；不在白名单里的键原样保留。"""
    overrides = dict(existing)
    brain_raw = overrides.get("brain")
    brain = dict(brain_raw) if isinstance(brain_raw, dict) else {}
    brain["mode"] = mode
    if mode == "key":
        section_raw = brain.get("key")
        section = dict(section_raw) if isinstance(section_raw, dict) else {}
        section["base_url"] = key["base_url"]
        section["model"] = key["model"]
        brain["key"] = section
    elif mode == "local_model":
        section_raw = brain.get("local_model")
        section = dict(section_raw) if isinstance(section_raw, dict) else {}
        section["base_url"] = local["base_url"]
        section["model"] = local["model"]
        if "context" in local:
            section["context"] = local["context"]
        if "lean" in local:
            section["lean"] = local["lean"]
        brain["local_model"] = section
    overrides["brain"] = brain
    return overrides


def _with_key_model(overrides: dict[str, Any], model: str) -> dict[str, Any]:
    """返回把 `brain.key.model` 换成给定值的覆盖层副本（不就地改入参）。"""
    out = dict(overrides)
    brain_raw = out.get("brain")
    brain = dict(brain_raw) if isinstance(brain_raw, dict) else {}
    section_raw = brain.get("key")
    section = dict(section_raw) if isinstance(section_raw, dict) else {}
    section["model"] = model
    brain["key"] = section
    out["brain"] = brain
    return out


async def _probe_model_ids(base_url: str, api_key: str) -> list[str]:
    """拉一次端点模型清单；任何失败都返回空列表。

    自动对齐是「尽力而为」：端点临时不可达、返回体不合规都不该让切档失败，
    所以这里吞掉异常（只留 debug 日志），调用方按空列表处理。
    """
    if not base_url:
        return []
    try:
        return await probe_models(
            base_url, api_key=api_key, timeout=MODEL_PROBE_TIMEOUT
        )
    except Exception:  # 对齐失败不得挡住保存
        log.debug("模型清单拉取失败，跳过 key 模型自动对齐", exc_info=True)
        return []


def _merged_section(overrides: dict[str, Any], name: str) -> dict[str, Any]:
    """取覆盖层里 `brain.<name>` 子段；形状不对就当没给。"""
    brain = overrides.get("brain")
    if not isinstance(brain, dict):
        return {}
    section = brain.get(name)
    return section if isinstance(section, dict) else {}


def _restore_overrides(existing: dict[str, Any], had_file: bool) -> None:
    """把覆盖层恢复到切换前的样子。

    恢复失败只记一条 warning：不能拿回滚错误盖住原始的生成失败原因。
    """
    try:
        if had_file:
            write_overrides(existing, OVERRIDES_PATH)
        else:
            Path(OVERRIDES_PATH).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("覆盖层回滚失败：%s", type(exc).__name__)


def _restore_secret(previous: str, path: Path = KEY_PATH) -> None:
    """把密钥文件恢复到切换前；空值等价于删除（`write_secret` 的契约）。"""
    try:
        write_secret(previous, path)
    except OSError as exc:
        log.warning("provider key 回滚失败：%s", type(exc).__name__)


def _mode_for_provider(provider: str) -> str:
    """老 provider 口径 → 用户可见三档（只在没有覆盖层时兜底）。"""
    if provider == "local_model":
        return "local_model"
    if provider == "token":
        return "key"
    return "free"


def _key_reference(config_dir: Path, path: Path = KEY_PATH) -> str:
    """密钥文件相对 opencode 配置目录的引用。

    `{file:}` 只保证相对路径可用（绝对路径在 JSONC 里还要转义，且跨平台
    不稳定），所以按实际密钥文件（`provider.key` / `provider-local.key`）与
    配置目录现算，而不是写死 `DEFAULT_KEY_REL`——自定义/留空的 `config_home`
    下那个常量会指向不存在的位置。Windows 跨盘符无法表达相对路径时才退化为
    绝对 POSIX 路径。
    """
    target = Path(path).resolve()
    try:
        relative = os.path.relpath(target, Path(config_dir).resolve())
    except ValueError:
        return target.as_posix()
    return Path(relative).as_posix()


async def _probe_endpoint(label: str, base_url: str, api_key: str) -> None:
    """探活一个 OpenAI 兼容端点；超时 504，其余失败 502。"""
    try:
        await probe_models(base_url, api_key=api_key, timeout=BRAIN_PROBE_TIMEOUT)
    except ProbeError as exc:
        if exc.timed_out:
            raise SwitchError(
                504, f"{label} 在 {BRAIN_PROBE_TIMEOUT:.0f}s 内没有响应"
            ) from exc
        if exc.status is not None:
            raise SwitchError(502, f"{label} 返回 HTTP {exc.status}") from exc
        raise SwitchError(502, f"连不上{label}（{base_url}）") from exc


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
) -> tuple[ChatType | None, str | None, web.Response | None]:
    raw_chat_type = (request.query.get("chat_type") or "").strip()
    chat_type: ChatType | None = None
    if raw_chat_type:
        try:
            chat_type = ChatType(raw_chat_type)
        except ValueError:
            return None, None, web.json_response(
                {"error": "chat_type 必须是 group 或 private"}, status=400
            )

    chat_id: str | None = None
    raw_chat_id = (request.query.get("chat_id") or "").strip()
    if raw_chat_id:
        try:
            chat_id = normalize_chat_id(raw_chat_id)
        except ValueError as exc:
            return None, None, web.json_response({"error": str(exc)}, status=400)
    if chat_id is not None and chat_type is None:
        return None, None, web.json_response(
            {"error": "指定 chat_id 时必须同时指定 chat_type"}, status=400
        )
    return chat_type, chat_id, None


def _path_target(
    request: web.Request,
) -> tuple[ChatType | None, str | None, web.Response | None]:
    raw_type = request.match_info.get("chat_type", "")
    try:
        chat_type = ChatType(raw_type)
    except ValueError:
        return None, None, web.json_response(
            {"error": "chat_type 必须是 group 或 private"}, status=400
        )
    raw_id = request.match_info.get("chat_id", "")
    try:
        chat_id = normalize_chat_id(raw_id)
    except ValueError as exc:
        return None, None, web.json_response({"error": str(exc)}, status=400)
    return chat_type, chat_id, None


def _path_id(
    request: web.Request, name: str, label: str
) -> tuple[str | None, web.Response | None]:
    raw = request.match_info.get(name, "")
    try:
        value = normalize_chat_id(raw, label=label)
    except ValueError as exc:
        return None, web.json_response({"error": str(exc)}, status=400)
    return value, None


def _user_target(
    request: web.Request,
) -> tuple[ChatType | None, str | None, str | None, web.Response | None]:
    raw_type = request.match_info.get("chat_type", "")
    try:
        chat_type = ChatType(raw_type)
    except ValueError:
        return None, None, None, web.json_response(
            {"error": "chat_type 必须是 group 或 private"}, status=400
        )
    chat_id, error = _path_id(request, "chat_id", "chat_id")
    if error is not None:
        return None, None, None, error
    user_id, error = _path_id(request, "user_id", "user_id")
    if error is not None:
        return None, None, None, error
    return chat_type, chat_id, user_id, None


def _wechat_adapter(control: Any) -> Any | None:
    adapters = getattr(control.service, "adapters", None)
    if isinstance(adapters, dict):
        adapter = adapters.get("wechat")
        if adapter is not None:
            return adapter
    return None


def _kill_switch_path(control: Any) -> Path:
    return Path(str(control.settings.wechat.kill_switch_file))


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
        "reply_to_message_id": message.reply_to_message_id,
        "reply_to_user_id": message.reply_to_user_id,
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
        chat_titles: Callable[[str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.service = service
        self.adapter = adapter
        self.runtime_path = runtime_path
        # 群白名单卡片要显示「wxg:<alias>」对应的微信群名；由启动方注入，
        # 读不到就退回显示裸 id（不影响保存）。
        self._chat_titles = chat_titles
        self.runtime: RuntimeState = load_runtime(runtime_path)
        self._lock = asyncio.Lock()
        self._last_notify_at: float | None = None
        # 由启动方注入（Task 16 的 POST /api/opencode/reload）。本模块不猜
        # supervisor 的地址与 token；没注入时切换照样落盘，只把 reload 标成 pending。
        self.opencode_reload: Callable[[], Awaitable[dict[str, Any]]] | None = None
        # switch_mode 成功后钉住「当前活着的 brain」：三档都由 opencode 提供，
        # snapshot 必须与切换响应一致，不能继续报启动时的 token/local_model。
        self._mode_provider = ""
        # 重启后也要能报出模型名：_mode_model 以前只在切档时赋值，于是进程一
        # 重启 GET /api/brain 就回空串，面板与脚本都读不到。这里按当前 settings
        # 直接重算一次，等价于「刚用这个档位切过档」。
        self._mode_model = self._initial_mode_model()
        # switch_mode 成功后的「生效视图」：self.settings 要到重启才重新加载，
        # 这里先钉住刚写进覆盖层的 key/local 配置，GET /api/brain 才不会回退。
        self._mode_view: dict[str, Any] | None = None
        self.budget_guard = BudgetGuard(store, settings.budget)
        # 免费档自检：结果只留在内存（RuntimeState），启动时探一次，之后按
        # FREE_PROBE_TTL 懒刷新。_free_probe_at 用单调时钟，不落盘。
        self._free_probe_at = 0.0
        self._free_probe_task: asyncio.Task | None = None

    def _initial_mode_model(self) -> str:
        """启动时按当前档位重算展示用模型名（free/key/local_model 三档同源）。

        只用 ``build_config`` 的 ``model`` 字段，不碰密钥与端点：这里要的是
        「面板该显示什么」，不是能用的客户端。配置本身非法时宁可为空，也不能
        因为一个展示字段把整个进程打挂。
        """
        cfg = self.settings.brain
        try:
            data = build_config(cfg.mode, cfg, local=self.settings.local_model)
            return str(data.get("model") or "")
        except (ValueError, KeyError, TypeError):
            return ""

    def _local_token(self) -> str:
        """本地档当前真正可用的令牌（不含 "local" 占位哨兵）。"""
        stored = read_secret(LOCAL_KEY_PATH)
        if stored:
            return stored
        configured = str(self.settings.local_model.api_key or "").strip()
        return "" if configured == "local" else configured

    def _free_probe_client(self) -> Any:
        """免费档自检用的 HTTP 传输：opencode brain 自己的 httpx client。

        没有 HTTP 传输（测试替身、还没换脑）时返回 None，自检直接跳过——
        这种情况下的 free_available 保持 None（未探测），不谎报 False。
        """
        brain = getattr(self.service, "brain", None)
        client = getattr(brain, "_client", None)
        if client is None:
            # 跳过要留痕：否则「面板一直不亮黄条」看不出是没探还是探不了。
            log.debug("免费模型自检跳过：未找到 HTTP 传输，本次不探测")
        return client

    async def _opencode_ready(self, client: Any) -> bool | None:
        """opencode 是否已就绪；拿不到就绪信号时返回 None。

        判据与 supervisor / OpenCodeServer 的就绪探测一致：GET /api/info，
        状态码 < 500 就算活着（opencode v2 起 serve 强制 Basic 认证，401
        同样说明服务在）。传输对象没有 ``get``（测试替身）时拿不到信号。
        """
        probe = getattr(client, "get", None)
        if not callable(probe):
            return None
        try:
            response = await probe("/api/info", timeout=FREE_PROBE_READY_TIMEOUT)
        except Exception:  # noqa: BLE001 - 连不上就是没就绪
            return False
        status = getattr(response, "status_code", None)
        if status is None:
            # 测试替身常常只有 raise_for_status()：拿不到状态码就当它活着。
            return True
        return int(status) < 500

    def _free_probe_due(self) -> bool:
        """自检是否该跑：没探过（含刚切回免费档）或结果已过期。"""
        if self.runtime.free_available is None:
            return True
        return time.monotonic() - self._free_probe_at >= FREE_PROBE_TTL

    def schedule_free_probe(self, *, force: bool = False) -> None:
        """安排一次后台自检：只在免费档、且结果过期时才真的去跑。

        启动流程与 GET /api/brain 都调这里。探测最慢要 8 秒，谁都不等它：
        任务异常只记日志，绝不冒进请求 handler，也不阻塞响应。
        """
        try:
            if self.snapshot().get("mode") != "free":
                return
            if self._free_probe_client() is None:
                return
            if not force and not self._free_probe_due():
                return
            task = self._free_probe_task
            if task is not None and not task.done():
                return
            # 先盖时间戳再去探：同一 TTL 窗口内最多一次，面板轮询不会堆任务。
            self._free_probe_at = time.monotonic()
            self._free_probe_task = asyncio.get_running_loop().create_task(
                self._run_free_probe()
            )
        except Exception as exc:  # noqa: BLE001 - 自检调度失败只降级
            log.debug("免费模型自检调度失败：%s", exc)

    async def _run_free_probe(self) -> None:
        client = self._free_probe_client()
        if client is None:
            return
        ready = await self._opencode_ready(client)
        if ready is False:
            # opencode 正在重启/没起来：这一轮直接跳过，保持上一次结论，
            # 也不把这个「不算数」的失败缓存进 TTL——下次轮询立刻重试。
            self._free_probe_at = 0.0
            log.debug("免费模型自检跳过：opencode 未就绪")
            return
        try:
            result = await probe_free_model_detail(client)
        except Exception:
            log.warning("免费模型自检异常", exc_info=True)
            result = FreeProbeResult(False, "探测异常")
        available, reason = result.available, result.reason
        if not available and ready is None and self.runtime.free_available is not None:
            # 拿不到就绪信号时区分不了「模型坏了」和「opencode 正好在重启」：
            # 保留旧结论，宁可晚一轮报警，也不谎报 600 秒。
            self._free_probe_at = 0.0
            log.debug("免费模型自检未通过，但无法确认 opencode 就绪，保留上一次结果")
            return
        self.runtime = replace(
            self.runtime, free_available=available, free_reason=reason
        )
        log.info("免费模型自检结果：%s", "可用" if available else f"不可用（{reason}）")

    def _brain_view(self, provider: str) -> dict[str, Any]:
        """GET /api/brain 的档位视图：只含 has_key，绝不含密钥本身。"""
        if self._mode_view is not None:
            view = self._mode_view
            return {
                "mode": view["mode"],
                "key": dict(view["key"]),
                "local": dict(view["local"]),
            }
        mode = (
            self.settings.brain.mode
            if self.settings.brain.mode_from_overrides
            else _mode_for_provider(provider)
        )
        return {
            "mode": mode,
            "key": {
                "base_url": self.settings.brain.key_base_url,
                "model": self.settings.brain.key_model,
                "has_key": secret_exists(KEY_PATH),
            },
            "local": {
                "base_url": self.settings.local_model.base_url,
                "model": local_model_name(self.settings, self.runtime),
                "context": self.settings.local_model.context,
                "lean": bool(self.settings.local_model.lean),
                "has_key": bool(self._local_token()),
            },
        }

    def snapshot(self) -> dict[str, Any]:
        provider = self._mode_provider or current_provider(self.settings, self.runtime)
        if provider == "local_model":
            model = local_model_name(self.settings, self.runtime)
        elif provider == "token":
            model = self.settings.model
        else:
            # opencode：展示 switch_mode 钉住的档位模型；老 switch() 进来时为空。
            model = self._mode_model
        view = self._brain_view(provider)
        return {
            "provider": provider,
            "mode": view["mode"],
            "model": model,
            "key": view["key"],
            "local": view["local"],
            "modes": mode_status(self.settings, self.runtime),
            "gateway_connected": bool(getattr(self.adapter, "connected", False)),
            # 免费档自检结果：None = 未探测，面板只在 False 时亮黄条。
            "free_available": self.runtime.free_available,
            "free_reason": self.runtime.free_reason,
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
            window = int(
                getattr(
                    self.settings.alerts.rules,
                    "reply_error_window_seconds",
                    ERROR_STATS_WINDOW_SECONDS,
                )
            )
            recent = self.store.error_stats(window)
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
        budget: dict[str, Any] = {
            "per_chat": {
                "calls": today_calls,
                "tokens": today_tokens,
                "daily_calls": self.settings.budget.daily_calls,
                "daily_tokens": self.settings.budget.daily_tokens,
            },
            "global_daily": {
                "calls": 0,
                "tokens": 0,
                "limit_calls": self.settings.budget.global_daily_calls,
                "limit_tokens": self.settings.budget.global_daily_tokens,
            },
            "monthly": {
                "calls": 0,
                "tokens": 0,
                "limit_calls": self.settings.budget.monthly_calls,
                "limit_tokens": self.settings.budget.monthly_tokens,
            },
            "cost": {
                "cost": 0.0,
                "cost_limit": self.settings.budget.monthly_cost_limit,
                "currency": self.settings.budget.currency,
                "estimated": False,
            },
            "warnings": [],
        }
        try:
            report = self.budget_guard.report(ChatType.GROUP, 0)
        except Exception as exc:  # noqa: BLE001 - 健康快照必须保持可用
            budget["warnings"] = [
                {"scope": "storage_error", "ratio": 1.0, "calls": 0, "limit": 0}
            ]
            log.warning("读取预算快照失败：%s", exc)
        else:
            budget["global_daily"] = dict(report.global_daily)
            budget["monthly"] = dict(report.monthly)
            budget["cost"] = dict(report.cost)
            budget["warnings"] = [dict(item) for item in report.warnings]

        channels: dict[str, Any] = {}
        channel_status = getattr(self.service, "channel_status", None)
        if callable(channel_status):
            try:
                raw_channels = channel_status()
            except Exception as exc:  # noqa: BLE001 - 通道状态失败不能让探活 500
                log.warning("读取通道状态失败：%s", exc)
            else:
                if isinstance(raw_channels, dict):
                    for name, item in raw_channels.items():
                        if not isinstance(item, dict):
                            item = {}
                        entry: dict[str, Any] = {
                            "connected": bool(item.get("connected")),
                            "degraded": bool(item.get("degraded")),
                            "last_event_at": _iso_ts(item.get("last_event_at")),
                            "last_disconnect_at": _iso_ts(
                                item.get("last_disconnect_at")
                            ),
                            "last_send_at": _iso_ts(item.get("last_send_at")),
                        }
                        # 微信适配器额外暴露登录态与桌面状态；QQ 没有这两个属性，
                        # 只按状态字典里真实存在的键补，避免凭空造字段。
                        for extra in ("logged_in", "desktop"):
                            if extra in item:
                                entry[extra] = item[extra]
                        channels[str(name)] = entry

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
            "channels": channels,
            "keepalive": (
                self.service.keepalive_status()
                if callable(getattr(self.service, "keepalive_status", None))
                else None
            ),
            "traffic": {
                "last_message_at": last_event_at,
                "last_reply_at": _iso_ts(getattr(self.adapter, "last_send_at", None)),
            },
            "scheduler": {
                "pending_chats": _count("pending_count"),
                "inflight_chats": _count("inflight_count"),
            },
            "budget": budget,
            "quota": {
                "paused_until": float(quota_until) if quota_paused else None,
                "reason": "额度熔断" if quota_paused else "",
            },
            "brain": {"provider": state["provider"], "model": state["model"]},
            # 免费档降级提示的唯一数据源：supervisor 的 /api/state 会把它透传给面板。
            "free_available": state["free_available"],
            "free_reason": state["free_reason"],
            "memory": memory,
            "errors": errors,
        }

    def budget_snapshot(self, chat_type: ChatType, chat_id: str) -> dict[str, Any]:
        """单会话预算报告，字段与健康快照的 budget 一致并附带会话标识。"""
        report = self.budget_guard.report(chat_type, chat_id)
        return {
            "chat_type": chat_type.value,
            "chat_id": chat_id,
            "per_chat": dict(report.per_chat),
            "global_daily": dict(report.global_daily),
            "monthly": dict(report.monthly),
            "cost": dict(report.cost),
            "warnings": [dict(item) for item in report.warnings],
        }

    def explain_trigger(
        self, msg: Message, *, seed: int | None = None
    ) -> dict[str, Any]:
        explain = getattr(self.service, "explain_trigger", None)
        if not callable(explain):
            raise NotImplementedError("当前服务不支持触发调试")
        return cast(dict[str, Any], explain(msg, seed=seed))

    def preview_batch(self, chat_type: ChatType, chat_id: str) -> dict[str, Any]:
        """只读预演下一次批次；服务不支持时抛 NotImplementedError（路由转 503）。"""
        preview = getattr(self.service, "preview_batch", None)
        if not callable(preview):
            raise NotImplementedError("当前服务不支持批次预览")
        result = preview(chat_type, chat_id)
        as_dict = getattr(result, "as_dict", None)
        if not callable(as_dict):
            raise NotImplementedError("当前服务不支持批次预览")
        return cast(dict[str, Any], as_dict())

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

            if target == "local_model":
                # 端点没起就先把 LM Studio 拉起来；失败也继续切换，只记日志。
                await ensure_server(
                    self.settings.local_model,
                    model or local_model_name(self.settings, self.runtime),
                )

            try:
                brain = build_brain(self.settings, self.store, target, model)
            except ConfigError as exc:
                raise SwitchError(409, str(exc)) from exc
            except Exception as exc:  # 构造期的其它失败同样不该打断现有大脑
                log.exception("构建 %s brain 失败", target)
                raise SwitchError(500, f"构建 {target} 失败：{exc}") from exc

            previous = self.service.set_brain(brain)
            self.runtime = self.runtime.with_provider(target, model)
            # 老协议接管后，snapshot 回到 runtime/config 的口径。
            self._mode_provider = ""
            self._mode_model = ""
            self._mode_view = None
            try:
                save_runtime(self.runtime, self.runtime_path)
            except OSError as exc:
                log.warning("runtime.json 写入失败：%s", exc)
            asyncio.create_task(self._close_later(previous))
            log.info("大脑已切换：%s -> %s", before, target)
            return self.snapshot()

    async def switch_mode(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按用户可见的三档（free / key / local_model）切换大脑。

        顺序固定：校验 →（非 force 时）探活 → 写 provider key → 写覆盖层 →
        重生 opencode.jsonc 与 agent 文件 → reload → 清会话缓存 → 重建 brain。
        校验与探活都发生在第一次写盘之前，所以填错端点不会留下半套配置。
        生成物以「刚持久化的覆盖层」为基线（省略 context/lean 时沿用上一轮），
        三档都由 opencode 承载，重建后 `snapshot()` 也切到 opencode。

        生成物路径：jsonc 在 `ensure_isolated_config(opencode.config_home)` 的
        配置根下（config_home 留空时沿用当前工作目录的 `opencode/`，与软回滚
        语义一致）；agent 在 `opencode.project_dir` 下，project_dir 为空才回退
        到安装根（仓库里的 `.opencode/agents/`）。

        reload 走注入的 `self.opencode_reload`：没注入、抛异常或没有显式
        `{"ok": true}` 回执都只降级为 `reload: "pending"` 并记一条 warning，
        绝不回滚已经写入的配置。生成物写失败则回滚覆盖层。

        清会话缓存 / 重建 brain 发生在全部落盘之后，失败只记 warning 并保持
        成功返回形状；下半段失败不该让面板以为配置没生效。
        """
        mode, key, local, force = _brain_mode_payload(payload)

        async with self._lock:
            try:
                existing = read_overrides(OVERRIDES_PATH)
            except OverrideError as exc:
                raise SwitchError(500, f"覆盖层读取失败：{exc}") from exc

            # 探活与生成都用「本次真的会生效的本地令牌」：payload 没给就沿用
            # 本地专用密钥文件，再回落到 config.yaml，最后才排掉 "local" 哨兵。
            # 自备 Key 文件（provider.key）不参与本地档，避免两档互相污染。
            local_stored = read_secret(LOCAL_KEY_PATH)
            local_configured = str(self.settings.local_model.api_key or "").strip()
            if local_configured == "local":
                local_configured = ""
            local_key = local["api_key"] or local_stored or local_configured
            local_has_key = bool(local_key)

            if not force:
                if mode == "key":
                    # 空 api_key = 沿用已保存的密钥；探活也得用它，否则需要鉴权
                    # 的端点会被误判成不可达。
                    await _probe_endpoint(
                        "自备 Key 端点",
                        key["base_url"],
                        key["api_key"] or read_secret(KEY_PATH),
                    )
                elif mode == "local_model":
                    await _probe_endpoint("本地模型端点", local["base_url"], local_key)

            # 生成基线是「刚持久化的覆盖层」，不是启动快照：面板省略
            # context/lean 时，覆盖层里上一轮的值仍然有效，生成物必须跟它一致。
            merged = _merged_overrides(existing, mode, key, local)

            # key 档的模型名会过期（官方换版本号，旧名从清单里消失），落盘前
            # 按端点清单对齐一次：清单里有就原样保留，没有就退到 flash。
            # 清单拉不到只跳过对齐——判定失败绝不能让保存跟着失败。
            model_warning = ""
            if mode == "key":
                section = _merged_section(merged, "key")
                probe_base = str(
                    section.get("base_url") or self.settings.brain.key_base_url
                )
                current_model = str(
                    section.get("model") or self.settings.brain.key_model
                )
                ids = await _probe_model_ids(
                    probe_base, key["api_key"] or read_secret(KEY_PATH)
                )
                if not current_model:
                    # 留空 = 让端点清单决定用哪个 flash。选不出来就明确报错，
                    # 绝不落盘 `dafeiyu-key/` 这种跑不起来的配置。
                    chosen = pick_flash_model(ids)
                    if not chosen:
                        raise SwitchError(
                            502, "端点清单里找不到 flash 模型，请手动填写 model"
                        )
                    merged = _with_key_model(merged, chosen)
                    model_warning = f"未指定模型，已按端点清单选择 {chosen}"
                elif ids and current_model not in ids:
                    chosen = pick_flash_model(ids)
                    if chosen:
                        merged = _with_key_model(merged, chosen)
                        model_warning = (
                            f"模型 {current_model} 不在端点清单内，"
                            f"已自动切换为 {chosen}"
                        )

            key_section = _merged_section(merged, "key")
            local_section = _merged_section(merged, "local_model")
            cfg = replace(
                self.settings.brain,
                mode=mode,
                key_base_url=str(
                    key_section.get("base_url") or self.settings.brain.key_base_url
                ),
                key_model=str(
                    key_section.get("model") or self.settings.brain.key_model
                ),
            )
            context = local_section.get("context", self.settings.local_model.context)
            lean = local_section.get("lean", self.settings.local_model.lean)
            local_cfg = replace(
                self.settings.local_model,
                base_url=str(
                    local_section.get("base_url") or self.settings.local_model.base_url
                ),
                model=str(
                    local_section.get("model") or self.settings.local_model.model
                ),
                context=(
                    context
                    if isinstance(context, int) and not isinstance(context, bool)
                    else self.settings.local_model.context
                ),
                lean=lean if isinstance(lean, bool) else self.settings.local_model.lean,
            )

            config_home = str(self.settings.opencode.config_home or "").strip()
            project_dir = str(self.settings.opencode.project_dir or "").strip()
            agent_root = Path(project_dir) if project_dir else Path(INSTALL_ROOT)
            # 配置目录与 ensure_isolated_config 的解析方式一致（空 config_home
            # = 当前工作目录），密钥引用据此现算。
            config_dir = config_path(Path(config_home or ".")).resolve().parent
            secret_path = LOCAL_KEY_PATH if mode == "local_model" else KEY_PATH
            key_rel = _key_reference(config_dir, secret_path)
            use_key_ref = mode == "key" or (mode == "local_model" and local_has_key)
            data = build_config(
                mode,
                cfg,
                key_rel=key_rel if use_key_ref else None,
                local=local_cfg,
            )
            try:
                template = AGENT_TEMPLATE_PATH.read_text(encoding="utf-8")
            except OSError as exc:
                raise SwitchError(500, "agent 模板读取失败，未做任何写入") from exc
            try:
                memory_template = MEMORY_AGENT_TEMPLATE_PATH.read_text(encoding="utf-8")
            except OSError as exc:
                raise SwitchError(500, "记忆 agent 模板读取失败，未做任何写入") from exc
            agent_text = build_agent(
                mode,
                lean=local_cfg.lean if mode == "local_model" else False,
                template=template,
                cfg=cfg,
                local=local_cfg,
            )
            memory_agent_text = build_memory_agent(
                mode, template=memory_template, cfg=cfg, local=local_cfg
            )

            # 两档各写各的密钥文件：key → provider.key，local → provider-local.key。
            # 为空都保持原文件不动；local 档永远不碰 provider.key。
            secret_value = key["api_key"] if mode == "key" else ""
            if mode == "local_model" and local_has_key:
                secret_value = local_key
            previous_secret = ""
            if secret_value:
                previous_secret = read_secret(secret_path)
                try:
                    write_secret(secret_value, secret_path)
                except OSError as exc:
                    raise SwitchError(
                        500, f"凭据写入失败（{type(exc).__name__}）"
                    ) from exc

            had_overrides = Path(OVERRIDES_PATH).exists()
            try:
                write_overrides(merged, OVERRIDES_PATH)
            except OSError as exc:
                # 密钥已经落盘但模式没写进去：同样只留一种状态。
                if secret_value:
                    _restore_secret(previous_secret, secret_path)
                raise SwitchError(
                    500, f"覆盖层写入失败（{type(exc).__name__}）"
                ) from exc

            try:
                backed_up = write_generated(
                    config_path(ensure_isolated_config(config_home)),
                    render_config(data),
                )
                backed_up = (
                    write_generated(agent_path(agent_root), agent_text) or backed_up
                )
                backed_up = (
                    write_generated(
                        memory_agent_path(agent_root), memory_agent_text
                    )
                    or backed_up
                )
            except OSError as exc:
                # 覆盖层已落盘但生成物没写成：恢复上一版覆盖层与密钥，别把
                # 「一半生效」的状态留给面板。
                if secret_value:
                    _restore_secret(previous_secret, secret_path)
                _restore_overrides(existing, had_overrides)
                raise SwitchError(
                    500,
                    f"生成 opencode 配置失败（{type(exc).__name__}），覆盖层已回滚",
                ) from exc

            warnings: list[str] = []
            if model_warning:
                warnings.append(model_warning)
            if backed_up:
                warnings.append("旧的 opencode 文件不是生成物，已备份为 .bak 后覆盖")
            reload_state, reload_warnings = await self._reload_opencode()
            warnings.extend(reload_warnings)

            try:
                self.store.clear_opencode_sessions()
            except Exception as exc:  # noqa: BLE001 - 清缓存失败不该让切换失败
                log.warning("清空 opencode 会话缓存失败：%s", type(exc).__name__)
                warnings.append("旧会话缓存清理失败，部分会话可能仍沿用切换前的模型")

            # 三档都由 opencode 提供，只是喂给它的 provider 配置不同，所以
            # 重建的是 opencode brain，snapshot 也跟着切到 opencode。
            provider = "opencode"
            try:
                brain = build_brain(self.settings, self.store, provider, "")
            except Exception as exc:  # noqa: BLE001 - 已落盘，失败只降级为 warning
                log.warning("重建 %s brain 失败：%s", provider, type(exc).__name__)
                warnings.append(f"大脑重建失败，重启后生效：{type(exc).__name__}")
            else:
                previous = self.service.set_brain(brain)
                self._mode_provider = provider
                self._mode_model = str(data.get("model") or "")
                self._mode_view = {
                    "mode": mode,
                    "key": {
                        "base_url": cfg.key_base_url,
                        "model": cfg.key_model,
                        "has_key": secret_exists(KEY_PATH),
                    },
                    "local": {
                        "base_url": local_cfg.base_url,
                        "model": local_cfg.model,
                        "context": local_cfg.context,
                        "lean": bool(local_cfg.lean),
                        "has_key": local_has_key,
                    },
                }
                runtime = self.runtime
                if mode == "local_model":
                    # 面板刚选的本地模型必须写回运行时：就绪判定只读 runtime 与
                    # 配置，不写回的话 modes[] 会继续报「未选择模型」，与同一份
                    # 快照里的 model 字段自相矛盾，前端会把本地档置灰。
                    runtime = runtime.with_provider("local_model", local_cfg.model)
                # 三档的 provider 都是 opencode；上面的本地模型记忆必须保留。
                self.runtime = runtime.with_provider(provider, "")
                try:
                    save_runtime(self.runtime, self.runtime_path)
                except OSError as exc:
                    log.warning("runtime.json 写入失败：%s", exc)
                    warnings.append("runtime.json 写入失败，重启后可能回到旧 provider")
                asyncio.create_task(self._close_later(previous))
            log.info("大脑模式已切换：%s（provider=%s）", mode, provider)
            return {
                "mode": mode,
                "model": str(data.get("model") or ""),
                "reload": reload_state,
                "warnings": warnings,
            }

    async def _reload_opencode(self) -> tuple[str, list[str]]:
        """让注入的重载通道重启 opencode；任何失败都只降级为 pending。

        警告里只带异常类名：reload 的异常消息可能来自 supervisor 的响应体，
        不该因为一次重载失败把任何凭据/内部细节带进面板。
        """
        reloader = self.opencode_reload
        if reloader is None:
            return "pending", ["未接入 opencode 重载通道，需重启 opencode 才会生效"]
        try:
            result = await reloader()
        except Exception as exc:  # noqa: BLE001 - 重载失败不回滚已写入的配置
            log.warning("opencode 重载失败：%s", type(exc).__name__)
            return "pending", [
                f"opencode 重载失败（{type(exc).__name__}），需重启 opencode 才会生效"
            ]
        if isinstance(result, dict) and result.get("ok") is True:
            return "ok", []
        # 只认显式 ack：None / 缺字段 / 其它真值都不能当成重载成功。
        # supervisor 的 error 已经过 redact()，带上它比「未确认成功」好排查。
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("error") or "").strip()[:120]
        if reason:
            return "pending", [
                f"opencode 重载失败：{reason}（需重启 opencode 才会生效）"
            ]
        return "pending", ["opencode 重载未确认成功，需重启 opencode 才会生效"]

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
        chat_id: str | None = None,
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

    def memory_health(self, *, now: datetime | None = None) -> dict[str, Any]:
        """记忆链路体检：最近几天有缺口、最近一条摘要、是否熔断。

        摘要 worker 只补「昨天」，缺口平时只出现在日志里。这里把缺口做成
        只读数据给面板，看板一眼就能判断「记忆是不是落后了」。

        「今天」永远不算缺口：摘要次日 4 点才生成。4 点前连「昨天」也还没到期，
        窗口再往前收一天，避免每天凌晨面板都在误报。
        """
        now = now or now_shanghai()
        lag = 1 if now.hour >= 4 else 2
        until = (now.date() - timedelta(days=lag)).isoformat()
        slots = self.store.pending_summary_slots(until=until)
        pending = sorted({slot.date for slot in slots})
        latest = self.store.recent_summaries(1, 0)
        quota_until = self.store.load_backoff().get(QUOTA_KEY)
        quota_paused = bool(quota_until and float(quota_until) > time.time())
        last = latest[0] if latest else None
        return {
            "ok": not pending and not quota_paused,
            "pending_days": pending,
            "pending_count": len(pending),
            "pending_slots": [
                {
                    "chat_type": slot.chat_type.value,
                    "chat_id": slot.chat_id,
                    "date": slot.date,
                    "messages": slot.messages,
                }
                for slot in slots
            ],
            "summary_total": self.store.count_summaries(),
            "impression_total": self.store.count_impressions(),
            "last_summary": None
            if last is None
            else {
                "chat_type": last.chat_type.value,
                "chat_id": last.chat_id,
                "date": last.date,
                "updated_at": last.updated_at,
            },
            "quota": {
                "paused": quota_paused,
                "until": float(quota_until) if quota_paused else None,
            },
        }

    async def list_models(self) -> list[str]:
        """探测本机 LM Studio 的模型列表；默认行为保持不变（409/504/502）。"""
        base = normalize_base_url(self.settings.local_model.base_url)
        if not base:
            raise SwitchError(409, "未配置 local_model.base_url")
        try:
            return await probe_models(base, timeout=MODEL_PROBE_TIMEOUT)
        except ProbeError as exc:
            if exc.timed_out:
                raise SwitchError(
                    504, f"LM Studio 在 {MODEL_PROBE_TIMEOUT:.0f}s 内没有响应"
                ) from exc
            if exc.status is not None:
                raise SwitchError(502, f"LM Studio 返回 HTTP {exc.status}") from exc
            raise SwitchError(502, f"连不上 LM Studio（{base}）") from exc

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
        chat_id: str | None = None,
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
        chat_id: str,
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
        self, chat_type: ChatType, chat_id: str, limit: int = 1000
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
        self, action: str, chat_type: ChatType, chat_id: str
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

    def _behavior(self):
        """当前生效的行为快照：优先 Service 的运行时值，兜底启动配置。"""
        return getattr(self.service, "behavior", self.settings.behavior)

    def behavior_snapshot(self) -> dict[str, Any]:
        return as_payload(self._behavior())

    def save_behavior(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验 → 落覆盖层 → 即时应用；任一步失败都不改线上行为。"""
        behavior = parse_behavior(payload)
        try:
            existing = read_overrides(OVERRIDES_PATH)
        except OverrideError as exc:
            raise OverrideError(str(exc)) from exc
        current = existing.get("behavior")
        section = dict(current) if isinstance(current, dict) else {}
        section.update(
            {
                "chat_intensity": behavior.chat_intensity,
                "restriction_mode": behavior.restriction_mode,
                "banned_phrases": list(behavior.banned_phrases),
                "banned_topics": list(behavior.banned_topics),
            }
        )
        existing["behavior"] = section
        write_overrides(existing, OVERRIDES_PATH)
        setter = getattr(self.service, "set_behavior", None)
        if callable(setter):
            setter(behavior)
        return as_payload(behavior)

    def _groups(self) -> GroupsConfig:
        """当前生效的群白名单：优先 Service 的运行时值，兜底启动配置。"""
        runtime = getattr(self.service, "groups", None)
        if isinstance(runtime, GroupsConfig):
            return runtime
        service_settings = getattr(self.service, "settings", None)
        groups = getattr(service_settings, "groups", None)
        if isinstance(groups, GroupsConfig):
            return groups
        return self.settings.groups

    def groups_snapshot(self) -> dict[str, Any]:
        groups = self._groups()
        known: list[dict[str, Any]] = []
        for item in self.store.chat_overview():
            if str(item.get("chat_type") or "") != "group":
                continue
            chat_id = str(item.get("chat_id") or "")
            if not chat_id:
                continue
            channel = chat_id.split(":", 1)[0] if ":" in chat_id else "qq"
            title = ""
            if self._chat_titles is not None:
                try:
                    title = str(self._chat_titles(chat_id) or "").strip()
                except Exception:  # noqa: BLE001 - 群名只是展示信息，读不到不影响白名单
                    title = ""
            known.append(
                {
                    "chat_id": chat_id,
                    "channel": channel,
                    "title": title,
                    "message_count": int(item.get("message_count") or 0),
                    "last_message_at": item.get("last_message_at"),
                }
            )
        return {
            "whitelist": sorted(groups.whitelist),
            "blacklist": sorted(groups.blacklist),
            # 前端据此显示「留空 = 不限制回复对象」，语义与 Service 判定一致。
            "empty_means": "all",
            "known": known,
        }

    def save_groups(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验 → 落覆盖层 → 即时应用；任一步失败都不改线上白名单。"""
        if not isinstance(payload, dict):
            raise ConfigError("请求体必须是对象")
        if "whitelist" not in payload:
            raise ConfigError("缺少 groups.whitelist")
        whitelist = parse_group_ids(payload["whitelist"], "groups.whitelist")
        current = self._groups()
        groups = GroupsConfig(
            whitelist=whitelist,
            blacklist=current.blacklist,
            admins=current.admins,
        )
        try:
            existing = read_overrides(OVERRIDES_PATH)
        except OverrideError as exc:
            raise OverrideError(str(exc)) from exc
        section = existing.get("groups")
        section = dict(section) if isinstance(section, dict) else {}
        section["whitelist"] = sorted(whitelist)
        existing["groups"] = section
        write_overrides(existing, OVERRIDES_PATH)
        setter = getattr(self.service, "set_groups", None)
        if callable(setter):
            setter(groups)
        else:
            self.settings = replace(self.settings, groups=groups)
        return self.groups_snapshot()

    def private_chats_snapshot(self) -> dict[str, Any]:
        """微信好友私聊只读列表；标题失败只回落 chat_id，不影响其它会话。"""

        def as_count(value: Any) -> int:
            """统计列只用于展示：非整数一律按 0，不让脏数据把整张卡带崩。"""
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0
            return int(value)

        chats: list[dict[str, Any]] = []
        for item in self.store.chat_overview():
            if str(item.get("chat_type") or "") != ChatType.PRIVATE.value:
                continue
            chat_id = str(item.get("chat_id") or "")
            if not chat_id.startswith("wxg:"):
                continue
            message_count = as_count(item.get("message_count"))
            title = chat_id
            try:
                recent = self.store.recent_messages(
                    ChatType.PRIVATE, chat_id, 20
                )
            except Exception as exc:  # noqa: BLE001 - 标题只是展示信息
                log.warning("读取微信私聊标题失败 %s：%s", chat_id, exc)
                recent = []
            for message in reversed(recent):
                nickname = str(message.nickname or "").strip()
                if nickname and not message.is_bot:
                    title = nickname
                    break
            chats.append(
                {
                    "chat_id": chat_id,
                    "title": title,
                    "message_count": message_count,
                    "last_message_at": item.get("last_message_at"),
                    "delivered_count": as_count(item.get("delivered_count")),
                    "session_id": item.get("session_id"),
                }
            )
        chats.sort(
            key=lambda chat: int(chat["last_message_at"] or 0), reverse=True
        )
        return {
            "chats": chats,
            "note": "仅列出已产生消息的微信好友私聊",
        }

    def _brain_supports_observer(self) -> bool:
        brain = getattr(self.service, "brain", None)
        return callable(getattr(brain, "complete_detailed", None))

    def mind_snapshot(self) -> dict[str, Any]:
        mind: MindConfig = self.settings.mind
        group_count = len(self._groups().whitelist)
        supports = self._brain_supports_observer()
        return {
            "enabled": bool(mind.enabled),
            "observer_enabled": bool(mind.observer_enabled),
            "observer_provider": mind.observer_provider,
            "observer_interval_seconds": int(mind.observer_interval_seconds),
            "observer_min_messages": int(mind.observer_min_messages),
            "observer_daily_calls": int(mind.observer_daily_calls),
            "group_count": group_count,
            # 白名单为空时没有观察对象，估算是 0 而不是「按 1 个群」硬凑。
            "estimated_daily_calls": int(mind.observer_daily_calls) * group_count,
            "brain_supports_observer": supports,
            # 与 ObserverWorker._enabled 一致：总闸 + 观察开关 + episode 开关，
            # 再叠加 brain 是否具备 complete_detailed（没有就永远调不动模型）。
            "effective": bool(
                mind.enabled
                and mind.observer_enabled
                and mind.episodes_enabled
                and supports
            ),
        }

    def save_mind(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验 → 落覆盖层 → 更新内存快照；不改运行中的 Observer。"""
        if not isinstance(payload, dict):
            raise ConfigError("请求体必须是对象")
        allowed = {"observer_enabled", "observer_daily_calls"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ConfigError(f"未知字段：{unknown[0]}")
        if not payload:
            raise ConfigError("至少提供 observer_enabled 或 observer_daily_calls")

        updates: dict[str, Any] = {}
        if "observer_enabled" in payload:
            value = payload["observer_enabled"]
            if not isinstance(value, bool):
                raise ConfigError("observer_enabled 必须是布尔值")
            updates["observer_enabled"] = value
        if "observer_daily_calls" in payload:
            value = payload["observer_daily_calls"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError("observer_daily_calls 必须是整数")
            if not 1 <= value <= 1000:
                raise ConfigError("observer_daily_calls 必须在 1-1000 之间")
            updates["observer_daily_calls"] = value

        try:
            existing = read_overrides(OVERRIDES_PATH)
        except OverrideError as exc:
            raise OverrideError(str(exc)) from exc
        raw = existing.get("mind")
        section = dict(raw) if isinstance(raw, dict) else {}
        section.update(updates)
        existing["mind"] = section
        write_overrides(existing, OVERRIDES_PATH)

        self.settings = replace(
            self.settings, mind=replace(self.settings.mind, **updates)
        )
        result = self.mind_snapshot()
        result["restart_required"] = True
        return result

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
                "repeat_guard": persona.repeat_guard,
                "opportunity_guard": persona.opportunity_guard,
            },
            "cooldown": {},
            "behavior": self.behavior_snapshot(),
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
        adapters = getattr(control.service, "adapters", None)

        def _pick_adapter(target: Any) -> tuple[Any, str]:
            if isinstance(adapters, dict) and adapters:
                try:
                    channel = channel_of(str(target))
                except ValueError:
                    channel = "qq"
                found = adapters.get(channel)
                if found is not None:
                    return found, channel
                if len(adapters) == 1:
                    only = next(iter(adapters.items()))
                    return only[1], only[0]
                return None, channel
            return control.adapter, "qq"

        adapter, channel = _pick_adapter(admins[0])
        if adapter is None or not bool(getattr(adapter, "connected", False)):
            return web.json_response({"error": f"{channel} 网关未连接"}, status=503)
        now = time.monotonic()
        last = control._last_notify_at
        if last is not None and now - last < NOTIFY_MIN_INTERVAL_SECONDS:
            return web.json_response(
                {"error": "通知过于频繁，请稍后再试"}, status=429
            )

        target = admins[0]
        try:
            await adapter.send(ChatType.PRIVATE, target, text)
        except Exception as exc:  # noqa: BLE001 - 发送失败回 503，不打断告警循环
            log.warning("管理员通知发送失败：%s", exc)
            return web.json_response({"error": "管理员通知发送失败"}, status=503)
        control._last_notify_at = now
        return web.json_response({"ok": True, "target": target})

    async def wechat_stop(_: web.Request) -> web.Response:
        path = _kill_switch_path(control)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stop\n", encoding="utf-8")
        log.warning("wechat_stopped: 急停已触发（%s）", path)
        adapter = _wechat_adapter(control)
        if adapter is not None:
            enter = getattr(adapter, "enter_kill_switch", None)
            if callable(enter):
                enter()
        return web.json_response({"ok": True, "event": "wechat_stopped"})

    async def wechat_resume(_: web.Request) -> web.Response:
        path = _kill_switch_path(control)
        if path.exists():
            return web.json_response(
                {"error": f"急停文件仍存在，请先删除 {path}"}, status=409
            )
        adapter = _wechat_adapter(control)
        if adapter is None:
            raise web.HTTPNotFound()
        resume = getattr(adapter, "resume", None)
        if not callable(resume):
            raise web.HTTPNotFound()
        try:
            await resume()
        except GatewayError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        return web.json_response({"ok": True, "event": "wechat_resumed"})

    async def wechat_inject(request: web.Request) -> web.Response:
        if not bool(control.settings.wechat.allow_test_inject):
            raise web.HTTPNotFound()
        adapter = _wechat_adapter(control)
        if adapter is None:
            raise web.HTTPNotFound()
        path = _kill_switch_path(control)
        if path.exists() or not bool(getattr(adapter, "connected", False)):
            return web.json_response(
                {"error": "微信通道已急停或未连接"}, status=409
            )
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        await adapter.dispatch_envelope(payload)
        return web.json_response({"ok": True})

    async def status(_: web.Request) -> web.Response:
        return web.json_response(control.snapshot())

    async def get_brain(request: web.Request) -> web.Response:
        # 面板每 2 秒轮询一次：顺手把过期的免费档自检排进后台，绝不等待它。
        # 面板刚打开时带 ?probe=1 强制重探，避免上次会话留下的旧黄条误导人。
        force = _truthy(request.query.get("probe"))
        control.schedule_free_probe(force=force)
        return web.json_response(control.snapshot())

    async def post_brain(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"error": "请求体必须是 JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "请求体必须是对象"}, status=400)
        if "mode" in payload:
            # 新协议：{mode, key, local, force}，落盘 + 重生生成物 + 重载。
            # 老协议仍带 provider，保留原分支，面板以外的调用方不用改。
            try:
                return web.json_response(await control.switch_mode(payload))
            except SwitchError as exc:
                return web.json_response({"error": exc.reason}, status=exc.status)
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
        try:
            user_id = normalize_chat_id(raw_user_id, label="user_id")
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

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
        if seed is not None and (
            isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
        ):
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
        # 调试器读失败不能把控制口打挂
        except Exception as exc:  # noqa: BLE001
            log.warning("触发调试失败：%s", exc)
            return web.json_response(
                {"error": f"触发调试失败：{exc.__class__.__name__}"}, status=500
            )

    async def debug_batch(request: web.Request) -> web.Response:
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        chat_type, chat_id, error = _chat_target(payload or {})
        if error is not None:
            return error
        try:
            return web.json_response(control.preview_batch(chat_type, chat_id))
        except NotImplementedError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        # 预览读失败不能把控制口打挂
        except Exception as exc:  # noqa: BLE001
            log.warning("批次预览失败：%s", exc)
            return web.json_response(
                {"error": f"批次预览失败：{exc.__class__.__name__}"}, status=500
            )

    async def models(request: web.Request) -> web.Response:
        """模型列表：不带 base_url 时探本机 LM Studio，带了就探任意兼容端点。

        `api_key` 只进请求头，任何响应都不回显。
        """
        base_url = (request.query.get("base_url") or "").strip()
        api_key = (request.query.get("api_key") or "").strip()
        if request.can_read_body:
            payload, error = await _read_json_object(request)
            if error is not None:
                return error
            base_url = str(payload.get("base_url") or base_url).strip()
            api_key = str(payload.get("api_key") or api_key).strip()
        try:
            base = normalize_base_url(base_url)
            if not base:
                return web.json_response({"models": await control.list_models()})
            found = await probe_models(
                base, api_key=api_key, timeout=MODEL_PROBE_TIMEOUT
            )
            return web.json_response({"models": found})
        except SwitchError as exc:
            return web.json_response({"error": exc.reason}, status=exc.status)
        except ProbeError as exc:
            return web.json_response(
                {"error": str(exc)}, status=504 if exc.timed_out else 502
            )

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

        chat_id: str | None = None
        raw_chat_id = (request.query.get("chat_id") or "").strip()
        if raw_chat_id:
            try:
                chat_id = normalize_chat_id(raw_chat_id)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)

        try:
            return web.json_response(
                control.memory_snapshot(limit, offset, chat_type, chat_id)
            )
        # 读库失败不能把控制口打挂
        except Exception as exc:  # noqa: BLE001
            log.warning("读取记忆失败：%s", exc)
            return web.json_response(
                {"error": f"读取记忆失败：{exc.__class__.__name__}"}, status=500
            )

    async def memory_health(request: web.Request) -> web.Response:
        try:
            return web.json_response(control.memory_health())
        # 体检读库失败同样不能把控制口打挂
        except Exception as exc:  # noqa: BLE001
            log.warning("读取记忆健康失败：%s", exc)
            return web.json_response(
                {"error": f"读取记忆健康失败：{exc.__class__.__name__}"}, status=500
            )

    async def memory_unblock(_: web.Request) -> web.Response:
        """手动解除配额熔断：清掉库里的退避记录。

        面板只在 ``quota.paused`` 时显示入口；清空后下一次健康快照会转为
        正常，后台 worker 也会重新尝试（退避只在失败时重新写入）。
        """
        try:
            cleared = control.store.clear_backoff()
            if memory_actions is not None and memory_actions.unblock is not None:
                memory_actions.unblock()
        except Exception as exc:  # noqa: BLE001 - 清库失败按 500 回给面板
            log.warning("解除配额熔断失败：%s", exc)
            return web.json_response(
                {"error": f"解除熔断失败：{exc.__class__.__name__}"}, status=500
            )
        return web.json_response({"ok": True, "cleared": cleared})

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

    async def delete_user_messages(request: web.Request) -> web.Response:
        chat_type, chat_id, user_id, error = _user_target(request)
        if error is not None:
            return error
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        if str(payload.get("confirm") or "") != "DELETE_USER":
            return web.json_response(
                {"error": "confirm 必须是 DELETE_USER"}, status=400
            )
        try:
            deleted = control.store.delete_user_messages(
                chat_type, chat_id, user_id
            )
        except Exception as exc:  # noqa: BLE001 - 不把 SQL/内部细节回给调用方
            log.warning("按用户删除消息失败：%s", exc)
            return web.json_response(
                {"error": "删除消息失败，请查看日志"}, status=500
            )
        return web.json_response({"ok": True, "deleted_messages": deleted})

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

    async def budget(request: web.Request) -> web.Response:
        chat_type, chat_id, error = _query_scope(request)
        if error is not None:
            return error
        if chat_type is None or chat_id is None:
            return web.json_response(
                {"error": "预算查询需要同时指定 chat_type 和 chat_id"}, status=400
            )
        try:
            return web.json_response(control.budget_snapshot(chat_type, chat_id))
        except Exception as exc:  # noqa: BLE001 - 读库失败不能把控制口打挂
            log.warning("读取预算失败：%s", exc)
            return web.json_response(
                {"error": f"读取预算失败：{exc.__class__.__name__}"}, status=500
            )

    async def decisions(request: web.Request) -> web.Response:
        """决策流水：最近 N 条 + 窗口统计。"""
        chat_type, chat_id, error = _query_scope(request)
        if error is not None:
            return error
        try:
            limit = _query_int(request, "limit", 50)
            days = _query_int(request, "days", 1)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        limit = max(1, min(200, limit))
        days = max(1, min(30, days))
        outcome = (request.query.get("outcome") or "").strip() or None
        try:
            return web.json_response(
                {
                    "items": control.store.list_decisions(
                        limit=limit,
                        chat_type=chat_type,
                        chat_id=chat_id,
                        outcome=outcome,
                    ),
                    "stats": control.store.decision_stats(
                        days=days, now_ts=time.time()
                    ),
                    "generated_at": now_shanghai().isoformat(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 读库失败不能把控制口打挂
            log.warning("读取决策流水失败：%s", exc)
            return web.json_response(
                {"error": f"读取决策流水失败：{exc.__class__.__name__}"}, status=500
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
            # 读临时文件放到线程里：handler 在事件循环上跑，不能用阻塞 open 卡住 loop。
            body = await asyncio.to_thread(Path(tmp).read_bytes)
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

    async def get_behavior(_: web.Request) -> web.Response:
        try:
            return web.json_response(control.behavior_snapshot())
        except Exception as exc:  # noqa: BLE001
            log.warning("读取聊天行为失败：%s", exc)
            return web.json_response(
                {"error": f"读取聊天行为失败：{exc.__class__.__name__}"}, status=500
            )

    async def put_behavior(request: web.Request) -> web.Response:
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        try:
            return web.json_response(control.save_behavior(payload))
        except BehaviorError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except OverrideError as exc:
            return web.json_response(
                {"error": f"覆盖层读写失败：{exc}"}, status=500
            )
        except OSError as exc:
            return web.json_response(
                {"error": f"覆盖层写入失败：{exc.__class__.__name__}"}, status=500
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("保存聊天行为失败：%s", exc)
            return web.json_response(
                {"error": f"保存聊天行为失败：{exc.__class__.__name__}"}, status=500
            )

    async def get_groups(_: web.Request) -> web.Response:
        try:
            return web.json_response(control.groups_snapshot())
        except Exception as exc:  # noqa: BLE001
            log.warning("读取群白名单失败：%s", exc)
            return web.json_response(
                {"error": f"读取群白名单失败：{exc.__class__.__name__}"}, status=500
            )

    async def put_groups(request: web.Request) -> web.Response:
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        try:
            return web.json_response(control.save_groups(payload))
        except ConfigError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except OverrideError as exc:
            return web.json_response(
                {"error": f"覆盖层读写失败：{exc}"}, status=500
            )
        except OSError as exc:
            return web.json_response(
                {"error": f"覆盖层写入失败：{exc.__class__.__name__}"}, status=500
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("保存群白名单失败：%s", exc)
            return web.json_response(
                {"error": f"保存群白名单失败：{exc.__class__.__name__}"}, status=500
            )

    async def get_private_chats(_: web.Request) -> web.Response:
        try:
            return web.json_response(control.private_chats_snapshot())
        except Exception as exc:  # noqa: BLE001
            log.warning("读取微信私聊失败：%s", exc)
            return web.json_response(
                {"error": f"读取微信私聊失败：{exc.__class__.__name__}"},
                status=500,
            )

    async def get_mind(_: web.Request) -> web.Response:
        try:
            return web.json_response(control.mind_snapshot())
        except Exception as exc:  # noqa: BLE001
            log.warning("读取心智配置失败：%s", exc)
            return web.json_response(
                {"error": f"读取心智配置失败：{exc.__class__.__name__}"}, status=500
            )

    async def put_mind(request: web.Request) -> web.Response:
        payload, error = await _read_json_object(request)
        if error is not None:
            return error
        if payload is None:
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
        try:
            return web.json_response(control.save_mind(payload))
        except ConfigError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except OverrideError as exc:
            return web.json_response(
                {"error": f"配置文件读写失败：{exc}"}, status=500
            )
        except OSError as exc:
            return web.json_response(
                {"error": f"配置文件写入失败：{exc.__class__.__name__}"}, status=500
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("保存心智配置失败：%s", exc)
            return web.json_response(
                {"error": f"保存心智配置失败：{exc.__class__.__name__}"}, status=500
            )

    async def workspace_file(request: web.Request) -> web.StreamResponse:
        """只读回放工作区附件；任何可疑输入都按 404 处理，不暴露真实路径。"""

        def missing() -> web.Response:
            return web.json_response({"error": "not found"}, status=404)

        workspace = control.settings.workspace
        if not workspace.enabled:
            return missing()
        raw = request.query.get("path", "").strip()
        if not raw or "\x00" in raw or _DRIVE_PREFIX_RE.match(raw):
            return missing()
        # 先做语法层拒绝（绝对路径/UNC/..），再进文件系统解析。
        posix = raw.replace("\\", "/")
        if posix.startswith("/"):
            return missing()
        parts = [part for part in posix.split("/") if part not in ("", ".")]
        if len(parts) < 2 or parts[0] not in WORKSPACE_FILE_DIRS:
            return missing()
        if any(part == ".." for part in parts):
            return missing()
        if PurePosixPath(posix).suffix.casefold() not in WORKSPACE_FILE_SUFFIXES:
            return missing()
        # base 与 target 都 resolve：软链接逃逸后 is_relative_to 会失败。
        try:
            base = (Path(workspace.dir) / parts[0]).resolve()
            target = base.joinpath(*parts[1:]).resolve()
        except (OSError, ValueError):
            return missing()
        if not target.is_relative_to(base):
            return missing()
        try:
            info = await asyncio.to_thread(target.stat)
        except OSError:
            return missing()
        # 目录、超限文件都先挡掉：绝不把 20 MiB 以上的内容读进内存。
        if not S_ISREG(info.st_mode) or info.st_size > MAX_WORKSPACE_FILE_BYTES:
            return missing()
        content_type = (
            mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        return web.FileResponse(
            target,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "private, max-age=60",
            },
        )

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/health/detail", health_detail)
    app.router.add_get("/api/workspace/file", workspace_file)
    app.router.add_post("/api/notify/admin", notify_admin)
    app.router.add_post("/api/channels/wechat/stop", wechat_stop)
    app.router.add_post("/api/channels/wechat/resume", wechat_resume)
    app.router.add_post("/api/channels/wechat/inject", wechat_inject)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/brain", get_brain)
    app.router.add_post("/api/brain", post_brain)
    app.router.add_get("/api/brain/models", models)
    app.router.add_post("/api/debug/trigger", debug_trigger)
    app.router.add_post("/api/debug/batch", debug_batch)
    app.router.add_get("/api/memory", memory)
    app.router.add_get("/api/memory/health", memory_health)
    app.router.add_post("/api/memory/unblock", memory_unblock)
    app.router.add_post("/api/memory/summaries", post_summary)
    app.router.add_post("/api/memory/impressions", post_impressions)
    app.router.add_patch(
        "/api/memory/impressions/{impression_id}", patch_impression
    )
    app.router.add_post("/api/memory/impressions/merge", merge_impressions)
    app.router.add_delete(
        "/api/memory/impressions/{impression_id}", delete_impression
    )
    app.router.add_delete(
        "/api/memory/{chat_type}/{chat_id}/user/{user_id}",
        delete_user_messages,
    )
    app.router.add_get("/api/usage", usage)
    app.router.add_get("/api/budget", budget)
    app.router.add_get("/api/decisions", decisions)
    app.router.add_get("/api/memory/search", search_messages)
    app.router.add_get("/api/memory/export", export_memory)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get(
        "/api/sessions/{chat_type}/{chat_id}/timeline", timeline
    )
    app.router.add_post("/api/sessions/{chat_type}/{chat_id}/{action}", session_action)
    app.router.add_delete("/api/sessions/{chat_type}/{chat_id}", delete_session)
    app.router.add_get("/api/config", config)
    app.router.add_get("/api/behavior", get_behavior)
    app.router.add_put("/api/behavior", put_behavior)
    app.router.add_get("/api/groups", get_groups)
    app.router.add_put("/api/groups", put_groups)
    app.router.add_get("/api/private/chats", get_private_chats)
    app.router.add_get("/api/mind", get_mind)
    app.router.add_put("/api/mind", put_mind)
    app.router.add_get("/api/backup", backup)
    return app


async def start_control_server(
    settings: Settings,
    store: Store,
    service: Any,
    adapter: Any,
    runtime_path: str = "data/runtime.json",
    memory_actions: MemoryActions | None = None,
    chat_titles: Callable[[str], str] | None = None,
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

    control = BrainControl(
        settings, store, service, adapter, runtime_path, chat_titles=chat_titles
    )
    # 切档后必须重启 opencode 才真的换模型：配置由本进程写，进程归 supervisor 管。
    control.opencode_reload = partial(
        reload_opencode, settings.supervisor.dashboard.port
    )
    # 免费档启动自检：supervisor 要等 opencode /api/info 就绪才拉起本进程，
    # 所以走到这里 opencode 已经 ready。只探测不阻塞，失败仅置标志。
    control.schedule_free_probe(force=True)
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
