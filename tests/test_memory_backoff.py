"""记忆旁路的失败退避：配额熔断要跨会话、跨重启生效。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dafeiyu.brain.client import BrainError
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import SHANGHAI
from dafeiyu.workers.failure import (
    QUOTA_PAUSE_SECONDS,
    RATE_LIMIT_BACKOFF_SECONDS,
    RATE_LIMIT_WINDOW_SECONDS,
    FailureBackoff,
)
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
    assert store.load_rate_limits() == []
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


RATE_LIMITED = BrainError("DeepSeek 限流（429），本条放弃")


def test_rate_limit_detection() -> None:
    from dafeiyu.workers.failure import is_rate_limit_error

    assert is_rate_limit_error(BrainError("HTTP 429"))
    assert is_rate_limit_error(BrainError("限流了，稍后再试", status=429))
    assert is_rate_limit_error(BrainError("Too Many Requests"))
    assert is_rate_limit_error(SimpleNamespace(status=429))
    assert is_rate_limit_error(BrainError("boom", status="429"))  # 类型不严谨的 provider
    assert not is_rate_limit_error(BrainError("network down"))
    assert not is_rate_limit_error(None)


def test_single_429_only_cools_that_chat() -> None:
    clock = Clock()
    backoff = FailureBackoff(monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall)

    backoff.mark_failed((ChatType.GROUP, 1), RATE_LIMITED)

    assert not backoff.is_paused()
    assert backoff.should_skip((ChatType.GROUP, 1))
    assert not backoff.should_skip((ChatType.GROUP, 2))


def test_three_consecutive_429_pause_globally(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    clock = Clock()
    backoff = FailureBackoff(
        monotonic=lambda: clock.mono,
        wall_clock=lambda: clock.wall,
        persistence=store,
    )

    backoff.mark_failed((ChatType.GROUP, 1), RATE_LIMITED)
    backoff.mark_failed((ChatType.GROUP, 2), RATE_LIMITED)
    assert not backoff.is_paused()

    backoff.mark_failed((ChatType.GROUP, 3), RATE_LIMITED)

    assert backoff.is_paused()
    assert backoff.should_skip((ChatType.GROUP, 99))
    assert "__quota__" in store.load_backoff()

    clock.advance(QUOTA_PAUSE_SECONDS + 1)
    assert not backoff.is_paused()
    store.close()


def test_success_does_not_erase_recent_429s() -> None:
    """多会话轮流撞、中间夹一次成功，仍然算账号级限流。"""
    clock = Clock()
    backoff = FailureBackoff(monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall)

    backoff.mark_failed((ChatType.GROUP, 1), RATE_LIMITED)
    backoff.mark_failed((ChatType.GROUP, 2), RATE_LIMITED)
    backoff.mark_ok((ChatType.GROUP, 2))
    backoff.mark_failed((ChatType.GROUP, 3), RATE_LIMITED)

    assert backoff.is_paused()


def test_non_429_failure_does_not_erase_recent_429s() -> None:
    clock = Clock()
    backoff = FailureBackoff(monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall)

    backoff.mark_failed((ChatType.GROUP, 1), RATE_LIMITED)
    backoff.mark_failed((ChatType.GROUP, 2), RATE_LIMITED)
    backoff.mark_failed((ChatType.GROUP, 3), BrainError("network down"))
    backoff.mark_failed((ChatType.GROUP, 4), RATE_LIMITED)

    assert backoff.is_paused()


def test_single_429_cools_only_five_minutes() -> None:
    clock = Clock()
    backoff = FailureBackoff(monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall)

    backoff.mark_failed((ChatType.GROUP, 1), RATE_LIMITED)

    clock.advance(RATE_LIMIT_BACKOFF_SECONDS + 1)
    assert not backoff.should_skip((ChatType.GROUP, 1))


def test_429_events_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "streak.db"
    clock = Clock()
    store = Store(path)
    store.init_schema()
    first = FailureBackoff(
        monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall, persistence=store
    )
    first.mark_failed((ChatType.GROUP, 1), RATE_LIMITED)
    first.mark_failed((ChatType.GROUP, 2), RATE_LIMITED)
    assert len(store.load_rate_limits()) == 2
    store.close()

    clock.advance(30)
    store = Store(path)
    store.init_schema()
    second = FailureBackoff(
        monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall, persistence=store
    )
    second.mark_failed((ChatType.GROUP, 3), RATE_LIMITED)

    assert second.is_paused()
    assert store.load_rate_limits() == []
    store.close()


def test_stale_429_events_are_discarded(tmp_path: Path) -> None:
    path = tmp_path / "stale.db"
    clock = Clock()
    store = Store(path)
    store.init_schema()
    first = FailureBackoff(
        monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall, persistence=store
    )
    first.mark_failed((ChatType.GROUP, 1), RATE_LIMITED)
    first.mark_failed((ChatType.GROUP, 2), RATE_LIMITED)
    store.close()

    clock.advance(RATE_LIMIT_WINDOW_SECONDS + 1)
    store = Store(path)
    store.init_schema()
    second = FailureBackoff(
        monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall, persistence=store
    )
    second.mark_failed((ChatType.GROUP, 3), RATE_LIMITED)
    second.mark_failed((ChatType.GROUP, 4), RATE_LIMITED)

    assert not second.is_paused()
    assert len(store.load_rate_limits()) == 2
    store.close()


def test_429_events_expire_from_window_in_process() -> None:
    """进程内也要按窗口淘汰：昨天的两次不该跟今天的一次凑成三次。"""
    clock = Clock()
    backoff = FailureBackoff(monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall)

    backoff.mark_failed((ChatType.GROUP, 1), RATE_LIMITED)
    backoff.mark_failed((ChatType.GROUP, 2), RATE_LIMITED)
    clock.advance(RATE_LIMIT_WINDOW_SECONDS + 1)
    backoff.mark_failed((ChatType.GROUP, 3), RATE_LIMITED)

    assert not backoff.is_paused()


def test_future_events_from_clock_skew_still_expire(tmp_path: Path) -> None:
    """墙钟被往回拨时，事件时间会落在未来；夹到 now 才不会永不过期。"""
    store = make_store(tmp_path)
    clock = Clock()
    store.save_rate_limits([clock.wall + 3600] * 2, clock.wall + 3600)

    backoff = FailureBackoff(
        monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall, persistence=store
    )
    clock.advance(RATE_LIMIT_WINDOW_SECONDS + 1)
    backoff.mark_failed((ChatType.GROUP, 1), RATE_LIMITED)

    assert not backoff.is_paused()


def test_trip_logs_why_global_pause_started(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """误停时需要能从日志看出是「窗口内 429 累计」，不是配额或代码 bug。"""
    clock = Clock()
    backoff = FailureBackoff(monotonic=lambda: clock.mono, wall_clock=lambda: clock.wall)

    with caplog.at_level(logging.WARNING, logger="dafeiyu.workers.failure"):
        for chat_id in (1, 2, 3):
            backoff.mark_failed((ChatType.GROUP, chat_id), RATE_LIMITED)

    assert backoff.is_paused()
    trips = [r for r in caplog.records if "429" in r.getMessage()]
    assert len(trips) == 1
    assert "阈值 3" in trips[0].getMessage()
