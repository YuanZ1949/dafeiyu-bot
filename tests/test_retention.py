from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import TEST_CONFIG_YAML

from dafeiyu.config import ConfigError, load_settings
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.workers.retention import prune_once, retention_loop

GROUP = ChatType.GROUP
CHAT_ID = 100
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def load(tmp_path: Path, retention: int | None = None):
    raw = TEST_CONFIG_YAML
    if retention is not None:
        raw = raw.replace(
            "  window_size: 20",
            f"  window_size: 20\n  retention_days: {retention}",
        )
    config = tmp_path / "config.yaml"
    config.write_text(raw, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    return load_settings(env_file=env, config_file=config)


def add_message(store: Store, message_id: str, timestamp: int) -> None:
    store.add_message(
        Message(
            chat_type=GROUP,
            chat_id=CHAT_ID,
            user_id=42,
            nickname="鱼片",
            content=f"内容 {message_id}",
            message_id=message_id,
            timestamp=timestamp,
        )
    )


def test_retention_days_defaults_to_180_and_parses(tmp_path: Path) -> None:
    assert load(tmp_path).memory.retention_days == 180
    assert load(tmp_path, 30).memory.retention_days == 30


def test_retention_days_rejects_negative(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="retention_days"):
        load(tmp_path, -1)


def test_prune_messages_keeps_recent_summaries_and_impressions(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.init_schema()
    old_ts = int((NOW - timedelta(days=40)).timestamp())
    new_ts = int((NOW - timedelta(days=1)).timestamp())
    add_message(store, "old", old_ts)
    add_message(store, "new", new_ts)
    add_message(store, "unknown", 0)
    store.mark_delivered(GROUP, CHAT_ID, "old", "new")
    store.upsert_summary(GROUP, CHAT_ID, "2026-10-01", "旧摘要仍在")
    store.upsert_impression(GROUP, CHAT_ID, 42, "nickname", "外号叫鱼片", 0.9)

    cutoff = int((NOW - timedelta(days=30)).timestamp())
    assert store.prune_messages(cutoff) == 1

    remaining = {m.message_id for m in store.recent_messages(GROUP, CHAT_ID, 10)}
    assert remaining == {"new", "unknown"}
    assert store.get_summary(GROUP, CHAT_ID, "2026-10-01") is not None
    assert [item.content for item in store.list_impressions(GROUP, CHAT_ID, 10)] == [
        "外号叫鱼片"
    ]
    delivered = {
        row["message_id"]
        for row in store._conn.execute(
            "SELECT message_id FROM delivered_messages WHERE chat_id = ?", (CHAT_ID,)
        )
    }
    assert delivered == {"new"}
    store.close()


def test_prune_messages_ignores_non_positive_cutoff(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.init_schema()
    add_message(store, "old", 1)
    assert store.prune_messages(0) == 0
    assert store.count_messages(GROUP, CHAT_ID) == 1
    store.close()


def test_prune_once_uses_retention_window() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.cutoffs: list[int] = []

        def prune_messages(self, cutoff: int) -> int:
            self.cutoffs.append(cutoff)
            return 1

    fake = FakeStore()
    assert prune_once(fake, 30, NOW) == 1  # type: ignore[arg-type]
    assert fake.cutoffs == [int((NOW - timedelta(days=30)).timestamp())]
    assert prune_once(fake, 0, NOW) == 0  # type: ignore[arg-type]
    assert len(fake.cutoffs) == 1


async def test_retention_loop_runs_immediately_until_stopped() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.calls = 0

        def prune_messages(self, cutoff: int) -> int:
            self.calls += 1
            return 0

    fake = FakeStore()
    stop = asyncio.Event()
    task = asyncio.create_task(
        retention_loop(fake, 7, interval=0.01, stop=stop)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert fake.calls >= 1


async def test_retention_loop_disabled_returns_immediately() -> None:
    class FakeStore:
        def prune_messages(self, cutoff: int) -> int:
            raise AssertionError("retention_days=0 不应清理")

    await asyncio.wait_for(
        retention_loop(FakeStore(), 0, interval=0.01),  # type: ignore[arg-type]
        timeout=1,
    )
