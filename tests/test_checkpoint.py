from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import threading
import time
from pathlib import Path

from dafeiyu.__main__ import CHECKPOINT_INTERVAL_SECONDS, checkpoint_loop
from dafeiyu.memory.store import CheckpointResult, Store
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


class RecordingStore(Store):
    """只覆盖 checkpoint 的 Store 子类：记录调用次数与执行线程。"""

    def __init__(self, result: CheckpointResult | None = None) -> None:
        super().__init__(":memory:")
        self.calls = 0
        self.thread_idents: list[int] = []
        self.result = result or CheckpointResult(0, -1, -1, False)

    def checkpoint(self) -> CheckpointResult:
        self.calls += 1
        self.thread_idents.append(threading.get_ident())
        return self.result


class RaisingStore(Store):
    """checkpoint 抛错的 Store 子类：循环必须吞掉异常继续活着。"""

    def __init__(self) -> None:
        super().__init__(":memory:")
        self.calls = 0

    def checkpoint(self) -> CheckpointResult:
        self.calls += 1
        raise sqlite3.Error("boom")


def test_checkpoint_result_has_structured_fields(tmp_path: Path) -> None:
    store = Store(tmp_path / "fields.db")
    try:
        store.init_schema()
        add_messages(store, 5)
        result = store.checkpoint()
        assert isinstance(result, CheckpointResult)
        assert [f.name for f in dataclasses.fields(result)] == [
            "busy",
            "log",
            "checkpointed",
            "truncated",
            "error",
        ]
        assert result.busy == 0
        assert result.log >= 0
        assert result.checkpointed >= 0
        assert result.truncated is True
        assert result.error is None
    finally:
        store.close()


def test_checkpoint_truncates_wal(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    try:
        store.init_schema()
        add_messages(store, 100)
        assert (
            store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        )
        assert wal_size(store) > 0

        result = store.checkpoint()
        assert result.busy == 0
        assert result.truncated is True
        assert result.error is None
        assert wal_size(store) == 0
    finally:
        store.close()


def test_checkpoint_is_repeatable(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    try:
        store.init_schema()
        add_messages(store, 5)
        assert store.checkpoint().busy == 0
        # 没有新写入时再 checkpoint 一次也不能出错，WAL 仍是 0 字节。
        # 这时 log == 0 表示「没有待回写的帧」，与「没有 WAL 文件」（log == -1）区分开。
        again = store.checkpoint()
        assert again.busy == 0
        assert again.log == 0
        assert again.error is None
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
        result = store.checkpoint()
        # 没有 -wal 文件：log == -1，且不算失败。
        assert result.busy == 0
        assert result.log == -1
        assert result.checkpointed == -1
        assert result.truncated is False
        assert result.error is None
    finally:
        store.close()


def test_checkpoint_memory_database_does_not_raise() -> None:
    store = Store(":memory:")
    try:
        store.init_schema()
        add_messages(store, 3)
        result = store.checkpoint()
        assert result.busy == 0
        assert result.log == -1
        assert result.truncated is False
        assert result.error is None
    finally:
        store.close()


def test_checkpoint_keeps_main_connection_busy_timeout(tmp_path: Path) -> None:
    store = Store(tmp_path / "timeout.db")
    try:
        store.init_schema()
        add_messages(store, 5)
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        store.checkpoint()
        # busy_timeout 是连接级的：checkpoint 用的是另一条短超时连接，
        # 不能顺手把主连接改成 200。
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        store.close()


def test_checkpoint_reports_busy_without_raising(tmp_path: Path) -> None:
    store = Store(tmp_path / "busy.db")
    blocker: sqlite3.Connection | None = None
    try:
        store.init_schema()
        add_messages(store, 20)
        # 另一条连接拿住写锁：TRUNCATE 检查点必须等写事务结束。
        blocker = sqlite3.connect(store.path)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            """
            INSERT INTO messages
                (chat_type, chat_id, message_id, user_id, nickname, content,
                 is_bot, timestamp)
            VALUES ('group', '1', 'locked', 2, '鱼片', '占着写锁', 0, 1700000000)
            """
        )
        started = time.monotonic()
        result = store.checkpoint()
        elapsed = time.monotonic() - started

        # 不抛异常，而是把冲突报出来。
        assert result.busy > 0
        assert result.truncated is False
        assert result.error is None
        # 200ms 左右的短超时，而不是主连接的 5000ms。
        assert elapsed < 3.0
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
        store.close()


def test_checkpoint_succeeds_after_blocker_releases(tmp_path: Path) -> None:
    store = Store(tmp_path / "release.db")
    try:
        store.init_schema()
        add_messages(store, 20)
        blocker = sqlite3.connect(store.path)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            assert store.checkpoint().busy > 0
        finally:
            blocker.rollback()
            blocker.close()
        result = store.checkpoint()
        assert result.busy == 0
        assert result.truncated is True
        assert result.error is None
    finally:
        store.close()


async def test_checkpoint_loop_waits_interval_then_checkpoints() -> None:
    fake = RecordingStore()
    try:
        stop = asyncio.Event()
        task = asyncio.create_task(checkpoint_loop(fake, interval=0.01, stop=stop))
        # 启动时不立刻 checkpoint：首个周期还没到。
        await asyncio.sleep(0)
        assert fake.calls == 0
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        assert fake.calls >= 1
    finally:
        fake.close()


async def test_checkpoint_loop_runs_checkpoint_in_worker_thread() -> None:
    fake = RecordingStore()
    try:
        loop_thread = threading.get_ident()
        stop = asyncio.Event()
        task = asyncio.create_task(checkpoint_loop(fake, interval=0.01, stop=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        assert fake.calls >= 1
        # 走 asyncio.to_thread：checkpoint 不在事件循环线程里执行。
        assert all(ident != loop_thread for ident in fake.thread_idents)
    finally:
        fake.close()


async def test_checkpoint_loop_survives_errors() -> None:
    fake = RaisingStore()
    try:
        stop = asyncio.Event()
        task = asyncio.create_task(checkpoint_loop(fake, interval=0.01, stop=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        assert fake.calls >= 1
    finally:
        fake.close()


async def test_checkpoint_loop_logs_error_without_raising() -> None:
    fake = RecordingStore(
        CheckpointResult(0, 0, 0, False, error="OperationalError: locked")
    )
    try:
        stop = asyncio.Event()
        task = asyncio.create_task(checkpoint_loop(fake, interval=0.01, stop=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        assert fake.calls >= 1
    finally:
        fake.close()


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
    # 循环体必须把阻塞调用丢给工作线程。
    assert "await asyncio.to_thread(store.checkpoint)" in source
