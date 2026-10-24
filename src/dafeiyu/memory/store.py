from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from dafeiyu.models import ChatType, Impression, Message

_SCHEMA = """
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chat_type, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_type, chat_id, id);

CREATE TABLE IF NOT EXISTS impressions (
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
CREATE INDEX IF NOT EXISTS idx_impressions_chat ON impressions (chat_type, chat_id);

CREATE TABLE IF NOT EXISTS summaries (
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id, date)
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    purpose TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_usage_chat_date ON usage (chat_type, chat_id, date);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS impressions_fts USING fts5(
    content,
    content='impressions',
    content_rowid='id',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS impressions_ai AFTER INSERT ON impressions BEGIN
    INSERT INTO impressions_fts (rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS impressions_ad AFTER DELETE ON impressions BEGIN
    INSERT INTO impressions_fts (impressions_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS impressions_au AFTER UPDATE ON impressions BEGIN
    INSERT INTO impressions_fts (impressions_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    INSERT INTO impressions_fts (rowid, content) VALUES (new.id, new.content);
END;
"""


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._fts = False

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        try:
            self._conn.executescript(_FTS_SCHEMA)
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    # ---- messages -------------------------------------------------
    def add_message(self, m: Message) -> bool:
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (chat_type, chat_id, message_id, user_id, nickname, content, is_bot, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                m.chat_type.value,
                m.chat_id,
                m.message_id,
                m.user_id,
                m.nickname,
                m.content,
                int(m.is_bot),
                m.timestamp,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def recent_messages(self, chat_type: ChatType, chat_id: int, limit: int) -> list[Message]:
        rows = self._conn.execute(
            """
            SELECT * FROM messages
            WHERE chat_type = ? AND chat_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (chat_type.value, chat_id, limit),
        ).fetchall()
        return [self._row_to_message(r) for r in reversed(rows)]

    def count_messages(self, chat_type: ChatType, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_type = ? AND chat_id = ?",
            (chat_type.value, chat_id),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _row_to_message(r: sqlite3.Row) -> Message:
        return Message(
            chat_type=ChatType(r["chat_type"]),
            chat_id=r["chat_id"],
            user_id=r["user_id"],
            nickname=r["nickname"],
            content=r["content"],
            message_id=r["message_id"],
            is_bot=bool(r["is_bot"]),
            timestamp=r["timestamp"],
        )

    # ---- impressions ----------------------------------------------
    def upsert_impression(
        self,
        chat_type: ChatType,
        chat_id: int,
        subject_user_id: int | None,
        kind: str,
        content: str,
        confidence: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO impressions
                (chat_type, chat_id, subject_user_id, kind, content, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_type, chat_id, subject_user_id, kind, content)
            DO UPDATE SET confidence = excluded.confidence, updated_at = datetime('now')
            """,
            (chat_type.value, chat_id, subject_user_id or 0, kind, content, confidence),
        )
        self._conn.commit()

    def search_impressions(
        self, chat_type: ChatType, chat_id: int, query: str, topk: int
    ) -> list[Impression]:
        rows: list[sqlite3.Row] = []
        if self._fts and query.strip():
            phrase = '"' + query.replace('"', '""') + '"'
            try:
                rows = self._conn.execute(
                    """
                    SELECT i.* FROM impressions_fts f
                    JOIN impressions i ON i.id = f.rowid
                    WHERE impressions_fts MATCH ?
                      AND i.chat_type = ? AND i.chat_id = ?
                    ORDER BY bm25(impressions_fts)
                    LIMIT ?
                    """,
                    (phrase, chat_type.value, chat_id, topk),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            rows = self._conn.execute(
                """
                SELECT * FROM impressions
                WHERE chat_type = ? AND chat_id = ? AND content LIKE ? ESCAPE '\\'
                ORDER BY confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (chat_type.value, chat_id, _like_pattern(query), topk),
            ).fetchall()
        return [self._row_to_impression(r) for r in rows]

    def list_impressions(self, chat_type: ChatType, chat_id: int, topk: int) -> list[Impression]:
        rows = self._conn.execute(
            """
            SELECT * FROM impressions
            WHERE chat_type = ? AND chat_id = ?
            ORDER BY confidence DESC, updated_at DESC
            LIMIT ?
            """,
            (chat_type.value, chat_id, topk),
        ).fetchall()
        return [self._row_to_impression(r) for r in rows]

    @staticmethod
    def _row_to_impression(r: sqlite3.Row) -> Impression:
        return Impression(
            chat_type=ChatType(r["chat_type"]),
            chat_id=r["chat_id"],
            subject_user_id=r["subject_user_id"] or None,
            kind=r["kind"],
            content=r["content"],
            confidence=r["confidence"],
            updated_at=r["updated_at"],
        )

    # ---- summaries ------------------------------------------------
    def upsert_summary(self, chat_type: ChatType, chat_id: int, date: str, content: str) -> None:
        self._conn.execute(
            """
            INSERT INTO summaries (chat_type, chat_id, date, content)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (chat_type, chat_id, date)
            DO UPDATE SET content = excluded.content, updated_at = datetime('now')
            """,
            (chat_type.value, chat_id, date, content),
        )
        self._conn.commit()

    def get_summary(self, chat_type: ChatType, chat_id: int, date: str) -> str | None:
        row = self._conn.execute(
            "SELECT content FROM summaries WHERE chat_type = ? AND chat_id = ? AND date = ?",
            (chat_type.value, chat_id, date),
        ).fetchone()
        return None if row is None else str(row["content"])

    # ---- usage ----------------------------------------------------
    def record_usage(
        self,
        chat_type: ChatType,
        chat_id: int,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO usage (chat_type, chat_id, date, purpose, prompt_tokens, completion_tokens)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_type.value, chat_id, self.today(), purpose, prompt_tokens, completion_tokens),
        )
        self._conn.commit()

    def usage_today(self, chat_type: ChatType, chat_id: int, today: str) -> tuple[int, int]:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
            FROM usage
            WHERE chat_type = ? AND chat_id = ? AND date = ?
            """,
            (chat_type.value, chat_id, today),
        ).fetchone()
        return int(row["calls"]), int(row["tokens"])

    # ---- forgetting -----------------------------------------------
    def forget_chat(self, chat_type: ChatType, chat_id: int) -> None:
        for table in ("messages", "impressions", "summaries", "usage"):
            self._conn.execute(
                f"DELETE FROM {table} WHERE chat_type = ? AND chat_id = ?",
                (chat_type.value, chat_id),
            )
        self._conn.commit()

    def forget_all(self) -> None:
        for table in ("messages", "impressions", "summaries", "usage"):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()
