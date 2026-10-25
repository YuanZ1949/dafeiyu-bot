from __future__ import annotations

import sqlite3
from pathlib import Path

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
