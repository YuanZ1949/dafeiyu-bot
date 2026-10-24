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

    with pytest.raises(BrainError, match="启动失败"):
        await server.start()
