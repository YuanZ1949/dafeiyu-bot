from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from dafeiyu.config import RestartConfig
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.manager import (
    EXTERNAL,
    FAILED,
    READY,
    STOPPED,
    ProcSpec,
    Supervisor,
    port_owner,
)

SLEEPER = "import time; print('ready-line', flush=True); time.sleep(60)"
EXITER = "import sys; sys.exit(3)"


def make_supervisor(
    tmp_path: Path,
    specs: dict[str, ProcSpec],
    backoff: tuple[float, ...] = (0.05,),
    max_failures: int = 3,
) -> tuple[Supervisor, LogHub]:
    hub = LogHub(tmp_path / "logs")
    supervisor = Supervisor(
        specs,
        hub,
        RestartConfig(backoff_seconds=backoff, max_failures=max_failures),
        ready_timeout=5.0,
    )
    return supervisor, hub


async def wait_for_log_line(
    hub: LogHub,
    source: str,
    needle: str,
    timeout: float = 5.0,
    interval: float = 0.02,
) -> list[str]:
    """轮询等待某行进入日志环，返回最后一次 tail 的内容。

    子进程冷启动（解释器加载）在 CI / 本机上可能远超 0.4s，固定 sleep 会
    在行还没读到时取样。这里只去掉对固定时长的依赖，断言本身不变。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        lines = hub.tail(source)
        if any(needle in line for line in lines):
            return lines
        if loop.time() >= deadline:
            return lines
        await asyncio.sleep(interval)


async def test_start_streams_output_and_stop(tmp_path: Path) -> None:
    specs = {"sleeper": ProcSpec("sleeper", [sys.executable, "-u", "-c", SLEEPER])}
    supervisor, hub = make_supervisor(tmp_path, specs)
    await supervisor.start("sleeper")
    try:
        lines = await wait_for_log_line(hub, "sleeper", "ready-line")
        state = supervisor.states["sleeper"]
        context = f"status={state.status!r} tail={lines!r}"
        assert state.status == READY, context
        assert any("ready-line" in line for line in lines), context
        assert state.proc is not None, context
        assert supervisor.status()[0]["pid"] == state.proc.pid, context
    finally:
        await supervisor.stop("sleeper")

    assert supervisor.states["sleeper"].status == STOPPED
    assert supervisor.states["sleeper"].proc is None


async def test_start_is_idempotent(tmp_path: Path) -> None:
    specs = {"sleeper": ProcSpec("sleeper", [sys.executable, "-u", "-c", SLEEPER])}
    supervisor, _ = make_supervisor(tmp_path, specs)
    await supervisor.start("sleeper")
    try:
        first_pid = supervisor.states["sleeper"].proc.pid
        await supervisor.start("sleeper")
        assert supervisor.states["sleeper"].proc.pid == first_pid
    finally:
        await supervisor.stop("sleeper")


async def test_crash_restarts_until_max_failures(tmp_path: Path) -> None:
    specs = {"boom": ProcSpec("boom", [sys.executable, "-c", EXITER])}
    supervisor, hub = make_supervisor(
        tmp_path, specs, backoff=(0.02,), max_failures=2
    )
    await supervisor.start("boom")
    for _ in range(80):
        if supervisor.states["boom"].status == FAILED:
            break
        await asyncio.sleep(0.05)

    state = supervisor.states["boom"]
    assert state.status == FAILED
    assert state.restart_count == 2
    assert state.last_exit_code == 3
    assert any("停止重启" in line for line in hub.tail("boom"))


async def test_manual_stop_does_not_trigger_restart(tmp_path: Path) -> None:
    code = "import time; time.sleep(0.2); raise SystemExit(1)"
    specs = {"boom": ProcSpec("boom", [sys.executable, "-c", code])}
    supervisor, _ = make_supervisor(tmp_path, specs, backoff=(0.02,), max_failures=3)
    await supervisor.start("boom")
    await supervisor.stop("boom")
    await asyncio.sleep(0.5)

    state = supervisor.states["boom"]
    assert state.status == STOPPED
    assert state.restart_count == 0


async def test_mark_external_blocks_start(tmp_path: Path) -> None:
    specs = {"napcat": ProcSpec("napcat", [sys.executable, "-c", SLEEPER])}
    supervisor, _ = make_supervisor(tmp_path, specs)
    supervisor.mark_external("napcat", 4321)
    await supervisor.start("napcat")

    state = supervisor.states["napcat"]
    assert state.status == EXTERNAL
    assert state.proc is None
    assert supervisor.status()[0]["pid"] == 4321


async def test_unknown_process_raises(tmp_path: Path) -> None:
    supervisor, _ = make_supervisor(tmp_path, {})
    with pytest.raises(KeyError):
        await supervisor.start("nope")


async def test_start_sweeps_leftover_processes(tmp_path: Path, monkeypatch) -> None:
    """NapCat 主进程崩溃后 worker 会占着登录态：启动前必须清掉。"""
    import dafeiyu.supervisor.manager as manager_mod

    killed: list[int] = []

    async def fake_kill_tree(pid: int) -> bool:
        killed.append(pid)
        return True

    monkeypatch.setattr(manager_mod, "_kill_tree", fake_kill_tree)
    specs = {
        "napcat": ProcSpec(
            "napcat",
            [sys.executable, "-u", "-c", SLEEPER],
            sweep_pids=lambda: [4321, 8765],
        )
    }
    supervisor, hub = make_supervisor(tmp_path, specs)
    await supervisor.start("napcat")

    assert killed == [4321, 8765]
    logs = hub.tail("napcat")
    assert any("清理残留进程 pid=4321" in line for line in logs)
    await supervisor.stop("napcat")


async def test_sweep_failure_does_not_block_start(tmp_path: Path) -> None:
    def boom() -> list[int]:
        raise OSError("powershell 挂了")

    specs = {
        "x": ProcSpec("x", [sys.executable, "-u", "-c", SLEEPER], sweep_pids=boom)
    }
    supervisor, hub = make_supervisor(tmp_path, specs)
    await supervisor.start("x")

    assert any("扫描残留进程失败" in line for line in hub.tail("x"))
    await supervisor.stop("x")


async def test_stop_all_marks_everything_stopped(tmp_path: Path) -> None:
    specs = {
        "a": ProcSpec("a", [sys.executable, "-c", SLEEPER]),
        "b": ProcSpec("b", [sys.executable, "-c", SLEEPER]),
    }
    supervisor, _ = make_supervisor(tmp_path, specs)
    await supervisor.start_all()
    await supervisor.stop_all()
    assert [p["status"] for p in supervisor.status()] == [STOPPED, STOPPED]


class _StubbornProc:
    """不肯优雅退出的子进程：terminate() 无效，wait() 一直挂着。"""

    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode

    def die(self) -> None:
        self.returncode = -9


def _stubborn_state(supervisor: Supervisor, proc: _StubbornProc):
    from dafeiyu.supervisor.manager import ProcState

    state = ProcState("napcat", ProcSpec("napcat", [sys.executable, "-c", SLEEPER]))
    state.proc = proc
    state.status = READY
    supervisor.states["napcat"] = state
    return state


async def test_terminate_retries_kill_tree_after_eperm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NapCat 的 worker 收尾时会 EPERM：第一次 kill 失败要再试一次。"""
    import dafeiyu.supervisor.manager as manager_mod

    monkeypatch.setattr(manager_mod, "STOP_TIMEOUT", 0.01)
    monkeypatch.setattr(manager_mod, "KILL_WAIT_TIMEOUT", 0.05)
    supervisor, hub = make_supervisor(tmp_path, {})
    proc = _StubbornProc()
    state = _stubborn_state(supervisor, proc)
    attempts: list[int] = []

    async def fake_kill_tree(pid: int) -> bool:
        attempts.append(pid)
        if len(attempts) == 1:
            return False  # 第一次 EPERM
        proc.die()
        return True

    monkeypatch.setattr(manager_mod, "_kill_tree", fake_kill_tree)
    await supervisor._terminate(state)

    assert attempts == [4321, 4321]
    logs = hub.tail("napcat")
    assert any("强制结束进程树" in line for line in logs)
    assert not any("强制结束失败" in line for line in logs)


async def test_terminate_logs_残留进程_when_kill_keeps_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dafeiyu.supervisor.manager as manager_mod

    monkeypatch.setattr(manager_mod, "STOP_TIMEOUT", 0.01)
    monkeypatch.setattr(manager_mod, "KILL_WAIT_TIMEOUT", 0.01)
    supervisor, hub = make_supervisor(tmp_path, {})
    proc = _StubbornProc()
    state = _stubborn_state(supervisor, proc)

    async def fake_kill_tree(pid: int) -> bool:
        return False

    monkeypatch.setattr(manager_mod, "_kill_tree", fake_kill_tree)
    await supervisor._terminate(state)

    logs = hub.tail("napcat")
    assert any("强制结束失败（pid=4321）" in line for line in logs)


def test_port_owner_returns_none_for_free_port() -> None:
    # 端口 1 在本地不会有人监听
    assert port_owner(1) is None


def test_log_hub_tail_and_subscribe(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs")
    queue = hub.subscribe("bot")
    hub.write("bot", "line1\nline2\n")

    tail = hub.tail("bot")
    assert len(tail) == 2
    assert "line1" in tail[0]
    first_seq, first_line = queue.get_nowait()
    second_seq, second_line = queue.get_nowait()
    assert (first_seq, second_seq) == (1, 2)
    assert first_line.endswith("line1")
    assert second_line.endswith("line2")

    hub.unsubscribe("bot", queue)
    hub.write("bot", "line3")
    assert queue.empty()


def test_log_hub_events_after_uses_sequence(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs")
    hub.write("bot", "line1\nline2\nline3")

    events = hub.tail_events("bot", 2)
    assert [seq for seq, _ in events] == [2, 3]
    assert events[0][1].endswith("line2")
    assert events[1][1].endswith("line3")
    assert [seq for seq, _ in hub.events_after("bot", 1)] == [2, 3]
    assert hub.events_after("bot", 3) == []
    assert hub.tail_events("unknown", 5) == []
    assert hub.events_after("unknown", 0) == []


def test_log_hub_tail_limits(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs")
    for index in range(5):
        hub.write("opencode", f"line-{index}")
    assert len(hub.tail("opencode", 2)) == 2
    assert hub.tail("opencode", 0) == []
    assert hub.tail("unknown", 5) == []


def test_log_hub_rotates_files(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs", max_bytes=80, backups=1)
    for index in range(30):
        hub.write("bot", f"a fairly long log line number {index}")
    assert (tmp_path / "logs" / "bot.log.1").exists()

async def test_restart_replaces_process(tmp_path: Path) -> None:
    specs = {"sleeper": ProcSpec("sleeper", [sys.executable, "-u", "-c", SLEEPER])}
    supervisor, _ = make_supervisor(tmp_path, specs)
    await supervisor.start("sleeper")
    first = supervisor.states["sleeper"].proc.pid
    try:
        await supervisor.restart("sleeper")
        state = supervisor.states["sleeper"]
        assert state.status == READY
        assert state.proc is not None and state.proc.pid != first
        assert state.restart_count == 0
    finally:
        await supervisor.stop("sleeper")


async def test_external_instance_turns_stopped_when_pid_dies(tmp_path: Path) -> None:
    specs = {"napcat": ProcSpec("napcat", [sys.executable, "-u", "-c", SLEEPER])}
    supervisor, hub = make_supervisor(tmp_path, specs)
    supervisor.external_poll = 0.05
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await proc.wait()
    supervisor.mark_external("napcat", proc.pid)
    assert supervisor.states["napcat"].status == EXTERNAL
    await asyncio.sleep(0.4)
    assert supervisor.states["napcat"].status == STOPPED
    assert supervisor.states["napcat"].external_pid is None
    assert any("外部实例已退出" in line for line in hub.tail("napcat"))
    await supervisor.shutdown()


RECOVER_CODE = (
    # 子进程 stdout 走管道时按区域设置编码（本机是 GBK），而 manager 统一按
    # UTF-8 读：显式 reconfigure 成 UTF-8，避免断言随机器区域设置漂移。
    "import sys\n"
    "sys.stdout.reconfigure(encoding='utf-8')\n"
    "import time\n"
    "print('当前账号(10001)已登录，无法重复登录', flush=True)\n"
    "print('当前账号(10001)已登录，无法重复登录', flush=True)\n"
    "time.sleep(60)\n"
)


async def test_recover_pattern_restarts_once_then_cools_down(
    tmp_path: Path, monkeypatch
) -> None:
    """命中「无法重复登录」→ 清残留 + 重启；同一条日志再刷也只在冷却期里记一笔。"""
    import dafeiyu.supervisor.manager as manager_mod

    async def fake_kill_tree(pid: int) -> bool:
        return True

    monkeypatch.setattr(manager_mod, "_kill_tree", fake_kill_tree)
    monkeypatch.setattr(manager_mod, "KILL_WAIT_TIMEOUT", 0.5)
    specs = {
        "napcat": ProcSpec(
            "napcat",
            [sys.executable, "-u", "-c", RECOVER_CODE],
            sweep_pids=list,
            recover_patterns=("无法重复登录",),
        )
    }
    supervisor, hub = make_supervisor(tmp_path, specs)
    await supervisor.start("napcat")

    lines = await wait_for_log_line(hub, "napcat", "命中自愈模式")
    assert any("清理残留并重启" in line for line in lines)

    lines = await wait_for_log_line(hub, "napcat", "冷却中", timeout=10.0)
    assert any("冷却中" in line for line in lines)
    await supervisor.stop("napcat")


async def test_recover_is_ignored_without_patterns(tmp_path: Path) -> None:
    specs = {
        "x": ProcSpec("x", [sys.executable, "-u", "-c", RECOVER_CODE]),
    }
    supervisor, hub = make_supervisor(tmp_path, specs)
    await supervisor.start("x")
    await wait_for_log_line(hub, "x", "无法重复登录")

    assert not any("命中自愈模式" in line for line in hub.tail("x"))
    await supervisor.stop("x")
