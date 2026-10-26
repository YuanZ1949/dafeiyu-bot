from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.supervisor.backup import (
    BackupError,
    BackupManager,
    validate_database,
)

TZ = timezone(timedelta(hours=8))


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 26, 4, 0, 0, tzinfo=TZ)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 1) -> None:
        self.now += timedelta(seconds=seconds)


def make_db(path: Path) -> Store:
    store = Store(path)
    store.init_schema()
    return store


def add_message(store: Store, content: str = "hi") -> None:
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=100,
            user_id=1,
            nickname="tester",
            content=content,
            message_id=f"m-{content}",
            timestamp=1,
        )
    )


async def test_create_writes_manifest_and_hash(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    manager = BackupManager(db, tmp_path / "backups", clock=FakeClock())

    manifest = await manager.create("manual")
    path = tmp_path / "backups" / manifest.filename

    assert path.exists()
    assert manifest.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest.integrity_ok is True
    assert manifest.size_bytes == path.stat().st_size
    manifest_raw = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest_raw["id"] == manifest.id
    assert manifest_raw["reason"] == "manual"
    assert len(manager.list()) == 1


async def test_backup_includes_committed_wal_data(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    store = make_db(db)
    add_message(store)

    manifest = await BackupManager(db, tmp_path / "backups").create()
    with sqlite3.connect(tmp_path / "backups" / manifest.filename) as conn:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    assert count == 1
    store.close()


async def test_missing_source_db_raises(tmp_path: Path) -> None:
    manager = BackupManager(tmp_path / "missing.db", tmp_path / "backups")
    with pytest.raises(BackupError):
        await manager.create()
    assert not list((tmp_path / "backups").glob("*.json"))


def test_validate_rejects_truncated_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 10)

    ok, reason = validate_database(bad)
    assert ok is False
    assert reason


def test_validate_rejects_missing_tables(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(empty)
    conn.execute("CREATE TABLE other (id INTEGER)")
    conn.commit()
    conn.close()

    ok, reason = validate_database(empty)
    assert ok is False
    assert "messages" in reason


async def test_validate_accepts_created_backup(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    manifest = await BackupManager(db, tmp_path / "backups").create()
    ok, reason = validate_database(tmp_path / "backups" / manifest.filename)
    assert ok is True
    assert reason == "ok"


async def test_retention_keeps_recent_scheduled(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    clock = FakeClock()
    manager = BackupManager(
        db, tmp_path / "backups", daily_keep=3, weekly_keep=0, clock=clock
    )

    for _ in range(10):
        await manager.create("scheduled")
        clock.advance(1)

    scheduled = [item for item in manager.list() if item.reason == "scheduled"]
    assert len(scheduled) == 3


async def test_manual_backups_are_not_pruned(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    clock = FakeClock()
    manager = BackupManager(
        db, tmp_path / "backups", daily_keep=1, weekly_keep=0, clock=clock
    )

    await manager.create("manual")
    for _ in range(4):
        clock.advance(1)
        await manager.create("scheduled")

    reasons = sorted(item.reason for item in manager.list())
    assert reasons.count("manual") == 1
    assert reasons.count("scheduled") == 1


async def test_delete_removes_db_and_manifest(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    manager = BackupManager(db, tmp_path / "backups")
    manifest = await manager.create()

    manager.delete(manifest.id)

    assert manager.list() == []
    assert not (tmp_path / "backups" / manifest.filename).exists()


def test_delete_rejects_path_traversal(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    manager = BackupManager(db, tmp_path / "backups")

    with pytest.raises(BackupError):
        manager.delete("../../x")


async def test_status_reports_last_success(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    clock = FakeClock()
    manager = BackupManager(db, tmp_path / "backups", clock=clock)

    assert manager.status()["count"] == 0
    await manager.create()
    status = manager.status()
    assert status["count"] == 1
    assert status["last_success_at"].startswith("2026-10-26T04:00:00")
    assert status["last_error_at"] is None
    assert manager.last_scheduled_date() is None


def test_validate_rejects_newer_schema(tmp_path: Path) -> None:
    db = tmp_path / "future.db"
    make_db(db).close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    ok, reason = validate_database(db)
    assert ok is False
    assert "版本" in reason
