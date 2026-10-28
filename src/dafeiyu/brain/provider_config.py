from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import BrainConfig, LocalModelConfig

# 生成物首行的标记：配置写盘前据此判断是不是我们自己的文件。
GENERATED_MARK = "// generated-by: dafeiyu"
FREE_MODEL = "opencode/deepseek-v4.1-flash-free"

# 自备 Key / 本地模型两档都走 OpenAI 兼容 provider 插件，只是端点与驱动方式不同。
PROVIDER_PACKAGE = "@opencode/ai/providers/openai-compatible"
KEY_PROVIDER_ID = "dafeiyu-key"
LOCAL_PROVIDER_ID = "dafeiyu-local"
# 本地端点没写上下文窗口 / 输出预算时的兜底值（面板未覆盖时用）。
DEFAULT_CONTEXT_LIMIT = 32768
DEFAULT_OUTPUT_LIMIT = 8192
MODEL_CAPABILITIES = {"tools": True, "vision": False}
SUPPORTED_MODES = ("free", "key", "local_model")
# 相对配置目录的密钥文件引用；自备 Key 档的默认值（也是该档唯一的意义所在）。
DEFAULT_KEY_REL = "../../../secrets/provider.key"


def normalize_base_url(url: str) -> str:
    """把端点规整成 opencode 能直接吃的 baseURL。

    去首尾空白、去尾部多余的 `/`，并把重复的尾部 `/v1/v1` 收敛成一个 `/v1`
    （面板里手填端点时很容易多粘一段）。其它路径不做改动。
    """
    text = (url or "").strip()
    while text.endswith("/"):
        text = text[:-1]
    while text.endswith("/v1/v1"):
        text = text[: -len("/v1")]
    return text


def _model_entry(context: int | None = None) -> dict[str, Any]:
    """单个模型的 limit / capabilities；两档共用同一形状。"""
    return {
        "limit": {
            "context": context if context else DEFAULT_CONTEXT_LIMIT,
            "output": DEFAULT_OUTPUT_LIMIT,
        },
        "capabilities": dict(MODEL_CAPABILITIES),
    }


def _key_config(cfg: BrainConfig, key_rel: str | None) -> dict[str, Any]:
    """自备 Key 档：只引用密钥文件，绝不把密钥内容写进配置。"""
    model = cfg.key_model
    settings: dict[str, Any] = {"baseURL": normalize_base_url(cfg.key_base_url)}
    if key_rel:
        # key_rel 是相对配置目录的文件引用，不是密钥本身。
        settings["apiKey"] = f"{{file:{key_rel}}}"
    return {
        "model": f"{KEY_PROVIDER_ID}/{model}",
        "plugins": [],
        "providers": {
            KEY_PROVIDER_ID: {
                "package": PROVIDER_PACKAGE,
                "settings": settings,
                "models": {model: _model_entry()},
            }
        },
    }


def _local_config(
    local: LocalModelConfig | None, key_rel: str | None
) -> dict[str, Any]:
    """本地模型档：任意 OpenAI 兼容端点，可选带上本地服务的访问令牌。"""
    local = local if local is not None else LocalModelConfig()
    model = local.model
    settings: dict[str, Any] = {"baseURL": normalize_base_url(local.base_url)}
    if key_rel:
        settings["apiKey"] = f"{{file:{key_rel}}}"
    if local.lean:
        settings["lean"] = True
    context = (
        local.context if isinstance(local.context, int) and local.context > 0 else None
    )
    return {
        "model": f"{LOCAL_PROVIDER_ID}/{model}",
        "plugins": [],
        "providers": {
            LOCAL_PROVIDER_ID: {
                "package": PROVIDER_PACKAGE,
                "settings": settings,
                "models": {model: _model_entry(context)},
            }
        },
    }


class _UnsetKeyRel:
    """哨兵：区分「未传 key_rel」与「显式传 None」。

    自备 Key 档不传 key_rel 时用 `DEFAULT_KEY_REL`（文档化的默认引用）；
    本地档不传时保持不写 apiKey。显式传 None 两档都不写 apiKey。
    """

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试输出
        return "DEFAULT_KEY_REL"


_UNSET_KEY_REL = _UnsetKeyRel()


def build_config(
    mode: str,
    cfg: BrainConfig,
    *,
    key_rel: str | None | _UnsetKeyRel = _UNSET_KEY_REL,
    local: LocalModelConfig | None = None,
) -> dict[str, Any]:
    """按用户可见档位生成 opencode 配置的 JSON 数据。

    三档形状：

    - free：固定免费模型 + 空插件列表，不写 providers；
    - key：`providers.dafeiyu-key`，顶层 model 为 `dafeiyu-key/<cfg.key_model>`，
      密钥只以 `{file:<key_rel>}` 形式引用；省略 key_rel 时用文档化的
      `DEFAULT_KEY_REL`（`../../../secrets/provider.key`）；
    - local_model：`providers.dafeiyu-local`，顶层 model 为
      `dafeiyu-local/<local.model>`，可选 apiKey（本地服务需要令牌时）与 lean；
      本地档不继承 key 档的默认引用。

    两档 provider 的模型条目都带 `limit.context` / `limit.output` / `capabilities`。
    未知档位直接报错，避免悄悄落回免费形状。
    """
    if mode == "free":
        # 免费档不碰用户密钥与本地端点，key_rel / local 一律忽略。
        return {"model": FREE_MODEL, "plugins": []}
    if mode == "key":
        resolved = DEFAULT_KEY_REL if key_rel is _UNSET_KEY_REL else key_rel
        return _key_config(cfg, resolved)
    if mode == "local_model":
        resolved = None if key_rel is _UNSET_KEY_REL else key_rel
        return _local_config(local, resolved)
    supported = " / ".join(SUPPORTED_MODES)
    raise ValueError(f"不支持的大脑模式：{mode}（仅支持 {supported}）")


def render_config(data: dict) -> str:
    """渲染成落盘文本：首行标记 + 缩进 2 的 JSON，末尾带换行。"""
    return f"{GENERATED_MARK}\n{json.dumps(data, indent=2, ensure_ascii=False)}\n"


def config_path(config_home: str) -> Path:
    """bot 专用 opencode 配置路径；只拼路径，不建目录。"""
    return Path(config_home) / "opencode" / "opencode.jsonc"
