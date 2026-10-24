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


async def test_start_streams_output_and_stop(tmp_path: Path) -> None:
    specs = {"sleeper": ProcSpec("sleeper", [sys.executable, "-u", "-c", SLEEPER])}
    supervisor, hub = make_supervisor(tmp_path, specs)
    await supervisor.start("sleeper")
    try:
        await asyncio.sleep(0.4)
        state = supervisor.states["sleeper"]
        assert state.status == READY
        assert any("ready-line" in line for line in hub.tail("sleeper"))
        assert supervisor.status()[0]["pid"] == state.proc.pid
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


async def test_stop_all_marks_everything_stopped(tmp_path: Path) -> None:
    specs = {
        "a": ProcSpec("a", [sys.executable, "-c", SLEEPER]),
        "b": ProcSpec("b", [sys.executable, "-c", SLEEPER]),
    }
    supervisor, _ = make_supervisor(tmp_path, specs)
    await supervisor.start_all()
    await supervisor.stop_all()
    assert [p["status"] for p in supervisor.status()] == [STOPPED, STOPPED]


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
    assert queue.get_nowait().endswith("line1")
    assert queue.get_nowait().endswith("line2")

    hub.unsubscribe("bot", queue)
    hub.write("bot", "line3")
    assert queue.empty()


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
