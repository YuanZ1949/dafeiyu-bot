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
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ..config import ConfigError
from .provider_config import (
    FREE_MODEL,
    GENERATED_MARK,
    config_path,
    render_config,
)

log = logging.getLogger(__name__)

# 隔离目录里的兜底模型。两个 agent 文件也各自钉住同一个模型，这里再写一份是
# 为了任何没带 agent 的 session 也有默认值。免费档的模型名只有 `FREE_MODEL`
# 一个来源，这里保留旧名字当别名（既有调用方与测试还在用）。
DEFAULT_OPENCODE_MODEL = FREE_MODEL

MINIMAL_CONFIG: dict[str, object] = {
    "$schema": "https://opencode.ai/config.json",
    "model": DEFAULT_OPENCODE_MODEL,
    # 显式空列表：隔离目录里不该再加载任何插件
    "plugins": [],
}

# 升级前写下的兜底配置原文（无标记、raw json.dumps）。识别它，免得每次启动都
# 把自家旧文件当成用户手改件备份一遍。
_LEGACY_MINIMAL = json.dumps(MINIMAL_CONFIG, ensure_ascii=False, indent=2)

# 本次调用备份出的文件；没备份时为 None。Task 19 的 doctor 据此提示用户。
LAST_BACKUP: Path | None = None


def _is_generated(text: str) -> bool:
    """首行就是生成标记（``render_config`` 的输出）。"""
    lines = text.splitlines()
    return bool(lines) and lines[0].strip() == GENERATED_MARK


def _is_legacy_minimal(text: str) -> bool:
    """旧兜底配置原文；容忍尾部空白。"""
    return text.strip() == _LEGACY_MINIMAL.strip()


def ensure_isolated_config(config_home: str | Path) -> Path:
    """建好 ``<config_home>/opencode/opencode.jsonc``，返回配置根目录。

    - 文件不存在：写带生成标记的兜底配置；
    - 首行是 ``// generated-by: dafeiyu``：生成物，一个字都不动；
    - 等于升级前的兜底配置原文：同样不动（无标记但确实是自家文件）；
    - 其余（用户手改过，或非 UTF-8 等读不出来）：先复制成
      ``<name>.bak.<YYYYmmdd-HHMMSS>`` 再写兜底配置，备份路径记在
      ``LAST_BACKUP`` 并打一条 warning。

    返回值始终是配置根目录（``server_env`` 用它拼 ``XDG_CONFIG_HOME``）。
    相对路径按当前工作目录解析，与 ``workspace.dir`` 一致。
    """

    global LAST_BACKUP
    LAST_BACKUP = None

    root = Path(config_home).expanduser().resolve()
    config_file = config_path(str(root))
    config_file.parent.mkdir(parents=True, exist_ok=True)
    content = render_config(MINIMAL_CONFIG)
    if not config_file.exists():
        config_file.write_text(content, encoding="utf-8", newline="\n")
        return root

    try:
        old: str | None = config_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 读不出来的旧文件一律当手改过：备份比覆盖安全。GBK 等非 UTF-8 编辑器
        # 写出的配置会抛 UnicodeDecodeError，不能让它冒到 server_env 外面把
        # 启动打挂（那里只兜 OSError）。策略同 provider_config.write_generated。
        old = None
    if old is not None and (_is_generated(old) or _is_legacy_minimal(old)):
        return root

    # 备份名用本地时间：用户在看目录时对得上自己刚改的时间。
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    backup = config_file.with_name(f"{config_file.name}.bak.{stamp}")
    shutil.copy2(config_file, backup)
    LAST_BACKUP = backup
    config_file.write_text(content, encoding="utf-8", newline="\n")
    log.warning("opencode 隔离配置被手改过，已备份到 %s 并恢复为生成的兜底配置", backup)
    return root


def server_env(config_home: str | Path | None) -> dict[str, str] | None:
    """给 ``OpenCodeServer`` 的 env；留空返回 None（沿用启动环境）。

    两个变量都要给：实测 ``OPENCODE_CONFIG`` 的优先级高于 ``XDG_CONFIG_HOME``，
    只改后者的话，父进程环境里带过来的全局 opencode.jsonc 仍会被读进来，
    全局插件（以及它们注册的外来 agent）照常加载。
    """

    raw = str(config_home or "").strip()
    if not raw:
        return None
    try:
        root = ensure_isolated_config(raw)
    except OSError as exc:
        raise ConfigError(f"opencode.config_home 无法创建：{raw}（{exc}）") from exc
    return {
        "XDG_CONFIG_HOME": str(root),
        "OPENCODE_CONFIG": str(root / "opencode" / "opencode.jsonc"),
    }
