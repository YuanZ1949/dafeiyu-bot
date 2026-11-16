from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from test_control_api import make_control, running

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "test.db")
    s.init_schema()
    yield s
    s.close()


def _move_usage_to(store: Store, date: str) -> None:
    store._conn.execute("UPDATE usage SET date = ?", (date,))
    store._conn.commit()


def test_usage_by_purpose_groups_side_channels_separately(store: Store) -> None:
    store.record_usage(ChatType.GROUP, 1, "reply", 10, 0)
    store.record_usage(ChatType.GROUP, 1, "ambient", 0, 20)
    store.record_usage(ChatType.GROUP, 1, "summary", 40, 0)
    store.record_usage(ChatType.GROUP, 1, "impression", 0, 80)
    store.record_usage(ChatType.GROUP, 1, "error", 5, 0)
    _move_usage_to(store, "2026-10-24")

    result = store.usage_by_purpose("2026-10-24", "2026-10-24")
    assert result == [
        {"purpose": "impression", "calls": 0, "errors": 0, "tokens": 80},
        {"purpose": "summary", "calls": 0, "errors": 0, "tokens": 40},
        {"purpose": "ambient", "calls": 1, "errors": 0, "tokens": 20},
        {"purpose": "reply", "calls": 1, "errors": 0, "tokens": 10},
        {"purpose": "error", "calls": 0, "errors": 1, "tokens": 5},
    ]
    assert sum(row["tokens"] for row in result) == 155


def test_usage_by_purpose_side_channel_only_and_empty_window(store: Store) -> None:
    store.record_usage(ChatType.GROUP, 1, "summary", 3, 4)
    store.record_usage(ChatType.GROUP, 1, "summary", 5, 6)
    _move_usage_to(store, "2026-10-24")

    result = store.usage_by_purpose("2026-10-24", "2026-10-24")
    assert result == [{"purpose": "summary", "calls": 0, "errors": 0, "tokens": 18}]
    assert result[0]["tokens"] > 0

    assert store.usage_by_purpose("2026-10-25", "2026-10-25") == []
    assert store.usage_totals("2026-10-25", "2026-10-25") == (0, 0)


async def test_usage_endpoint_exposes_purposes(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    control.store.record_usage(ChatType.GROUP, 100, "reply", 10, 5)
    control.store.record_usage(ChatType.GROUP, 100, "ambient", 2, 1)
    control.store.record_usage(ChatType.GROUP, 100, "summary", 4, 2)
    control.store.record_usage(ChatType.GROUP, 100, "error", 0, 0)

    async with running(app) as client:
        resp = await client.get("/api/usage?days=7")
        payload = await resp.json()

    assert resp.status == 200
    for key in ("days", "start", "end", "series", "totals", "top", "recent_errors"):
        assert key in payload
    assert set(payload["totals"]) == {"calls", "errors", "tokens"}
    purposes = payload["purposes"]
    assert {"purpose", "calls", "errors", "tokens"} == set(purposes[0])
    assert sum(p["tokens"] for p in purposes) == payload["totals"]["tokens"]
    by_purpose = {p["purpose"]: p for p in purposes}
    assert by_purpose["reply"]["calls"] == 1
    assert by_purpose["ambient"]["calls"] == 1
    assert by_purpose["summary"]["calls"] == 0
    assert by_purpose["error"]["errors"] == 1


async def test_usage_endpoint_empty_window_keeps_totals_zero(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)

    async with running(app) as client:
        resp = await client.get("/api/usage?days=7")
        payload = await resp.json()

    assert resp.status == 200
    assert payload["purposes"] == []
    assert payload["totals"] == {"calls": 0, "errors": 0, "tokens": 0}
    assert all(day["calls"] == 0 and day["tokens"] == 0 for day in payload["series"])
