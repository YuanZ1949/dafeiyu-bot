from datetime import datetime, timedelta, timezone

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.workers.summary import SummaryScheduler, summarize_chat

TZ = timezone(timedelta(hours=8))
YESTERDAY = "2026-10-23"
NOW = datetime(2026, 10, 24, 4, 0, tzinfo=TZ)
BEFORE = datetime(2026, 10, 24, 3, 59, tzinfo=TZ)


class FakeBrain:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def complete(self, system, messages, max_tokens=400, temperature=0.2) -> str:
        self.calls.append({"system": system, "messages": messages})
        return self.reply


def make_store() -> Store:
    store = Store(":memory:")
    store.init_schema()
    return store


def seed(store: Store, ts: datetime, content: str = "今天聊了显卡", chat_id: int = 9001) -> None:
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=chat_id,
            user_id=42,
            nickname="鱼片",
            content=content,
            message_id=f"m{int(ts.timestamp())}",
            timestamp=int(ts.timestamp()),
        )
    )


@pytest.mark.asyncio
async def test_summarize_truncates_to_200_and_stores():
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    brain = FakeBrain("鱼" * 250)

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert len(text) == 200
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) == text
    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_empty_day_does_not_call_brain():
    store = make_store()
    brain = FakeBrain("不该出现")

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text == ""
    assert brain.calls == []
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) is None


@pytest.mark.asyncio
async def test_scheduler_waits_until_hour():
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    brain = FakeBrain("摘要")
    scheduler = SummaryScheduler(store, brain, clock=lambda: BEFORE, hour=4)

    assert await scheduler.run_pending() == 0
    assert brain.calls == []


@pytest.mark.asyncio
async def test_scheduler_summarizes_yesterday_and_is_idempotent():
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    brain = FakeBrain("摘要")
    scheduler = SummaryScheduler(store, brain, clock=lambda: NOW, hour=4)

    assert await scheduler.run_pending() == 1
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) == "摘要"

    assert await scheduler.run_pending() == 0
    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_scheduler_ignores_day_without_messages():
    store = make_store()
    brain = FakeBrain("摘要")
    scheduler = SummaryScheduler(store, brain, clock=lambda: NOW, hour=4)

    assert await scheduler.run_pending() == 0
    assert brain.calls == []


@pytest.mark.asyncio
async def test_scheduler_uses_brain_from_getter_after_switch():
    """dashboard 换脑只改 service.brain，scheduler 必须每轮现取，不能啃旧引用。"""
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    old = FakeBrain("旧大脑")
    new = FakeBrain("新大脑")
    current = {"brain": old}
    scheduler = SummaryScheduler(
        store,
        old,
        clock=lambda: NOW,
        hour=4,
        brain_getter=lambda: current["brain"],
    )

    current["brain"] = new  # 模拟控制口换脑
    assert await scheduler.run_pending() == 1

    assert old.calls == []
    assert len(new.calls) == 1
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) == "新大脑"
