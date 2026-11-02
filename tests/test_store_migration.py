from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dafeiyu.config import ConfigError
from dafeiyu.memory.store import (
    _IMPRESSION_FTS_SCHEMA,
    _MESSAGE_FTS_SCHEMA,
    Store,
)
from dafeiyu.models import ChatType, Message, channel_of


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

# v1 夹具 DDL：与 task 5 改造前的 _SCHEMA 逐字一致（INTEGER chat_id/user_id 布局），
# 含全部 12 张会被 v2 重建的表与其索引（memory_backoff/memory_rate_limit 无整型
# id、不参与重建，留给 init_schema 建）。这是「真 schema-1 库」的快照，故故意
# 不随 _SCHEMA 演化；task 5 负责在重建落地的同一把提交里更新夹具，让旧版本列
# 布局一直有真实代表。
# FTS5 部分直接复用模块常量：v1 → v2 只把正文表 id 列 TEXT 化，两个外部内容
# FTS 表与其触发器的 DDL（含各自 tokenizer）有意原样保留。
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
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_type, chat_id, id);
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

CREATE TABLE IF NOT EXISTS impression_cursors (
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    last_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id)
);

CREATE TABLE IF NOT EXISTS chat_state (
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    last_bot_spoke REAL,
    last_user_message REAL,
    muted_until REAL,
    ambient_streak INTEGER NOT NULL DEFAULT 0,
    no_reply_streak INTEGER NOT NULL DEFAULT 0,
    downshift_until REAL,
    last_prompt TEXT NOT NULL DEFAULT '',
    last_prompt_at REAL,
    thread_closed_until REAL,
    last_closed_at REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id)
);

CREATE TABLE IF NOT EXISTS summaries (
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id, date)
);

CREATE TABLE IF NOT EXISTS weekly_summaries (
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id, week_start)
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
CREATE INDEX IF NOT EXISTS idx_usage_date ON usage (date);

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

CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_session_summaries_chat
    ON session_summaries (chat_type, chat_id, id);

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


def make_v1_db(path: Path) -> None:
    """直接拼出 schema-1 库（绕开 init_schema，不然测试对象自己盖了自己的前身）。

    含 FTS5：v1 运行库里两个外部内容 FTS 表与 6 个触发器本来就存在，迁移要
    靠 sqlite_master 逐字抄录的正是它们。
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_SCHEMA)
        conn.executescript(_IMPRESSION_FTS_SCHEMA)
        conn.executescript(_MESSAGE_FTS_SCHEMA)
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.execute(
            "INSERT INTO impressions_fts(impressions_fts) VALUES('rebuild')"
        )
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


def test_rebuild_fts_after_v2_migration(tmp_path: Path) -> None:
    """v1 → v2 重建：数据一条不丢，FTS/trigram/触发器/哨兵清零全部就位。"""
    db = tmp_path / "memory.db"
    make_v1_db(db)

    conn = sqlite3.connect(db)
    try:
        for i in range(20):
            conn.execute(
                """
                INSERT INTO messages
                    (chat_type, chat_id, message_id, user_id, nickname,
                     content, timestamp)
                VALUES ('group', 100, ?, '7', '测试员', ?, ?)
                """,
                (
                    f"m{i}",
                    f"飞鱼提醒{i}：天气预报说明天多云出门记得带伞。",
                    i,
                ),
            )
            conn.execute(
                """
                INSERT INTO impressions
                    (chat_type, chat_id, subject_user_id, kind, content)
                VALUES ('group', 100, 0, 'fact', ?)
                """,
                (f"历史印象第{i}条，无主语",),
            )
        conn.execute(
            """
            INSERT INTO impressions
                (chat_type, chat_id, subject_user_id, kind, content)
            VALUES ('group', 100, 42, 'preference', '喜欢聊天气')
            """
        )
        # 迁移前先回填 v1 索引，保证「迁移后 FTS 命中」测到 rebuild 环节本身。
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.execute(
            "INSERT INTO impressions_fts(impressions_fts) VALUES('rebuild')"
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM messages_fts"
        ).fetchone()[0] == 20
    finally:
        conn.close()

    store = Store(db)
    try:
        store.init_schema()
        # 迁移后新写入仍要被触发器同步进 FTS（触发器体没有被 RENAME 改歪）。
        store.add_message(
            Message(
                chat_type=ChatType.GROUP,
                chat_id="qq:100",
                user_id="qq:7",
                message_id="mafter",
                nickname="测试员",
                content="迁移后新增的天气预报一条。",
                timestamp=99,
            )
        )
        total, hits = store.search_messages("天气预报")
        assert total == 21
        assert all("天气预报" in hit.content for hit in hits)
    finally:
        store.close()

    conn = sqlite3.connect(db)
    try:
        # 数据一条不丢。
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 21
        assert conn.execute(
            "SELECT COUNT(*) FROM impressions"
        ).fetchone()[0] == 21
        # 重建后的值是字符串且带 'qq:' 前缀（TEXT 亲和性 + CASE 补前缀）。
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = 'qq:100'"
        ).fetchone()[0] == 21
        # 恰好 3 个 messages 触发器，6 个全量。
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            " AND name LIKE 'messages_%'"
        ).fetchall()
        assert sorted(r[0] for r in triggers) == [
            "messages_ad", "messages_ai", "messages_au",
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master"
            " WHERE type = 'trigger' AND name LIKE 'impressions_%'"
        ).fetchone()[0] == 3
        # 两个 tokenizer 原样保留（不许统一成同一个）。
        m_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'messages_fts'"
        ).fetchone()[0]
        i_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'impressions_fts'"
        ).fetchone()[0]
        assert "tokenize='trigram'" in m_sql
        assert "content='messages'" in m_sql and "content_rowid='id'" in m_sql
        assert "tokenize='unicode61'" in i_sql
        assert "content='impressions'" in i_sql
        # 空值哨兵清零：v1 的 0 → '0' 被改成 ''，真实 subject 原样保留（带前缀）。
        assert conn.execute(
            "SELECT COUNT(*) FROM impressions WHERE subject_user_id = '0'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM impressions WHERE subject_user_id = ''"
        ).fetchone()[0] == 20
        assert conn.execute(
            "SELECT COUNT(*) FROM impressions WHERE subject_user_id = 'qq:42'"
        ).fetchone()[0] == 1
        # 完整性。
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # id 列类型抽查（TEXT 声明）。
        for table, column in (
            ("messages", "chat_id"),
            ("messages", "user_id"),
            ("messages", "reply_to_user_id"),
            ("impressions", "subject_user_id"),
            ("impression_cursors", "chat_id"),
            ("summaries", "chat_id"),
            ("delivered_cursors", "chat_id"),
        ):
            row = conn.execute(
                "SELECT type FROM pragma_table_info(?) WHERE name = ?",
                (table, column),
            ).fetchone()
            assert row is not None and row[0].lower() == "text"
        assert db_version(conn) == 2
    finally:
        conn.close()


def test_migrated_db_after_drop_of_messages_is_not_malformed(tmp_path: Path) -> None:
    """重建顺序保险：正文表被删掉的残缺库迁移后，检索不许报 malformed。

    messages 是外部内容 FTS5 的内容表。若迁移先动了 FTS（或重建后内容表
    缺失），search_messages 会直接抛「database disk image is malformed」。
    """
    db = tmp_path / "memory.db"
    make_v1_db(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE messages")
        conn.commit()
    finally:
        conn.close()

    store = Store(db)
    try:
        store.init_schema()
        total, hits = store.search_messages("天气预报")
        assert total == 0
        assert hits == []
    finally:
        store.close()


def test_fresh_and_migrated_layout_identical(tmp_path: Path) -> None:
    """同构保证：空库直建 v2 与 v1 迁移出 v2 的结构必须一致（防 _SCHEMA 漂移）。"""
    fresh_db = tmp_path / "fresh.db"
    migrated_db = tmp_path / "migrated.db"

    store = Store(fresh_db)
    try:
        store.init_schema()
    finally:
        store.close()
    make_v1_db(migrated_db)
    store = Store(migrated_db)
    try:
        store.init_schema()
    finally:
        store.close()

    def layout(path: Path) -> tuple[dict[str, list[tuple]], list[tuple]]:
        conn = sqlite3.connect(path)
        try:
            tables = {
                table: [
                    tuple(row)
                    for row in conn.execute(
                        """
                        SELECT name, type, "notnull", dflt_value, pk
                        FROM pragma_table_info(?)
                        """,
                        (table,),
                    ).fetchall()
                ]
                for table in Store._V2_REBUILD_TABLES
            }
            master = [
                (row[0], row[1], row[2])
                for row in conn.execute(
                    "SELECT type, name, tbl_name FROM sqlite_master"
                    " WHERE name <> 'migration_marker'"
                    " AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            return tables, sorted(master)
        finally:
            conn.close()

    fresh_tables, fresh_master = layout(fresh_db)
    migrated_tables, migrated_master = layout(migrated_db)
    assert fresh_tables == migrated_tables
    assert fresh_master == migrated_master

    # 触发器体逐字一致（RENAME 不许把触发器改写到临时表名）。
    def trigger_sql(path: Path) -> list[tuple[str, str]]:
        conn = sqlite3.connect(path)
        try:
            return sorted(
                (row[0], row[1])
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            )
        finally:
            conn.close()

    assert trigger_sql(fresh_db) == trigger_sql(migrated_db)


def test_migration_prefixes_legacy_qq_ids(tmp_path: Path) -> None:
    """v1 裸整数 id 迁移后必须带 'qq:' 前缀：老 QQ 记忆不许失联。

    只覆盖 TEXT 化（'100'）而不补前缀的话，recent_messages('qq:100') 返回
    空、channel_of('100') 直接抛错。这里把 channel-scoped id 列全查一遍，
    并验证 NULL/哨兵不受影响、FTS 命中数不变、重复初始化不二次加前缀。
    """
    db = tmp_path / "memory.db"
    make_v1_db(db)

    # 本测试局部扩种 v1 数据：含 reply_to_user_id 的 NULL 与 7 两例、
    # impressions 的哨兵 0 与真实 42、以及四张带 chat_id 的状态表。
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            INSERT INTO messages
                (chat_type, chat_id, message_id, user_id, nickname,
                 content, timestamp)
            VALUES ('group', 100, 'm-none', 7, '测试员', '天气预报多云带伞。', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO messages
                (chat_type, chat_id, message_id, user_id, nickname,
                 content, timestamp, reply_to_user_id)
            VALUES ('group', 100, 'm-reply', 7, '测试员', '回复也提天气预报', 2, 7)
            """
        )
        conn.execute(
            """
            INSERT INTO impressions
                (chat_type, chat_id, subject_user_id, kind, content)
            VALUES ('group', 100, 0, 'fact', '无主语印象')
            """
        )
        conn.execute(
            """
            INSERT INTO impressions
                (chat_type, chat_id, subject_user_id, kind, content)
            VALUES ('group', 100, 42, 'preference', '喜欢聊天气')
            """
        )
        conn.execute(
            "INSERT INTO chat_state (chat_type, chat_id, ambient_streak)"
            " VALUES ('group', 100, 1)"
        )
        conn.execute(
            "INSERT INTO summaries (chat_type, chat_id, date, content)"
            " VALUES ('group', 100, '2026-01-01', '旧日程')"
        )
        conn.execute(
            "INSERT INTO usage (chat_type, chat_id, date, purpose)"
            " VALUES ('group', 100, '2026-01-01', 'chat')"
        )
        conn.execute(
            "INSERT INTO delivered_cursors (chat_type, chat_id, last_message_id)"
            " VALUES ('group', 100, 3)"
        )
        # 迁移前回填 v1 FTS 索引，记录内容命中水平：迁移必须只动 id，不动检索。
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 2
    finally:
        conn.close()

    store = Store(db)
    try:
        store.init_schema()

        # 老消息按 'qq:100' 重新可见，派生通道是 qq。
        msgs = store.recent_messages(ChatType.GROUP, "qq:100", 10)
        assert [m.message_id for m in msgs] == ["m-none", "m-reply"]
        assert channel_of("qq:100") == "qq"

        # FTS 内容检索照常：迁移前后命中数一致，且可按新 id 过滤。
        total, hits = store.search_messages("天气预报", ChatType.GROUP, "qq:100")
        assert total == 2
        assert all("天气预报" in hit.content for hit in hits)

        # 哨兵与可空列不受前缀污染。
        conn2 = store._conn
        assert conn2.execute(
            "SELECT COUNT(*) FROM messages WHERE reply_to_user_id IS NULL"
        ).fetchone()[0] == 1
        assert conn2.execute(
            "SELECT COUNT(*) FROM messages WHERE reply_to_user_id = 'qq:7'"
        ).fetchone()[0] == 1
        assert conn2.execute(
            "SELECT COUNT(*) FROM impressions WHERE subject_user_id = ''"
        ).fetchone()[0] == 1
        assert conn2.execute(
            "SELECT COUNT(*) FROM impressions WHERE subject_user_id = 'qq:42'"
        ).fetchone()[0] == 1

        def assert_all_ids_prefixed() -> None:
            """全表全列：无裸 id、无二次前缀；非 id 列不许被加前缀。"""
            for table, cols in Store._V2_QQ_PREFIX_COLUMNS.items():
                for col in cols:
                    row = conn2.execute(
                        f"SELECT COUNT(*) FROM {table}"
                        f" WHERE ({col} LIKE 'qq:qq:%' OR"
                        f"  ({col} IS NOT NULL AND {col} <> ''"
                        f"   AND {col} NOT LIKE 'qq:%'))"
                    ).fetchone()
                    assert row[0] == 0, f"{table}.{col} 前缀不对"
            assert conn2.execute(
                "SELECT COUNT(*) FROM messages WHERE message_id LIKE 'qq:%'"
            ).fetchone()[0] == 0

        assert_all_ids_prefixed()

        # 状态表的 chat_id 落点抽查：旧行全部归到 'qq:100' 名下。
        for table in ("chat_state", "summaries", "usage", "delivered_cursors"):
            row = conn2.execute(
                f"SELECT COUNT(*) FROM {table} WHERE chat_id = 'qq:100'"
            ).fetchone()
            assert row[0] == 1, table

        # 重复 init_schema 不许二次加前缀（'qq:qq:100' 不得出现）。
        store.init_schema()
        assert conn2.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = 'qq:100'"
        ).fetchone()[0] == 2
        assert_all_ids_prefixed()
        assert db_version(conn2) == 2
    finally:
        store.close()
