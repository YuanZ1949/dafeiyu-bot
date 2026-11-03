from __future__ import annotations

import calendar
import json
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from dafeiyu.config import ConfigError
from dafeiyu.models import ChatType, Impression, Message, Summary, WeeklySummary
from dafeiyu.timeutil import SHANGHAI, today_shanghai

# dashboard 只读面板单页上限，接口层同样按这个值校验。
MAX_MEMORY_PAGE = 200
# 时间线单页上限；首屏默认小一些，避免一次把长会话全塞给浏览器。
MAX_TIMELINE_PAGE = 200
# 预算只统计对话用途：摘要/批评测/记忆提取不属于对话预算。
_BUDGET_PURPOSES: tuple[str, ...] = ("reply", "ambient")


@dataclass(frozen=True)
class MessagePage:
    """按自增 id 游标分页的消息页；next_before 可直接传给下一次请求。"""

    messages: list[Message]
    has_more: bool
    next_before: int | None
    anchor_found: bool

# v2 布局：chat_id / user_id 系列一律 TEXT（会话 id 带通道前缀，如 qq:123）。
# 老库升级走 _rebuild_v2_tables：v1 的整数值由 TEXT 亲和性隐式转成十进制字符串。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    nickname TEXT NOT NULL,
    content TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    timestamp INTEGER NOT NULL DEFAULT 0,
    -- 点名语义要跨进程存活：批次靠它决定 purpose=reply 与 prompt 标记。
    at_bot INTEGER NOT NULL DEFAULT 0,
    reply_to_bot INTEGER NOT NULL DEFAULT 0,
    -- 回复目标：id 用于回查被回复消息的昵称，uid 是消息被清理后的退路。
    reply_to_message_id TEXT,
    reply_to_user_id TEXT,
    -- 非空 = 已被撤回；原文保留但不再进入 prompt。
    recalled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chat_type, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_type, chat_id, id);
-- 摘要/体检/清理按时间窗扫全表：(会话, 时间) 走覆盖范围，单列时间给跨会话统计。
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts
    ON messages (chat_type, chat_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (timestamp);

CREATE TABLE IF NOT EXISTS impressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    -- v1 是 INTEGER DEFAULT 0；v2 改 TEXT 后空值哨兵是 ''（0/'0' 都算空）。
    subject_user_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    -- 抽取时该用户的最新群昵称，注入时用来标明「这是谁」。
    subject_nickname TEXT NOT NULL DEFAULT '',
    -- 来源只记最近一条同用户消息，删消息后仍能看出这条印象从哪来。
    source_message_id TEXT NOT NULL DEFAULT '',
    source_excerpt TEXT NOT NULL DEFAULT '',
    -- 最近一次被重新观察到的时刻：排序降权用，老内容不删只靠后。
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chat_type, chat_id, subject_user_id, kind, content)
);
CREATE INDEX IF NOT EXISTS idx_impressions_chat ON impressions (chat_type, chat_id);

-- 印象抽取游标：记录该会话已抽到第几条消息。重启后据此续跑，
-- 避免 _last_count 只存在内存导致每个活跃会话立刻白跑一次。
CREATE TABLE IF NOT EXISTS impression_cursors (
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    last_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id)
);

-- 会话级运行状态：静音、话题收尾、ambient 计数。时间存 epoch 秒，
-- 避免重启后时区/夏令时转换把窗口算歪。
CREATE TABLE IF NOT EXISTS chat_state (
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
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
    chat_id TEXT NOT NULL,
    date TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id, date)
);

-- 周汇总单独建表：summaries.date 是「某一天」，用周起始日期混进去会被
-- pending_summary_slots 的按日 LEFT JOIN 误判成那一天的日摘要。
CREATE TABLE IF NOT EXISTS weekly_summaries (
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    week_start TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id, week_start)
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    date TEXT NOT NULL,
    purpose TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    estimated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_usage_chat_date ON usage (chat_type, chat_id, date);
-- 全局/月度预算按日期扫全表，单独建索引（老库 init_schema 时自动补上）。
CREATE INDEX IF NOT EXISTS idx_usage_date ON usage (date);

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
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id)
);

CREATE TABLE IF NOT EXISTS brain_sessions (
    provider TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
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
    chat_id TEXT NOT NULL,
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
    chat_id TEXT NOT NULL,
    last_message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id)
);

CREATE TABLE IF NOT EXISTS delivered_messages (
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT (datetime('now')),
    skipped_reason TEXT,
    PRIMARY KEY (chat_type, chat_id, message_id)
);

-- 待发队列：发送瞬间通道/桌面不可用时，把没发出去的分片留在这里，等恢复后
-- 补发。没有它，「桥崩在发送路上」就等于永久沉默（用户只看到「稍等」）。
CREATE TABLE IF NOT EXISTS pending_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    origin_message_id TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pending_outbox_due
    ON pending_outbox (next_attempt_at, id);
"""

# 老库升级：SQLite 的 ALTER TABLE ADD COLUMN 必须带默认值（NOT NULL 也要给）。
_MIGRATIONS: dict[str, dict[str, str]] = {
    "messages": {
        "at_bot": "INTEGER NOT NULL DEFAULT 0",
        "reply_to_bot": "INTEGER NOT NULL DEFAULT 0",
        "reply_to_message_id": "TEXT",
        "reply_to_user_id": "TEXT",
        "recalled_at": "TEXT",
    },
    "brain_sessions": {
        "created_at": "TEXT",
        "delivered_count": "INTEGER NOT NULL DEFAULT 0",
        "token_estimate": "INTEGER NOT NULL DEFAULT 0",
    },
    "delivered_messages": {
        "skipped_reason": "TEXT",
    },
    "impressions": {
        "source_message_id": "TEXT NOT NULL DEFAULT ''",
        "source_excerpt": "TEXT NOT NULL DEFAULT ''",
        "last_seen_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
        "subject_nickname": "TEXT NOT NULL DEFAULT ''",
    },
    "usage": {
        "estimated": "INTEGER NOT NULL DEFAULT 0",
    },
    # 早期的 429 计数表用 streak 列；现在改存事件时间数组，老库补列即可。
    "memory_rate_limit": {
        "events": "TEXT NOT NULL DEFAULT '[]'",
    },
    # chat_state 是本轮新表；补列项留给跑过中间版本库的本地环境。
    "chat_state": {
        "no_reply_streak": "INTEGER NOT NULL DEFAULT 0",
        "downshift_until": "REAL",
        "last_prompt": "TEXT NOT NULL DEFAULT ''",
        "last_prompt_at": "REAL",
    },
}

# brain_sessions 目前只有 opencode 这条路在用（token/local_model 没有 session 概念），
# 所以会话统计接口不把 provider 暴露给调用方。
_SESSION_PROVIDER = "opencode"

_IMPRESSION_FTS_SCHEMA = """
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

# 消息检索用 trigram：中文 3 字以上可做子串匹配；1-2 字由调用方回落 LIKE。
_MESSAGE_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
END;
"""


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# 印象归一化去重时忽略的常见中英文标点（引号含全角/半角两种写法）。
_IMPRESSION_PUNCT = frozenset("，。！？、；：“”‘’\"'（）()[]【】,.!?;:`")

# 昵称有自己的唯一规则（同用户只留最新一条），不参与归一化合并。
_NORMALIZED_KINDS = frozenset({"preference", "fact"})


def _normalize_impression(text: str) -> str:
    """印象去重键：去掉所有空白与常见中英文标点后转小写。"""
    return "".join(
        ch for ch in text if not ch.isspace() and ch not in _IMPRESSION_PUNCT
    ).lower()


@dataclass(frozen=True)
class UsageTotals:
    """区间内的用量合计；``estimated`` 表示区间内是否含推算值。"""

    calls: int
    prompt_tokens: int
    completion_tokens: int
    estimated: bool = False

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class SessionStats:
    """当前 session 已经吃掉多少：投递条数、token 估算、建立时间（ISO 日期）。"""

    delivered_count: int = 0
    token_estimate: int = 0
    created_at: str | None = None


@dataclass(frozen=True)
class PendingSummary:
    """一个「有消息却没摘要」的槽位：会话 + 日期 + 当天消息数。"""

    chat_type: ChatType
    chat_id: str
    date: str
    messages: int


@dataclass(frozen=True)
class PendingWeekly:
    """一个「有日摘要却没周汇总」的槽位：会话 + 周起始 + 该周日摘要条数。"""

    chat_type: ChatType
    chat_id: str
    week_start: str
    days: int


def _parse_day(value: str) -> datetime:
    """把 YYYY-MM-DD 解析成上海时区的当天零点。"""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=SHANGHAI)


def _memory_scope(
    chat_type: ChatType | None, chat_id: str | None
) -> tuple[str, list[object]]:
    """只读面板的会话过滤条件；两个参数都可为空。"""
    clauses: list[str] = []
    params: list[object] = []
    if chat_type is not None:
        clauses.append("chat_type = ?")
        params.append(chat_type.value)
    if chat_id is not None:
        clauses.append("chat_id = ?")
        params.append(chat_id)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _page_bounds(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(MAX_MEMORY_PAGE, int(limit))), max(0, int(offset))


class Store:
    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._fts = False
        self._messages_fts = False

    def init_schema(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > self.SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库版本 {version} 高于程序支持版本 {self.SCHEMA_VERSION}"
            )
        # 上一次迁移中途崩溃的库：启动即拒绝，绝不能带残留标记继续跑/自愈。
        self._refuse_incomplete_migration()
        if version == 1:
            self._migrate_v2()
            version = self.SCHEMA_VERSION
        self._conn.executescript(_SCHEMA)
        self._ensure_columns()
        self._backfill_session_created_at()
        try:
            self._conn.executescript(_IMPRESSION_FTS_SCHEMA)
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False

        # messages_fts 是独立脚本：老 SQLite 不支持 trigram 时不能把印象 FTS 一起拖死。
        existed = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
        ).fetchone()
        try:
            self._conn.executescript(_MESSAGE_FTS_SCHEMA)
            self._messages_fts = True
            if not existed:
                self._conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            else:
                indexed = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM messages_fts"
                ).fetchone()
                total = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM messages"
                ).fetchone()
                if int(indexed["n"] or 0) == 0 and int(total["n"] or 0) > 0:
                    # 上一次迁移中途失败留下的空索引，补一次回填。
                    self._conn.execute(
                        "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')"
                    )
        except sqlite3.OperationalError:
            self._messages_fts = False
        if version < self.SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        self._conn.commit()

    def _migrate_v2(self) -> None:
        """v1 → v2 迁移：备份不成功不动库、标记先落库、重建走单事务、版本号最后写。

        顺序本身就是崩溃安全的下限：

        1. 备份失败 → 直接 raise 中止（没有备份就没有回退手段，绝不半迁移）；
        2. 标记行先于事务单独提交，之后任何一步崩溃，重开库都能凭标记残留
           拒绝启动（处理手段只有一个：用备份恢复，不是修复半个库）；
        3. 重建 + 删标记在同一个 ``BEGIN IMMEDIATE`` 事务里，要么整体提交、
           要么整体回滚；
        4. ``user_version = 2`` 写在 COMMIT 之后 —— 任何时刻读到的库要么是
           完整 v1、要么是完整 v2。

        全程用 ``execute`` 显式控制事务，**不得**改用 ``executescript()``：
        它会在执行脚本前隐式提交，把这里的事务边界打散。
        """
        conn = self._conn
        # 隐式事务陷阱：进入迁移前可能已有未提交的写挂在 legacy sqlite3 的隐式
        # 事务里（init_schema 之前路径的 UPDATE/ALTER，如 _backfill_session_created_at）。
        # Python 3.12 legacy 模式下直接 BEGIN IMMEDIATE 会抛
        # "OperationalError: cannot start a transaction within a transaction"，
        # 必须先 commit 清掉隐式事务。
        conn.commit()
        db = Path(self.path)
        backup = db.with_name(
            f"{db.name}.pre-wechat-"
            f"{datetime.now(SHANGHAI).strftime('%Y%m%dT%H%M%S')}"
        )
        self.backup_to(backup)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS migration_marker"
            " (step TEXT PRIMARY KEY, started_at TEXT)"
        )
        conn.execute(
            "INSERT INTO migration_marker VALUES ('v2', ?)",
            (datetime.now(SHANGHAI).isoformat(timespec="seconds"),),
        )
        # 标记必须独立提交：重建事务回滚不能把「库是半个」的证据一并抹掉。
        conn.commit()
        # WAL 未 checkpoint 就重建表，老数据仍留在 WAL 里；重建前先收敛。
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._rebuild_v2_tables()
            conn.execute("DELETE FROM migration_marker WHERE step = 'v2'")
            conn.execute("COMMIT")
        except BaseException:
            conn.rollback()
            raise
        conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def _refuse_incomplete_migration(self) -> None:
        """崩溃恢复：migration_marker 有残留行 → 拒绝启动，指定备份文件。"""
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table'"
            " AND name = 'migration_marker'"
        ).fetchone()
        if exists is None:
            return
        row = self._conn.execute("SELECT 1 FROM migration_marker").fetchone()
        if row is None:
            return
        db = Path(self.path)
        # 标记在备份之后、事务之前落库，所以名称最新的备份就是本次迁移的快照。
        backups = sorted(db.parent.glob(f"{db.name}.pre-wechat-*"))
        point = str(backups[-1]) if backups else "<备份路径>"
        raise ConfigError(f"迁移未完成，请用 {point} 恢复")

    # v2 重建的 12 张表（顺序即执行顺序）：chat_id / user_id 系列全部 TEXT 化。
    # messages/impressions 先重建 —— 两块外部内容 FTS 表靠它们才有可指的内容表。
    _V2_REBUILD_TABLES: tuple[str, ...] = (
        "messages",
        "impressions",
        "impression_cursors",
        "chat_state",
        "summaries",
        "weekly_summaries",
        "usage",
        "opencode_sessions",
        "brain_sessions",
        "session_summaries",
        "delivered_cursors",
        "delivered_messages",
    )

    # v2 迁移要补 'qq:' 通道前缀的 channel-scoped id 列（表 → 列清单，逐表对应
    # 上面的重建表）。v1 里 QQ 是唯一通道，这些列落库的是裸整数，TEXT 化只会变成
    # '100'/'7' —— 不补前缀，老 QQ 记忆对 recent_messages('qq:100') 等查询全部
    # 失联，channel_of('100') 还会直接抛错。只列 id 列：message_id/session_id/
    # content 不带通道语义，绝不加前缀。
    _V2_QQ_PREFIX_COLUMNS: ClassVar[dict[str, tuple[str, ...]]] = {
        "messages": ("chat_id", "user_id", "reply_to_user_id"),
        "impressions": ("chat_id", "subject_user_id"),
        "impression_cursors": ("chat_id",),
        "chat_state": ("chat_id",),
        "summaries": ("chat_id",),
        "weekly_summaries": ("chat_id",),
        "usage": ("chat_id",),
        "opencode_sessions": ("chat_id",),
        "brain_sessions": ("chat_id",),
        "session_summaries": ("chat_id",),
        "delivered_cursors": ("chat_id",),
        "delivered_messages": ("chat_id",),
    }

    @staticmethod
    def _qq_prefix_expr(column: str) -> str:
        """SELECT 里给 id 列补 'qq:' 前缀的 CASE 表达式；对已带前缀的值幂等。"""
        if column == "reply_to_user_id":
            # 可空列：NULL 必须保持 NULL（掉进 ELSE 会拼出 'qq:' 垃圾），
            # '' 保持 ''，其余补前缀。
            return (
                f"CASE WHEN {column} IS NULL THEN NULL"
                f" WHEN {column} = '' THEN ''"
                f" WHEN {column} LIKE 'qq:%' THEN {column}"
                f" ELSE 'qq:' || {column} END"
            )
        if column == "subject_user_id":
            # 空值哨兵：'' 与 empty-TEXT 化的 '0' 都解析成 ''，
            # 绝不能变 'qq:0' 与真实 subject 撞车。
            return (
                f"CASE WHEN {column} IN ('', '0') THEN ''"
                f" WHEN {column} LIKE 'qq:%' THEN {column}"
                f" ELSE 'qq:' || {column} END"
            )
        return (
            f"CASE WHEN {column} = '' THEN ''"
            f" WHEN {column} LIKE 'qq:%' THEN {column}"
            f" ELSE 'qq:' || {column} END"
        )

    def _rebuild_v2_tables(self) -> None:
        """v1 → v2 的表重建：12 张表的 id 列 TEXT 化并补 'qq:' 前缀，FTS5 与触发器还原。

        必须运行在 ``BEGIN IMMEDIATE`` 事务内（调用方保证）：任何一步失败整体
        回滚，库里要么完整是 v1、要么完整是 v2。固定顺序：

        1. 从 ``sqlite_master`` 把两个 FTS5 表、6 个触发器、这 12 张表上的全部
           普通索引的 DDL **原样抄下来**（不手写 —— trigram/unicode61 两个
           tokenizer 必须逐字保留，统一或重写会改变中文子串检索语义）；
        2. ``DROP TRIGGER`` 拆掉 6 个 FTS 触发器，``DROP TABLE`` 拆掉两个 FTS 表
           —— 不先拆会在随后 DROP/RENAME 正文表时把它们的 schema 改歪；
        3. 逐表 ``CREATE <t>_new``（重声明完整 DDL，取自 _SCHEMA 的 v2 版）→
           ``INSERT ... SELECT`` 显式点名（老库经 _MIGRATIONS 补列的列序与
           _SCHEMA 不同，``SELECT *`` 会按位置错位）→ ``DROP`` 旧表 →
           ``RENAME`` 回原名。
           不用 ``ALTER TABLE ... RENAME`` 直接改名再迁数据：SQLite ≥3.25 会把
           触发器体/视图里的没改引用重写到临时名上。TEXT 亲和性在 INSERT 时把
           v1 的整数值隐式转成十进制字符串（例如 100 → ``'100'``）；channel-scoped
           id 列（_V2_QQ_PREFIX_COLUMNS）再用 CASE 补 ``'qq:'`` 通道前缀 —— 裸
           '100' 对 ``recent_messages('qq:100')`` 是查不到的。旧表里已经有前缀的
           值（幂等重跑）与 NULL/空值哨兵原样保留；
        4. 清掉 impressions 的空值哨兵：v1 的 ``0`` 会变成字符串 ``'0'``，
           与真实 subject 撞车，统一改成 ``''``（新 DDL 默认值也是 ``''``；
           步骤 3 的 CASE 已把哨兵解析成 ''，这条 UPDATE 是兜底，一并保留）；
        5. 按抄录的 DDL 原样重建索引、FTS 表与触发器，再各跑一次
           ``INSERT INTO <fts>(<fts>) VALUES('rebuild')`` 回填索引；
        6. ``PRAGMA foreign_key_check`` 必须为空，否则回滚拒迁。
        """
        conn = self._conn
        # 步骤 1：DROP 之前先把要保留的 DDL 从 sqlite_master 原样抄下来。
        fts_sql: dict[str, str | None] = {}
        for name in ("messages_fts", "impressions_fts"):
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            fts_sql[name] = None if row is None else row["sql"]
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
            " AND tbl_name IN ('messages', 'impressions')"
        ).fetchall()
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            " AND tbl_name IN ('" + "', '".join(self._V2_REBUILD_TABLES) + "')"
        ).fetchall()

        # 步骤 2：拆触发器与两个 FTS 表。
        for name in (
            "messages_ai", "messages_ad", "messages_au",
            "impressions_ai", "impressions_ad", "impressions_au",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for name in ("messages_fts", "impressions_fts"):
            conn.execute(f"DROP TABLE IF EXISTS {name}")

        # 步骤 3：逐表 CREATE _new → INSERT SELECT（显式点名）→ DROP → RENAME。
        # 重建前先补齐 _MIGRATIONS 声明的列：老库可能缺某一列，显式列清单要求
        # 新旧两侧列名并集一致。
        self._ensure_columns()
        for table in self._V2_REBUILD_TABLES:
            ddl = self._v2_new_table_ddl(table)
            conn.execute(ddl)
            new_cols = [
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table}_new)").fetchall()
            ]
            old_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if old_info:
                # DROP／RENAME 前先把 _new 里声明、旧表却没有的列找出来：迁移是
                # 数据不可丢的操作，缺列宁可中止回滚，也不能把值错位塞进去。
                old_cols = {row["name"] for row in old_info}
                missing = [name for name in new_cols if name not in old_cols]
                if missing:
                    raise RuntimeError(
                        f"v2 迁移中止：表 {table} 缺列 {missing}，"
                        "请回滚后核对 _MIGRATIONS 列清单"
                    )
                cols = ", ".join(new_cols)
                # channel-scoped id 列用 CASE 补 'qq:' 前缀（幂等，哨兵不碰），
                # 其余列原点搬 —— message_id/session_id/content 不带通道语义。
                prefix_cols = self._V2_QQ_PREFIX_COLUMNS[table]
                select_items = [
                    self._qq_prefix_expr(col) if col in prefix_cols else col
                    for col in new_cols
                ]
                conn.execute(
                    f"INSERT INTO {table}_new ({cols})"
                    f" SELECT {', '.join(select_items)} FROM {table}"
                )
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")

        # 步骤 4：清空值哨兵（在同一个事务里，和重建一起原子生效）。
        conn.execute(
            "UPDATE impressions SET subject_user_id = '' WHERE subject_user_id = '0'"
        )

        # 步骤 5：按 sqlite_master 抄录的 DDL 原样还原索引、FTS 表与触发器；
        # 索引先于触发器无要求，但 FTS 表必须先建 —— 触发器体往里写。
        for row in index_sql:
            conn.execute(row["sql"])
        for name in ("messages_fts", "impressions_fts"):
            if fts_sql[name] is not None:
                conn.execute(fts_sql[name])
        for row in trigger_sql:
            conn.execute(row["sql"])
        for name in ("messages_fts", "impressions_fts"):
            if fts_sql[name] is not None:
                conn.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")

        # 步骤 6：提交前自检；有违例说明重建歪了，回滚回 v1 拒绝半迁移。
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"v2 迁移后 PRAGMA foreign_key_check 有 {len(violations)} 行违例"
            )

    @staticmethod
    def _v2_new_table_ddl(table: str) -> str:
        """从 `_SCHEMA` 抽出该表的 v2 DDL，把表名变成 ``<t>_new``。

        单一来源：迁移目标布局必须与空库直建的 v2 完全一致（plan 的
        「新装库同构」要求），所以 ``_new`` 的 DDL 直接取自 _SCHEMA 本身而不是
        再维护一份拷贝；test_fresh_and_migrated_layout_identical 兜底防漂移。
        """
        match = re.search(
            rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \(.*?^\);",
            _SCHEMA,
            re.MULTILINE | re.DOTALL,
        )
        if match is None:
            raise RuntimeError(f"_SCHEMA 里找不到表 {table} 的 DDL")
        return match.group(0).replace(
            f"IF NOT EXISTS {table}", f"{table}_new", 1
        )

    def db_stats(self) -> dict[str, Any]:
        """健康快照用的轻量库统计；不读消息正文。"""
        memory = self.path == ":memory:"
        db_path = Path(self.path)
        db_bytes = db_path.stat().st_size if not memory and db_path.exists() else 0
        wal = Path(f"{self.path}-wal")
        wal_bytes = wal.stat().st_size if not memory and wal.exists() else 0
        row = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        return {
            "db_path": self.path,
            "db_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "message_count": int(row["n"] or 0),
            "schema_version": version,
        }

    def _ensure_columns(self) -> None:
        """给老库补列：CREATE TABLE IF NOT EXISTS 不会改已存在的表。"""
        for table, columns in _MIGRATIONS.items():
            info = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not info:
                continue
            existing = {row["name"] for row in info}
            for name, decl in columns.items():
                if name in existing:
                    continue
                try:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}"
                    )
                except sqlite3.OperationalError:
                    # SQLite 的 ADD COLUMN 不接受非常量默认值（如 datetime('now')）：
                    # 用当前时间的常量字面量重试，老行同样补成迁移时刻。
                    constant = self._materialize_default(decl)
                    if constant == decl:
                        raise
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {constant}"
                    )

    def _materialize_default(self, decl: str) -> str:
        """把 ``DEFAULT (datetime('now'))`` 换成等价的常量默认值。"""
        if "(datetime('now'))" not in decl:
            return decl
        now = self._conn.execute("SELECT datetime('now')").fetchone()[0]
        return decl.replace("(datetime('now'))", f"'{now}'")

    def _backfill_session_created_at(self) -> None:
        """老库的数据迁移：created_at 补列后历史行是 NULL，天数判据会一直跳过。

        回填成「今天」（启动日）是保守选择：无法还原真实建立时间时，最长也只能
        晚一次轮换周期，不会把老会话立刻当成过期。WHERE 只挑 NULL，重复执行幂等。
        """
        self._conn.execute(
            "UPDATE brain_sessions SET created_at = ? WHERE created_at IS NULL",
            (today_shanghai(),),
        )

    def close(self) -> None:
        self._conn.close()

    def checkpoint(self) -> tuple[int, int, int]:
        """执行一次 WAL TRUNCATE 检查点，返回 (busy, log, checkpointed)。

        非 WAL 库（含 ``:memory:``）上 SQLite 返回 (0, -1, -1)：统一归一成
        (0, 0, 0)，调用方不必区分。任何 sqlite3.Error 也不外抛 —— 定时任务
        不能因为一次锁冲突就退出。
        """
        try:
            row = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error:
            return (0, 0, 0)
        if row is None:
            return (0, 0, 0)
        busy, log_pages, checkpointed = int(row[0]), int(row[1]), int(row[2])
        if busy < 0 or log_pages < 0 or checkpointed < 0:
            return (0, 0, 0)
        return (busy, log_pages, checkpointed)

    def backup_to(self, path: str | Path) -> None:
        """用 SQLite backup API 写一致性快照（WAL 中已提交的内容也会带上）。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(target))
        try:
            self._conn.backup(dest)
        finally:
            dest.close()

    @staticmethod
    def today() -> str:
        return today_shanghai()

    # ---- messages -------------------------------------------------
    def add_message(self, m: Message) -> bool:
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (chat_type, chat_id, message_id, user_id, nickname, content, is_bot,
                 timestamp, at_bot, reply_to_bot, reply_to_message_id, reply_to_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                m.reply_to_message_id,
                m.reply_to_user_id,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def is_bot_message(
        self, chat_type: ChatType, chat_id: str, message_id: str
    ) -> bool:
        """这条 message_id 是否是本会话里 bot 自己发的消息。

        重启后网关内存里的 bot_message_ids 是空的；引用回复老消息时只能靠库
        判断「被引用的是不是我」，否则会把对 bot 的引用当成普通回复。
        """
        if not message_id:
            return False
        row = self._conn.execute(
            """
            SELECT is_bot FROM messages
            WHERE chat_type = ? AND chat_id = ? AND message_id = ?
            """,
            (chat_type.value, chat_id, message_id),
        ).fetchone()
        return bool(row and row["is_bot"])

    def recent_messages(
        self,
        chat_type: ChatType,
        chat_id: str,
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

    def recent_bot_messages(self, since_ts: int, limit: int = 200) -> list[tuple[str, str, int]]:
        """回声表重建源：最近的机器人消息（必须带文本）。

        微信桥重启 resync 后，主进程从 `messages WHERE is_bot=1` 取种子；
        SQL 里 LIMIT 固定 200（与回声表容量一致），窗口在 Python 侧按
        ``timestamp`` 比较——`timestamp` 是本地 epoch 秒（service 落库时写入），
        **不是** UTC 字符串 `created_at`。绝不查 `delivered_messages`：它没有
        文本列，重建不出 ``(chat_id, text)`` 匹配键。返回 newest-first 元组。
        """
        limit = max(1, min(int(limit), 200))
        rows = self._conn.execute(
            "SELECT chat_id, content, timestamp FROM messages WHERE is_bot=1 ORDER BY id DESC LIMIT 200"
        ).fetchall()
        since = int(since_ts)
        results: list[tuple[str, str, int]] = []
        for row in rows:
            ts = int(row["timestamp"])
            if ts < since:
                continue
            results.append((str(row["chat_id"]), str(row["content"]), ts))
        return results[:limit]

    def messages_page(
        self,
        chat_type: ChatType,
        chat_id: str,
        limit: int = 50,
        before: int | None = None,
        anchor_message_id: str | None = None,
    ) -> MessagePage:
        """按数据库自增 id 分页，返回时间正序的一页。

        - before：只取 id 小于该游标的更早消息；游标来自上一页的 next_before。
        - anchor_message_id：以命中的消息结尾取一页；找不到时回落最新一页并置
          anchor_found=False，方便前端提示后仍能展示内容。
        """
        limit = max(1, min(int(limit), MAX_TIMELINE_PAGE))
        anchor_found = False
        anchor_id: int | None = None
        if anchor_message_id:
            anchor = self._conn.execute(
                """
                SELECT id FROM messages
                WHERE chat_type = ? AND chat_id = ? AND message_id = ?
                """,
                (chat_type.value, chat_id, anchor_message_id),
            ).fetchone()
            if anchor is not None:
                anchor_id = int(anchor["id"])
                anchor_found = True

        clauses = ["chat_type = ?", "chat_id = ?"]
        params: list[object] = [chat_type.value, chat_id]
        if anchor_id is not None:
            clauses.append("id <= ?")
            params.append(anchor_id)
        elif before is not None:
            clauses.append("id < ?")
            params.append(int(before))

        rows = self._conn.execute(
            f"""
            SELECT * FROM messages
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC LIMIT ?
            """,
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        next_before = int(rows[-1]["id"]) if has_more and rows else None
        return MessagePage(
            messages=[self._row_to_message(row) for row in reversed(rows)],
            has_more=has_more,
            next_before=next_before,
            anchor_found=anchor_found,
        )

    def messages_between(
        self, chat_type: ChatType, chat_id: str, start_ts: int, end_ts: int
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

    def message_nicknames(
        self, chat_type: ChatType, chat_id: str, message_ids: Sequence[str]
    ) -> dict[str, str]:
        """按 message_id 批量回查昵称；摘要补「回复 谁」时一次查完，别逐条打库。"""
        ids = [str(mid) for mid in message_ids if mid]
        if not ids:
            return {}
        found: dict[str, str] = {}
        # SQLite 变量上限 999，按 500 一批足够覆盖单日消息量。
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            marks = ",".join("?" for _ in batch)
            rows = self._conn.execute(
                f"""
                SELECT message_id, nickname FROM messages
                WHERE chat_type = ? AND chat_id = ? AND message_id IN ({marks})
                """,
                (chat_type.value, chat_id, *batch),
            ).fetchall()
            for row in rows:
                found[str(row["message_id"])] = str(row["nickname"])
        return found

    def active_chats_between(self, start_ts: int, end_ts: int) -> list[tuple[ChatType, str]]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT chat_type, chat_id FROM messages
            WHERE timestamp >= ? AND timestamp < ?
            ORDER BY chat_type,
                     CASE WHEN chat_id LIKE 'qq:%' THEN 0 ELSE 1 END,
                     CAST(SUBSTR(chat_id, 4) AS INTEGER),
                     chat_id
            """,
            (start_ts, end_ts),
        ).fetchall()
        return [(ChatType(r["chat_type"]), r["chat_id"]) for r in rows]

    def count_messages(self, chat_type: ChatType, chat_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_type = ? AND chat_id = ?",
            (chat_type.value, chat_id),
        ).fetchone()
        return int(row["n"])

    def prune_messages(self, before_ts: int) -> int:
        """删除 timestamp 早于 cutoff 的消息；timestamp=0（未知时间）不动。

        摘要与印象是独立表，保留；delivered_messages 里失去对应消息的孤儿行一并清掉，
        避免投递记录比消息本体活得更久。返回删除的消息数。
        """
        if before_ts <= 0:
            return 0
        cur = self._conn.execute(
            "DELETE FROM messages WHERE timestamp > 0 AND timestamp < ?",
            (before_ts,),
        )
        deleted = max(cur.rowcount, 0)
        self._conn.execute(
            """
            DELETE FROM delivered_messages
            WHERE NOT EXISTS (
                SELECT 1 FROM messages m
                WHERE m.chat_type = delivered_messages.chat_type
                  AND m.chat_id = delivered_messages.chat_id
                  AND m.message_id = delivered_messages.message_id
            )
            """
        )
        self._conn.commit()
        return deleted

    def delete_user_messages(
        self, chat_type: ChatType, chat_id: str, user_id: str
    ) -> int:
        """按用户删除某会话的原始消息，并清理对应投递记录。

        摘要/印象等聚合表无法可靠归因，保留不动；消息与 delivered_messages
        在同一事务内删除，返回删除的消息行数（不含投递行）。
        """
        with self._conn:
            self._conn.execute(
                """
                DELETE FROM delivered_messages
                WHERE chat_type = ? AND chat_id = ?
                  AND message_id IN (
                      SELECT message_id FROM messages
                      WHERE chat_type = ? AND chat_id = ? AND user_id = ?
                  )
                """,
                (chat_type.value, chat_id, chat_type.value, chat_id, user_id),
            )
            cur = self._conn.execute(
                """
                DELETE FROM messages
                WHERE chat_type = ? AND chat_id = ? AND user_id = ?
                """,
                (chat_type.value, chat_id, user_id),
            )
        return max(cur.rowcount, 0)

    def search_messages(
        self,
        query: str,
        chat_type: ChatType | None = None,
        chat_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[Message]]:
        """跨会话内容搜索，最新/相关度优先；LIKE 通配符已转义。"""
        total, hits = self.search_message_hits(query, chat_type, chat_id, limit, offset)
        return total, [message for message, _ in hits]

    def search_message_hits(
        self,
        query: str,
        chat_type: ChatType | None = None,
        chat_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[tuple[Message, str]]]:
        """返回 (总数, [(消息, 命中摘录)])。

        3 字以上优先走 trigram FTS + bm25；1-2 字（trigram 无法索引）或 FTS
        不可用时回落 LIKE。FTS 查不到时也回落一次，避免分词边界导致漏召回。
        """
        query = (query or "").strip()
        if not query:
            return 0, []
        page, skip = _page_bounds(limit, offset)
        if self._messages_fts and len(query) >= 3:
            phrase = '"' + query.replace('"', '""') + '"'
            clauses: list[str] = []
            params: list[object] = []
            if chat_type is not None:
                clauses.append("m.chat_type = ?")
                params.append(chat_type.value)
            if chat_id is not None:
                clauses.append("m.chat_id = ?")
                params.append(chat_id)
            where = " AND ".join(["messages_fts MATCH ?", *clauses])
            try:
                total_row = self._conn.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM messages_fts
                    JOIN messages m ON m.id = messages_fts.rowid
                    WHERE {where}
                    """,
                    (phrase, *params),
                ).fetchone()
                total = int(total_row["total"] or 0)
                if total:
                    rows = self._conn.execute(
                        f"""
                        SELECT m.*
                        FROM messages_fts
                        JOIN messages m ON m.id = messages_fts.rowid
                        WHERE {where}
                        ORDER BY bm25(messages_fts), m.id DESC
                        LIMIT ? OFFSET ?
                        """,
                        (phrase, *params, page, skip),
                    ).fetchall()
                    return total, [
                        (self._row_to_message(r), self._snippet(r["content"], query))
                        for r in rows
                    ]
            except sqlite3.OperationalError:
                pass
        return self._search_messages_like(query, chat_type, chat_id, page, skip)

    def search_message_terms(
        self,
        terms: Sequence[str],
        chat_type: ChatType | None = None,
        chat_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[tuple[Message, str]]]:
        """用多个词项做 OR 检索：一次 FTS 查询覆盖整句，适合召回长问句。"""
        cleaned: list[str] = []
        for raw in terms:
            term = (raw or "").strip()
            if term and term not in cleaned:
                cleaned.append(term)
        if not cleaned:
            return 0, []
        page, skip = _page_bounds(limit, offset)

        if self._messages_fts:
            match = " OR ".join('"' + term.replace('"', '""') + '"' for term in cleaned)
            clauses: list[str] = []
            params: list[object] = []
            if chat_type is not None:
                clauses.append("m.chat_type = ?")
                params.append(chat_type.value)
            if chat_id is not None:
                clauses.append("m.chat_id = ?")
                params.append(chat_id)
            where = " AND ".join(["messages_fts MATCH ?", *clauses])
            try:
                total_row = self._conn.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM messages_fts
                    JOIN messages m ON m.id = messages_fts.rowid
                    WHERE {where}
                    """,
                    (match, *params),
                ).fetchone()
                total = int(total_row["total"] or 0)
                if total:
                    rows = self._conn.execute(
                        f"""
                        SELECT m.*
                        FROM messages_fts
                        JOIN messages m ON m.id = messages_fts.rowid
                        WHERE {where}
                        ORDER BY bm25(messages_fts), m.id DESC
                        LIMIT ? OFFSET ?
                        """,
                        (match, *params, page, skip),
                    ).fetchall()
                    return total, [
                        (self._row_to_message(r), self._snippet_for_terms(r["content"], cleaned))
                        for r in rows
                    ]
            except sqlite3.OperationalError:
                pass

        scope, params = _memory_scope(chat_type, chat_id)
        clause = f"{scope} AND" if scope else "WHERE"
        likes = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in cleaned)
        condition = f"{clause} ({likes})"
        like_params = [_like_pattern(term) for term in cleaned]
        total_row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM messages {condition}",
            (*params, *like_params),
        ).fetchone()
        rows = self._conn.execute(
            f"""
            SELECT * FROM messages
            {condition}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, *like_params, page, skip),
        ).fetchall()
        return int(total_row["total"] or 0), [
            (self._row_to_message(r), self._snippet_for_terms(r["content"], cleaned))
            for r in rows
        ]

    @classmethod
    def _snippet_for_terms(cls, content: str, terms: Sequence[str]) -> str:
        folded = (content or "").casefold()
        for term in terms:
            if term.casefold() in folded:
                return cls._snippet(content, term)
        return cls._snippet(content, terms[0] if terms else "")

    def _search_messages_like(
        self,
        query: str,
        chat_type: ChatType | None,
        chat_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[tuple[Message, str]]]:
        scope, params = _memory_scope(chat_type, chat_id)
        needle = _like_pattern(query)
        clause = f"{scope} AND" if scope else "WHERE"
        condition = f"{clause} content LIKE ? ESCAPE '\\'"
        total_row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM messages {condition}",
            (*params, needle),
        ).fetchone()
        rows = self._conn.execute(
            f"""
            SELECT * FROM messages
            {condition}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, needle, limit, offset),
        ).fetchall()
        return int(total_row["total"] or 0), [
            (self._row_to_message(r), self._snippet(r["content"], query)) for r in rows
        ]

    @staticmethod
    def _snippet(content: str, query: str, radius: int = 30) -> str:
        text = (content or "").strip()
        if not text:
            return ""
        needle = query.strip()
        index = text.casefold().find(needle.casefold()) if needle else -1
        if index < 0:
            return text[: radius * 2] + ("…" if len(text) > radius * 2 else "")
        start = max(0, index - radius)
        end = min(len(text), index + len(needle) + radius)
        return (
            ("…" if start > 0 else "")
            + text[start:end]
            + ("…" if end < len(text) else "")
        )

    # ---- delivery cursors -----------------------------------------
    def _row_id(self, chat_type: ChatType, chat_id: str, message_id: str) -> int | None:
        row = self._conn.execute(
            """
            SELECT id FROM messages
            WHERE chat_type = ? AND chat_id = ? AND message_id = ?
            """,
            (chat_type.value, chat_id, message_id),
        ).fetchone()
        return None if row is None else int(row["id"])

    def last_delivered_id(self, chat_type: ChatType, chat_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT last_message_id FROM delivered_cursors WHERE chat_type = ? AND chat_id = ?",
            (chat_type.value, chat_id),
        ).fetchone()
        return None if row is None else int(row["last_message_id"])

    def pending_messages(
        self, chat_type: ChatType, chat_id: str, upto_message_id: str, limit: int
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

    def mark_delivered(self, chat_type: ChatType, chat_id: str, *message_ids: str) -> None:
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
        chat_id: str,
        anchor_id: str,
        limit: int = 20,
    ) -> list[Message]:
        """批次投递要看的消息：游标之后、未投递、id 升序，最多 limit 条。

        首次投递（还没有投递下限）时只返回 anchor 这一条，不回填历史 —
        与 pending_messages 的语义保持一致，但这里是 id 正序且不带「被省略条数」。
        积压超过 limit 时保留**最新**的一段：回来先接上最近的话题，陈旧消息不占窗口。
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
            ORDER BY m.id DESC LIMIT ?
            """,
            (chat_type.value, chat_id, floor, anchor, limit),
        ).fetchall()
        return [self._row_to_message(r) for r in reversed(rows)]

    def count_undelivered_before(
        self, chat_type: ChatType, chat_id: str, message_id: str
    ) -> int:
        """投递窗口之前还有多少条未投递消息。

        积压超过 pending_batch 的 limit 时，更早的消息既不进 prompt 也不会被标记投递；
        这个计数用来在 prompt 里留一句「更早还有 N 条未展开」，让模型知道有断层。
        """
        row_id = self._row_id(chat_type, chat_id, message_id)
        if row_id is None:
            return 0
        floor = self.last_delivered_id(chat_type, chat_id)
        if floor is None:
            return 0
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM messages m
            WHERE m.chat_type = ? AND m.chat_id = ? AND m.is_bot = 0
              AND m.id > ? AND m.id < ?
              AND NOT EXISTS (
                  SELECT 1 FROM delivered_messages d
                  WHERE d.chat_type = m.chat_type AND d.chat_id = m.chat_id
                    AND d.message_id = m.message_id
              )
            """,
            (chat_type.value, chat_id, floor, row_id),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def mark_recalled(self, chat_type: ChatType, chat_id: str, message_id: str) -> bool:
        """把一条已入库消息标记为撤回；原文保留，只是不再进入 prompt。"""
        cur = self._conn.execute(
            """
            UPDATE messages SET recalled_at = datetime('now')
            WHERE chat_type = ? AND chat_id = ? AND message_id = ?
              AND recalled_at IS NULL
            """,
            (chat_type.value, chat_id, message_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def is_recalled(self, chat_type: ChatType, chat_id: str, message_id: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM messages
            WHERE chat_type = ? AND chat_id = ? AND message_id = ?
              AND recalled_at IS NOT NULL
            """,
            (chat_type.value, chat_id, message_id),
        ).fetchone()
        return row is not None

    def chats_with_pending(self, since_ts: int) -> list[tuple[ChatType, str]]:
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
            ORDER BY m.chat_type,
                     CASE WHEN m.chat_id LIKE 'qq:%' THEN 0 ELSE 1 END,
                     CAST(SUBSTR(m.chat_id, 4) AS INTEGER),
                     m.chat_id
            """,
            (since_ts,),
        ).fetchall()
        return [(ChatType(r["chat_type"]), r["chat_id"]) for r in rows]

    def mark_skipped(
        self, chat_type: ChatType, chat_id: str, message_ids: Sequence[str], reason: str
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

    def skip_reason(self, chat_type: ChatType, chat_id: str, message_id: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT skipped_reason FROM delivered_messages
            WHERE chat_type = ? AND chat_id = ? AND message_id = ?
            """,
            (chat_type.value, chat_id, message_id),
        ).fetchone()
        return None if row is None else row["skipped_reason"]

    # ------------------------------------------------------------ 待发队列
    def enqueue_outbox(
        self,
        chat_type: ChatType,
        chat_id: str,
        text: str,
        *,
        origin_message_id: str = "",
        now: float | None = None,
    ) -> int:
        """把没发出去的一条分片排进待发队列；返回行 id（0 = 空文本，未入队）。"""
        body = str(text or "")
        if not body.strip():
            return 0
        stamp = time.time() if now is None else float(now)
        cur = self._conn.execute(
            """
            INSERT INTO pending_outbox
                (chat_type, chat_id, text, origin_message_id, enqueued_at, next_attempt_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_type.value, chat_id, body, str(origin_message_id or ""), stamp, stamp),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def due_outbox(self, now: float | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        """到点可补发的行，按入队顺序返回（同一会话天然保序）。"""
        stamp = time.time() if now is None else float(now)
        rows = self._conn.execute(
            """
            SELECT id, chat_type, chat_id, text, origin_message_id, attempts, enqueued_at
            FROM pending_outbox
            WHERE next_attempt_at <= ?
            ORDER BY id
            LIMIT ?
            """,
            (stamp, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def has_outbox(self, chat_type: ChatType | None = None, chat_id: str | None = None) -> bool:
        if chat_type is None:
            row = self._conn.execute("SELECT 1 FROM pending_outbox LIMIT 1").fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM pending_outbox WHERE chat_type = ? AND chat_id = ? LIMIT 1",
                (chat_type.value, chat_id),
            ).fetchone()
        return row is not None

    def outbox_size(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM pending_outbox").fetchone()[0])

    def drop_outbox(self, row_id: int) -> None:
        self._conn.execute("DELETE FROM pending_outbox WHERE id = ?", (int(row_id),))
        self._conn.commit()

    def defer_outbox(self, row_id: int, *, error: str, next_attempt_at: float) -> int:
        """记一次失败并推迟；返回累计尝试次数。"""
        self._conn.execute(
            """
            UPDATE pending_outbox
            SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?
            WHERE id = ?
            """,
            (str(error or "")[:300], float(next_attempt_at), int(row_id)),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT attempts FROM pending_outbox WHERE id = ?", (int(row_id),)
        ).fetchone()
        return int(row["attempts"]) if row is not None else 0

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
            reply_to_message_id=r["reply_to_message_id"],
            reply_to_user_id=r["reply_to_user_id"],
            timestamp=r["timestamp"],
            recalled_at=r["recalled_at"],
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
        self, provider: str, chat_type: ChatType, chat_id: str
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
        self, provider: str, chat_type: ChatType, chat_id: str, session_id: str
    ) -> None:
        """写入/更新会话 id；created_at 是这一轮 session 的起点，只落一次。

        老库升级前建的行 created_at 为 NULL，ON CONFLICT 时用 COALESCE 补上，
        已有值绝不覆盖——否则每次握手都会把轮换天数判据重置掉。
        """
        self._conn.execute(
            """
            INSERT INTO brain_sessions
                (provider, chat_type, chat_id, session_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (provider, chat_type, chat_id) DO UPDATE SET
                session_id = excluded.session_id,
                updated_at = datetime('now'),
                created_at = COALESCE(brain_sessions.created_at, excluded.created_at)
            """,
            (provider, chat_type.value, chat_id, session_id, today_shanghai()),
        )
        self._conn.commit()

    def clear_brain_session(
        self, provider: str, chat_type: ChatType, chat_id: str
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
    def session_stats(self, chat_type: ChatType, chat_id: str) -> SessionStats:
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
        self, chat_type: ChatType, chat_id: str, *, messages: int, tokens: int
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
                token_estimate = brain_sessions.token_estimate + excluded.token_estimate,
                created_at = COALESCE(brain_sessions.created_at, excluded.created_at)
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

    def reset_session_stats(self, chat_type: ChatType, chat_id: str) -> None:
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

    def get_opencode_session(self, chat_type: ChatType, chat_id: str) -> str | None:
        return self.get_brain_session("opencode", chat_type, chat_id)

    def set_opencode_session(
        self, chat_type: ChatType, chat_id: str, session_id: str
    ) -> None:
        self.set_brain_session("opencode", chat_type, chat_id, session_id)

    def clear_opencode_session(self, chat_type: ChatType, chat_id: str) -> None:
        self.clear_brain_session("opencode", chat_type, chat_id)

    def clear_opencode_sessions(self) -> int:
        """清空全部 opencode 会话映射，返回删掉的行数。

        换 provider 档位后旧 session 的模型已经过时（opencode 以会话里选的
        模型优先于配置默认值），必须整表清掉；只清单个会话会漏掉其余仍在
        用旧档位的群与私聊。
        """
        cur = self._conn.execute(
            "DELETE FROM brain_sessions WHERE provider = ?", (_SESSION_PROVIDER,)
        )
        self._conn.commit()
        return cur.rowcount

    # ---- impressions ----------------------------------------------
    def upsert_impression(
        self,
        chat_type: ChatType,
        chat_id: str,
        subject_user_id: str | None,
        kind: str,
        content: str,
        confidence: float,
        source_message_id: str = "",
        source_excerpt: str = "",
        subject_nickname: str = "",
    ) -> None:
        # 空值哨兵是空串（列默认 ''），不是 0：NULL subject 表示「整群印象」。
        subject = subject_user_id if subject_user_id is not None else ""
        nickname = subject_nickname.strip()[:40]
        if kind == "nickname":
            # 昵称是「唯一称呼」：同一用户只留最新一条，旧行交给 DELETE 让 FTS 触发器同步。
            self._conn.execute(
                """
                DELETE FROM impressions
                WHERE chat_type = ? AND chat_id = ? AND subject_user_id = ?
                  AND kind = 'nickname' AND content <> ?
                """,
                (chat_type.value, chat_id, subject, content),
            )
        elif kind in _NORMALIZED_KINDS:
            # 同义写法（空白/标点/大小写差异）归一到已有行，不新增重复项。
            rows = self._conn.execute(
                """
                SELECT * FROM impressions
                WHERE chat_type = ? AND chat_id = ? AND subject_user_id = ? AND kind = ?
                """,
                (chat_type.value, chat_id, subject, kind),
            ).fetchall()
            key = _normalize_impression(content)
            if key:
                for row in rows:
                    if _normalize_impression(row["content"]) == key:
                        self._merge_impression_row(
                            row,
                            content,
                            confidence,
                            source_message_id,
                            source_excerpt,
                            nickname,
                        )
                        self._conn.commit()
                        return
        self._conn.execute(
            """
            INSERT INTO impressions
                (chat_type, chat_id, subject_user_id, kind, content, confidence,
                 subject_nickname, source_message_id, source_excerpt, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (chat_type, chat_id, subject_user_id, kind, content)
            DO UPDATE SET
                confidence = excluded.confidence,
                updated_at = datetime('now'),
                last_seen_at = datetime('now'),
                subject_nickname = CASE
                    WHEN excluded.subject_nickname <> ''
                    THEN excluded.subject_nickname
                    ELSE impressions.subject_nickname
                END,
                source_message_id = CASE
                    WHEN excluded.source_message_id <> ''
                    THEN excluded.source_message_id
                    ELSE impressions.source_message_id
                END,
                source_excerpt = CASE
                    WHEN excluded.source_excerpt <> ''
                    THEN excluded.source_excerpt
                    ELSE impressions.source_excerpt
                END
            """,
            (
                chat_type.value,
                chat_id,
                subject,
                kind,
                content,
                confidence,
                nickname,
                source_message_id,
                source_excerpt,
            ),
        )
        self._conn.commit()

    def _merge_impression_row(
        self,
        row: sqlite3.Row,
        content: str,
        confidence: float,
        source_message_id: str,
        source_excerpt: str,
        subject_nickname: str = "",
    ) -> None:
        """归一化去重命中：沿用 merge_impressions 的语义就地更新，保留原 id。"""
        merged_content = content.strip() or row["content"]
        self._conn.execute(
            """
            UPDATE impressions
            SET content = ?,
                confidence = ?,
                updated_at = datetime('now'),
                last_seen_at = datetime('now'),
                subject_nickname = CASE
                    WHEN ? <> '' THEN ? ELSE subject_nickname
                END,
                source_message_id = CASE
                    WHEN ? <> '' THEN ? ELSE source_message_id
                END,
                source_excerpt = CASE
                    WHEN ? <> '' THEN ? ELSE source_excerpt
                END
            WHERE id = ?
            """,
            (
                merged_content,
                max(float(row["confidence"]), float(confidence)),
                subject_nickname,
                subject_nickname,
                source_message_id,
                source_message_id,
                source_excerpt,
                source_excerpt,
                int(row["id"]),
            ),
        )

    def get_impression(self, impression_id: int) -> Impression | None:
        row = self._conn.execute(
            "SELECT * FROM impressions WHERE id = ?", (int(impression_id),)
        ).fetchone()
        return None if row is None else self._row_to_impression(row)

    def update_impression(
        self,
        impression_id: int,
        *,
        content: str | None = None,
        confidence: float | None = None,
        kind: str | None = None,
    ) -> Impression | None:
        """编辑一条印象；返回更新后的行，id 不存在时返回 None。"""
        sets: list[str] = []
        params: list[object] = []
        if content is not None:
            sets.append("content = ?")
            params.append(content)
        if confidence is not None:
            sets.append("confidence = ?")
            params.append(float(confidence))
        if kind is not None:
            sets.append("kind = ?")
            params.append(kind)
        if not sets:
            return self.get_impression(impression_id)
        sets.append("updated_at = datetime('now')")
        cur = self._conn.execute(
            f"UPDATE impressions SET {', '.join(sets)} WHERE id = ?",
            (*params, int(impression_id)),
        )
        self._conn.commit()
        if cur.rowcount <= 0:
            return None
        return self.get_impression(impression_id)

    def merge_impressions(
        self, impression_ids: Sequence[int], content: str | None = None
    ) -> Impression | None:
        """把同一会话、同一主体的多条印象合并到最早的一条。

        调用方负责把 content 校验为非空；跨会话/跨主体合并没有语义，直接拒绝。
        """
        ids = list(dict.fromkeys(int(i) for i in impression_ids))
        if len(ids) < 2:
            raise ValueError("至少选择两条印象")
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT * FROM impressions WHERE id IN ({placeholders}) ORDER BY id ASC",
            ids,
        ).fetchall()
        if len(rows) != len(ids):
            return None
        first = rows[0]
        scope = (first["chat_type"], first["chat_id"], first["subject_user_id"])
        if any(
            (r["chat_type"], r["chat_id"], r["subject_user_id"]) != scope for r in rows
        ):
            raise ValueError("只能合并同一会话、同一主体的印象")
        merged_content = (content or first["content"]).strip()
        confidence = max(float(r["confidence"]) for r in rows)
        target = self.update_impression(
            int(first["id"]), content=merged_content, confidence=confidence
        )
        if target is None:
            return None
        others = [int(r["id"]) for r in rows[1:]]
        self._conn.execute(
            f"DELETE FROM impressions WHERE id IN ({','.join('?' for _ in others)})",
            others,
        )
        self._conn.commit()
        return self.get_impression(int(first["id"]))

    def delete_impression(self, impression_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM impressions WHERE id = ?", (int(impression_id),)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def search_impressions(
        self, chat_type: ChatType, chat_id: str, query: str, topk: int
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
                    ORDER BY (i.last_seen_at >= datetime('now', '-30 days')) DESC,
                             i.confidence DESC, i.updated_at DESC,
                             bm25(impressions_fts)
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
                ORDER BY (last_seen_at >= datetime('now', '-30 days')) DESC,
                         confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (chat_type.value, chat_id, _like_pattern(query), topk),
            ).fetchall()
        return [self._row_to_impression(r) for r in rows]

    def list_impressions(
        self,
        chat_type: ChatType,
        chat_id: str,
        topk: int,
        focus_user_ids: Sequence[str] = (),
    ) -> list[Impression]:
        """按新鲜度/置信度排序；focus_user_ids 里的人优先占名额。

        focus 只影响谁先入选，名额内仍按 30 天内见过 > confidence > updated_at 排。
        """
        focus = list(dict.fromkeys(focus_user_ids))[:32]
        if focus:
            marks = ",".join("?" for _ in focus)
            order = f"(subject_user_id IN ({marks})) DESC,"
            params: tuple[Any, ...] = (chat_type.value, chat_id, *focus, topk)
        else:
            order = ""
            params = (chat_type.value, chat_id, topk)
        rows = self._conn.execute(
            f"""
            SELECT * FROM impressions
            WHERE chat_type = ? AND chat_id = ?
            ORDER BY {order} (last_seen_at >= datetime('now', '-30 days')) DESC,
                     confidence DESC, updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._row_to_impression(r) for r in rows]

    def recent_impressions(
        self,
        limit: int = 20,
        offset: int = 0,
        chat_type: ChatType | None = None,
        chat_id: str | None = None,
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
        self, chat_type: ChatType | None = None, chat_id: str | None = None
    ) -> int:
        scope, params = _memory_scope(chat_type, chat_id)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM impressions {scope}", params
        ).fetchone()
        return int(row["total"] or 0)

    # ---- 印象抽取游标 ----------------------------------------------
    def get_impression_cursor(self, chat_type: ChatType, chat_id: str) -> int:
        """已抽取到的消息条数；没有记录时返回 0。"""
        row = self._conn.execute(
            """
            SELECT last_count FROM impression_cursors
            WHERE chat_type = ? AND chat_id = ?
            """,
            (chat_type.value, chat_id),
        ).fetchone()
        return 0 if row is None else int(row["last_count"] or 0)

    def set_impression_cursor(
        self, chat_type: ChatType, chat_id: str, last_count: int
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO impression_cursors (chat_type, chat_id, last_count, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT (chat_type, chat_id)
            DO UPDATE SET last_count = excluded.last_count,
                          updated_at = datetime('now')
            """,
            (chat_type.value, chat_id, max(0, int(last_count))),
        )
        self._conn.commit()

    # ---- 会话运行状态 ----------------------------------------------
    def load_chat_state(
        self, chat_type: ChatType, chat_id: str
    ) -> dict[str, object] | None:
        """静音/话题收尾等运行状态；没有记录返回 None。"""
        row = self._conn.execute(
            """
            SELECT last_bot_spoke, last_user_message, muted_until,
                   ambient_streak, no_reply_streak, downshift_until,
                   last_prompt, last_prompt_at,
                   thread_closed_until, last_closed_at
            FROM chat_state
            WHERE chat_type = ? AND chat_id = ?
            """,
            (chat_type.value, chat_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "last_bot_spoke": row["last_bot_spoke"],
            "last_user_message": row["last_user_message"],
            "muted_until": row["muted_until"],
            "ambient_streak": int(row["ambient_streak"] or 0),
            "no_reply_streak": int(row["no_reply_streak"] or 0),
            "downshift_until": row["downshift_until"],
            "last_prompt": row["last_prompt"] or "",
            "last_prompt_at": row["last_prompt_at"],
            "thread_closed_until": row["thread_closed_until"],
            "last_closed_at": row["last_closed_at"],
        }

    def save_chat_state(
        self,
        chat_type: ChatType,
        chat_id: str,
        state: dict[str, object],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO chat_state (
                chat_type, chat_id, last_bot_spoke, last_user_message,
                muted_until, ambient_streak, no_reply_streak, downshift_until,
                last_prompt, last_prompt_at,
                thread_closed_until, last_closed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (chat_type, chat_id) DO UPDATE SET
                last_bot_spoke = excluded.last_bot_spoke,
                last_user_message = excluded.last_user_message,
                muted_until = excluded.muted_until,
                ambient_streak = excluded.ambient_streak,
                no_reply_streak = excluded.no_reply_streak,
                downshift_until = excluded.downshift_until,
                last_prompt = excluded.last_prompt,
                last_prompt_at = excluded.last_prompt_at,
                thread_closed_until = excluded.thread_closed_until,
                last_closed_at = excluded.last_closed_at,
                updated_at = datetime('now')
            """,
            (
                chat_type.value,
                chat_id,
                state.get("last_bot_spoke"),
                state.get("last_user_message"),
                state.get("muted_until"),
                max(0, int(state.get("ambient_streak") or 0)),
                max(0, int(state.get("no_reply_streak") or 0)),
                state.get("downshift_until"),
                str(state.get("last_prompt") or ""),
                state.get("last_prompt_at"),
                state.get("thread_closed_until"),
                state.get("last_closed_at"),
            ),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_impression(r: sqlite3.Row) -> Impression:
        # v1 的整数 0 经 TEXT 重建已是字符串 '0'，同样按「无 subject」读回。
        v = r["subject_user_id"]
        return Impression(
            chat_type=ChatType(r["chat_type"]),
            chat_id=r["chat_id"],
            subject_user_id=None if v in ("", "0") else v,
            kind=r["kind"],
            content=r["content"],
            confidence=r["confidence"],
            subject_nickname=r["subject_nickname"] or "",
            source_message_id=r["source_message_id"] or "",
            source_excerpt=r["source_excerpt"] or "",
            updated_at=r["updated_at"],
            id=r["id"],
        )

    # ---- summaries ------------------------------------------------
    def all_summaries(self, chat_type: ChatType, chat_id: str) -> list[Summary]:
        """导出用：某会话全部按日摘要，时间正序。"""
        rows = self._conn.execute(
            """
            SELECT chat_type, chat_id, date, content, updated_at FROM summaries
            WHERE chat_type = ? AND chat_id = ?
            ORDER BY date ASC, updated_at ASC
            """,
            (chat_type.value, chat_id),
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

    def upsert_summary(self, chat_type: ChatType, chat_id: str, date: str, content: str) -> None:
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

    def get_summary(self, chat_type: ChatType, chat_id: str, date: str) -> str | None:
        row = self._conn.execute(
            "SELECT content FROM summaries WHERE chat_type = ? AND chat_id = ? AND date = ?",
            (chat_type.value, chat_id, date),
        ).fetchone()
        return None if row is None else str(row["content"])

    def count_summaries(
        self, chat_type: ChatType | None = None, chat_id: str | None = None
    ) -> int:
        scope, params = _memory_scope(chat_type, chat_id)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM summaries {scope}", params
        ).fetchone()
        return int(row["total"] or 0)

    def pending_summary_slots(
        self, days: int = 7, *, until: str | None = None
    ) -> list[PendingSummary]:
        """最近 `days` 天里 (会话, 日期) 有消息却缺摘要的槽位，日期倒序。

        `until` 是窗口最后一天（含），默认昨天：worker 次日凌晨才跑，今天本来
        就不该有摘要，算进缺口只会天天误报。调度器传自己时钟算出的日期，
        测试与线上才对齐。
        """
        window = max(1, min(int(days), 90))
        end = _parse_day(until) if until else _parse_day(self.today()) - timedelta(days=1)
        start = end - timedelta(days=window - 1)
        start_ts = int(start.timestamp())
        end_ts = int((end + timedelta(days=1)).timestamp())
        rows = self._conn.execute(
            """
            SELECT m.chat_type,
                   m.chat_id,
                   strftime('%Y-%m-%d', m.timestamp, 'unixepoch', '+8 hours') AS day,
                   COUNT(*) AS n
            FROM messages AS m
            LEFT JOIN summaries AS s
              ON s.chat_type = m.chat_type
             AND s.chat_id = m.chat_id
             AND s.date = strftime('%Y-%m-%d', m.timestamp, 'unixepoch', '+8 hours')
            WHERE m.timestamp >= ? AND m.timestamp < ? AND s.date IS NULL
            GROUP BY m.chat_type, m.chat_id, day
            ORDER BY day DESC,
                     CASE WHEN m.chat_id LIKE 'qq:%' THEN 0 ELSE 1 END,
                     CAST(SUBSTR(m.chat_id, 4) AS INTEGER),
                     m.chat_id ASC
            """,
            (start_ts, end_ts),
        ).fetchall()
        return [
            PendingSummary(
                chat_type=ChatType(row["chat_type"]),
                chat_id=row["chat_id"],
                date=str(row["day"]),
                messages=int(row["n"]),
            )
            for row in rows
        ]

    def pending_summary_days(self, days: int = 7, *, until: str | None = None) -> list[str]:
        """缺摘要的日期（去重升序），面板横幅只关心「哪几天」。

        「有缺口」按 (会话, 日期) 判定：同一天两个群，只有一个群生成了
        摘要，这天仍算没补完。只读、无副作用。
        """
        return sorted({slot.date for slot in self.pending_summary_slots(days, until=until)})

    def summaries_between(
        self, chat_type: ChatType, chat_id: str, start: str, end: str
    ) -> list[Summary]:
        """按日期闭区间取日摘要，升序；周汇总的输入就是这 7 天里实际存在的条目。"""
        rows = self._conn.execute(
            """
            SELECT chat_type, chat_id, date, content, updated_at FROM summaries
            WHERE chat_type = ? AND chat_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (chat_type.value, chat_id, start, end),
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

    # ---- 周汇总 -----------------------------------------------------
    def upsert_weekly_summary(
        self, chat_type: ChatType, chat_id: str, week_start: str, content: str
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO weekly_summaries (chat_type, chat_id, week_start, content)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (chat_type, chat_id, week_start)
            DO UPDATE SET content = excluded.content, updated_at = datetime('now')
            """,
            (chat_type.value, chat_id, week_start, content),
        )
        self._conn.commit()

    def get_weekly_summary(
        self, chat_type: ChatType, chat_id: str, week_start: str
    ) -> str | None:
        row = self._conn.execute(
            """
            SELECT content FROM weekly_summaries
            WHERE chat_type = ? AND chat_id = ? AND week_start = ?
            """,
            (chat_type.value, chat_id, week_start),
        ).fetchone()
        return None if row is None else str(row["content"])

    def latest_weekly_summary(
        self, chat_type: ChatType, chat_id: str
    ) -> WeeklySummary | None:
        row = self._conn.execute(
            """
            SELECT chat_type, chat_id, week_start, content, updated_at
            FROM weekly_summaries
            WHERE chat_type = ? AND chat_id = ?
            ORDER BY week_start DESC LIMIT 1
            """,
            (chat_type.value, chat_id),
        ).fetchone()
        if row is None:
            return None
        return WeeklySummary(
            chat_type=ChatType(row["chat_type"]),
            chat_id=row["chat_id"],
            week_start=row["week_start"],
            content=row["content"],
            updated_at=row["updated_at"],
        )

    def pending_weekly_slots(self, week_starts: Sequence[str]) -> list[PendingWeekly]:
        """给定周起始里「有日摘要、还没周汇总」的会话，按周与 chat_id 排序。"""
        pending: list[PendingWeekly] = []
        for week_start in week_starts:
            start = _parse_day(week_start).date()
            end = (start + timedelta(days=6)).isoformat()
            rows = self._conn.execute(
                """
                SELECT s.chat_type, s.chat_id, COUNT(*) AS n
                FROM summaries AS s
                WHERE s.date >= ? AND s.date <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM weekly_summaries AS w
                      WHERE w.chat_type = s.chat_type
                        AND w.chat_id = s.chat_id
                        AND w.week_start = ?
                  )
                GROUP BY s.chat_type, s.chat_id
                ORDER BY CASE WHEN s.chat_id LIKE 'qq:%' THEN 0 ELSE 1 END,
                         CAST(SUBSTR(s.chat_id, 4) AS INTEGER),
                         s.chat_id ASC
                """,
                (week_start, end, week_start),
            ).fetchall()
            pending.extend(
                PendingWeekly(
                    chat_type=ChatType(row["chat_type"]),
                    chat_id=row["chat_id"],
                    week_start=week_start,
                    days=int(row["n"]),
                )
                for row in rows
            )
        return pending

    def recent_summaries(
        self,
        limit: int = 20,
        offset: int = 0,
        chat_type: ChatType | None = None,
        chat_id: str | None = None,
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
    def add_session_summary(self, chat_type: ChatType, chat_id: str, content: str) -> None:
        self._conn.execute(
            """
            INSERT INTO session_summaries (chat_type, chat_id, content)
            VALUES (?, ?, ?)
            """,
            (chat_type.value, chat_id, content),
        )
        self._conn.commit()

    def latest_session_summary(self, chat_type: ChatType, chat_id: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT content FROM session_summaries
            WHERE chat_type = ? AND chat_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (chat_type.value, chat_id),
        ).fetchone()
        return None if row is None else str(row["content"])

    def recent_session_summary(
        self, chat_type: ChatType, chat_id: str, within_hours: float = 24
    ) -> str | None:
        """`within_hours` 内的最新接续摘要；重启后靠它把轮换接续语捞回来。

        created_at 是 UTC（datetime('now')），窗口比较也交给 SQLite，避免 Python
        侧混时区。过期的旧摘要不返回：几天前的交接语接到当前对话上只会串台。
        """
        seconds = int(max(0.0, float(within_hours)) * 3600)
        row = self._conn.execute(
            """
            SELECT content FROM session_summaries
            WHERE chat_type = ? AND chat_id = ?
              AND created_at >= datetime('now', ?)
            ORDER BY id DESC LIMIT 1
            """,
            (chat_type.value, chat_id, f"-{seconds} seconds"),
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
        chat_id: str,
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

    def usage_today(self, chat_type: ChatType, chat_id: str, today: str) -> tuple[int, int]:
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
        chat_id: str,
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

    def usage_totals(self, start_date: str, end_date: str) -> tuple[int, int]:
        """日期闭区间内的全会话用量，供健康快照使用。"""
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
            FROM usage
            WHERE date >= ? AND date <= ?
            """,
            (start_date, end_date),
        ).fetchone()
        return int(row["calls"]), int(row["tokens"])

    def usage_between(
        self,
        start_date: str,
        end_date: str,
        *,
        purposes: Sequence[str] | None = None,
        chat_type: ChatType | None = None,
        chat_id: str | None = None,
    ) -> UsageTotals:
        """日期闭区间内的用量合计；``purposes=None`` 表示所有用途。

        WHERE 只动态拼结构，值一律走参数绑定。
        """
        clauses = ["date >= ?", "date <= ?"]
        params: list[object] = [start_date, end_date]
        if purposes is not None:
            names = [str(name) for name in purposes]
            if not names:
                return UsageTotals(calls=0, prompt_tokens=0, completion_tokens=0)
            placeholders = ", ".join("?" for _ in names)
            clauses.append(f"purpose IN ({placeholders})")
            params.extend(names)
        if chat_type is not None:
            clauses.append("chat_type = ?")
            params.append(chat_type.value)
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        where = " AND ".join(clauses)
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(MAX(estimated), 0) AS estimated
            FROM usage
            WHERE {where}
            """,
            tuple(params),
        ).fetchone()
        return UsageTotals(
            calls=int(row["calls"] or 0),
            prompt_tokens=int(row["prompt_tokens"] or 0),
            completion_tokens=int(row["completion_tokens"] or 0),
            estimated=bool(row["estimated"]),
        )

    def usage_global_today(self, date: str) -> UsageTotals:
        """所有会话当天的对话用量（预算口径：只要 reply/ambient）。"""
        return self.usage_between(date, date, purposes=_BUDGET_PURPOSES)

    def usage_month(self, month: str) -> UsageTotals:
        """自然月对话用量；``month`` 形如 ``2026-10``（上海时区口径）。"""
        try:
            year_text, month_text = month.split("-", 1)
            year, mon = int(year_text), int(month_text)
            if not 1 <= mon <= 12:
                raise ValueError
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"月份格式必须为 YYYY-MM：{month}") from exc
        last_day = calendar.monthrange(year, mon)[1]
        start = f"{year:04d}-{mon:02d}-01"
        end = f"{year:04d}-{mon:02d}-{last_day:02d}"
        return self.usage_between(start, end, purposes=_BUDGET_PURPOSES)

    def error_stats(self, window_seconds: int) -> dict[str, Any]:
        """窗口内 brain 错误次数与最后一次时间（上海时区 ISO 串）。"""
        if window_seconds <= 0:
            return {"count": 0, "last_at": None}
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n,
                   strftime('%Y-%m-%dT%H:%M:%S', MAX(created_at), '+8 hours')
                       || '+08:00' AS last_at
            FROM usage
            WHERE purpose = 'error' AND created_at >= datetime('now', ?)
            """,
            (f"-{int(window_seconds)} seconds",),
        ).fetchone()
        last_at = row["last_at"] if row else None
        return {"count": int(row["n"] or 0) if row else 0, "last_at": last_at}

    def usage_series(self, start_date: str, end_date: str) -> list[dict[str, object]]:
        """按日落库的用量聚合；缺日由调用方补齐。"""
        rows = self._conn.execute(
            """
            SELECT date,
                   SUM(CASE WHEN purpose IN ('reply', 'ambient') THEN 1 ELSE 0 END) AS calls,
                   SUM(CASE WHEN purpose = 'error' THEN 1 ELSE 0 END) AS errors,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
            FROM usage
            WHERE date >= ? AND date <= ?
            GROUP BY date
            ORDER BY date ASC
            """,
            (start_date, end_date),
        ).fetchall()
        return [
            {
                "date": str(row["date"]),
                "calls": int(row["calls"] or 0),
                "errors": int(row["errors"] or 0),
                "tokens": int(row["tokens"] or 0),
            }
            for row in rows
        ]

    def usage_top(self, since_date: str, limit: int = 5) -> list[dict[str, object]]:
        """区间内回复量最高的会话，附带错误数与 token。"""
        rows = self._conn.execute(
            """
            SELECT chat_type, chat_id,
                   SUM(CASE WHEN purpose IN ('reply', 'ambient') THEN 1 ELSE 0 END) AS calls,
                   SUM(CASE WHEN purpose = 'error' THEN 1 ELSE 0 END) AS errors,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
            FROM usage
            WHERE date >= ?
            GROUP BY chat_type, chat_id
            HAVING calls > 0
            ORDER BY calls DESC, tokens DESC
            LIMIT ?
            """,
            (since_date, max(1, int(limit))),
        ).fetchall()
        return [
            {
                "chat_type": str(row["chat_type"]),
                "chat_id": row["chat_id"],
                "calls": int(row["calls"] or 0),
                "errors": int(row["errors"] or 0),
                "tokens": int(row["tokens"] or 0),
            }
            for row in rows
        ]

    def recent_errors(self, limit: int = 10) -> list[dict[str, object]]:
        """最近的 brain 错误时间线；created_at 是 UTC，这里直接换成上海时间展示串。"""
        rows = self._conn.execute(
            """
            SELECT chat_type, chat_id,
                   strftime('%m-%d %H:%M', created_at, '+8 hours') AS at
            FROM usage
            WHERE purpose = 'error'
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {
                "chat_type": str(row["chat_type"]),
                "chat_id": row["chat_id"],
                "at": str(row["at"] or ""),
            }
            for row in rows
        ]

    # ---- sessions / forgetting ------------------------------------
    def chat_overview(self) -> list[dict[str, object]]:
        """会话总览：消息/摘要/印象计数 + 当前 brain session 元信息。

        以消息、摘要、印象、brain_sessions 的并集为会话集合，避免“只剩摘要”的会话消失。
        按最近消息时间倒序，没有消息的排最后。
        """
        chats: dict[tuple[str, str], dict[str, object]] = {}

        def bucket(raw_type: str, raw_id: str) -> dict[str, object]:
            key = (raw_type, raw_id)
            return chats.setdefault(
                key,
                {
                    "chat_type": raw_type,
                    "chat_id": raw_id,
                    "message_count": 0,
                    "last_message_at": None,
                    "summary_count": 0,
                    "impression_count": 0,
                    "provider": None,
                    "session_id": None,
                    "session_updated_at": None,
                    "delivered_count": 0,
                    "token_estimate": 0,
                },
            )

        for row in self._conn.execute(
            """
            SELECT chat_type, chat_id, COUNT(*) AS n, MAX(timestamp) AS last_ts
            FROM messages GROUP BY chat_type, chat_id
            """
        ):
            item = bucket(row["chat_type"], row["chat_id"])
            item["message_count"] = int(row["n"])
            item["last_message_at"] = (
                None if row["last_ts"] is None else int(row["last_ts"])
            )

        for row in self._conn.execute(
            "SELECT chat_type, chat_id, COUNT(*) AS n FROM summaries GROUP BY chat_type, chat_id"
        ):
            bucket(row["chat_type"], row["chat_id"])["summary_count"] = int(row["n"])

        for row in self._conn.execute(
            "SELECT chat_type, chat_id, COUNT(*) AS n FROM impressions GROUP BY chat_type, chat_id"
        ):
            bucket(row["chat_type"], row["chat_id"])["impression_count"] = int(row["n"])

        # 同一会话可能有多个 provider 的映射；优先展示 opencode，其次最近更新的。
        sessions: dict[tuple[str, str], sqlite3.Row] = {}
        for row in self._conn.execute("SELECT * FROM brain_sessions"):
            key = (row["chat_type"], row["chat_id"])
            current = sessions.get(key)
            if current is None:
                sessions[key] = row
                continue
            if current["provider"] != _SESSION_PROVIDER and row["provider"] == _SESSION_PROVIDER:  # noqa: SIM114
                sessions[key] = row
            elif (
                current["provider"] != _SESSION_PROVIDER
                and row["provider"] != _SESSION_PROVIDER
                and str(row["updated_at"]) > str(current["updated_at"])
            ):
                sessions[key] = row
        for (raw_type, raw_id), row in sessions.items():
            item = bucket(raw_type, raw_id)
            item["provider"] = row["provider"]
            item["session_id"] = row["session_id"]
            item["session_updated_at"] = row["updated_at"]
            item["delivered_count"] = int(row["delivered_count"] or 0)
            item["token_estimate"] = int(row["token_estimate"] or 0)

        return sorted(
            chats.values(),
            key=lambda item: (
                int(item["last_message_at"] or 0),
                int(item["message_count"]),
            ),
            reverse=True,
        )

    def forget_messages(self, chat_type: ChatType, chat_id: str) -> None:
        """软忘记：清消息与 session 映射，保留按日摘要、印象和用量。"""
        for table in (
            "messages",
            "opencode_sessions",
            "brain_sessions",
            "delivered_cursors",
            "delivered_messages",
            "pending_outbox",
            "session_summaries",
        ):
            self._conn.execute(
                f"DELETE FROM {table} WHERE chat_type = ? AND chat_id = ?",
                (chat_type.value, chat_id),
            )
        self._conn.commit()

    def reset_chat(self, chat_type: ChatType, chat_id: str) -> None:
        """只换 session：消息、摘要、印象、用量全部保留。"""
        for table in ("opencode_sessions", "brain_sessions", "session_summaries"):
            self._conn.execute(
                f"DELETE FROM {table} WHERE chat_type = ? AND chat_id = ?",
                (chat_type.value, chat_id),
            )
        self._conn.commit()

    def forget_chat(self, chat_type: ChatType, chat_id: str) -> None:
        for table in (
            "messages",
            "impressions",
            "summaries",
            "usage",
            "opencode_sessions",
            "brain_sessions",
            "delivered_cursors",
            "delivered_messages",
            "pending_outbox",
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
            "pending_outbox",
            "session_summaries",
        ):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()
