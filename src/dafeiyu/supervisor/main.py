from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path

from ..brain.client import BrainError
from ..brain.opencode import resolve_opencode_bin
from ..config import ConfigError, Settings, load_settings
from .dashboard import serve
from .logs import LogHub
from .manager import ProcSpec, Supervisor, pid_alive, port_owner

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
QR_PATH = REPO_ROOT / "vendor" / "napcat" / "napcat" / "cache" / "qrcode.png"
NAPCAT_DIR = REPO_ROOT / "vendor" / "napcat"
TOKEN_PATH = REPO_ROOT / "data" / "supervisor.token"
PID_PATH = REPO_ROOT / "data" / "supervisor.pid"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _HubHandler(logging.Handler):
    """把 supervisor 自己的日志也塞进同一套环形缓冲。"""

    def __init__(self, hub: LogHub) -> None:
        super().__init__()
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.hub.write("supervisor", self.format(record))
        except Exception:  # 日志系统本身不能把主流程带崩
            pass


def setup_logging(settings: Settings, logs: LogHub) -> None:
    level = getattr(logging, settings.logging.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = []
    if sys.stdout is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        handlers.append(stream)
    hub = _HubHandler(logs)
    hub.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handlers.append(hub)
    root.handlers[:] = handlers


def load_or_create_token(path: Path = TOKEN_PATH) -> str:
    with contextlib.suppress(OSError):
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
    except OSError as exc:
        log.warning("控制口令无法写入 %s：%s（本次仅内存生效）", path, exc)
    return token


def _console_python() -> str:
    """supervisor 用 pythonw 跑；bot 用 python.exe 才能把 stdout 交给管道。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _eprint(message: str) -> None:
    if sys.stderr is not None:
        print(message, file=sys.stderr)


def find_napcat_pid(directory: Path = NAPCAT_DIR) -> int | None:
    """NapCat 是 node.exe，用命令行是否包含安装目录来认，端口认不出来。"""
    if os.name != "nt":
        return None
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{directory}') }} | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return int(lines[-1])
    except ValueError:
        return None


def build_specs(settings: Settings, token: str) -> dict[str, ProcSpec]:
    try:
        opencode_bin = resolve_opencode_bin(settings.opencode.bin)
    except BrainError as exc:
        raise ConfigError(
            f"找不到 OpenCode 可执行文件：{exc}。请先 npm i -g opencode-ai"
        ) from exc

    node = NAPCAT_DIR / "node.exe"
    if not node.exists():
        raise ConfigError(
            f"NapCat 未安装：{node} 不存在。请先运行 scripts/setup-napcat.ps1"
        )

    db_path = Path(settings.opencode.db_path)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    control_host = settings.supervisor.control_host
    control_port = settings.supervisor.control_port

    specs: dict[str, ProcSpec] = {}
    specs["opencode"] = ProcSpec(
        name="opencode",
        command=[
            opencode_bin,
            "serve",
            "--hostname",
            settings.opencode.host,
            "--port",
            str(settings.opencode.port),
        ],
        cwd=str(REPO_ROOT),
        env={"OPENCODE_DB": str(db_path), "NO_COLOR": "1"},
        ready_url=f"http://{settings.opencode.host}:{settings.opencode.port}/api/info",
        creationflags=CREATE_NO_WINDOW,
    )
    specs["bot"] = ProcSpec(
        name="bot",
        command=[_console_python(), "-m", "dafeiyu"],
        cwd=str(REPO_ROOT),
        env={
            "DAFEIYU_CONTROL_HOST": control_host,
            "DAFEIYU_CONTROL_PORT": str(control_port),
            "DAFEIYU_CONTROL_TOKEN": token,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        },
        ready_url=f"http://127.0.0.1:{control_port}/api/health",
        creationflags=CREATE_NO_WINDOW,
    )
    specs["napcat"] = ProcSpec(
        name="napcat",
        command=[str(node), "index.js"],
        cwd=str(NAPCAT_DIR),
        env={"NAPCAT_QUICK_LOGIN": "0"},
        creationflags=CREATE_NO_WINDOW,
    )
    return specs


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def request() -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(request))
            except (ValueError, OSError):
                pass


def _write_pid() -> None:
    try:
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _read_pid() -> int | None:
    try:
        raw = PID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _clear_pid() -> None:
    with contextlib.suppress(OSError):
        PID_PATH.unlink()


async def run() -> int:
    settings = load_settings()
    logs = LogHub(Path(settings.logging.dir) / "supervisor")
    setup_logging(settings, logs)

    token = load_or_create_token()
    try:
        specs = build_specs(settings, token)
    except ConfigError as exc:
        logs.write("supervisor", f"启动失败：{exc}")
        _eprint(f"[大肥鱼] {exc}")
        return 1

    supervisor = Supervisor(
        specs,
        logs,
        settings.supervisor.restart,
        ready_timeout=float(settings.opencode.startup_timeout_seconds),
    )

    owner = find_napcat_pid()
    if owner is not None:
        supervisor.mark_external("napcat", owner)
        logs.write("supervisor", f"NapCat 已在外运行（pid={owner}），接管为外部实例")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop)

    # 双开拦截：pid 文件 + dashboard 端口占用。Windows 的 SO_REUSEADDR
    # 会让两个进程同时绑定同一端口，不能只靠 bind 失败来发现。
    running_pid = _read_pid()
    if running_pid is not None and running_pid != os.getpid() and pid_alive(running_pid):
        logs.write("supervisor", f"已有 supervisor 在运行（pid={running_pid}），本次退出")
        _eprint(f"[大肥鱼] 已有 supervisor 在运行（pid={running_pid}）")
        return 1
    dashboard_port = settings.supervisor.dashboard.port
    busy = port_owner(dashboard_port)
    if busy is not None:
        logs.write(
            "supervisor",
            f"dashboard 端口 {dashboard_port} 已被 pid={busy} 占用，本次退出",
        )
        _eprint(f"[大肥鱼] dashboard 端口 {dashboard_port} 已被占用（pid={busy}）")
        return 1

    _write_pid()

    bot_url = f"http://{settings.supervisor.control_host}:{settings.supervisor.control_port}"

    async def request_shutdown() -> None:
        stop.set()

    runner = await serve(
        supervisor,
        settings,
        logs,
        QR_PATH,
        bot_url,
        token=token,
        on_shutdown=request_shutdown,
    )

    url = (
        f"http://{settings.supervisor.dashboard.host}:"
        f"{settings.supervisor.dashboard.port}/"
    )
    logs.write("supervisor", f"dashboard: {url}")
    if settings.supervisor.dashboard.open_browser:
        with contextlib.suppress(Exception):
            loop.run_in_executor(None, webbrowser.open, url)

    try:
        await supervisor.start_all()
        await stop.wait()
    finally:
        logs.write("supervisor", "正在关闭所有进程…")
        try:
            await asyncio.wait_for(supervisor.shutdown(), timeout=45)
        except asyncio.TimeoutError:
            logs.write("supervisor", "停止子进程超时，强制退出")
        logs.write("supervisor", "子进程已停止，关闭 dashboard")
        try:
            await asyncio.wait_for(runner.cleanup(), timeout=15)
        except asyncio.TimeoutError:
            logs.write("supervisor", "dashboard 关闭超时，强制退出")
        _clear_pid()
        logs.write("supervisor", "已退出")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except ConfigError as exc:
        _eprint(f"[大肥鱼] 配置错误：{exc}")
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
