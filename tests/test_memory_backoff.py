"""记忆旁路的失败退避：配额熔断要跨会话、跨重启生效。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from dafeiyu.brain.client import BrainError
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import SHANGHAI
from dafeiyu.workers.failure import QUOTA_PAUSE_SECONDS, FailureBackoff
from dafeiyu.workers.impressions import ImpressionWorker
from dafeiyu.workers.summary import SummaryScheduler

QUOTA = BrainError("provider.quota: 免费额度已用完")


class Clock:
    def __init__(self, mono: float = 1000.0, wall: float = 1_700_000_000.0) -> None:
        self.mono = mono
        self.wall = wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


def make_store(tmp_path: Path | None = None) -> Store:
    store = Store(tmp_path / "backoff.db" if tmp_path else ":memory:")
    store.init_schema()
    return store


def test_quota_error_pauses_every_chat(tmp_path: Path) -> None:
    store = make_store()
    clock = Clock()
    backoff = FailureBackoff(monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall)

    assert not backoff.is_paused()
    backoff.mark_failed((ChatType.GROUP, 1), QUOTA)

    assert backoff.is_paused()
    assert backoff.should_skip((ChatType.GROUP, 2))
    assert backoff.should_skip((ChatType.PRIVATE, 3))

    clock.advance(QUOTA_PAUSE_SECONDS - 1)
    assert backoff.is_paused()

    clock.advance(2)
    assert not backoff.is_paused()
    assert not backoff.should_skip((ChatType.GROUP, 2))
    store.close()


def test_quota_pause_survives_restart(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    clock = Clock()
    first = FailureBackoff(
        monotonic=lambda: clock.mono,
        wall_clock=lambda: clock.wall,
        persistence=store,
    )
    first.mark_failed((ChatType.GROUP, 1), QUOTA)

    second = FailureBackoff(
        monotonic=lambda: clock.mono,
        wall_clock=lambda: clock.wall,
        persistence=store,
    )
    assert second.is_paused()

    clock.advance(QUOTA_PAUSE_SECONDS + 1)
    assert not second.is_paused()
    assert store.load_backoff() == {}
    store.close()


def test_per_chat_backoff_also_persists(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    clock = Clock()
    first = FailureBackoff(
        monotonic=lambda: clock.mono,
        wall_clock=lambda: clock.wall,
        persistence=store,
    )
    first.mark_failed((ChatType.GROUP, 7), BrainError("network down"))

    second = FailureBackoff(
        monotonic=lambda: clock.mono,
        wall_clock=lambda: clock.wall,
        persistence=store,
    )
    assert second.should_skip((ChatType.GROUP, 7))
    assert not second.is_paused()

    second.mark_ok((ChatType.GROUP, 7))
    assert not second.should_skip((ChatType.GROUP, 7))
    assert store.load_backoff() == {}
    store.close()


def test_success_clears_quota_pause() -> None:
    clock = Clock()
    backoff = FailureBackoff(monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall)
    backoff.mark_failed((ChatType.GROUP, 1), QUOTA)
    assert backoff.is_paused()

    backoff.mark_ok((ChatType.GROUP, 9))
    assert not backoff.is_paused()


class QuotaBrain:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_detailed(self, system, messages, max_tokens=400, temperature=0.2):
        self.calls += 1
        raise QUOTA


@pytest.mark.asyncio
async def test_scheduler_stops_after_quota_error() -> None:
    store = make_store()
    ts = int(datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI).timestamp())
    for chat_id in (1, 2):
        store.add_message(
            Message(ChatType.GROUP, chat_id, 1, "鱼片", "在吗", f"m{chat_id}", timestamp=ts)
        )
    brain = QuotaBrain()
    clock = datetime(2026, 10, 25, 5, 0, tzinfo=SHANGHAI)
    scheduler = SummaryScheduler(
        store,
        brain,
        clock=lambda: clock,
        backoff=FailureBackoff(persistence=store),
    )

    created = await scheduler.run_pending()

    assert created == 0
    assert brain.calls == 1  # 第二个会话被全局熔断拦住
    assert scheduler._backoff.is_paused()
    assert "__quota__" in store.load_backoff()
    store.close()


@pytest.mark.asyncio
async def test_impressions_stop_after_quota_error() -> None:
    store = make_store()
    for chat_id in (1, 2):
        store.add_message(
            Message(ChatType.GROUP, chat_id, 1, "鱼片", "在吗", f"m{chat_id}")
        )
    brain = QuotaBrain()
    worker = ImpressionWorker(store, brain, every=1)

    assert await worker.maybe_extract(ChatType.GROUP, 1) == 0
    assert await worker.maybe_extract(ChatType.GROUP, 2) == 0

    assert brain.calls == 1
    assert worker._backoff.is_paused()
    store.close()
