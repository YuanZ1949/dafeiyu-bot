"""决策流水表的建表、读写与窗口统计。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.init_schema()
    return store


def test_decisions_table_is_created(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    row = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'decisions'"
    ).fetchone()

    assert row is not None


def test_record_and_list_roundtrip(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    row_id = store.record_decision(
        ChatType.GROUP,
        "qq:1001",
        outcome="silent",
        message_ids=["m1", "m2"],
        addressed=True,
        read="读到的内容",
        think="想到的内容",
        reason="不想接话",
        reply_preview="不该出现",
        detail={"scope": "per_chat"},
    )

    assert row_id > 0
    items = store.list_decisions(limit=10)
    assert len(items) == 1
    item = items[0]
    assert item["id"] == row_id
    assert item["outcome"] == "silent"
    assert item["message_ids"] == ["m1", "m2"]
    assert item["detail"] == {"scope": "per_chat"}
    assert item["addressed"] is True
    assert item["read"] == "读到的内容"
    assert item["think"] == "想到的内容"
    assert item["at"].endswith("+08:00")


def test_list_filters_and_limit(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_decision(ChatType.GROUP, "qq:1001", outcome="replied")
    store.record_decision(ChatType.GROUP, "qq:1001", outcome="silent")
    store.record_decision(ChatType.PRIVATE, "qq:2002", outcome="silent")
    store.record_decision(ChatType.GROUP, "qq:1003", outcome="blocked")

    by_outcome = store.list_decisions(outcome="silent")
    assert len(by_outcome) == 2
    assert {item["chat_id"] for item in by_outcome} == {"qq:1001", "qq:2002"}

    by_chat = store.list_decisions(
        chat_type=ChatType.GROUP, chat_id="qq:1001"
    )
    assert len(by_chat) == 2
    assert all(item["chat_type"] == "group" for item in by_chat)

    # limit 夹在 1..200：0 也至少返回一条。
    assert len(store.list_decisions(limit=0)) == 1
    assert len(store.list_decisions(limit=999)) == 4


def test_list_parses_bad_json_as_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_decision(ChatType.GROUP, "qq:1001", outcome="replied")
    store._conn.execute(
        "UPDATE decisions SET detail = 'not-json', message_ids = 'not-json'"
    )
    store._conn.commit()

    item = store.list_decisions()[0]

    assert item["detail"] == {}
    assert item["message_ids"] == []
    assert item["messages"] == []


def test_list_decisions_attaches_seen_messages_in_order(tmp_path: Path) -> None:
    """思考回放：按 message_ids 原顺序带出消息，缺 ID 跳过，bot 标记保留。"""
    store = make_store(tmp_path)
    now = int(time.time())
    for message_id, nickname, content, is_bot in (
        ("m1", "阿猫", "今晚吃啥", False),
        ("m2", "小北", "火锅吧", False),
        ("m3", "大肥鱼", "我投火锅一票", True),
    ):
        assert store.add_message(
            Message(
                chat_type=ChatType.GROUP,
                chat_id="qq:1001",
                user_id=f"u-{nickname}",
                nickname=nickname,
                content=content,
                message_id=message_id,
                is_bot=is_bot,
                timestamp=now,
            )
        )
    # 同 message_id 在别的会话里不能串进来。
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id="qq:2002",
            user_id="u-other",
            nickname="路人",
            content="别的群",
            message_id="m1",
            timestamp=now,
        )
    )
    store.record_decision(
        ChatType.GROUP,
        "qq:1001",
        outcome="replied",
        message_ids=["m2", "m1", "missing", "m3"],
    )

    item = store.list_decisions()[0]

    assert [m["message_id"] for m in item["messages"]] == ["m2", "m1", "m3"]
    assert item["messages"][0]["content"] == "火锅吧"
    assert item["messages"][0]["is_bot"] is False
    assert item["messages"][2]["is_bot"] is True
    assert item["messages"][0]["timestamp"] == float(now)


def test_message_ids_are_capped_at_twenty(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_decision(
        ChatType.GROUP,
        "qq:1001",
        outcome="replied",
        message_ids=[f"m{i}" for i in range(30)],
    )

    item = store.list_decisions()[0]

    assert len(item["message_ids"]) == 20
    assert item["message_count"] == 20


def test_decision_stats_zero_fills_and_rates(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_decision(ChatType.GROUP, "qq:1001", outcome="replied", reason="")
    for index in range(3):
        store.record_decision(
            ChatType.GROUP, "qq:1001", outcome="silent", reason="热聊"
        )

    stats = store.decision_stats(days=1, now_ts=time.time())

    assert stats["total"] == 4
    assert stats["by_outcome"] == {
        "replied": 1,
        "silent": 3,
        "blocked": 0,
        "held": 0,
        "error": 0,
    }
    assert stats["reply_rate"] == 0.25
    assert stats["silence_rate"] == 0.75
    assert stats["top_reasons"] == [{"reason": "热聊", "count": 3}]
    assert stats["by_checkpoint"] == {}


def test_decision_stats_aggregates_checkpoint_and_latency(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_decision(
        ChatType.GROUP,
        "qq:1001",
        outcome="blocked",
        checkpoint="budget",
        latency_ms=100,
        prompt_tokens=10,
        completion_tokens=5,
    )
    store.record_decision(
        ChatType.GROUP,
        "qq:1001",
        outcome="blocked",
        checkpoint="budget",
        latency_ms=300,
        prompt_tokens=20,
        completion_tokens=7,
    )

    stats = store.decision_stats(days=1, now_ts=time.time())

    assert stats["by_checkpoint"] == {"budget": 2}
    assert stats["avg_latency_ms"] == 200
    assert stats["tokens"] == {"prompt": 30, "completion": 12}
    assert stats["reply_rate"] == 0.0
    assert stats["silence_rate"] == 0.0


def test_decision_stats_respects_window(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_decision(ChatType.GROUP, "qq:1001", outcome="replied")
    store._conn.execute(
        "UPDATE decisions SET created_at = ?", (time.time() - 3 * 86400,)
    )
    store._conn.commit()

    assert store.decision_stats(days=1, now_ts=time.time())["total"] == 0
    assert store.decision_stats(days=7, now_ts=time.time())["total"] == 1


def test_held_decisions_are_throttled_per_chat_and_checkpoint(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    first = store.record_decision(
        ChatType.GROUP, "qq:1001", outcome="held", checkpoint="opportunity_hold"
    )
    second = store.record_decision(
        ChatType.GROUP, "qq:1001", outcome="held", checkpoint="opportunity_hold"
    )
    other_chat = store.record_decision(
        ChatType.GROUP, "qq:2002", outcome="held", checkpoint="opportunity_hold"
    )
    other_reason = store.record_decision(
        ChatType.GROUP, "qq:1001", outcome="held", checkpoint="intensity_quiet"
    )

    assert first > 0
    assert second == 0
    assert other_chat > 0
    assert other_reason > 0
    assert len(store.list_decisions()) == 3

    # 超过 60 秒后同因可以再记一条。
    store._conn.execute(
        "UPDATE decisions SET created_at = ? WHERE id = ?",
        (time.time() - 61, first),
    )
    store._conn.commit()
    assert (
        store.record_decision(
            ChatType.GROUP, "qq:1001", outcome="held", checkpoint="opportunity_hold"
        )
        > 0
    )


def test_mark_decision_retried_updates_failed_row(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    row_id = store.record_decision(
        ChatType.GROUP,
        "qq:1001",
        outcome="error",
        checkpoint="send_failed",
        message_ids=["m1"],
        reply_preview="在呢",
        detail={"existing": 1},
    )

    updated = store.mark_decision_retried(
        ChatType.GROUP,
        "qq:1001",
        "m1",
        retry_after_seconds=12,
        outbox_id=7,
    )

    assert updated == row_id
    items = store.list_decisions()
    # 原地更新，不新增行：一次决策只占一行。
    assert len(items) == 1
    item = items[0]
    assert item["outcome"] == "replied"
    assert item["checkpoint"] == ""
    assert item["detail"] == {
        "existing": 1,
        "retried": True,
        "retry_after_seconds": 12,
        "outbox_id": 7,
    }


def test_mark_decision_retried_ignores_unmatched_rows(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_decision(
        ChatType.GROUP,
        "qq:1001",
        outcome="error",
        checkpoint="send_failed",
        message_ids=["m1"],
    )

    assert (
        store.mark_decision_retried(
            ChatType.GROUP, "qq:1001", "other", retry_after_seconds=1
        )
        == 0
    )
    # 不同会话 / 非 send_failed 都不该被改写。
    assert (
        store.mark_decision_retried(
            ChatType.GROUP, "qq:2002", "m1", retry_after_seconds=1
        )
        == 0
    )
    assert store.list_decisions()[0]["outcome"] == "error"


def test_init_schema_upgrades_old_database(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_type TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            content TEXT NOT NULL,
            is_bot INTEGER NOT NULL DEFAULT 0,
            timestamp INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (chat_type, chat_id, message_id)
        );
        PRAGMA user_version = 2;
        """
    )
    conn.close()

    store = Store(path)
    store.init_schema()

    row = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'decisions'"
    ).fetchone()
    assert row is not None
    assert store.record_decision(ChatType.GROUP, "qq:1001", outcome="replied") > 0
