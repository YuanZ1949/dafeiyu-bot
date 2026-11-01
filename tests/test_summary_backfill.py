"""摘要缺口回补：worker 不能只盯「昨天」。

线上真事：某天配额耗尽，当天摘要没生成；第二天 worker 的目标日期前移，
那一整天就永久留空（release 库里 10-27 两个群就是这样）。这里钉住三件事：
更早的缺口会被补、每轮有上限、今天不算缺口。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.workers.summary import SummaryScheduler

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 24, 5, 0, tzinfo=TZ)


class FakeBrain:
    def __init__(self, reply: str = "摘要") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def complete(self, system, messages, max_tokens=400, temperature=0.2) -> str:
        self.calls.append({"system": system, "messages": messages})
        return self.reply


def make_store() -> Store:
    store = Store(":memory:")
    store.init_schema()
    return store


def seed(store: Store, when: datetime, chat_id: int = 9001, text: str = "聊了显卡") -> None:
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=chat_id,
            user_id=42,
            nickname="鱼片",
            content=text,
            message_id=f"{chat_id}-{int(when.timestamp())}",
            timestamp=int(when.timestamp()),
        )
    )


def at(month: int, day: int, hour: int = 10) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=TZ)


@pytest.mark.asyncio
async def test_scheduler_backfills_day_older_than_yesterday():
    """10-21 那天失败了，到了 10-24 仍要补上，而不是只补 10-23。"""
    store = make_store()
    seed(store, at(10, 21))
    seed(store, at(10, 23))
    brain = FakeBrain()
    scheduler = SummaryScheduler(store, brain, clock=lambda: NOW, hour=4)

    created = await scheduler.run_pending()

    assert created == 2
    assert store.get_summary(ChatType.GROUP, 9001, "2026-10-21") == "摘要"
    assert store.get_summary(ChatType.GROUP, 9001, "2026-10-23") == "摘要"


@pytest.mark.asyncio
async def test_scheduler_backfills_at_most_three_slots_per_run():
    """一次补太多会顶满当轮配额；剩下的留给下一轮，而不是全堆在一次调用里。"""
    store = make_store()
    for offset, chat_id in enumerate((9101, 9102, 9103, 9104)):
        seed(store, at(10, 20) - timedelta(days=offset), chat_id=chat_id)
    brain = FakeBrain()
    scheduler = SummaryScheduler(store, brain, clock=lambda: NOW, hour=4)

    assert await scheduler.run_pending() == 3
    # 第二轮：1 个日摘要缺口 + 2 个已结束周的周汇总（与日摘要共享每轮上限）。
    assert await scheduler.run_pending() == 3
    assert len(brain.calls) == 6
    assert store.count_summaries() == 4


@pytest.mark.asyncio
async def test_scheduler_backfills_gap_older_than_a_week():
    """N4：回补窗口 14 天，10 天前的缺口也要补上。"""
    store = make_store()
    seed(store, at(10, 14))
    brain = FakeBrain()
    scheduler = SummaryScheduler(store, brain, clock=lambda: NOW, hour=4)

    await scheduler.run_pending()

    assert store.get_summary(ChatType.GROUP, 9001, "2026-10-14") == "摘要"


@pytest.mark.asyncio
async def test_scheduler_never_summarizes_today():
    store = make_store()
    seed(store, at(10, 24, hour=1))
    brain = FakeBrain()
    scheduler = SummaryScheduler(store, brain, clock=lambda: NOW, hour=4)

    assert await scheduler.run_pending() == 0
    assert brain.calls == []


def test_pending_slots_carry_chat_and_message_count() -> None:
    """面板要能说清「哪个群、哪天、多少条」，只给日期不够用。"""
    store = make_store()
    yesterday = datetime.now(TZ) - timedelta(days=1)
    day = yesterday.replace(hour=10, minute=0, second=0, microsecond=0)
    for i in range(3):
        seed(store, day, chat_id=100, text=f"消息{i}")
    seed(store, day, chat_id=200)
    store.upsert_summary(ChatType.GROUP, 100, day.date().isoformat(), "已有摘要")

    slots = store.pending_summary_slots()

    assert [(s.chat_id, s.date, s.messages) for s in slots] == [
        ("200", day.date().isoformat(), 1)
    ]
