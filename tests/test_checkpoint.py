from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from dafeiyu.__main__ import CHECKPOINT_INTERVAL_SECONDS, checkpoint_loop
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

REPO_ROOT = Path(__file__).resolve().parents[1]


def wal_size(store: Store) -> int:
    path = Path(f"{store.path}-wal")
    return path.stat().st_size if path.exists() else 0


def add_messages(store: Store, count: int) -> None:
    for i in range(count):
        store.add_message(
            Message(
                chat_type=ChatType.GROUP,
                chat_id=1,
                user_id=2,
                nickname="鱼片",
                content=f"第{i}条" + "内容" * 200,
                message_id=f"m{i}",
                timestamp=1700000000 + i,
            )
        )


def test_checkpoint_truncates_wal(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    try:
        store.init_schema()
        add_messages(store, 100)
        assert (
            store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        )
        assert wal_size(store) > 0

        busy, _log_pages, _checkpointed = store.checkpoint()
        assert busy == 0
        assert wal_size(store) == 0
    finally:
        store.close()


def test_checkpoint_is_repeatable(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    try:
        store.init_schema()
        add_messages(store, 5)
        assert store.checkpoint()[0] == 0
        # 没有新写入时再 checkpoint 一次也不能出错，WAL 仍是 0 字节。
        assert store.checkpoint()[0] == 0
        assert wal_size(store) == 0
    finally:
        store.close()


def test_checkpoint_non_wal_database_does_not_raise(tmp_path: Path) -> None:
    store = Store(tmp_path / "plain.db")
    try:
        store.init_schema()
        # Store 启动时强制 WAL；这里切回 rollback journal 模拟非 WAL 库。
        store._conn.execute("PRAGMA journal_mode=DELETE")
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
        assert store.checkpoint() == (0, 0, 0)
    finally:
        store.close()


def test_checkpoint_memory_database_does_not_raise() -> None:
    store = Store(":memory:")
    try:
        store.init_schema()
        add_messages(store, 3)
        assert store.checkpoint() == (0, 0, 0)
    finally:
        store.close()


async def test_checkpoint_loop_waits_interval_then_checkpoints() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.calls = 0

        def checkpoint(self) -> tuple[int, int, int]:
            self.calls += 1
            return (0, 0, 0)

    fake = FakeStore()
    stop = asyncio.Event()
    task = asyncio.create_task(
        checkpoint_loop(fake, interval=0.01, stop=stop)  # type: ignore[arg-type]
    )
    # 启动时不立刻 checkpoint：首个周期还没到。
    await asyncio.sleep(0)
    assert fake.calls == 0
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert fake.calls >= 1


async def test_checkpoint_loop_survives_errors() -> None:
    class FlakyStore:
        def __init__(self) -> None:
            self.calls = 0

        def checkpoint(self) -> tuple[int, int, int]:
            self.calls += 1
            raise sqlite3.Error("boom")

    fake = FlakyStore()
    stop = asyncio.Event()
    task = asyncio.create_task(
        checkpoint_loop(fake, interval=0.01, stop=stop)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert fake.calls >= 1


def test_main_wires_checkpoint_task() -> None:
    """main() 是单体启动流程，没有可注入的测试缝；这里做源码级接线断言。

    retention_loop 的接线同样没有单测，只测它自己的循环体；checkpoint 的
    循环体已在上面的 FakeStore 用例里覆盖，这里只保证 main() 真的把它挂到
    后台任务并在 shutdown 里取消。
    """
    assert CHECKPOINT_INTERVAL_SECONDS == 6 * 3600
    source = (REPO_ROOT / "src" / "dafeiyu" / "__main__.py").read_text(
        encoding="utf-8"
    )
    assert "checkpoint_task = " in source
    assert "checkpoint_loop(store" in source
    assert "checkpoint_task.cancel()" in source
