"""bot 专用 opencode 配置目录（Task 13.1）。

把 bot 的 serve 子进程指向独立的 ``XDG_CONFIG_HOME`` 后，它不再加载用户全局的
agents / plugins / mcp：全局插件注册的外来 agent、全局 MCP 子进程、全局 provider
默认值都进不来。两个 agent 文件在仓库 ``.opencode/agents/`` 下，按 project 发现，
不受影响。

凭据不在配置目录：opencode 的 ``auth.json`` 在 data 目录
（``~/.local/share/opencode``），所以隔离配置不需要复制或搬移任何凭据。

``config_home`` 留空（默认）= 沿用启动环境的配置，即软回滚。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import ConfigError

# 隔离目录里的兜底模型。两个 agent 文件也各自钉住同一个模型，这里再写一份是
# 为了任何没带 agent 的 session 也有默认值。
DEFAULT_OPENCODE_MODEL = "opencode/deepseek-v4.1-flash-free"

MINIMAL_CONFIG: dict[str, object] = {
    "$schema": "https://opencode.ai/config.json",
    "model": DEFAULT_OPENCODE_MODEL,
    # 显式空列表：隔离目录里不该再加载任何插件
    "plugins": [],
}


def ensure_isolated_config(config_home: str | Path) -> Path:
    """建好 ``<config_home>/opencode/opencode.jsonc``，返回绝对路径。

    幂等：文件已存在时一个字都不改（用户可以自己往里加配置）。
    相对路径按当前工作目录解析，与 ``workspace.dir`` 一致。
    """

    root = Path(config_home).expanduser().resolve()
    config_dir = root / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "opencode.jsonc"
    if not config_file.exists():
        config_file.write_text(
            json.dumps(MINIMAL_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return root


def server_env(config_home: str | Path | None) -> dict[str, str] | None:
    """给 ``OpenCodeServer`` 的 env；留空返回 None（沿用启动环境）。"""

    raw = str(config_home or "").strip()
    if not raw:
        return None
    try:
        root = ensure_isolated_config(raw)
    except OSError as exc:
        raise ConfigError(f"opencode.config_home 无法创建：{raw}（{exc}）") from exc
    return {"XDG_CONFIG_HOME": str(root)}
