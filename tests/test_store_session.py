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


def test_set_brain_session_stamps_created_at(store: Store) -> None:
    """线上 created_at 恒为 NULL，天数轮换判据等于没接上。"""
    store.set_brain_session("opencode", ChatType.GROUP, 1, "s-1")

    st = store.session_stats(ChatType.GROUP, 1)
    assert st.created_at
    assert st.created_at == store.today()

    # 已有值不能被后来的 set 覆盖（否则每天都在「刷新」session 年龄）。
    store._conn.execute(
        "UPDATE brain_sessions SET created_at = '2026-01-01'"
        " WHERE provider = 'opencode'"
    )
    store._conn.commit()
    store.set_brain_session("opencode", ChatType.GROUP, 1, "s-2")

    assert store.session_stats(ChatType.GROUP, 1).created_at == "2026-01-01"


def test_set_then_bump_keeps_created_at(store: Store) -> None:
    store.set_brain_session("opencode", ChatType.GROUP, 1, "s-1")
    store._conn.execute(
        "UPDATE brain_sessions SET created_at = NULL WHERE provider = 'opencode'"
    )
    store._conn.commit()

    store.bump_session_stats(ChatType.GROUP, 1, messages=1, tokens=10)

    assert store.session_stats(ChatType.GROUP, 1).created_at


def test_init_schema_backfills_null_created_at(tmp_path: Path) -> None:
    """老库升级：created_at 补列后历史行是 NULL，启动时要回填。"""
    path = tmp_path / "legacy.db"
    _old_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO brain_sessions (provider, chat_type, chat_id, session_id, updated_at)"
        " VALUES ('opencode', 'group', 1, 's-old', '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    store.init_schema()
    try:
        assert store.session_stats(ChatType.GROUP, 1).created_at == store.today()
        # 幂等：再跑一次不会把已有值重新盖一遍。
        store._conn.execute(
            "UPDATE brain_sessions SET created_at = '2026-01-01' WHERE chat_id = 1"
        )
        store._conn.commit()
        store.init_schema()
        assert store.session_stats(ChatType.GROUP, 1).created_at == "2026-01-01"
    finally:
        store.close()


def test_session_summary_roundtrip(store: Store) -> None:
    store.add_session_summary(ChatType.GROUP, 1, "上次聊到猫粮")
    store.add_session_summary(ChatType.GROUP, 1, "又聊了冻干")

    assert store.latest_session_summary(ChatType.GROUP, 1) == "又聊了冻干"
    assert store.latest_session_summary(ChatType.GROUP, 2) is None


def test_recent_session_summary_only_returns_fresh(store: Store) -> None:
    store.add_session_summary(ChatType.GROUP, 1, "刚聊完轮换")
    assert store.recent_session_summary(ChatType.GROUP, 1) == "刚聊完轮换"
    assert store.recent_session_summary(ChatType.GROUP, 2) is None

    # 过期的接续语不该在重启后又被注入一次。
    store._conn.execute(
        "UPDATE session_summaries SET created_at = datetime('now', '-25 hours')"
    )
    store._conn.commit()

    assert store.recent_session_summary(ChatType.GROUP, 1, within_hours=24) is None
    assert store.recent_session_summary(ChatType.GROUP, 1, within_hours=48) == "刚聊完轮换"
