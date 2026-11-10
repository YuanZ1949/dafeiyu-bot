"""bot 专用 opencode 运行时目录（Task 13.1）。

把 bot 的 serve 子进程指向独立的 ``XDG_CONFIG_HOME`` 后，它不再加载用户全局的
agents / plugins / mcp：全局插件注册的外来 agent、全局 MCP 子进程、全局 provider
默认值都进不来。两个 agent 文件在仓库 ``.opencode/agents/`` 下，按 project 发现，
不受影响。

配置之外的运行时状态也一并隔离：``XDG_DATA_HOME`` / ``XDG_STATE_HOME`` /
``XDG_CACHE_HOME`` 全部指向 ``config_home`` 下的兄弟目录，所以日志、快照、
tool-output、模型缓存都留在项目目录里，不会和用户主 opencode 共用同一份
数据目录（共用快照桶时两个 serve 会竞争同一批 git 仓库）。

凭据单独处理：opencode 的 ``auth.json`` 也在 data 目录
（``~/.local/share/opencode/auth.json``）。隔离 data 目录后它不会自动出现，
所以首次启动时从全局 data 目录复制一份到隔离目录，让隔离目录自带一份凭据；
此后两边各写各的，互不影响。v2 已经把凭据迁进 sqlite 的 ``credential`` 表
（即 ``OPENCODE_DB`` 那个库），``auth.json`` 仍是旧版导入源、也有 provider
（如 gitlab）直接读它。本仓库的两种档位都不依赖它：免费档匿名可用，自备
Key 档用 ``data/secrets/provider.key``。

``config_home`` 留空（默认）= 沿用启动环境的配置，即软回滚。
"""

from __future__ import annotations

import json
import logging
import os
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
    """给 ``OpenCodeServer`` / CLI 子进程的 env；留空返回 None（沿用启动环境）。

    ``OPENCODE_CONFIG`` 必须给：实测它的优先级高于 ``XDG_CONFIG_HOME``，只改
    后者的话，父进程环境里带过来的全局 opencode.jsonc 仍会被读进来，全局
    插件（以及它们注册的外来 agent）照常加载。

    三个 XDG 目录也一起指向隔离根（默认 ``<config_home>/{data,state,cache}``）：
    opencode 的日志、快照、tool-output、模型缓存因此都落在项目目录里。全部
    放在 ``config_home`` 之下而不是它的父目录，是为了 ``config_home`` 被配成
    任意值（含 ``.``）时都不会把状态写到项目外面。

    首次运行会把全局 data 目录里的 ``auth.json`` 复制进隔离 data 目录，让
    隔离目录自带一份凭据（v2 的凭据在 sqlite 里，这里是兼容旧导入源与直读
    ``auth.json`` 的 provider）；复制失败只告警，不阻断启动。
    """

    raw = str(config_home or "").strip()
    if not raw:
        return None
    try:
        root = ensure_isolated_config(raw)
    except OSError as exc:
        raise ConfigError(f"opencode.config_home 无法创建：{raw}（{exc}）") from exc
    data_home = root / "data"
    _seed_isolated_auth(data_home)
    return {
        "XDG_CONFIG_HOME": str(root),
        "OPENCODE_CONFIG": str(root / "opencode" / "opencode.jsonc"),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
    }


def _global_auth_file() -> Path | None:
    """定位用户主 opencode 的 ``auth.json``；找不到返回 None。

    先看 ``XDG_DATA_HOME``（Linux 惯例，Windows 上的 opencode 也认），再试
    默认路径与 Windows 的 LOCALAPPDATA / APPDATA，取第一个真实存在的文件。
    """

    candidates: list[Path] = []
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    if raw:
        candidates.append(Path(raw).expanduser() / "opencode" / "auth.json")
    candidates.append(Path.home() / ".local" / "share" / "opencode" / "auth.json")
    for name in ("LOCALAPPDATA", "APPDATA"):
        value = os.environ.get(name, "").strip()
        if value:
            candidates.append(Path(value) / "opencode" / "auth.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _seed_isolated_auth(data_home: Path) -> None:
    """把全局凭据复制进隔离 data 目录（仅当目标不存在）。

    只在缺失时复制：隔离目录里的 ``auth.json`` 可能已被刷新过，不能拿全局的
    旧文件覆盖。复制失败只告警——key / local 档不依赖它，不该因此起不来。
    """

    target = data_home / "opencode" / "auth.json"
    if target.exists():
        return
    source = _global_auth_file()
    if source is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError:
        log.warning(
            "opencode 隔离凭据复制失败（免费档可能不可用）：%s", source, exc_info=True
        )
        return
    log.info("已复制全局 opencode 凭据到隔离目录（仅首次）：%s", target)
