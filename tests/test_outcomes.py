"""结果打标：纯 SQL 回算自己的发言有没有人接。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.workers.outcome import OutcomeWorker, outcome_loop

GROUP = ChatType.GROUP
CHAT = "qq:1"
BOT = "qq:999"


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "outcome.db")
    s.init_schema()
    yield s
    s.close()


def add(
    store: Store,
    message_id: str,
    timestamp: int,
    *,
    user_id: str = "qq:2",
    content: str = "接一句",
    is_bot: bool = False,
    reply_to_message_id: str | None = None,
    reply_to_bot: bool = False,
) -> None:
    store.add_message(
        Message(
            chat_type=GROUP,
            chat_id=CHAT,
            user_id=user_id,
            nickname="阿猫",
            content=content,
            message_id=message_id,
            is_bot=is_bot,
            reply_to_message_id=reply_to_message_id,
            reply_to_bot=reply_to_bot,
            timestamp=timestamp,
        )
    )


def make_worker(store: Store, now: int = 2000) -> OutcomeWorker:
    return OutcomeWorker(store, now=lambda: now)


def stats(store: Store) -> dict[str, int]:
    return store.reply_outcome_stats(GROUP, CHAT, now_ts=2000)


def test_direct_reply_marks_engaged_and_quoted(store: Store) -> None:
    add(store, "b1", 1000, user_id=BOT, is_bot=True, content="买冻干吧")
    add(store, "u1", 1050, reply_to_message_id="b1")

    assert make_worker(store).run_once() == 1

    result = stats(store)
    assert result["total"] == 1
    assert result["engaged"] == 1
    assert result["quoted"] == 1
    assert result["replied"] == 1
    assert result["silent"] == 0


def test_reply_to_bot_without_quote_still_counts_as_replied(store: Store) -> None:
    add(store, "b1", 1000, user_id=BOT, is_bot=True, content="买冻干吧")
    add(store, "u1", 1040, reply_to_bot=True)

    make_worker(store).run_once()

    result = stats(store)
    assert result["engaged"] == 1
    assert result["quoted"] == 0
    assert result["replied"] == 1


def test_silence_after_is_recorded(store: Store) -> None:
    add(store, "b1", 1000, user_id=BOT, is_bot=True, content="有人吗")

    make_worker(store).run_once()

    result = stats(store)
    assert result["total"] == 1
    assert result["engaged"] == 0
    assert result["silent"] == 1


def test_fresh_bot_message_waits_for_window(store: Store) -> None:
    add(store, "b1", 1900, user_id=BOT, is_bot=True, content="刚说完")

    assert make_worker(store, now=2000).run_once() == 0
    assert stats(store)["total"] == 0


def test_rerun_is_idempotent(store: Store) -> None:
    add(store, "b1", 1000, user_id=BOT, is_bot=True, content="买冻干吧")
    add(store, "u1", 1050, reply_to_message_id="b1")
    worker = make_worker(store)

    worker.run_once()
    worker.run_once()

    result = stats(store)
    assert result["total"] == 1
    assert result["engaged"] == 1


def test_window_excludes_late_messages(store: Store) -> None:
    add(store, "b1", 1000, user_id=BOT, is_bot=True, content="买冻干吧")
    add(store, "u1", 1300)  # 窗口 180 秒，太晚了

    make_worker(store, now=3000).run_once()

    assert stats(store)["silent"] == 1


def test_bot_messages_do_not_count_as_engagement(store: Store) -> None:
    add(store, "b1", 1000, user_id=BOT, is_bot=True, content="买冻干吧")
    add(store, "b2", 1050, user_id=BOT, is_bot=True, content="另一句")

    make_worker(store).run_once()

    assert stats(store)["silent"] == 2


async def test_outcome_loop_stops(store: Store) -> None:
    worker = make_worker(store)
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(outcome_loop(worker, interval=0.01, stop=stop), timeout=1)
