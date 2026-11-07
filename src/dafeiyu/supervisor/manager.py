from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import RestartConfig
from ..winproc import hidden_kwargs
from .logs import LogHub

log = logging.getLogger(__name__)

STOP_TIMEOUT = 5.0
# taskkill 之后等进程真正退出的上限：Windows 上 /F 发出信号到进程消失有延迟。
KILL_WAIT_TIMEOUT = 5.0
# 自愈冷却：同一条恢复路径 10 分钟内只做一次，避免日志刷屏时反复重启。
RECOVER_COOLDOWN = 600.0
HEALTHY_UPTIME = 30.0

READY = "running"
STARTING = "starting"
STOPPING = "stopping"
STOPPED = "stopped"
RESTARTING = "restarting"
FAILED = "failed"
EXTERNAL = "external"


@dataclass
class ProcSpec:
    name: str
    command: list[str]
    cwd: str | None = None
    # 子进程环境里可能含口令（opencode/control/wechat bridge token），
    # repr 一律不展开，避免被日志或异常文本顺带带出去。
    env: dict[str, str] | None = field(default=None, repr=False)
    ready_url: str = ""
    # 没有 HTTP 端点的进程（例如微信桥是出站 WS 客户端）用同步探针代替 URL；
    # 探针由 manager 丢到线程池执行，绝不在事件循环里同步跑。
    ready_probe: Callable[[], bool] | None = None
    # 单个进程的就绪窗口覆盖；None 表示沿用 Supervisor.ready_timeout。
    ready_timeout: float | None = None
    creationflags: int = 0
    # 启动前先清掉同目录的残留进程（NapCat 的 worker 会在主进程崩溃后
    # 继续占着 QQ 登录态，新实例只会报「当前账号已登录，无法重复登录」）。
    sweep_pids: Callable[[], list[int]] | None = None
    # 输出里命中这些片段时自愈：清残留 + 重启自己（有冷却，见 RECOVER_COOLDOWN）。
    recover_patterns: tuple[str, ...] = ()


@dataclass
class ProcState:
    name: str
    spec: ProcSpec
    proc: Any = None
    status: str = STOPPED
    restart_count: int = 0
    failures: int = 0
    last_exit_code: int | None = None
    last_exit_at: str = ""
    started_at: float = 0.0
    manual_stop: bool = False
    external_pid: int | None = None
    drains: list[asyncio.Task] = field(default_factory=list)
    watcher: asyncio.Task | None = None
    # 自愈：上次触发时间、正在跑、已排队；三者一起防重复重启。
    last_recover_at: float = 0.0
    recovering: bool = False
    recover_pending: bool = False

    def uptime(self) -> float:
        return 0.0 if not self.started_at else max(0.0, time.monotonic() - self.started_at)

    def as_dict(self) -> dict[str, Any]:
        pid = self.external_pid
        if pid is None and self.proc is not None:
            pid = self.proc.pid
        return {
            "name": self.name,
            "status": self.status,
            "pid": pid,
            "port": _port_from_url(self.spec.ready_url),
            "restart_count": self.restart_count,
            "last_exit_code": self.last_exit_code,
            "last_exit_at": self.last_exit_at,
            "uptime_seconds": round(self.uptime(), 1),
        }


class Supervisor:
    def __init__(
        self,
        specs: dict[str, ProcSpec],
        logs: LogHub,
        restart: RestartConfig,
        ready_timeout: float = 30.0,
        probe: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self.specs = specs
        self.logs = logs
        self.restart_config = restart
        self.ready_timeout = ready_timeout
        self._probe = probe or self._default_probe
        self._probe_session: Any = None
        self.states: dict[str, ProcState] = {
            name: ProcState(name=name, spec=spec) for name, spec in specs.items()
        }
        self._closing = False
        self._external_watch: asyncio.Task | None = None
        self.external_poll = 2.0

    async def _default_probe(self, url: str) -> bool:
        # 复用 aiohttp 会话：http:// 地址不会构造 SSLContext，
        # 每次新建 httpx.Client 在 Windows 上会阻塞事件循环。
        import aiohttp

        if self._probe_session is None or self._probe_session.closed:
            self._probe_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=1.5)
            )
        try:
            async with self._probe_session.get(url) as resp:
                return resp.status < 500
        # 就绪探测失败等价于「不可达」，由 start() 负责超时与失败状态。
        except Exception:  # noqa: BLE001
            return False

    # ---- lifecycle -------------------------------------------------
    async def start(self, name: str) -> None:
        state = self._state(name)
        if state.status == EXTERNAL:
            return
        if state.proc is not None and state.proc.returncode is None:
            return
        state.manual_stop = False
        state.status = STARTING
        spec = state.spec
        if spec.sweep_pids is not None:
            await self._sweep(name, spec.sweep_pids)
        env = dict(os.environ)
        if spec.env:
            env.update(spec.env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.command,
                cwd=spec.cwd or None,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=spec.creationflags,
            )
        except OSError as exc:
            state.status = FAILED
            self.logs.write(name, f"启动失败：{exc}")
            raise
        state.proc = proc
        state.started_at = time.monotonic()
        self.logs.write(name, f"已启动 pid={proc.pid}")
        # 自动重启时这个 start() 就跑在 _watch 自己里面：此时 watcher 就是当前
        # 任务，取消它等于把自己打断，状态会永远停在 starting，新进程也没有
        # 看门狗（后续崩溃再没人拉起）。只有「换掉另一个还活着的看门狗」才取消。
        if (
            state.watcher is not None
            and not state.watcher.done()
            and state.watcher is not asyncio.current_task()
        ):
            state.watcher.cancel()
        state.drains = [
            asyncio.create_task(self._drain(name, stream))
            for stream in (proc.stdout, proc.stderr)
            if stream is not None
        ]
        if (spec.ready_url or spec.ready_probe) and not await self._wait_ready(state):
            await self._terminate(state)
            state.status = FAILED
            timeout = self._ready_timeout(state)
            raise RuntimeError(f"{name} 未在 {timeout:.0f}s 内就绪")
        state.status = READY
        state.watcher = asyncio.create_task(self._watch(state))

    async def _sweep(self, name: str, finder: Callable[[], list[int]]) -> None:
        """启动前清理同目录残留进程；finder 是同步调用（可能跑 powershell）。"""
        try:
            pids = await asyncio.to_thread(finder)
        except Exception as exc:  # noqa: BLE001 - 扫不到就当没有残留
            self.logs.write(name, f"扫描残留进程失败：{exc.__class__.__name__}")
            return
        for pid in pids or []:
            if pid == os.getpid():
                continue
            await _kill_tree(pid)
            self.logs.write(name, f"清理残留进程 pid={pid}（启动前）")

    def _ready_timeout(self, state: ProcState) -> float:
        override = state.spec.ready_timeout
        return self.ready_timeout if override is None else override

    async def _wait_ready(self, state: ProcState) -> bool:
        deadline = time.monotonic() + self._ready_timeout(state)
        while time.monotonic() < deadline:
            if state.proc is None or state.proc.returncode is not None:
                return False
            try:
                probe = state.spec.ready_probe
                if probe is not None:
                    # 同步探针（HTTP GET）必须离开事件循环：否则连接超时会把
                    # 整个 supervisor 的调度一起卡住。
                    ready = await asyncio.to_thread(probe)
                else:
                    ready = await self._probe(state.spec.ready_url)
                if ready:
                    return True
            # 等待就绪期间探测异常按「未就绪」处理，继续重试直到超时。
            except Exception:  # noqa: BLE001, S110
                pass
            await asyncio.sleep(0.25)
        return False

    async def _watch(self, state: ProcState) -> None:
        proc = state.proc
        if proc is None:
            return
        code = await proc.wait()
        if state.proc is not proc:
            # 已被 stop/restart 换掉，这条看门狗作废
            return
        uptime = state.uptime()
        state.last_exit_code = code
        state.last_exit_at = datetime.now().isoformat(timespec="seconds")
        state.proc = None
        await self._cancel_drains(state)
        self.logs.write(state.name, f"进程退出 code={code} uptime={uptime:.1f}s")

        if state.manual_stop or self._closing:
            state.status = STOPPED
            return
        if state.status == EXTERNAL:
            return
        if code == 0:
            state.status = STOPPED
            return
        if uptime >= HEALTHY_UPTIME:
            state.failures = 0
        state.failures += 1
        if state.failures > self.restart_config.max_failures:
            state.status = FAILED
            self.logs.write(
                state.name, f"连续失败 {state.failures} 次，停止重启"
            )
            return
        delay = self.restart_config.backoff_seconds[
            min(state.failures - 1, len(self.restart_config.backoff_seconds) - 1)
        ]
        state.status = RESTARTING
        state.restart_count += 1
        self.logs.write(state.name, f"{delay:.0f}s 后重启（第 {state.restart_count} 次）")
        await asyncio.sleep(delay)
        if state.manual_stop or self._closing:
            state.status = STOPPED
            return
        try:
            await self.start(state.name)
        # 自动重启失败标记 FAILED 并写日志，不能让看门狗线程退出。
        except Exception as exc:  # noqa: BLE001
            state.status = FAILED
            self.logs.write(state.name, f"重启失败：{exc}")

    async def stop(self, name: str) -> None:
        state = self._state(name)
        if state.status == EXTERNAL:
            return
        state.manual_stop = True
        state.status = STOPPING
        self.logs.write(name, "正在停止…")
        if state.proc is not None and state.proc.returncode is None:
            await self._terminate(state)
        state.proc = None
        await self._cancel_drains(state)
        state.status = STOPPED
        self.logs.write(name, "已停止")

    async def _terminate(self, state: ProcState) -> None:
        proc = state.proc
        if proc is None or proc.returncode is not None:
            return
        pid = proc.pid
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=STOP_TIMEOUT)
            return
        except TimeoutError:
            self.logs.write(
                state.name,
                f"{STOP_TIMEOUT:.0f}s 内未退出（pid={pid}），强制结束进程树",
            )
        # 第一次 taskkill 失败常见于 NapCat：它的 worker 正在自己收尾，
        # 这时 /F 会拿到 EPERM。隔一秒再试一次，别让残留进程占着 QQ 登录态。
        for attempt in (1, 2):
            if await _kill_tree(pid):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=KILL_WAIT_TIMEOUT)
            if proc.returncode is not None:
                return
            if attempt == 1:
                await asyncio.sleep(1.0)
        self.logs.write(
            state.name,
            f"强制结束失败（pid={pid}），残留进程可能占着端口或 QQ 登录态",
        )

    async def restart(self, name: str) -> None:
        await self.stop(name)
        await self.start(name)

    async def start_all(self) -> None:
        for name in self.specs:
            try:
                await self.start(name)
            # start_all 中单个进程失败只记日志，不阻断其余进程。
            except Exception as exc:  # noqa: BLE001
                self.logs.write("supervisor", f"{name} 启动失败：{exc}")

    async def stop_all(self) -> None:
        for name in reversed(list(self.specs)):
            with contextlib.suppress(Exception):
                await self.stop(name)

    async def shutdown(self) -> None:
        self._closing = True
        if self._external_watch is not None and not self._external_watch.done():
            self._external_watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._external_watch
        await self.stop_all()
        session = self._probe_session
        self._probe_session = None
        if session is not None and not session.closed:
            with contextlib.suppress(Exception):
                await session.close()

    # ---- helpers ---------------------------------------------------
    def _state(self, name: str) -> ProcState:
        state = self.states.get(name)
        if state is None:
            raise KeyError(f"未知进程：{name}")
        return state

    async def _drain(self, name: str, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                self.logs.write(name, text)
                self._maybe_recover(name, text)
        except asyncio.CancelledError:
            raise
        except OSError:
            return

    def _maybe_recover(self, name: str, text: str) -> None:
        """输出里出现已知的「卡死」文案时，安排一次自愈（不阻塞读流）。"""
        state = self.states.get(name)
        if state is None:
            return
        patterns = state.spec.recover_patterns
        if not patterns:
            return
        hit = next((p for p in patterns if p in text), None)
        if hit is None or state.recovering or state.recover_pending:
            return
        state.recover_pending = True
        asyncio.create_task(self._recover(state, hit))

    async def _recover(self, state: ProcState, hit: str) -> None:
        """清掉残留进程并重启自己；冷却期内只记一行日志。"""
        name = state.name
        try:
            now = time.monotonic()
            if state.last_recover_at and now - state.last_recover_at < RECOVER_COOLDOWN:
                self.logs.write(
                    name,
                    f"命中自愈模式（{hit}），但 {RECOVER_COOLDOWN / 60:.0f} 分钟冷却中，跳过",
                )
                return
            state.last_recover_at = now
            state.recovering = True
            self.logs.write(name, f"命中自愈模式（{hit}），清理残留并重启")
            # 先钉住 manual_stop：杀进程会让 _watch 醒来，别让它再排一次重启。
            state.manual_stop = True
            proc = state.proc
            if proc is not None and proc.returncode is None:
                await _kill_tree(proc.pid)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=KILL_WAIT_TIMEOUT)
            state.proc = None
            await self._cancel_drains(state)
            state.status = STOPPED
            await self.start(name)
        except Exception as exc:  # noqa: BLE001 - 自愈失败要写日志，不能吞掉
            state.status = FAILED
            self.logs.write(name, f"自愈失败：{exc.__class__.__name__}: {exc}")
        finally:
            state.recovering = False
            state.recover_pending = False

    async def _cancel_drains(self, state: ProcState) -> None:
        tasks = state.drains
        state.drains = []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def mark_external(self, name: str, pid: int) -> None:
        state = self._state(name)
        state.status = EXTERNAL
        state.external_pid = pid
        if self._external_watch is None or self._external_watch.done():
            self._external_watch = asyncio.create_task(self._watch_external())

    async def _watch_external(self) -> None:
        """外部实例不接管，但它死了要把状态改成 stopped，否则永远卡在 external。"""
        while not self._closing:
            await asyncio.sleep(self.external_poll)
            for state in self.states.values():
                if state.status != EXTERNAL:
                    continue
                if state.external_pid is not None and pid_alive(state.external_pid):
                    continue
                state.status = STOPPED
                state.external_pid = None
                self.logs.write(
                    state.name, "外部实例已退出，状态转为 stopped（可从控制台重新启动）"
                )
            if not any(s.status == EXTERNAL for s in self.states.values()):
                return

    def status(self) -> list[dict[str, Any]]:
        return [self.states[name].as_dict() for name in self.specs]


def _port_from_url(url: str) -> int | None:
    if not url:
        return None
    try:
        tail = url.split("//", 1)[1].split("/", 1)[0]
        return int(tail.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


async def _http_probe(url: str, timeout: float = 1.0) -> bool:
    """单次探测（测试用）。生产路径走 Supervisor._default_probe 的复用会话。"""

    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as client, client.get(url) as resp:
            return resp.status < 500
    # 无会话就绪探测失败按「不可达」处理。
    except Exception:  # noqa: BLE001
        return False


def pid_alive(pid: int) -> bool:
    """判断 PID 是否还活着；Windows 上绝不能用 os.kill(pid, 0)（会真杀进程）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def port_owner(port: int) -> int | None:
    """返回监听该端口的 PID；无人监听返回 None。

    `dashboard.host` 可以是 `0.0.0.0`、`127.0.0.1`，也可以是 `::`/`::1`：
    后者只监听 IPv6，只探 `127.0.0.1` 会把「端口仍被占着」误判成没人监听。
    先探 IPv4，失败再单独尝试 IPv6（部分容器/机器没有 `::1`，connect 会立刻失败）。
    """
    reachable = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            pass
        reachable = True
    except OSError:
        pass
    if not reachable:
        try:
            with socket.create_connection(("::1", port), timeout=0.3):
                pass
            reachable = True
        except OSError:
            pass
    if not reachable:
        return None
    return _find_listener_pid(port)


def _listener_pid_from_netstat(text: str, port: int) -> int | None:
    """从一段 netstat 输出里找监听 `port` 的 PID。

    本地地址可能是 `127.0.0.1:8080`，也可能是 `[::1]:8080` / `[::]:8080`：
    IPv6 地址自身带冒号，因此端口只取最后一个冒号之后的那段（不能按冒号 split 取固定下标）。
    """
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local, state, pid = parts[1], parts[3].upper(), parts[4]
        if state != "LISTENING":
            continue
        _, _, port_text = local.rpartition(":")
        if not port_text.isdigit() or int(port_text) != port:
            continue
        try:
            return int(pid)
        except ValueError:
            return None
    return None


def _find_listener_pid(port: int) -> int | None:
    if os.name != "nt":
        return None
    import subprocess

    # IPv4 与 IPv6 的监听项分属两张表：host 为 `::` 时只有 TCPv6 里有记录。
    # 任一表查询失败（老系统无 TCPv6 等）都只表示「这张表查不到」。
    for family in ("TCP", "TCPv6"):
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", family],
                capture_output=True,
                text=True,
                timeout=5,
                encoding="mbcs",
                errors="replace",
                check=False,
                **hidden_kwargs(),
            ).stdout
        except Exception:  # noqa: BLE001 - 探测失败只表示「查不到」，调用方按无 PID 处理
            continue
        pid = _listener_pid_from_netstat(out or "", port)
        if pid is not None:
            return pid
    return None


async def _kill_tree(pid: int) -> bool:
    """强制结束进程树；返回 taskkill 是否成功（失败由调用方决定要不要再试）。"""
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)
            return True
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except OSError:
        return False
    return proc.returncode == 0
