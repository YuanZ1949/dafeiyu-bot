from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

# 真 schema-1 夹具：复用 test_store_migration 的 v1 库拼装（INTEGER id 布局 +
# FTS），替代旧版「把 v2 库重置 user_version=0」的假升级用例。
from test_store_migration import make_v1_db


def make_store(path: Path) -> Store:
    store = Store(path)
    store.init_schema()
    return store


def message(content: str = "hi") -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=100,
        user_id=1,
        nickname="tester",
        content=content,
        message_id="m1",
        timestamp=1,
    )


def test_new_db_sets_schema_version(tmp_path: Path) -> None:
    store = make_store(tmp_path / "t.db")
    assert store.db_stats()["schema_version"] == 2
    store.close()


def test_old_db_is_upgraded_to_schema_version(tmp_path: Path) -> None:
    """真 v1 库（INTEGER id + user_version=1）重开后迁移到 v2。"""
    path = tmp_path / "old.db"
    make_v1_db(path)

    store = Store(path)
    store.init_schema()
    assert store.db_stats()["schema_version"] == 2
    store.close()


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="99"):
        Store(path).init_schema()


def test_db_stats_reports_sizes_and_message_count(tmp_path: Path) -> None:
    store = make_store(tmp_path / "t.db")
    assert store.db_stats()["message_count"] == 0
    store.add_message(message())
    stats = store.db_stats()
    assert stats["message_count"] == 1
    assert stats["db_bytes"] > 0
    assert stats["wal_bytes"] >= 0
    assert stats["db_path"].endswith("t.db")
    store.close()


def test_usage_totals_and_error_stats(tmp_path: Path) -> None:
    store = make_store(tmp_path / "t.db")
    store.record_usage(ChatType.GROUP, 100, "reply", 10, 20)
    store.record_usage(ChatType.GROUP, 100, "reply", 5, 5)
    store.record_usage(ChatType.GROUP, 100, "error", 0, 0)

    today = Store.today()
    assert store.usage_totals(today, today) == (3, 40)
    assert store.error_stats(900)["count"] == 1
    assert store.error_stats(900)["last_at"]
    assert store.error_stats(0)["count"] == 0
    store.close()
