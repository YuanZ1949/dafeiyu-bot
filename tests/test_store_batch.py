from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message


def make_msg(
    idx: int,
    *,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: int = 1,
    ts: int = 0,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=2,
        nickname="鱼片",
        content=f"第{idx}句",
        message_id=f"m{idx}",
        timestamp=ts or 10_000 + idx,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "test.db")
    s.init_schema()
    yield s
    s.close()


def test_pending_batch_takes_oldest_not_newest(store: Store) -> None:
    for i in range(25):
        store.add_message(make_msg(i))
    store.mark_delivered(ChatType.GROUP, 1, "m0")  # 钉住游标

    batch = store.pending_batch(ChatType.GROUP, 1, anchor_id="m24", limit=20)
    assert [m.message_id for m in batch] == [f"m{i}" for i in range(1, 21)]


def test_first_delivery_does_not_backfill(store: Store) -> None:
    for i in range(5):
        store.add_message(make_msg(i))

    assert [m.message_id for m in store.pending_batch(ChatType.GROUP, 1, anchor_id="m4")] == [
        "m4"
    ]


def test_chats_with_pending_respects_window(store: Store) -> None:
    store.add_message(make_msg(0, ts=1000))  # 老消息，且没有游标
    store.add_message(make_msg(1, ts=10_000))

    assert store.chats_with_pending(since_ts=9000) == []

    store.mark_delivered(ChatType.GROUP, 1, "m0")
    assert store.chats_with_pending(since_ts=9000) == [(ChatType.GROUP, 1)]
    assert store.chats_with_pending(since_ts=20_000) == []


def test_mark_skipped_records_reason(store: Store) -> None:
    store.add_message(make_msg(0))

    store.mark_skipped(ChatType.GROUP, 1, ["m0"], "budget")

    assert store.skip_reason(ChatType.GROUP, 1, "m0") == "budget"
    assert store.pending_batch(ChatType.GROUP, 1, anchor_id="m0") == []
    assert store.last_delivered_id(ChatType.GROUP, 1) is not None


def test_usage_today_by_purpose_filters(store: Store) -> None:
    store.record_usage(ChatType.GROUP, 1, "ambient", 10, 5)
    store.record_usage(ChatType.GROUP, 1, "ambient", 1, 1)
    store.record_usage(ChatType.GROUP, 1, "summary", 100, 50)

    assert store.usage_today_by_purpose(
        ChatType.GROUP, 1, store.today(), ("ambient",)
    ) == (2, 17)
    assert store.usage_today_by_purpose(
        ChatType.GROUP, 1, store.today(), ("reply", "ambient")
    ) == (2, 17)


def test_forget_chat_clears_session_summaries(store: Store) -> None:
    store.add_session_summary(ChatType.GROUP, 1, "x")

    store.forget_chat(ChatType.GROUP, 1)

    assert store.latest_session_summary(ChatType.GROUP, 1) is None
