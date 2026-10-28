from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor.auth import DashboardAuth
from dafeiyu.supervisor.dashboard import create_app
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.manager import ProcSpec, ProcState

BOT_URL = "http://127.0.0.1:1"
SECRET = "sk-reload-secret-should-not-leak"


class FakeSupervisor:
    def __init__(self, *, fail: bool = False) -> None:
        self.specs = {"opencode": object(), "bot": object()}
        self.states = {
            name: ProcState(name=name, spec=ProcSpec(name=name, command=[]))
            for name in self.specs
        }
        self.states["opencode"].external_pid = 100
        self.states["bot"].external_pid = 200
        self.restarted: list[str] = []
        self.fail = fail
        self._next_pid = 100

    def status(self):
        return []

    async def start(self, name):
        raise AssertionError(f"不应直接 start {name}")

    async def stop(self, name):
        raise AssertionError(f"不应直接 stop {name}")

    async def restart(self, name):
        self.restarted.append(name)
        if self.fail:
            raise RuntimeError(f"startup timeout OPENCODE_API_KEY={SECRET}")
        self._next_pid += 1
        self.states[name].external_pid = self._next_pid


def make_app(tmp_path: Path, supervisor: FakeSupervisor):
    return create_app(
        supervisor,
        settings=None,
        logs=LogHub(tmp_path / "logs"),
        qr_path=tmp_path / "qrcode.png",
        bot_url=BOT_URL,
        auth=DashboardAuth(enabled=False, username="", password=""),
    )


@pytest.fixture
def fake_manager() -> FakeSupervisor:
    return FakeSupervisor()


@pytest.fixture
def fake_manager_failing() -> FakeSupervisor:
    return FakeSupervisor(fail=True)


@pytest.fixture
async def client(tmp_path: Path, fake_manager: FakeSupervisor):
    test_client = TestClient(TestServer(make_app(tmp_path, fake_manager)))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


@pytest.fixture
async def failing_client(tmp_path: Path, fake_manager_failing: FakeSupervisor):
    test_client = TestClient(TestServer(make_app(tmp_path, fake_manager_failing)))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


async def test_reload_restarts_opencode_and_waits_ready(
    client: TestClient, fake_manager: FakeSupervisor
) -> None:
    resp = await client.post("/api/opencode/reload")

    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert isinstance(body["pid"], int)
    assert fake_manager.restarted == ["opencode"]


async def test_reload_reports_failure(
    failing_client: TestClient, fake_manager_failing: FakeSupervisor
) -> None:
    resp = await failing_client.post("/api/opencode/reload")

    assert resp.status == 503
    assert fake_manager_failing.restarted == ["opencode"]


async def test_reload_is_scoped_and_failure_makes_no_pid_claim(
    client: TestClient,
    failing_client: TestClient,
    fake_manager: FakeSupervisor,
    fake_manager_failing: FakeSupervisor,
) -> None:
    first = await (await client.post("/api/opencode/reload")).json()
    second = await (await client.post("/api/opencode/reload")).json()

    # 每次调用都重新拉起 opencode，并返回新的 pid；bot 永远不被动到。
    assert fake_manager.restarted == ["opencode", "opencode"]
    assert second["pid"] != first["pid"]
    assert fake_manager.states["bot"].external_pid == 200

    body = await (await failing_client.post("/api/opencode/reload")).json()
    assert body["ok"] is False
    assert "pid" not in body
    assert "error" in body
    assert SECRET not in body["error"]
    assert fake_manager_failing.restarted == ["opencode"]
