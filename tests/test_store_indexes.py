from __future__ import annotations

import sqlite3
from pathlib import Path

from dafeiyu.memory.store import Store

TIME_INDEXES = {"idx_messages_chat_ts", "idx_messages_ts"}


def index_flags(store: Store) -> dict[str, int]:
    """把 PRAGMA index_list 翻成 {索引名: unique}，方便断言存在性与非唯一。"""
    return {
        str(row["name"]): int(row["unique"])
        for row in store._conn.execute("PRAGMA index_list('messages')").fetchall()
    }


def test_init_schema_creates_message_time_indexes(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    try:
        store.init_schema()
        flags = index_flags(store)
        assert TIME_INDEXES <= set(flags)
        # 原有按会话分页的索引必须保留，且三个索引都不是 UNIQUE。
        for name in {"idx_messages_chat", *TIME_INDEXES}:
            assert flags[name] == 0
    finally:
        store.close()


def test_init_schema_backfills_indexes_on_old_database(tmp_path: Path) -> None:
    """老库只有 idx_messages_chat：升级后 init_schema 必须补上时间索引。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_type TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            content TEXT NOT NULL,
            is_bot INTEGER NOT NULL DEFAULT 0,
            timestamp INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (chat_type, chat_id, message_id)
        );
        CREATE INDEX idx_messages_chat ON messages (chat_type, chat_id, id);
        INSERT INTO messages
            (chat_type, chat_id, message_id, user_id, nickname, content, timestamp)
        VALUES ('group', 1, 'm1', 2, '鱼片', '老消息', 1700000000);
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        store.init_schema()
        assert TIME_INDEXES <= set(index_flags(store))
        # 老数据还在，补索引不能重建表。
        assert int(store._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]) == 1
    finally:
        store.close()


def test_init_schema_is_idempotent_about_indexes(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    try:
        store.init_schema()
        store.init_schema()
        store.init_schema()
        assert TIME_INDEXES <= set(index_flags(store))
    finally:
        store.close()
