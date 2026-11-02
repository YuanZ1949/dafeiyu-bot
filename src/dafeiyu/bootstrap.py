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
from .env_probe import LOW, probe_environment

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


def _env_encoding_issue(env_path: Path) -> Issue | None:
    """``.env`` 存在但不是合法 UTF-8 时给出阻塞项；正常/不存在返回 None。

    P10：从旧机器拷来的 .env 常是 GBK/有损编码，dotenv 会解析出空凭据，
    最后只表现为「缺 DEEPSEEK_API_KEY」，很容易查错方向。
    """
    if not env_path.is_file():
        return None
    try:
        env_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Issue(
            "error",
            "`.env` 编码不是 UTF-8",
            f"{env_path} 不是合法的 UTF-8（从旧机器拷来的 GBK/有损编码最常见）；"
            "凭据会解析为空或只读到一半，例如 DEEPSEEK_API_KEY 看起来「没填」",
            "用记事本「另存为」选 UTF-8 重存，或按 .env.example 重写 .env（保留原有键值）",
        )
    except OSError:
        return None
    return None


def _missing_bom_scripts(root_path: Path) -> list[str]:
    """列出「含非 ASCII 但没有 UTF-8 BOM」的 .ps1（P8）。

    只看 scripts/*.ps1 和 data/runtime-launcher.ps1（后者是任务计划的实际入口，
    存在才查）。纯 ASCII 的脚本不受 PowerShell 5.1 的 ANSI 解码影响，不算问题。
    """
    candidates: list[Path] = []
    scripts_dir = root_path / "scripts"
    if scripts_dir.is_dir():
        candidates.extend(sorted(scripts_dir.glob("*.ps1"), key=lambda p: p.name))
    launcher = root_path / "data" / "runtime-launcher.ps1"
    if launcher.is_file():
        candidates.append(launcher)

    missing: list[str] = []
    for path in candidates:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data or data.startswith(b"\xef\xbb\xbf"):
            continue
        if any(byte > 0x7F for byte in data):
            missing.append(path.name)
    return missing


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

    # P10：编码坏掉的 .env 会让后面所有凭据解析失真，先修编码再谈别的。
    env_issue = _env_encoding_issue(env_path)
    if env_issue is not None:
        issues.append(env_issue)
        return issues

    try:
        settings = load_settings(env_file=env_path, config_file=config)
    except UnicodeDecodeError:
        issues.append(
            Issue(
                "error",
                "`config.yaml` 编码不是 UTF-8",
                f"{config} 不是合法的 UTF-8（中文注释被存成 ANSI/GBK 时常见）",
                "用 UTF-8（无 BOM）另存 config.yaml，或运行 dafeiyu init --force 重新生成",
            )
        )
        return issues
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

    # P4/P5：工作区里的可执行文件可能被沙箱打成 Low 完整性，写用户目录/临时目录
    # EPERM，经常被误诊成杀毒软件拦截。这里提前把真实原因指出来。
    probe = probe_environment()
    if probe.integrity == LOW or not probe.temp_writable:
        issues.append(
            Issue(
                "warn",
                "运行环境疑似被沙箱降权（Low 完整性）",
                f"完整性级别 {probe.integrity}；{probe.detail}",
                "写用户目录/临时目录出现 EPERM 时先查完整性级别，别怀疑杀毒软件；"
                "把 DSH 权限切成「完全访问」或换一个干净工作区后重建 .venv，"
                "详见 docs/deployment-notes-windows.md 的 P4/P5",
            )
        )

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

    # P8：PowerShell 5.1 对无 BOM 的 UTF-8 中文脚本按 ANSI 解码，可能把中文
    # 注释的下一行吞掉（曾导致 OPENCODE_SERVER_PASSWORD 赋值整行丢失）。
    missing_bom = _missing_bom_scripts(root_path)
    if missing_bom:
        shown = "、".join(missing_bom[:5])
        if len(missing_bom) > 5:
            shown += f" 等 {len(missing_bom)} 个文件"
        issues.append(
            Issue(
                "warn",
                "PowerShell 脚本缺少 UTF-8 BOM",
                f"{shown} 含中文但不是 UTF-8 with BOM；PowerShell 5.1 会按 ANSI 解码，"
                "可能吞掉中文注释的下一行（P8）",
                "按 P8 转存为 UTF-8 with BOM：$t=[IO.File]::ReadAllText($p,[Text.UTF8Encoding]::new($false)); "
                "[IO.File]::WriteAllText($p,$t,[Text.UTF8Encoding]::new($true))",
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
