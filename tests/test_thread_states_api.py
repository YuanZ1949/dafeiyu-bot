"""话题状态只读接口：``GET /api/thread-states``。

口径：只读 ``chat_state`` 里**已落库**的收尾信号，绝不调用 ``router.is_thread_closed``
（那个方法会写库，GET 产生写入不可接受）。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.control import BrainControl, create_app
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType

GROUP = ChatType.GROUP
CHAT_ID = "100"


def make_harness(tmp_path: Path):
    settings = Settings(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        bot_qq=999,
        ws_host="127.0.0.1",
        ws_port=8080,
        ws_token="",
        db_path=":memory:",
        persona=PersonaConfig("溟月", ("大肥鱼",), 120, 300, 0.05),
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",), mode="model"),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    service = SimpleNamespace(
        brain=None, behavior=settings.behavior, groups=settings.groups, mirror=None
    )
    control = BrainControl(
        settings, store, service, None, runtime_path=str(tmp_path / "runtime.json")
    )
    return control, create_app(control, "", None)


async def test_open_thread_reports_the_remaining_seconds(tmp_path: Path) -> None:
    control, app = make_harness(tmp_path)
    now = int(time.time())
    control.store.save_chat_state(
        GROUP, CHAT_ID, {"thread_closed_until": now + 120, "last_closed_at": now}
    )

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/thread-states")
        data = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert data["total"] == 1
    item = data["items"][0]
    assert (item["chat_type"], item["chat_id"]) == ("group", CHAT_ID)
    assert 119 <= item["closed_left_seconds"] <= 121
    assert item["closed_until"] == now + 120
    assert item["last_closed_at"] == now


async def test_expired_thread_reports_zero_not_a_negative(tmp_path: Path) -> None:
    control, app = make_harness(tmp_path)
    now = int(time.time())
    control.store.save_chat_state(
        GROUP, CHAT_ID, {"thread_closed_until": now - 300, "last_closed_at": now - 400}
    )

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        data = await (await client.get("/api/thread-states")).json()
    finally:
        await client.close()

    assert data["items"][0]["closed_left_seconds"] == 0


async def test_thread_without_a_close_deadline_stays_zero(tmp_path: Path) -> None:
    control, app = make_harness(tmp_path)
    now = int(time.time())
    control.store.save_chat_state(
        GROUP, CHAT_ID, {"thread_closed_until": None, "last_closed_at": now}
    )

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        data = await (await client.get("/api/thread-states")).json()
    finally:
        await client.close()

    item = data["items"][0]
    assert item["closed_until"] is None
    assert item["closed_left_seconds"] == 0


async def test_sessions_without_any_close_signal_are_not_listed(tmp_path: Path) -> None:
    control, app = make_harness(tmp_path)
    control.store.save_chat_state(GROUP, "200", {"last_bot_spoke": 1_700_000_000.0})
    control.store.save_chat_state(GROUP, "300", {"muted_until": 1_700_000_000.0})

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        data = await (await client.get("/api/thread-states")).json()
    finally:
        await client.close()

    assert data == {"items": [], "total": 0}


async def test_reading_thread_states_does_not_write_the_state_row(tmp_path: Path) -> None:
    control, app = make_harness(tmp_path)
    now = int(time.time())
    control.store.save_chat_state(
        GROUP, CHAT_ID, {"thread_closed_until": now + 120, "last_closed_at": now}
    )
    before = control.store.load_chat_state(GROUP, CHAT_ID)

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.get("/api/thread-states")
    finally:
        await client.close()

    assert control.store.load_chat_state(GROUP, CHAT_ID) == before
