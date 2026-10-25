import asyncio
from types import SimpleNamespace

from dafeiyu.config import TriggerConfig
from dafeiyu.delivery.scheduler import BatchScheduler
from dafeiyu.models import ChatType, Message


def cfg():
    return SimpleNamespace(
        trigger=TriggerConfig(
            cooldown_seconds=30,
            sample_rate=0.1,
            quiet_hours=("23:00", "08:00"),
            keywords=("大肥鱼",),
        )
    )


def make_msg(idx, *, chat_type=ChatType.GROUP, chat_id=1):
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=1001,
        nickname="阿猫",
        content=f"m{idx}",
        message_id=f"m{idx}",
        timestamp=idx,
    )


def test_tick_returns_due_chats_once():
    now = [0.0]

    async def fake_flush(chat_type, chat_id):
        raise AssertionError("tick 不该直接执行投递")

    s = BatchScheduler(None, cfg(), on_flush=fake_flush, clock=lambda: now[0])
    s.note(make_msg(0), addressed=False)
    assert s.tick() == []

    now[0] = 46.0
    assert s.tick() == [(ChatType.GROUP, 1)]
    # 已置 inflight，下一拍不能重复触发。
    assert s.tick() == []


def test_resume_schedules_immediately():
    now = [0.0]

    async def fake_flush(chat_type, chat_id):
        return None

    s = BatchScheduler(None, cfg(), on_flush=fake_flush, clock=lambda: now[0])
    s.resume([(ChatType.GROUP, 1)])
    assert s.tick() == [(ChatType.GROUP, 1)]


async def test_run_flushes_then_clears_window():
    now = [0.0]
    seen = []

    async def fake_flush(chat_type, chat_id):
        seen.append((chat_type, chat_id))

    s = BatchScheduler(None, cfg(), on_flush=fake_flush, clock=lambda: now[0])
    s.note(make_msg(0), addressed=False)
    now[0] = 46.0

    task = asyncio.create_task(s.run(interval=0.01))
    try:
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()

    assert seen == [(ChatType.GROUP, 1)]
    # 投递完窗口清空，非点名投递记了 last_ambient_at，下一拍无事可做。
    assert s.tick() == []


async def test_note_during_flush_keeps_new_window():
    now = [0.0]
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_flush(chat_type, chat_id):
        started.set()
        await release.wait()

    s = BatchScheduler(None, cfg(), on_flush=slow_flush, clock=lambda: now[0])
    s.note(make_msg(0), addressed=False)
    now[0] = 46.0

    task = asyncio.create_task(s.run(interval=0.01))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        # 投递期间又来了一条点名消息：不能被 on_flushed 抹掉。
        now[0] = 50.0
        s.note(make_msg(1), addressed=True)
        release.set()
        await asyncio.sleep(0.1)
    finally:
        task.cancel()

    # 新窗口还在，防抖到 58s，现在 50s 还不到点。
    assert s.tick() == []

    now[0] = 60.0
    assert s.tick() == [(ChatType.GROUP, 1)]
