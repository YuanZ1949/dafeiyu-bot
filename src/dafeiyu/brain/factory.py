from __future__ import annotations

from typing import Any

from .client import BrainClient
from .opencode import OpenCodeBrain, resolve_opencode_bin
from .opencode_http import DENY_ALL_PERMISSIONS, DEFAULT_MEMORY_AGENT, OpenCodeHttpBrain
from ..config import ConfigError, Settings
from ..memory.store import Store
from ..runtime import RuntimeState

MODES: tuple[tuple[str, str], ...] = (
    ("local_model", "本地模型"),
    ("token", "Token"),
    ("opencode", "OpenCode"),
)
LABELS = dict(MODES)


def normalize_provider(value: str) -> str:
    provider = (value or "").strip().casefold()
    return "token" if provider == "local" else provider


def current_provider(settings: Settings, runtime: RuntimeState) -> str:
    return runtime.provider or normalize_provider(settings.brain.provider)


def local_model_name(settings: Settings, runtime: RuntimeState) -> str:
    # 记住用户上次在 dashboard 选的模型。若只在 provider == local_model 时才读 runtime，
    # 切到别的模式后本模式会显示「未选择模型」并被前端置灰，成了回不去的死路。
    return runtime.local_model_model or settings.local_model.model


def mode_status(settings: Settings, runtime: RuntimeState) -> list[dict[str, Any]]:
    """只读配置的就绪判定，供控制口与 dashboard 使用。"""
    modes: list[dict[str, Any]] = []
    for mode_id, label in MODES:
        reason = _unready_reason(settings, runtime, mode_id)
        modes.append(
            {
                "id": mode_id,
                "label": label,
                "ready": not reason,
                "reason": reason,
            }
        )
    return modes


def _unready_reason(settings: Settings, runtime: RuntimeState, provider: str) -> str:
    if provider == "opencode":
        # manage_server 时 url 由 host/port 拼，不需要用户填 server_url。
        if settings.opencode.server_url or settings.opencode.manage_server:
            return ""
        return "未配置 opencode.server_url"
    if provider == "token":
        return "" if settings.api_key else "缺少 DEEPSEEK_API_KEY"
    if provider == "local_model":
        if not settings.local_model.base_url:
            return "未配置 local_model.base_url"
        if not local_model_name(settings, runtime):
            return "未选择模型"
        return ""
    return "未知模式"


def build_brain(
    settings: Settings,
    store: Store,
    provider: str,
    model: str = "",
    server_url: str = "",
    memory_agent: str = DEFAULT_MEMORY_AGENT,
) -> Any:
    provider = normalize_provider(provider)
    if provider == "opencode":
        # manage_server 时 server_url 为空，用 host/port 拼出真实地址。
        resolved = (
            server_url.strip()
            or settings.opencode.server_url
            or f"http://{settings.opencode.host}:{settings.opencode.port}"
        )
        if settings.opencode.transport == "http":
            return OpenCodeHttpBrain(
                base_url=resolved,
                store=store,
                password=settings.opencode.server_password,
                agent=settings.opencode.agent,
                project_dir=settings.opencode.project_dir,
                timeout=settings.opencode.timeout_seconds,
                memory_agent=memory_agent,
                # 工作区关掉就下推会话级 deny：模型没有任何工具可用，
                # 也不用改 agent 文件重启 opencode。
                permissions=None if settings.workspace.enabled else DENY_ALL_PERMISSIONS,
            )
        try:
            bin_path = resolve_opencode_bin(settings.opencode.bin)
        except Exception as exc:  # BrainError，避免这里重复导入异常类型
            raise ConfigError(str(exc)) from exc
        return OpenCodeBrain(
            bin_path=bin_path,
            store=store,
            agent=settings.opencode.agent,
            project_dir=settings.opencode.project_dir,
            timeout=settings.opencode.timeout_seconds,
            server_url=server_url or settings.opencode.server_url,
        )
    if provider == "token":
        if not settings.api_key:
            raise ConfigError("缺少 DEEPSEEK_API_KEY")
        return BrainClient(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            trust_env=False,
            reasoning_effort=settings.reasoning_effort or None,
        )
    if provider == "local_model":
        chosen = model.strip() or settings.local_model.model
        if not chosen:
            raise ConfigError("未选择本地模型，请先在 dashboard 里从 LM Studio 拉取模型列表")
        return BrainClient(
            api_key=settings.local_model.api_key or "local",
            base_url=settings.local_model.base_url,
            model=chosen,
            trust_env=False,
            max_tokens=settings.local_model.max_tokens,
            timeout=settings.local_model.timeout_seconds,
        )
    raise ConfigError(f"未知的大脑模式：{provider}")
