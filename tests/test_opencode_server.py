import asyncio

import pytest

from dafeiyu.brain.client import BrainError
from dafeiyu.brain.opencode_server import OpenCodeServer


class FakeProc:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.stderr = None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def test_command_and_env(tmp_path):
    server = OpenCodeServer(
        "opencode.exe",
        str(tmp_path),
        "data/bot.db",
        port=4310,
    )
    assert server.command == [
        "opencode.exe",
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "4310",
    ]
    assert server.url == "http://127.0.0.1:4310"
    env = server._build_env()
    assert env["OPENCODE_DB"] == str((tmp_path / "data" / "bot.db").resolve())
    assert env["NO_COLOR"] == "1"


async def test_start_waits_until_probe_ready(monkeypatch, tmp_path):
    proc = FakeProc()
    calls = 0

    async def fake_exec(*args, **kwargs):
        return proc

    async def probe():
        nonlocal calls
        calls += 1
        return calls >= 2

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer(
        "opencode.exe", str(tmp_path), "data/bot.db", startup_timeout=1.0
    )
    monkeypatch.setattr(server, "_probe", probe)
    monkeypatch.setattr(server, "_port_in_use", lambda: False)

    await server.start()
    assert calls == 2
    assert server.is_running is True

    await server.stop()
    assert proc.terminated is True
    assert server.is_running is False


async def test_start_timeout_stops_process(monkeypatch, tmp_path):
    proc = FakeProc()

    async def fake_exec(*args, **kwargs):
        return proc

    async def probe():
        return False

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer(
        "opencode.exe", str(tmp_path), "data/bot.db", startup_timeout=0.01
    )
    monkeypatch.setattr(server, "_probe", probe)
    monkeypatch.setattr(server, "_port_in_use", lambda: False)

    with pytest.raises(BrainError, match="启动超时"):
        await server.start()
    assert proc.terminated is True


async def test_start_fails_when_process_exits(monkeypatch, tmp_path):
    proc = FakeProc(returncode=1)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer(
        "opencode.exe", str(tmp_path), "data/bot.db", startup_timeout=1.0
    )
    monkeypatch.setattr(server, "_port_in_use", lambda: False)

    with pytest.raises(BrainError, match="启动失败"):
        await server.start()


async def test_probe_ignores_system_proxy(monkeypatch, tmp_path):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def get(self, url):
            captured["url"] = url

            class _Response:
                status_code = 401

            return _Response()

        async def aclose(self):
            captured["closed"] = True

    monkeypatch.setattr(
        "dafeiyu.brain.opencode_server.httpx.AsyncClient", FakeClient
    )
    server = OpenCodeServer("opencode.exe", str(tmp_path), "data/bot.db")

    assert await server._probe() is True
    assert captured["trust_env"] is False
    assert captured["url"] == "http://127.0.0.1:24310/api/info"


async def test_start_fails_when_port_already_in_use(monkeypatch, tmp_path):
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    spawned: list[int] = []

    async def fake_exec(*args, **kwargs):
        spawned.append(1)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer(
        "opencode.exe", str(tmp_path), "data/bot.db", port=port, startup_timeout=0.5
    )
    try:
        with pytest.raises(BrainError, match="已被占用"):
            await server.start()
    finally:
        listener.close()
    assert spawned == []


class FakeStream:
    """模拟子进程的 stdout/stderr：逐行吐出，读完 EOF。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class StreamingProc(FakeProc):
    def __init__(self, stdout: FakeStream | None, stderr: FakeStream | None) -> None:
        super().__init__()
        self.stdout = stdout
        self.stderr = stderr


async def test_child_output_is_drained_continuously(monkeypatch, tmp_path):
    """stderr 曾是只写不读的 PIPE：日志攒满 64KB 后 serve 会阻塞在写日志上。"""
    lines = [f"line {i}\n".encode() for i in range(500)]
    proc = StreamingProc(FakeStream(list(lines)), FakeStream(list(lines)))

    async def fake_exec(*args, **kwargs):
        return proc

    async def probe():
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer("opencode.exe", str(tmp_path), "data/bot.db")
    monkeypatch.setattr(server, "_probe", probe)
    monkeypatch.setattr(server, "_port_in_use", lambda: False)

    await server.start()
    await asyncio.sleep(0.05)

    assert "line 499" in server.recent_output()
    assert len(server.recent_output().splitlines()) <= 50

    await server.stop()
    assert server.drain_tasks == []


async def test_start_reports_missing_bin_as_brain_error(monkeypatch, tmp_path):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", args[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer(
        "C:/nope/opencode.exe", str(tmp_path), "data/bot.db", startup_timeout=0.5
    )
    monkeypatch.setattr(server, "_port_in_use", lambda: False)

    with pytest.raises(BrainError, match="opencode"):
        await server.start()


class PendingStream(FakeStream):
    """readline() 永不返回：模拟 serve 还没吐 EOF 的管道。"""

    def __init__(self) -> None:
        super().__init__([])

    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        return b""


async def test_cancel_during_wait_ready_stops_process(monkeypatch, tmp_path):
    """启动被取消时（不是异常）也必须收回子进程与读管道任务。"""
    proc = StreamingProc(PendingStream(), PendingStream())

    async def fake_exec(*args, **kwargs):
        return proc

    async def probe():
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer(
        "opencode.exe", str(tmp_path), "data/bot.db", startup_timeout=30.0
    )
    monkeypatch.setattr(server, "_probe", probe)
    monkeypatch.setattr(server, "_port_in_use", lambda: False)

    task = asyncio.create_task(server.start())
    await asyncio.sleep(0.05)
    assert server.is_running is True
    assert server.drain_tasks != []

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.terminated is True
    assert proc.returncode is not None
    assert server._proc is None
    assert server.is_running is False
    assert server.drain_tasks == []

