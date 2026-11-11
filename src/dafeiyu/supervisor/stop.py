"""命令行停止 supervisor（`python -m dafeiyu.supervisor.stop`）。

优先走面板的 `/api/shutdown`（会带上子进程的优雅退出），失败再强制结束进程树。
`scripts/stop.ps1` 只是这个模块的薄包装。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from ..config import load_settings
from ..winproc import hidden_kwargs
from .auth import COOKIE_NAME
from .lan_access import ensure_dashboard_password
from .manager import port_owner

REPO_ROOT = Path(__file__).resolve().parents[3]
PID_PATH = REPO_ROOT / "data" / "supervisor.pid"

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 退出码：0 成功/未运行；1 通用失败；2 进程已退出但端口被别的进程占着（R17）。
EXIT_FAILED = 1
EXIT_PORT_BUSY = 2


@dataclass
class StopResult:
    status: str  # not_running | stopped | failed
    pid: int | None = None
    method: str | None = None  # shutdown | forced
    stale: bool = False
    # 进程已退出（或 pid 文件指向的进程不是 supervisor）但端口仍被占用：
    # 用来把「端口被别的进程占着」和普通的停止失败区分开。
    port_busy: bool = False


def read_pid(path: Path = PID_PATH) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


async def request_shutdown(
    host: str,
    port: int,
    *,
    username: str,
    password: str,
    timeout: float = 5.0,
) -> bool:
    """登录面板并请求关停；登录未启用（400）时直接请求，凭据错误返回 False。"""
    base = f"http://{host}:{port}"
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        try:
            async with session.post(
                f"{base}/api/auth/login",
                json={"username": username, "password": password},
            ) as resp:
                if resp.status == 204 or resp.status == 200:
                    cookie = resp.cookies.get(COOKIE_NAME)
                    token = cookie.value if cookie is not None else None
                elif resp.status == 400:
                    token = None  # 登录未启用（loopback + auth=off）
                else:
                    return False
        except (aiohttp.ClientError, TimeoutError):
            return False

        headers = {"Cookie": f"{COOKIE_NAME}={token}"} if token else {}
        try:
            async with session.post(
                f"{base}/api/shutdown", headers=headers
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False


ShutdownFn = Callable[..., Awaitable[bool]]
PortFinder = Callable[[int], int | None]
CmdlineFn = Callable[[int], str | None]


def _process_command_line(pid: int) -> str | None:
    """查询进程命令行；查询失败时返回 None（按不匹配处理）。"""
    if os.name != "nt":
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        return text or None
    command = (
        f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").CommandLine'
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
            **hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    return text or None


def _looks_like_supervisor(command_line: str) -> bool:
    lowered = command_line.lower()
    return "dafeiyu" in lowered and "supervisor" in lowered


def _confirmed_supervisor(pid: int, cmdline_fn: CmdlineFn) -> bool:
    """pid 的命令行确实是 supervisor 才返回 True。

    取不到命令行（空/异常）时按「无法确认」处理：宁可不动，也不误杀（B3-1）。
    """
    try:
        command_line = cmdline_fn(pid)
    except Exception:  # noqa: BLE001 - 查不到就当未确认
        return False
    if not command_line:
        return False
    return _looks_like_supervisor(command_line)


def _supervisor_pid_on_port(
    port: int,
    port_finder: PortFinder,
    cmdline_fn: CmdlineFn,
) -> int | None:
    """端口占用者确认是 supervisor 时返回其 pid；否则 None（绝不误杀）。"""
    try:
        owner = port_finder(port)
    except Exception:  # noqa: BLE001 - 查不到就当没有
        return None
    if owner is None or owner <= 0:
        return None
    if owner == os.getpid():
        return None
    if not pid_alive(owner):
        return None
    try:
        command_line = cmdline_fn(owner)
    except Exception:  # noqa: BLE001 - 查不到就当没有
        return None
    if not command_line or not _looks_like_supervisor(command_line):
        return None
    return owner


def _port_occupied(port: int, port_finder: PortFinder) -> bool:
    """端口是否仍有进程监听；查询失败一律按「没占用」处理（与旧行为一致）。"""
    try:
        owner = port_finder(port)
    except Exception:  # noqa: BLE001 - 查不到就不算占用
        return False
    # 自己持有端口不算「被别的进程占着」（进程退出后端口就释放了）。
    return owner is not None and owner > 0 and owner != os.getpid()


async def stop_supervisor(
    *,
    host: str = "127.0.0.1",
    port: int,
    username: str = "admin",
    password: str = "",
    pid_path: Path = PID_PATH,
    timeout: float = 20.0,
    force: bool = True,
    shutdown_fn: ShutdownFn = request_shutdown,
    poll_seconds: float = 0.25,
    port_finder: PortFinder = port_owner,
    cmdline_fn: CmdlineFn = _process_command_line,
) -> StopResult:
    pid = read_pid(pid_path)
    stale = False
    if pid is None or not pid_alive(pid):
        recorded_pid = pid
        if pid is not None:
            # pid 文件里的进程已经死了：保持原来的清理行为。
            _unlink(pid_path)
            stale = True
        # pid 文件丢了/损坏/过期时，按端口兜底找回仍活着的 supervisor。
        fallback = _supervisor_pid_on_port(port, port_finder, cmdline_fn)
        if fallback is None:
            if _port_occupied(port, port_finder):
                # supervisor 已经不在，端口却还被别的进程占着：下次启动必然冲突，
                # 不能再报 not_running（R17）。
                return StopResult(
                    status="failed",
                    pid=recorded_pid,
                    stale=stale,
                    port_busy=True,
                )
            return StopResult(status="not_running", pid=recorded_pid, stale=stale)
        pid = fallback
        stale = True
    elif not _confirmed_supervisor(pid, cmdline_fn):
        # pid 文件里的 pid 还活着，但命令行不是 supervisor（pid 被系统复用）：
        # 绝不能对它 taskkill，只清理失效的 pid 文件（B3-1）。
        _unlink(pid_path)
        return StopResult(
            status="not_running",
            pid=pid,
            stale=True,
            port_busy=_port_occupied(port, port_finder),
        )

    method: str | None = None
    graceful = await shutdown_fn(
        host, port, username=username, password=password
    )
    if graceful and await _wait_dead(pid, timeout, poll_seconds):
        method = "shutdown"
    elif force:
        killed = _force_kill(pid)
        if killed and await _wait_dead(pid, min(timeout, 10.0), poll_seconds):
            method = "forced"

    if method is None:
        return StopResult(status="failed", pid=pid, stale=stale)
    _unlink(pid_path)
    return StopResult(status="stopped", pid=pid, method=method, stale=stale)


async def _wait_dead(pid: int, timeout: float, poll_seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if not pid_alive(pid):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(poll_seconds)


def _force_kill(pid: int) -> bool:
    """强制结束进程树；返回命令是否成功（成功才允许报「已停止」）。"""
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                **hidden_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def main() -> int:
    settings = load_settings()
    dashboard = settings.supervisor.dashboard
    password = ensure_dashboard_password(dashboard)
    result = asyncio.run(
        stop_supervisor(
            port=dashboard.port,
            username=dashboard.username,
            password=password,
        )
    )
    if result.status == "not_running":
        note = "（已清理残留 pid 文件）" if result.stale else ""
        print(f"supervisor 未在运行{note}")
        return 0
    if result.status == "stopped":
        if result.method == "forced":
            print(f"优雅关闭未生效，已强制结束进程 {result.pid}（含子进程）")
        else:
            print(f"supervisor 已关闭（pid {result.pid}）")
        return 0
    if result.port_busy:
        note = "（已清理残留 pid 文件）" if result.stale else ""
        print(
            f"停止失败：supervisor 进程已退出{note}，但端口 {dashboard.port} "
            "仍被其它进程占用；请先结束占用该端口的进程再启动",
            file=sys.stderr,
        )
        return EXIT_PORT_BUSY
    print(f"停止失败：进程 {result.pid} 仍在运行", file=sys.stderr)
    return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
