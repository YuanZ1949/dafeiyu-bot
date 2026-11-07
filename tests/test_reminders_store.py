"""reminders 表与 Store API：落库、到期选取、发送/取消的终态。

先把口径钉死：
* 到期只取 status='pending'，按 (due_at, id) 排序，limit 生效；
* mark_sent 是终态（at-most-once），失败原因写在 last_error 上；
* 取消只允许创建者、只在 pending 时，越权与跨会话都返回 False。
"""

from __future__ import annotations

import time

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType

GROUP = ChatType.GROUP
CHAT = "qq:100"
CREATOR = "qq:2"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "reminders.db")
    s.init_schema()
    yield s
    s.close()


def test_add_then_due_then_sent(store: Store) -> None:
    now = time.time()
    reminder_id = store.add_reminder(GROUP, CHAT, CREATOR, "喝水", now + 60)
    assert reminder_id > 0

    listed = store.list_reminders(GROUP, CHAT)
    assert [row["id"] for row in listed] == [reminder_id]
    assert listed[0]["status"] == "pending"
    assert listed[0]["last_error"] is None

    # 还没到点：不出现。
    assert store.due_reminders(now=now) == []

    due = store.due_reminders(now=now + 120)
    assert [row["id"] for row in due] == [reminder_id]
    assert due[0]["content"] == "喝水"
    assert due[0]["chat_type"] == GROUP.value
    assert due[0]["chat_id"] == CHAT
    assert "last_error" in due[0]

    assert store.mark_reminder_sent(reminder_id) is True
    assert store.due_reminders(now=now + 3600) == []

    sent = store.list_reminders(GROUP, CHAT)[0]
    assert sent["status"] == "sent"
    assert sent["sent_at"] is not None
    # 已发是终态，重复标记不应再改一次。
    assert store.mark_reminder_sent(reminder_id) is False


def test_cancel_only_by_creator(store: Store) -> None:
    reminder_id = store.add_reminder(GROUP, CHAT, CREATOR, "交作业", time.time() + 60)

    assert store.cancel_reminder(reminder_id, GROUP, CHAT, "qq:999") is False
    unchanged = store.list_reminders(GROUP, CHAT)[0]
    assert unchanged["status"] == "pending"
    assert unchanged["canceled_at"] is None

    # 另一个会话里同 id 也不放行。
    assert store.cancel_reminder(reminder_id, GROUP, "qq:999", CREATOR) is False

    assert store.cancel_reminder(reminder_id, GROUP, CHAT, CREATOR) is True
    canceled = store.list_reminders(GROUP, CHAT)[0]
    assert canceled["status"] == "canceled"
    assert canceled["canceled_at"] is not None
    # 取消后不再是 pending，重复取消返回 False。
    assert store.cancel_reminder(reminder_id, GROUP, CHAT, CREATOR) is False


def test_mark_sent_records_last_error(store: Store) -> None:
    now = time.time()
    reminder_id = store.add_reminder(GROUP, CHAT, CREATOR, "开会", now + 60)

    assert store.mark_reminder_sent(reminder_id, last_error="boom") is True
    row = store.list_reminders(GROUP, CHAT)[0]
    assert row["status"] == "sent"
    assert row["last_error"] == "boom"
    assert store.due_reminders(now=now + 3600) == []


def test_failure_paths(store: Store) -> None:
    assert store.cancel_reminder(999999, GROUP, CHAT, CREATOR) is False
    assert store.mark_reminder_sent(999999) is False
    assert store.due_reminders(now=time.time(), limit=0) == []
    assert store.add_reminder(GROUP, CHAT, CREATOR, "   ", time.time()) == 0


def test_due_order_and_limit(store: Store) -> None:
    now = time.time()
    late = store.add_reminder(GROUP, CHAT, CREATOR, "晚", now + 300)
    early = store.add_reminder(GROUP, CHAT, CREATOR, "早", now + 100)
    same_a = store.add_reminder(GROUP, CHAT, CREATOR, "同刻A", now + 200)
    same_b = store.add_reminder(GROUP, CHAT, CREATOR, "同刻B", now + 200)

    due = store.due_reminders(now=now + 1000)
    assert [row["id"] for row in due] == [early, same_a, same_b, late]
    assert [row["id"] for row in store.due_reminders(now=now + 1000, limit=2)] == [
        early,
        same_a,
    ]


def test_list_puts_pending_first(store: Store) -> None:
    first = store.add_reminder(GROUP, CHAT, CREATOR, "第一条", time.time() + 60)
    second = store.add_reminder(GROUP, CHAT, CREATOR, "第二条", time.time() + 60)
    assert store.cancel_reminder(first, GROUP, CHAT, CREATOR) is True

    rows = store.list_reminders(GROUP, CHAT)
    assert [row["id"] for row in rows] == [second, first]
    assert rows[0]["status"] == "pending"
    assert rows[1]["status"] == "canceled"

    # 别的会话看不到。
    assert store.list_reminders(GROUP, "qq:200") == []
