from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor.auth import DashboardAuth
from dafeiyu.supervisor.dashboard import create_app
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.manager import EXTERNAL, ProcSpec, ProcState

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
        self.gate: asyncio.Event | None = None
        self.active = 0
        self.max_active = 0
        self._next_pid = 100

    def status(self):
        return []

    async def start(self, name):
        raise AssertionError(f"不应直接 start {name}")

    async def stop(self, name):
        raise AssertionError(f"不应直接 stop {name}")

    async def restart(self, name):
        self.restarted.append(name)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.fail:
                raise RuntimeError(f"startup timeout OPENCODE_API_KEY={SECRET}")
            self._next_pid += 1
            self.states[name].external_pid = self._next_pid
        finally:
            self.active -= 1


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, action, **kwargs) -> None:
        self.records.append({"action": action, **kwargs})


def make_app(
    tmp_path: Path,
    supervisor: FakeSupervisor,
    *,
    ops=None,
    auth: DashboardAuth | None = None,
):
    return create_app(
        supervisor,
        settings=None,
        logs=LogHub(tmp_path / "logs"),
        qr_path=tmp_path / "qrcode.png",
        bot_url=BOT_URL,
        auth=auth or DashboardAuth(enabled=False, username="", password=""),
        ops=ops,
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
    assert type(body["pid"]) is int
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
    first_resp = await client.post("/api/opencode/reload")
    second_resp = await client.post("/api/opencode/reload")
    first = await first_resp.json()
    second = await second_resp.json()

    # 每次调用都重新拉起 opencode，并返回新的 pid；bot 永远不被动到。
    assert fake_manager.restarted == ["opencode", "opencode"]
    assert second["pid"] != first["pid"]
    assert fake_manager.states["bot"].external_pid == 200

    failing_resp = await failing_client.post("/api/opencode/reload")
    assert failing_resp.status == 503
    body = await failing_resp.json()
    assert body["ok"] is False
    assert "pid" not in body
    assert "error" in body
    assert SECRET not in body["error"]
    assert fake_manager_failing.restarted == ["opencode"]


async def test_reload_rejects_external_opencode(tmp_path: Path) -> None:
    supervisor = FakeSupervisor()
    supervisor.states["opencode"].status = EXTERNAL
    client = TestClient(TestServer(make_app(tmp_path, supervisor)))
    await client.start_server()
    try:
        resp = await client.post("/api/opencode/reload")
        body = await resp.json()
    finally:
        await client.close()

    # EXTERNAL 下 restart 是空操作，不能返回 ok + 旧的 external_pid。
    assert resp.status == 503
    assert body["ok"] is False
    assert "pid" not in body
    assert supervisor.restarted == []


async def test_reload_serializes_concurrent_requests(tmp_path: Path) -> None:
    supervisor = FakeSupervisor()
    supervisor.gate = asyncio.Event()
    client = TestClient(TestServer(make_app(tmp_path, supervisor)))
    await client.start_server()
    try:
        first = asyncio.create_task(client.post("/api/opencode/reload"))
        for _ in range(100):
            if supervisor.active == 1:
                break
            await asyncio.sleep(0.01)
        assert supervisor.active == 1

        second = asyncio.create_task(client.post("/api/opencode/reload"))
        await asyncio.sleep(0.05)
        # 第一个 restart 未完成前，第二个必须排队而不是同时 stop/start。
        assert supervisor.active == 1
        assert supervisor.restarted == ["opencode"]

        supervisor.gate.set()
        first_resp, second_resp = await asyncio.gather(first, second)
        first_body = await first_resp.json()
        second_body = await second_resp.json()
    finally:
        await client.close()

    assert first_resp.status == 200
    assert second_resp.status == 200
    assert supervisor.max_active == 1
    assert supervisor.restarted == ["opencode", "opencode"]
    assert first_body["pid"] != second_body["pid"]


async def test_reload_writes_audit_on_success_and_failure(tmp_path: Path) -> None:
    supervisor = FakeSupervisor()
    audit = RecordingAudit()
    client = TestClient(
        TestServer(make_app(tmp_path, supervisor, ops=SimpleNamespace(audit=audit)))
    )
    await client.start_server()
    try:
        ok_resp = await client.post("/api/opencode/reload")
        assert ok_resp.status == 200
        pid = (await ok_resp.json())["pid"]

        supervisor.fail = True
        failed_resp = await client.post("/api/opencode/reload")
        assert failed_resp.status == 503
    finally:
        await client.close()

    assert len(audit.records) == 2
    success, failure = audit.records
    assert success["action"] == "opencode.reload"
    assert success["target"] == "opencode"
    assert success["result"] == "ok"
    assert success["detail"] == {"pid": pid}

    assert failure["action"] == "opencode.reload"
    assert failure["target"] == "opencode"
    assert failure["result"] == "error"
    assert "error" in failure["detail"]
    assert SECRET not in failure["detail"]["error"]
