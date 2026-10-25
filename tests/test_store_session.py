from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.memory.store import SessionStats, Store
from dafeiyu.models import ChatType


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "test.db")
    s.init_schema()
    yield s
    s.close()


def _old_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE brain_sessions (provider TEXT NOT NULL, chat_type TEXT NOT NULL,"
        " chat_id INTEGER NOT NULL, session_id TEXT NOT NULL, updated_at TEXT NOT NULL,"
        " PRIMARY KEY (provider, chat_type, chat_id))"
    )
    conn.execute(
        "CREATE TABLE delivered_messages (chat_type TEXT NOT NULL, chat_id INTEGER NOT NULL,"
        " message_id TEXT NOT NULL, delivered_at TEXT NOT NULL,"
        " PRIMARY KEY (chat_type, chat_id, message_id))"
    )
    conn.commit()
    conn.close()


def test_init_schema_migrates_old_db(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    _old_db(path)

    store = Store(path)
    store.init_schema()
    try:
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(brain_sessions)")}
        assert {"created_at", "delivered_count", "token_estimate"} <= cols
        dcols = {
            r["name"] for r in store._conn.execute("PRAGMA table_info(delivered_messages)")
        }
        assert "skipped_reason" in dcols
        tables = {
            r["name"]
            for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "session_summaries" in tables
    finally:
        store.close()


def test_session_stats_default_and_bump(store: Store) -> None:
    assert store.session_stats(ChatType.GROUP, 1) == SessionStats(0, 0, None)

    store.bump_session_stats(ChatType.GROUP, 1, messages=5, tokens=1200)
    st = store.session_stats(ChatType.GROUP, 1)
    assert (st.delivered_count, st.token_estimate) == (5, 1200)
    assert st.created_at

    store.bump_session_stats(ChatType.GROUP, 1, messages=2, tokens=100)
    st = store.session_stats(ChatType.GROUP, 1)
    assert (st.delivered_count, st.token_estimate) == (7, 1300)

    store.reset_session_stats(ChatType.GROUP, 1)
    st = store.session_stats(ChatType.GROUP, 1)
    assert (st.delivered_count, st.token_estimate) == (0, 0)
    assert st.created_at


def test_session_summary_roundtrip(store: Store) -> None:
    store.add_session_summary(ChatType.GROUP, 1, "上次聊到猫粮")
    store.add_session_summary(ChatType.GROUP, 1, "又聊了冻干")

    assert store.latest_session_summary(ChatType.GROUP, 1) == "又聊了冻干"
    assert store.latest_session_summary(ChatType.GROUP, 2) is None
