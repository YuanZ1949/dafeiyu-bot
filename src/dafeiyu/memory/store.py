from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dafeiyu.models import ChatType, Impression, Message, Summary
from dafeiyu.timeutil import today_shanghai

# dashboard 只读面板单页上限，接口层同样按这个值校验。
MAX_MEMORY_PAGE = 200

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
    -- 点名语义要跨进程存活：批次靠它决定 purpose=reply 与 prompt 标记。
    at_bot INTEGER NOT NULL DEFAULT 0,
    reply_to_bot INTEGER NOT NULL DEFAULT 0,
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
    estimated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_usage_chat_date ON usage (chat_type, chat_id, date);

-- 记忆旁路的失败冷却与配额熔断；存到库里才能跨进程重启存活。
CREATE TABLE IF NOT EXISTS memory_backoff (
    key TEXT PRIMARY KEY,
    until REAL NOT NULL
);

-- 429 事件的墙钟时间（JSON 数组）：跨重启保留，窗口外的旧事件淘汰（只存一行）。
CREATE TABLE IF NOT EXISTS memory_rate_limit (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    events TEXT NOT NULL DEFAULT '[]',
    updated REAL NOT NULL DEFAULT 0
);

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
    created_at TEXT,
    delivered_count INTEGER NOT NULL DEFAULT 0,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, chat_type, chat_id)
);

-- 轮换时产出的一次性接续摘要（与 summaries 的按日摘要分开存，避免互相覆盖）。
CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_session_summaries_chat
    ON session_summaries (chat_type, chat_id, id);

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
    skipped_reason TEXT,
    PRIMARY KEY (chat_type, chat_id, message_id)
);
"""

# 老库升级：SQLite 的 ALTER TABLE ADD COLUMN 必须带默认值（NOT NULL 也要给）。
_MIGRATIONS: dict[str, dict[str, str]] = {
    "messages": {
        "at_bot": "INTEGER NOT NULL DEFAULT 0",
        "reply_to_bot": "INTEGER NOT NULL DEFAULT 0",
    },
    "brain_sessions": {
        "created_at": "TEXT",
        "delivered_count": "INTEGER NOT NULL DEFAULT 0",
        "token_estimate": "INTEGER NOT NULL DEFAULT 0",
    },
    "delivered_messages": {
        "skipped_reason": "TEXT",
    },
    "usage": {
        "estimated": "INTEGER NOT NULL DEFAULT 0",
    },
    # 早期的 429 计数表用 streak 列；现在改存事件时间数组，老库补列即可。
    "memory_rate_limit": {
        "events": "TEXT NOT NULL DEFAULT '[]'",
    },
}

# brain_sessions 目前只有 opencode 这条路在用（token/local_model 没有 session 概念），
# 所以会话统计接口不把 provider 暴露给调用方。
_SESSION_PROVIDER = "opencode"

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


@dataclass(frozen=True)
class SessionStats:
    """当前 session 已经吃掉多少：投递条数、token 估算、建立时间（ISO 日期）。"""

    delivered_count: int = 0
    token_estimate: int = 0
    created_at: str | None = None


def _memory_scope(
    chat_type: ChatType | None, chat_id: int | None
) -> tuple[str, list[object]]:
    """只读面板的会话过滤条件；两个参数都可为空。"""
    clauses: list[str] = []
    params: list[object] = []
    if chat_type is not None:
        clauses.append("chat_type = ?")
        params.append(chat_type.value)
    if chat_id is not None:
        clauses.append("chat_id = ?")
        params.append(int(chat_id))
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _page_bounds(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(MAX_MEMORY_PAGE, int(limit))), max(0, int(offset))


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
        self._ensure_columns()
        try:
            self._conn.executescript(_FTS_SCHEMA)
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
        self._conn.commit()

    def _ensure_columns(self) -> None:
        """给老库补列：CREATE TABLE IF NOT EXISTS 不会改已存在的表。"""
        for table, columns in _MIGRATIONS.items():
            info = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not info:
                continue
            existing = {row["name"] for row in info}
            for name, decl in columns.items():
                if name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}"
                    )

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
                (chat_type, chat_id, message_id, user_id, nickname, content, is_bot,
                 timestamp, at_bot, reply_to_bot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                int(m.at_bot),
                int(m.reply_to_bot),
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

    def pending_batch(
        self,
        chat_type: ChatType,
        chat_id: int,
        anchor_id: str,
        limit: int = 20,
    ) -> list[Message]:
        """批次投递要看的消息：游标之后、未投递、id 升序，最多 limit 条。

        首次投递（还没有投递下限）时只返回 anchor 这一条，不回填历史 —
        与 pending_messages 的语义保持一致，但这里是 id 正序且不带「被省略条数」。
        """
        anchor = self._row_id(chat_type, chat_id, anchor_id)
        if anchor is None:
            return []

        floor = self.last_delivered_id(chat_type, chat_id)
        if floor is None:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?", (anchor,)
            ).fetchone()
            return [] if row is None else [self._row_to_message(row)]

        rows = self._conn.execute(
            """
            SELECT m.* FROM messages m
            WHERE m.chat_type = ? AND m.chat_id = ? AND m.is_bot = 0
              AND m.id > ? AND m.id <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM delivered_messages d
                  WHERE d.chat_type = m.chat_type AND d.chat_id = m.chat_id
                    AND d.message_id = m.message_id
              )
            ORDER BY m.id ASC LIMIT ?
            """,
            (chat_type.value, chat_id, floor, anchor, limit),
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def chats_with_pending(self, since_ts: int) -> list[tuple[ChatType, int]]:
        """哪些会话还有「游标之后、窗口内、未投递」的人类消息（无游标的会话不算）。"""
        rows = self._conn.execute(
            """
            SELECT DISTINCT m.chat_type, m.chat_id
            FROM messages m
            JOIN delivered_cursors c
              ON c.chat_type = m.chat_type AND c.chat_id = m.chat_id
            WHERE m.is_bot = 0 AND m.id > c.last_message_id AND m.timestamp >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM delivered_messages d
                  WHERE d.chat_type = m.chat_type AND d.chat_id = m.chat_id
                    AND d.message_id = m.message_id
              )
            ORDER BY m.chat_type, m.chat_id
            """,
            (since_ts,),
        ).fetchall()
        return [(ChatType(r["chat_type"]), int(r["chat_id"])) for r in rows]

    def mark_skipped(
        self, chat_type: ChatType, chat_id: int, message_ids: Sequence[str], reason: str
    ) -> None:
        """把一批消息标成「已处理但不投递」，并记下原因（quiet/budget/error…）。

        仍然推进投递游标：跳过是终局，不是待办，否则次日会把积压再灌一遍。
        """
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
        self._conn.executemany(
            """
            UPDATE delivered_messages SET skipped_reason = ?
            WHERE chat_type = ? AND chat_id = ? AND message_id = ?
            """,
            [(reason, chat_type.value, chat_id, mid) for mid, _ in present],
        )
        self._conn.execute(
            """
            INSERT INTO delivered_cursors (chat_type, chat_id, last_message_id)
            VALUES (?, ?, ?)
            ON CONFLICT (chat_type, chat_id) DO NOTHING
            """,
            (chat_type.value, chat_id, max(row for _, row in present)),
        )
        self._conn.commit()

    def skip_reason(self, chat_type: ChatType, chat_id: int, message_id: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT skipped_reason FROM delivered_messages
            WHERE chat_type = ? AND chat_id = ? AND message_id = ?
            """,
            (chat_type.value, chat_id, message_id),
        ).fetchone()
        return None if row is None else row["skipped_reason"]

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
            at_bot=bool(r["at_bot"]),
            reply_to_bot=bool(r["reply_to_bot"]),
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

    # ---- session stats (轮换判据) ----------------------------------
    def session_stats(self, chat_type: ChatType, chat_id: int) -> SessionStats:
        row = self._conn.execute(
            """
            SELECT delivered_count, token_estimate, created_at FROM brain_sessions
            WHERE provider = ? AND chat_type = ? AND chat_id = ?
            """,
            (_SESSION_PROVIDER, chat_type.value, chat_id),
        ).fetchone()
        if row is None:
            return SessionStats()
        return SessionStats(
            delivered_count=int(row["delivered_count"]),
            token_estimate=int(row["token_estimate"]),
            created_at=None if row["created_at"] is None else str(row["created_at"]),
        )

    def bump_session_stats(
        self, chat_type: ChatType, chat_id: int, *, messages: int, tokens: int
    ) -> None:
        """累计本轮投递：无行则建（created_at 只写这一次），有行则累加。"""
        self._conn.execute(
            """
            INSERT INTO brain_sessions
                (provider, chat_type, chat_id, session_id, created_at,
                 delivered_count, token_estimate)
            VALUES (?, ?, ?, '', ?, ?, ?)
            ON CONFLICT (provider, chat_type, chat_id) DO UPDATE SET
                delivered_count = brain_sessions.delivered_count + excluded.delivered_count,
                token_estimate = brain_sessions.token_estimate + excluded.token_estimate
            """,
            (
                _SESSION_PROVIDER,
                chat_type.value,
                chat_id,
                today_shanghai(),
                messages,
                tokens,
            ),
        )
        self._conn.commit()

    def reset_session_stats(self, chat_type: ChatType, chat_id: int) -> None:
        """轮换后清零；created_at 跟着新 session 重算。"""
        self._conn.execute(
            """
            UPDATE brain_sessions
            SET delivered_count = 0, token_estimate = 0, created_at = ?
            WHERE provider = ? AND chat_type = ? AND chat_id = ?
            """,
            (today_shanghai(), _SESSION_PROVIDER, chat_type.value, chat_id),
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

    def delete_impression(self, impression_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM impressions WHERE id = ?", (int(impression_id),)
        )
        self._conn.commit()
        return cur.rowcount > 0

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

    def recent_impressions(
        self,
        limit: int = 20,
        offset: int = 0,
        chat_type: ChatType | None = None,
        chat_id: int | None = None,
    ) -> list[Impression]:
        """跨会话的最近印象，仅供 dashboard 只读展示；可按会话过滤与分页。"""
        scope, params = _memory_scope(chat_type, chat_id)
        page, skip = _page_bounds(limit, offset)
        rows = self._conn.execute(
            f"""
            SELECT * FROM impressions
            {scope}
            ORDER BY updated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page, skip),
        ).fetchall()
        return [self._row_to_impression(r) for r in rows]

    def count_impressions(
        self, chat_type: ChatType | None = None, chat_id: int | None = None
    ) -> int:
        scope, params = _memory_scope(chat_type, chat_id)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM impressions {scope}", params
        ).fetchone()
        return int(row["total"] or 0)

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
            id=r["id"],
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

    def count_summaries(
        self, chat_type: ChatType | None = None, chat_id: int | None = None
    ) -> int:
        scope, params = _memory_scope(chat_type, chat_id)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM summaries {scope}", params
        ).fetchone()
        return int(row["total"] or 0)

    def recent_summaries(
        self,
        limit: int = 20,
        offset: int = 0,
        chat_type: ChatType | None = None,
        chat_id: int | None = None,
    ) -> list[Summary]:
        """跨会话的最近按日摘要，仅供 dashboard 只读展示；可按会话过滤与分页。"""
        scope, params = _memory_scope(chat_type, chat_id)
        page, skip = _page_bounds(limit, offset)
        rows = self._conn.execute(
            f"""
            SELECT chat_type, chat_id, date, content, updated_at FROM summaries
            {scope}
            ORDER BY date DESC, updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page, skip),
        ).fetchall()
        return [
            Summary(
                chat_type=ChatType(r["chat_type"]),
                chat_id=r["chat_id"],
                date=r["date"],
                content=r["content"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # ---- 轮换摘要（一次性接续，与按日 summaries 分开） --------------
    def add_session_summary(self, chat_type: ChatType, chat_id: int, content: str) -> None:
        self._conn.execute(
            """
            INSERT INTO session_summaries (chat_type, chat_id, content)
            VALUES (?, ?, ?)
            """,
            (chat_type.value, chat_id, content),
        )
        self._conn.commit()

    def latest_session_summary(self, chat_type: ChatType, chat_id: int) -> str | None:
        row = self._conn.execute(
            """
            SELECT content FROM session_summaries
            WHERE chat_type = ? AND chat_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (chat_type.value, chat_id),
        ).fetchone()
        return None if row is None else str(row["content"])

    # ---- worker backoff -------------------------------------------
    def load_backoff(self) -> dict[str, float]:
        rows = self._conn.execute(
            "SELECT key, until FROM memory_backoff"
        ).fetchall()
        return {str(row["key"]): float(row["until"]) for row in rows}

    def save_backoff(self, key: str, until: float) -> None:
        self._conn.execute(
            """
            INSERT INTO memory_backoff (key, until) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET until = excluded.until
            """,
            (key, float(until)),
        )
        self._conn.commit()

    def clear_backoff(self, key: str) -> None:
        self._conn.execute("DELETE FROM memory_backoff WHERE key = ?", (key,))
        self._conn.commit()

    def load_rate_limits(self) -> list[float]:
        """429 事件的墙钟时间戳（窗口裁剪由调用方做，库里只负责存）。"""
        row = self._conn.execute(
            "SELECT events FROM memory_rate_limit WHERE id = 1"
        ).fetchone()
        if row is None:
            return []
        try:
            raw = json.loads(str(row["events"]))
        except ValueError:
            return []
        if not isinstance(raw, list):
            return []
        return [float(item) for item in raw if isinstance(item, (int, float))]

    def save_rate_limits(self, events: Sequence[float], updated: float) -> None:
        payload = json.dumps([round(float(item), 3) for item in events])
        self._conn.execute(
            """
            INSERT INTO memory_rate_limit (id, events, updated) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                events = excluded.events, updated = excluded.updated
            """,
            (payload, float(updated)),
        )
        self._conn.commit()

    def clear_rate_limit(self) -> None:
        self._conn.execute("DELETE FROM memory_rate_limit WHERE id = 1")
        self._conn.commit()

    # ---- usage ----------------------------------------------------
    def record_usage(
        self,
        chat_type: ChatType,
        chat_id: int,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
        estimated: bool = False,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO usage
                (chat_type, chat_id, date, purpose, prompt_tokens, completion_tokens, estimated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_type.value,
                chat_id,
                self.today(),
                purpose,
                prompt_tokens,
                completion_tokens,
                1 if estimated else 0,
            ),
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

    def usage_today_by_purpose(
        self,
        chat_type: ChatType,
        chat_id: int,
        today: str,
        purposes: Sequence[str],
    ) -> tuple[int, int]:
        """按用途过滤的当日用量（批评测/摘要不该占用对话预算）。"""
        names = list(purposes)
        if not names:
            return (0, 0)
        placeholders = ", ".join("?" for _ in names)
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
            FROM usage
            WHERE chat_type = ? AND chat_id = ? AND date = ? AND purpose IN ({placeholders})
            """,
            (chat_type.value, chat_id, today, *names),
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
            "session_summaries",
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
            "session_summaries",
        ):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()
