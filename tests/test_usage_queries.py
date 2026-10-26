"""usage 聚合查询：闭区间、按会话/用途过滤与索引。"""

from __future__ import annotations

import sqlite3

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init_schema()
    add(s, "2026-10-25", ChatType.GROUP, 1, "ambient", 10, 4)
    add(s, "2026-10-26", ChatType.GROUP, 1, "reply", 20, 8)
    # 非对话用途不该计入全局/月度预算。
    add(s, "2026-10-20", ChatType.GROUP, 1, "summary", 50, 50)
    yield s
    s.close()


def add(
    store: Store,
    date: str,
    chat_type: ChatType,
    chat_id: int,
    purpose: str,
    prompt: int,
    completion: int,
    estimated: int = 0,
) -> None:
    store._conn.execute(
        "INSERT INTO usage "
        "(chat_type, chat_id, date, purpose, prompt_tokens, completion_tokens, estimated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_type.value, chat_id, date, purpose, prompt, completion, estimated),
    )
    store._conn.commit()


def test_usage_between_is_closed_interval_and_filters(store):
    totals = store.usage_between(
        "2026-10-01",
        "2026-10-31",
        purposes=("reply", "ambient"),
        chat_type=ChatType.GROUP,
        chat_id=1,
    )
    assert totals.calls == 2
    assert totals.prompt_tokens == 30
    assert totals.completion_tokens == 12
    assert totals.tokens == 42
    assert totals.estimated is False


def test_usage_between_filters_chat_and_purpose(store):
    add(store, "2026-09-15", ChatType.GROUP, 2, "reply", 5, 5)
    add(store, "2026-09-16", ChatType.GROUP, 1, "reply", 5, 5, estimated=1)
    add(store, "2026-09-16", ChatType.PRIVATE, 1, "reply", 7, 7)
    add(store, "2026-09-17", ChatType.GROUP, 1, "report", 9, 9)

    group_one = store.usage_between(
        "2026-09-01",
        "2026-09-30",
        purposes=("reply",),
        chat_type=ChatType.GROUP,
        chat_id=1,
    )
    assert (group_one.calls, group_one.prompt_tokens, group_one.estimated) == (1, 5, True)

    private_one = store.usage_between(
        "2026-09-01",
        "2026-09-30",
        purposes=("reply",),
        chat_type=ChatType.PRIVATE,
        chat_id=1,
    )
    assert (private_one.calls, private_one.prompt_tokens) == (1, 7)

    # 闭区间：端点当天算在内，区间外不算。
    assert store.usage_between("2026-09-16", "2026-09-16").calls == 2
    assert store.usage_between("2026-09-18", "2026-09-30").calls == 0


def test_usage_between_empty_purposes_returns_zero(store):
    totals = store.usage_between("2026-10-01", "2026-10-31", purposes=())
    assert (totals.calls, totals.tokens) == (0, 0)


def test_usage_month_and_global_today(store):
    assert store.usage_global_today("2026-10-26").calls == 1
    assert store.usage_month("2026-10").calls == 2
    assert store.usage_month("2026-10").tokens == 42


def test_usage_date_index_exists(store):
    row = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_usage_date'"
    ).fetchone()
    assert row is not None


def test_usage_date_index_added_to_old_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE usage ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "chat_type TEXT NOT NULL,"
        "chat_id INTEGER NOT NULL,"
        "date TEXT NOT NULL,"
        "purpose TEXT NOT NULL,"
        "prompt_tokens INTEGER NOT NULL DEFAULT 0,"
        "completion_tokens INTEGER NOT NULL DEFAULT 0,"
        "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    store.init_schema()
    try:
        row = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_usage_date'"
        ).fetchone()
        assert row is not None
    finally:
        store.close()
