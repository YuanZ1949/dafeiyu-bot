"""发布与首次安装支持：配置生成 + 环境体检。

别人拿到这个项目最常见的失败不是逻辑错误，而是「少一样东西」：
配置文件、BOT_QQ、NapCat、opencode、被占用的端口。这里把这些问题
一次性查出来，而不是让用户撞一条、报一条。
"""

from __future__ import annotations

import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from .brain.client import BrainError
from .brain.opencode import resolve_opencode_bin
from .config import ConfigError, load_settings

INSTALL_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = INSTALL_ROOT / "config.example.yaml"
CONFIG_NAME = "config.yaml"
ENV_NAME = ".env"
MIN_PYTHON = (3, 11)


@dataclass(frozen=True)
class InitResult:
    """init 的结果。created=False 表示目标已存在且未覆盖。"""

    path: Path
    created: bool
    source: Path


@dataclass(frozen=True)
class Issue:
    """体检发现的一条问题。level 只有 error / warn 两种。"""

    level: str
    title: str
    detail: str
    fix: str


def init_config(
    target: Path | str | None = None,
    *,
    source: Path | str | None = None,
    force: bool = False,
) -> InitResult:
    """从示例配置生成 config.yaml。已存在时默认不覆盖。"""
    target_path = Path(target) if target is not None else Path(CONFIG_NAME)
    source_path = Path(source) if source is not None else EXAMPLE_CONFIG
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到示例配置：{source_path}")
    if target_path.exists() and not force:
        return InitResult(target_path, False, source_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target_path)
    return InitResult(target_path, True, source_path)


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    # 故意不设 SO_REUSEADDR：Windows 下设了会让 bind 在端口被占用时也成功。
    # 面板可能绑在 0.0.0.0，必须按它实际的地址试，否则会误报「空闲」。
    bind_host = "" if host in {"", "0.0.0.0", "::", "*"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind_host, port))
        except OSError:
            return False
    return True


def preflight(
    config_path: Path | str | None = None,
    env_file: Path | str | None = None,
    *,
    root: Path | str | None = None,
    check_ports: bool = True,
) -> list[Issue]:
    """检查「能不能起来」，返回按严重程度排列的问题列表。空列表 = 可以启动。"""
    root_path = Path(root) if root is not None else INSTALL_ROOT
    config = Path(config_path) if config_path is not None else Path(CONFIG_NAME)
    env_path = Path(env_file) if env_file is not None else Path(ENV_NAME)
    issues: list[Issue] = []

    if sys.version_info < MIN_PYTHON:
        need = ".".join(str(part) for part in MIN_PYTHON)
        issues.append(
            Issue(
                "error",
                "Python 版本过低",
                f"当前 {sys.version_info.major}.{sys.version_info.minor}，需要 {need} 或更高",
                f"安装 Python {need}+ 后重建 .venv",
            )
        )

    if not config.is_file():
        issues.append(
            Issue(
                "error",
                "缺少配置文件",
                f"{config} 不存在",
                "运行 dafeiyu init 生成，或手动复制 config.example.yaml 为 config.yaml",
            )
        )
        return issues

    try:
        settings = load_settings(env_file=env_path, config_file=config)
    except ConfigError as exc:
        issues.append(
            Issue(
                "error",
                "配置无法加载",
                str(exc),
                "按提示修正 config.yaml 或 .env 后重新运行 dafeiyu doctor",
            )
        )
        return issues

    napcat_node = root_path / "vendor" / "napcat" / "node.exe"
    if not napcat_node.is_file():
        issues.append(
            Issue(
                "error",
                "NapCat 未安装",
                f"{napcat_node} 不存在",
                "运行 scripts/setup-napcat.ps1 下载（约 100MB，含 node）",
            )
        )

    provider = getattr(settings.brain, "provider", "token")
    if provider == "opencode":
        try:
            resolve_opencode_bin(settings.opencode.bin)
        except BrainError as exc:
            issues.append(
                Issue(
                    "error",
                    "找不到 opencode",
                    str(exc),
                    "运行 scripts\\setup-opencode.ps1 下载 opencode.exe（无需 Node.js）",
                )
            )
    elif provider == "token" and not settings.api_key:
        issues.append(
            Issue(
                "error",
                "缺少 DEEPSEEK_API_KEY",
                "brain.provider=token 需要 DeepSeek API 密钥",
                "在 .env 里设置 DEEPSEEK_API_KEY=sk-...",
            )
        )
    elif provider == "local_model" and not settings.local_model.model:
        issues.append(
            Issue(
                "warn",
                "本地模型未选择",
                "brain.provider=local_model 且 local_model.model 为空",
                "启动后在面板里从 /v1/models 侦测并选择模型",
            )
        )

    data_dir = Path(settings.db_path).parent
    if not data_dir.is_absolute():
        data_dir = Path.cwd() / data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        issues.append(
            Issue(
                "error",
                "数据目录不可写",
                f"{data_dir}：{exc}",
                "换一个可写位置，或用 DAFEIYU_DB_PATH 指定",
            )
        )

    if check_ports:
        ports = {
            "NapCat 反向 WS": (settings.ws_host, settings.ws_port),
            "bot 控制口": ("127.0.0.1", settings.supervisor.control_port),
            "运维面板": (settings.supervisor.dashboard.host, settings.supervisor.dashboard.port),
            "opencode": (settings.opencode.host, settings.opencode.port),
        }
        busy = [
            f"{name} {port}"
            for name, (host, port) in ports.items()
            if not _port_free(port, host)
        ]
        if busy:
            issues.append(
                Issue(
                    "warn",
                    "端口已被占用",
                    "、".join(busy),
                    "dafeiyu 已在本机运行时属正常；否则先 stop，或改配置里的端口",
                )
            )

    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.level == "error" for issue in issues)


def format_issues(issues: list[Issue]) -> str:
    """把体检结果排成给人看的文本。"""
    if not issues:
        return "体检通过：配置、依赖、端口都没问题，可以启动。"
    lines: list[str] = []
    for issue in issues:
        mark = "✗" if issue.level == "error" else "!"
        lines.append(f"{mark} {issue.title}")
        lines.append(f"    {issue.detail}")
        if issue.fix:
            lines.append(f"    → {issue.fix}")
    errors = sum(1 for issue in issues if issue.level == "error")
    lines.append("")
    lines.append(f"共 {errors} 个阻塞项、{len(issues) - errors} 个提醒项。")
    return "\n".join(lines)
