from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import BrainConfig, LocalModelConfig

# 生成物首行的标记：配置写盘前据此判断是不是我们自己的文件。
GENERATED_MARK = "// generated-by: dafeiyu"
FREE_MODEL = "opencode/deepseek-v4.1-flash-free"


def build_config(
    mode: str,
    cfg: BrainConfig,
    *,
    key_rel: str | None = None,
    local: LocalModelConfig | None = None,
) -> dict[str, Any]:
    """按用户可见档位生成 opencode 配置的 JSON 数据。

    目前只实现免费档：固定模型 + 空插件列表，不写 providers。
    key / local_model 档由后续任务补齐，这里先显式拒绝，避免悄悄落回免费形状。
    """
    if mode == "free":
        # 免费档不碰用户密钥与本地端点，key_rel / local 一律忽略。
        return {"model": FREE_MODEL, "plugins": []}
    raise ValueError(f"未实现的大脑模式：{mode}")


def render_config(data: dict) -> str:
    """渲染成落盘文本：首行标记 + 缩进 2 的 JSON，末尾带换行。"""
    return f"{GENERATED_MARK}\n{json.dumps(data, indent=2, ensure_ascii=False)}\n"


def config_path(config_home: str) -> Path:
    """bot 专用 opencode 配置路径；只拼路径，不建目录。"""
    return Path(config_home) / "opencode" / "opencode.jsonc"
