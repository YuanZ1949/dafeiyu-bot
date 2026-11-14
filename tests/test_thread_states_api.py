"""话题状态只读接口：``GET /api/thread-states``。

口径：只读 ``chat_state`` 里**已落库**的收尾信号，绝不调用 ``router.is_thread_closed``
（那个方法会写库，GET 产生写入不可接受）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.models import ChatType

from test_control_api import make_control, running  # pyright: ignore[reportImplicitRelativeImport]
from test_supervisor_dashboard import make_app  # pyright: ignore[reportImplicitRelativeImport]

GROUP = ChatType.GROUP
CHAT_ID = "100"


def write_state(store, **fields) -> None:
    store.save_chat_state(GROUP, CHAT_ID, dict(fields))


async def get_thread_states(tmp_path: Path):
    control, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/thread-states")
        data = await resp.json()
    assert resp.status == 200
    return control, data


async def test_open_thread_reports_the_remaining_seconds(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    now = int(time.time())
    write_state(control.store, thread_closed_until=now + 120, last_closed_at=now)

    async with running(app) as client:
        data = await (await client.get("/api/thread-states")).json()

    assert data["total"] == 1
    item = data["items"][0]
    assert (item["chat_type"], item["chat_id"]) == ("group", CHAT_ID)
    assert 119 <= item["closed_left_seconds"] <= 121
    assert item["closed_until"] == now + 120
    assert item["last_closed_at"] == now


async def test_expired_thread_reports_zero_not_a_negative(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    now = int(time.time())
    write_state(control.store, thread_closed_until=now - 300, last_closed_at=now - 400)

    async with running(app) as client:
        data = await (await client.get("/api/thread-states")).json()

    assert data["items"][0]["closed_left_seconds"] == 0


async def test_thread_without_a_close_deadline_stays_zero(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    now = int(time.time())
    write_state(control.store, thread_closed_until=None, last_closed_at=now)

    async with running(app) as client:
        data = await (await client.get("/api/thread-states")).json()

    item = data["items"][0]
    assert item["closed_until"] is None
    assert item["closed_left_seconds"] == 0


async def test_sessions_without_any_close_signal_are_not_listed(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    write_state(control.store, last_bot_spoke=1_700_000_000.0)
    write_state(control.store, muted_until=1_700_000_000.0)

    async with running(app) as client:
        data = await (await client.get("/api/thread-states")).json()

    assert data == {"items": [], "total": 0}


async def test_reading_thread_states_does_not_write_the_state_row(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    now = int(time.time())
    write_state(control.store, thread_closed_until=now + 120, last_closed_at=now)
    before = control.store.load_chat_state(GROUP, CHAT_ID)

    async with running(app) as client:
        await client.get("/api/thread-states")

    assert control.store.load_chat_state(GROUP, CHAT_ID) == before


async def test_dashboard_proxy_forwards_bot_payload(tmp_path: Path) -> None:
    payload = {
        "items": [
            {
                "chat_type": "group",
                "chat_id": "100",
                "closed_until": 1_700_000_000.0,
                "closed_left_seconds": 90,
                "last_closed_at": 1_699_999_900.0,
            }
        ],
        "total": 1,
    }

    async def thread_states_endpoint(_: web.Request) -> web.Response:
        return web.json_response(payload)

    bot_app = web.Application()
    bot_app.router.add_get("/api/thread-states", thread_states_endpoint)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    url = str(bot_client.server.make_url("/")).rstrip("/")
    try:
        app, _, _ = make_app(tmp_path, bot_url=url)
        async with running(app) as client:
            resp = await client.get("/api/thread-states")
            data = await resp.json()
        assert resp.status == 200
        assert data == payload
    finally:
        await bot_client.close()
