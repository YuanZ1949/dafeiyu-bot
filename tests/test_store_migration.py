from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dafeiyu.config import ConfigError
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType


def test_old_impressions_table_gets_source_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE impressions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_type TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            subject_user_id INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (chat_type, chat_id, subject_user_id, kind, content)
        );
        INSERT INTO impressions
            (chat_type, chat_id, subject_user_id, kind, content, confidence)
        VALUES ('group', 100, 42, 'fact', '旧印象', 0.7);
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    store.init_schema()
    try:
        columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(impressions)").fetchall()
        }
        assert {"source_message_id", "source_excerpt"} <= columns

        old = store.list_impressions(ChatType.GROUP, 100, topk=10)[0]
        assert old.content == "旧印象"
        assert old.source_message_id == ""
        assert old.source_excerpt == ""

        store.upsert_impression(
            ChatType.GROUP,
            100,
            42,
            "fact",
            "新印象",
            0.8,
            source_message_id="m1",
            source_excerpt="新印象的来源",
        )
        new = store.get_impression(old.id + 1)
        assert new is not None
        assert new.source_message_id == "m1"
        assert new.source_excerpt == "新印象的来源"
    finally:
        store.close()

def test_old_chat_state_table_gets_new_columns(tmp_path: Path) -> None:
    """跑过中间版本的库只有基础列；启动后应自动补齐降频与判重字段。"""
    path = tmp_path / "old-chat.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chat_state (
            chat_type TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            last_bot_spoke REAL,
            last_user_message REAL,
            muted_until REAL,
            ambient_streak INTEGER NOT NULL DEFAULT 0,
            thread_closed_until REAL,
            last_closed_at REAL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (chat_type, chat_id)
        );
        INSERT INTO chat_state (chat_type, chat_id, ambient_streak)
        VALUES ('group', 100, 2);
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    store.init_schema()
    try:
        row = store.load_chat_state(ChatType.GROUP, 100)
        assert row is not None
        assert row["ambient_streak"] == 2
        assert row["no_reply_streak"] == 0
        assert row["downshift_until"] is None
        assert row["last_prompt"] == ""
        assert row["last_prompt_at"] is None
    finally:
        store.close()

# v1 夹具 DDL：与 task 5 改造前的 _SCHEMA 逐字一致（INTEGER user_id 布局）。
# 这是「真 schema-1 库」的快照，故故意不随 _SCHEMA 演化；task 5 负责
# 在重建落地的同一把提交里更新夹具，让旧版本列布局一直有真实代表。
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    content TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    timestamp INTEGER NOT NULL DEFAULT 0,
    at_bot INTEGER NOT NULL DEFAULT 0,
    reply_to_bot INTEGER NOT NULL DEFAULT 0,
    reply_to_message_id TEXT,
    reply_to_user_id INTEGER,
    recalled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chat_type, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat
    ON messages (chat_type, chat_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts
    ON messages (chat_type, chat_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (timestamp);

CREATE TABLE IF NOT EXISTS impressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    subject_user_id INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    subject_nickname TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_excerpt TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chat_type, chat_id, subject_user_id, kind, content)
);
CREATE INDEX IF NOT EXISTS idx_impressions_chat ON impressions (chat_type, chat_id);
"""


def make_v1_db(path: Path) -> None:
    """直接拼出 schema-1 库（绕开 init_schema，不然测试对象自己盖了自己的前身）。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def db_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def test_v2_skeleton(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    make_v1_db(db)

    store = Store(db)
    try:
        store.init_schema()
        assert store.db_stats()["schema_version"] == 2
    finally:
        store.close()

    # 同目录出现 .pre-wechat-* 备份：迁移动手前的一致性快照。
    backups = list(tmp_path.glob("*.pre-wechat-*"))
    assert len(backups) == 1

    # 骨架不动表数据；迁移后的库必须完整可用、不留崩溃标记。
    conn = sqlite3.connect(db)
    try:
        assert db_version(conn) == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM migration_marker"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_migration_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "memory.db"
    make_v1_db(db)

    def boom(self: Store) -> None:
        raise RuntimeError("rebuild 中途爆炸")

    monkeypatch.setattr(Store, "_rebuild_v2_tables", boom)
    store = Store(db)
    try:
        with pytest.raises(RuntimeError, match="中途爆炸"):
            store.init_schema()
    finally:
        store.close()

    backups = list(tmp_path.glob("*.pre-wechat-*"))
    assert len(backups) == 1
    backup_name = backups[0].name

    # 库仍然是 v1：事务回滚 + 标记残留，等着下次启动拒绝。
    conn = sqlite3.connect(db)
    try:
        assert db_version(conn) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM migration_marker"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    # 重开库 init_schema 必须拒绝启动，错误信息点名备份文件 —— 不是修复半个库。
    with pytest.raises(ConfigError) as excinfo:
        Store(db).init_schema()
    message = str(excinfo.value)
    assert "迁移未完成" in message
    assert backup_name in message
