from __future__ import annotations

import sqlite3
from pathlib import Path

from dafeiyu.models import ChatType, Impression, Message
from dafeiyu.timeutil import today_shanghai

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

CREATE TABLE IF NOT EXISTS opencode_sessions (
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id)
);

CREATE TABLE IF NOT EXISTS brain_sessions (
    provider TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, chat_type, chat_id)
);

-- 会话型大脑（OpenCode）的投递记录：
--   * delivered_cursors 是该群的「投递下限」，id 小于等于它的消息视为已归档，不再回溯；
--   * delivered_messages 逐条记录模型真正看过的消息，被冷却/抽样跳过的消息不在其中，
--     下一次投递时自动补上（单靠单调游标会跨过空洞，漏掉更早的缺口）。
CREATE TABLE IF NOT EXISTS delivered_cursors (
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id)
);

CREATE TABLE IF NOT EXISTS delivered_messages (
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id, message_id)
);
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
        return today_shanghai()

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

    def recent_messages(
        self,
        chat_type: ChatType,
        chat_id: int,
        limit: int,
        upto_message_id: str | None = None,
    ) -> list[Message]:
        """取最近 limit 条消息；传入 upto_message_id 时只取该消息（含）之前的。"""
        if upto_message_id is None:
            rows = self._conn.execute(
                """
                SELECT * FROM messages
                WHERE chat_type = ? AND chat_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (chat_type.value, chat_id, limit),
            ).fetchall()
            return [self._row_to_message(r) for r in reversed(rows)]

        anchor = self._conn.execute(
            """
            SELECT id FROM messages
            WHERE chat_type = ? AND chat_id = ? AND message_id = ?
            """,
            (chat_type.value, chat_id, upto_message_id),
        ).fetchone()
        if anchor is None:
            return self.recent_messages(chat_type, chat_id, limit)

        rows = self._conn.execute(
            """
            SELECT * FROM messages
            WHERE chat_type = ? AND chat_id = ? AND id <= ?
            ORDER BY id DESC LIMIT ?
            """,
            (chat_type.value, chat_id, int(anchor["id"]), limit),
        ).fetchall()
        return [self._row_to_message(r) for r in reversed(rows)]

    def messages_between(
        self, chat_type: ChatType, chat_id: int, start_ts: int, end_ts: int
    ) -> list[Message]:
        rows = self._conn.execute(
            """
            SELECT * FROM messages
            WHERE chat_type = ? AND chat_id = ? AND timestamp >= ? AND timestamp < ?
            ORDER BY id ASC
            """,
            (chat_type.value, chat_id, start_ts, end_ts),
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def active_chats_between(self, start_ts: int, end_ts: int) -> list[tuple[ChatType, int]]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT chat_type, chat_id FROM messages
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start_ts, end_ts),
        ).fetchall()
        return [(ChatType(r["chat_type"]), int(r["chat_id"])) for r in rows]

    def count_messages(self, chat_type: ChatType, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_type = ? AND chat_id = ?",
            (chat_type.value, chat_id),
        ).fetchone()
        return int(row["n"])

    # ---- delivery cursors -----------------------------------------
    def _row_id(self, chat_type: ChatType, chat_id: int, message_id: str) -> int | None:
        row = self._conn.execute(
            """
            SELECT id FROM messages
            WHERE chat_type = ? AND chat_id = ? AND message_id = ?
            """,
            (chat_type.value, chat_id, message_id),
        ).fetchone()
        return None if row is None else int(row["id"])

    def last_delivered_id(self, chat_type: ChatType, chat_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT last_message_id FROM delivered_cursors WHERE chat_type = ? AND chat_id = ?",
            (chat_type.value, chat_id),
        ).fetchone()
        return None if row is None else int(row["last_message_id"])

    def pending_messages(
        self, chat_type: ChatType, chat_id: int, upto_message_id: str, limit: int
    ) -> tuple[int, list[Message]]:
        """取「模型还没看过」的人类消息，按时间正序，最多 limit 条。

        返回 (被省略的条数, 消息列表)。首次投递（还没有投递下限）时只算当前这条，
        不回填历史 —— 老会话里已经有的内容不该再灌一遍。
        逐条投递记录保证：中间被跳过的消息即使后面接话了，也仍然留在待投队列里。
        """
        anchor = self._row_id(chat_type, chat_id, upto_message_id)
        if anchor is None:
            return 0, []

        floor = self.last_delivered_id(chat_type, chat_id)
        if floor is None:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?", (anchor,)
            ).fetchone()
            return 0, ([] if row is None else [self._row_to_message(row)])
        floor = min(floor, anchor)

        where = """
            FROM messages m
            WHERE m.chat_type = ? AND m.chat_id = ? AND m.is_bot = 0
              AND m.id > ? AND m.id <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM delivered_messages d
                  WHERE d.chat_type = m.chat_type AND d.chat_id = m.chat_id
                    AND d.message_id = m.message_id
              )
        """
        params = (chat_type.value, chat_id, floor, anchor)
        total = int(
            self._conn.execute(f"SELECT COUNT(*) AS n {where}", params).fetchone()["n"]
        )
        rows = self._conn.execute(
            f"SELECT m.* {where} ORDER BY m.id DESC LIMIT ?", (*params, limit)
        ).fetchall()
        messages = [self._row_to_message(r) for r in reversed(rows)]
        if not messages:
            # 当前这条已经投递过（重复事件），至少让模型再看到它一次。
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?", (anchor,)
            ).fetchone()
            return 0, ([] if row is None else [self._row_to_message(row)])
        return max(0, total - len(messages)), messages

    def mark_delivered(self, chat_type: ChatType, chat_id: int, *message_ids: str) -> None:
        """把一批消息记为「已送达模型」；首次投递同时钉住回溯下限。"""
        rows = [self._row_id(chat_type, chat_id, mid) for mid in message_ids]
        present = [(mid, row) for mid, row in zip(message_ids, rows) if row is not None]
        if not present:
            return
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO delivered_messages (chat_type, chat_id, message_id)
            VALUES (?, ?, ?)
            """,
            [(chat_type.value, chat_id, mid) for mid, _ in present],
        )
        # 第一次投递时把下限钉在本批最新一条上：更早的历史属于「上线前」。
        self._conn.execute(
            """
            INSERT INTO delivered_cursors (chat_type, chat_id, last_message_id)
            VALUES (?, ?, ?)
            ON CONFLICT (chat_type, chat_id) DO NOTHING
            """,
            (chat_type.value, chat_id, max(row for _, row in present)),
        )
        self._conn.commit()

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

    # ---- OpenCode session mapping ---------------------------------
    def migrate_opencode_sessions(self) -> int:
        """把旧表数据搬进 brain_sessions(provider='opencode')，幂等。"""
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO brain_sessions (provider, chat_type, chat_id, session_id)
            SELECT 'opencode', chat_type, chat_id, session_id FROM opencode_sessions
            """
        )
        self._conn.commit()
        return cur.rowcount

    def get_brain_session(
        self, provider: str, chat_type: ChatType, chat_id: int
    ) -> str | None:
        row = self._conn.execute(
            """
            SELECT session_id FROM brain_sessions
            WHERE provider = ? AND chat_type = ? AND chat_id = ?
            """,
            (provider, chat_type.value, chat_id),
        ).fetchone()
        return None if row is None else str(row["session_id"])

    def set_brain_session(
        self, provider: str, chat_type: ChatType, chat_id: int, session_id: str
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO brain_sessions (provider, chat_type, chat_id, session_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (provider, chat_type, chat_id)
            DO UPDATE SET session_id = excluded.session_id, updated_at = datetime('now')
            """,
            (provider, chat_type.value, chat_id, session_id),
        )
        self._conn.commit()

    def clear_brain_session(
        self, provider: str, chat_type: ChatType, chat_id: int
    ) -> None:
        self._conn.execute(
            """
            DELETE FROM brain_sessions
            WHERE provider = ? AND chat_type = ? AND chat_id = ?
            """,
            (provider, chat_type.value, chat_id),
        )
        self._conn.commit()

    def get_opencode_session(self, chat_type: ChatType, chat_id: int) -> str | None:
        return self.get_brain_session("opencode", chat_type, chat_id)

    def set_opencode_session(
        self, chat_type: ChatType, chat_id: int, session_id: str
    ) -> None:
        self.set_brain_session("opencode", chat_type, chat_id, session_id)

    def clear_opencode_session(self, chat_type: ChatType, chat_id: int) -> None:
        self.clear_brain_session("opencode", chat_type, chat_id)

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
        for table in (
            "messages",
            "impressions",
            "summaries",
            "usage",
            "opencode_sessions",
            "brain_sessions",
            "delivered_cursors",
            "delivered_messages",
        ):
            self._conn.execute(
                f"DELETE FROM {table} WHERE chat_type = ? AND chat_id = ?",
                (chat_type.value, chat_id),
            )
        self._conn.commit()

    def forget_all(self) -> None:
        for table in (
            "messages",
            "impressions",
            "summaries",
            "usage",
            "opencode_sessions",
            "brain_sessions",
            "delivered_cursors",
            "delivered_messages",
        ):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()
