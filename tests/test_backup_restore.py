from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
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


# ---- workspace（长期记忆） --------------------------------------------------


def make_workspace(root: Path) -> Path:
    workspace = root / "bot-workspace"
    (workspace / "chats").mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("- 记住：群规\n", encoding="utf-8", newline="\n")
    (workspace / "chats" / "a.md").write_text("群事\n", encoding="utf-8", newline="\n")
    return workspace


async def test_backup_includes_workspace_zip(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    workspace = make_workspace(tmp_path)
    manager = BackupManager(
        db, tmp_path / "backups", workspace_dir=workspace, clock=FakeClock()
    )

    manifest = await manager.create()

    assert manifest.workspace_filename == f"{Path(manifest.filename).stem}-workspace.zip"
    zip_path = tmp_path / "backups" / manifest.workspace_filename
    assert zip_path.exists()
    assert manifest.workspace_sha256 == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "MEMORY.md" in names
        assert "chats/a.md" in names
        assert zf.read("chats/a.md").decode("utf-8") == "群事\n"


async def test_backup_without_workspace_dir_skips_zip(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    manager = BackupManager(
        db, tmp_path / "backups", workspace_dir=tmp_path / "missing", clock=FakeClock()
    )

    manifest = await manager.create()

    assert manifest.workspace_filename == ""
    assert manifest.workspace_sha256 == ""
    assert not list((tmp_path / "backups").glob("*workspace*.zip"))
    assert manifest.sha256 == hashlib.sha256(
        (tmp_path / "backups" / manifest.filename).read_bytes()
    ).hexdigest()


async def test_backup_succeeds_when_workspace_packaging_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    workspace = make_workspace(tmp_path)
    manager = BackupManager(
        db, tmp_path / "backups", workspace_dir=workspace, clock=FakeClock()
    )

    def fail_workspace(db_backup: Path) -> tuple[str, str]:
        raise OSError("boom")

    monkeypatch.setattr(manager, "_backup_workspace", fail_workspace)

    manifest = await manager.create()

    assert manifest.workspace_filename == ""
    assert manifest.workspace_sha256 == ""
    db_path = tmp_path / "backups" / manifest.filename
    manifest_path = db_path.with_suffix(".json")
    assert db_path.exists()
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["id"] == manifest.id
    assert [item.id for item in manager.list()] == [manifest.id]


async def test_delete_removes_workspace_zip(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    workspace = make_workspace(tmp_path)
    manager = BackupManager(
        db, tmp_path / "backups", workspace_dir=workspace, clock=FakeClock()
    )
    manifest = await manager.create()
    zip_path = tmp_path / "backups" / manifest.workspace_filename
    assert zip_path.exists()

    manager.delete(manifest.id)

    assert not zip_path.exists()
    assert manager.list() == []


async def test_prune_removes_workspace_zip(tmp_path: Path) -> None:
    db = tmp_path / "dafeiyu.db"
    make_db(db).close()
    workspace = make_workspace(tmp_path)
    clock = FakeClock()
    manager = BackupManager(
        db,
        tmp_path / "backups",
        daily_keep=1,
        weekly_keep=0,
        workspace_dir=workspace,
        clock=clock,
    )

    for _ in range(3):
        await manager.create("scheduled")
        clock.advance(1)

    zips = list((tmp_path / "backups").glob("*workspace.zip"))
    assert len(zips) == 1
    assert len(manager.list()) == 1


def test_read_manifest_tolerates_missing_workspace_fields(tmp_path: Path) -> None:
    from dafeiyu.supervisor.backup import _read_manifest

    raw = {
        "id": "dafeiyu-20261026-040000-manual",
        "filename": "dafeiyu-20261026-040000-manual.db",
        "created_at": "2026-10-26T04:00:00+08:00",
        "reason": "manual",
        "size_bytes": 1,
        "sha256": "0" * 64,
        "schema_version": 1,
        "integrity_ok": True,
        "app_version": "0.0.0",
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    manifest = _read_manifest(path)

    assert manifest is not None
    assert manifest.workspace_filename == ""
    assert manifest.workspace_sha256 == ""


# ---- restore ---------------------------------------------------------------

class FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_start = 0
        self.fail_stop = False

    async def stop(self, name: str) -> None:
        self.calls.append(("stop", name))
        if self.fail_stop:
            raise RuntimeError("stop failed")

    async def start(self, name: str) -> None:
        self.calls.append(("start", name))
        if self.fail_start > 0:
            self.fail_start -= 1
            raise RuntimeError("start failed")


async def make_restore(tmp_path: Path, supervisor=None, ready=True):
    from dafeiyu.supervisor.restore import RestoreService

    db = tmp_path / "dafeiyu.db"
    store = make_db(db)
    add_message(store, "v1")
    store.close()
    backups = BackupManager(db, tmp_path / "backups", clock=FakeClock())
    service = RestoreService(
        db_path=db,
        backups=backups,
        supervisor=supervisor or FakeSupervisor(),
        ready_check=lambda: _ready(ready),
    )
    return service, db, backups


async def _ready(value: bool) -> bool:
    return value


def rows(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT content FROM messages ORDER BY id")]


async def test_restore_replaces_db_and_returns_summary(tmp_path: Path) -> None:
    from dafeiyu.supervisor.restore import RestoreService

    db = tmp_path / "dafeiyu.db"
    store = make_db(db)
    add_message(store, "v1")
    store.close()
    supervisor = FakeSupervisor()
    backups = BackupManager(db, tmp_path / "backups", clock=FakeClock())
    service = RestoreService(
        db_path=db,
        backups=backups,
        supervisor=supervisor,
        ready_check=lambda: _ready(True),
    )

    upload = tmp_path / "upload.db"
    upload.write_bytes((tmp_path / "backups" / (await backups.create()).filename).read_bytes())
    store = Store(db)
    store.init_schema()
    add_message(store, "v2")
    store.close()

    result = await service.restore(upload, actor="dashboard:admin@127.0.0.1")

    assert result["ok"] is True
    assert result["bot_started"] is True
    assert rows(db) == ["v1"]
    assert supervisor.calls == [("stop", "bot"), ("start", "bot")]
    assert result["pre_restore_backup"].endswith("-pre-restore.db")
    assert (tmp_path / "backups" / result["pre_restore_backup"]).exists()


async def test_corrupt_upload_leaves_db_unchanged(tmp_path: Path) -> None:
    from dafeiyu.supervisor.restore import RestoreError

    service, db, _backups = await make_restore(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")

    with pytest.raises(RestoreError) as exc:
        await service.restore(bad, actor="test")

    assert exc.value.stage == "validate"
    assert exc.value.status == 400
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert service.supervisor.calls == []


async def test_start_failure_rolls_back(tmp_path: Path) -> None:
    from dafeiyu.supervisor.restore import RestoreError

    db = tmp_path / "dafeiyu.db"
    store = make_db(db)
    add_message(store, "v1")
    store.close()
    supervisor = FakeSupervisor()
    backups = BackupManager(db, tmp_path / "backups", clock=FakeClock())
    from dafeiyu.supervisor.restore import RestoreService

    service = RestoreService(
        db_path=db,
        backups=backups,
        supervisor=supervisor,
        ready_check=lambda: _ready(True),
    )
    upload = tmp_path / "upload.db"
    upload.write_bytes((tmp_path / "backups" / (await backups.create()).filename).read_bytes())
    store = Store(db)
    store.init_schema()
    add_message(store, "v2")
    store.close()

    supervisor.fail_start = 1
    with pytest.raises(RestoreError) as exc:
        await service.restore(upload, actor="test")

    assert exc.value.stage == "health"
    assert rows(db) == ["v1", "v2"]
    assert supervisor.calls[-1] == ("start", "bot")


async def test_concurrent_restore_is_rejected(tmp_path: Path) -> None:
    from dafeiyu.supervisor.restore import RestoreError

    service, _db, _backups = await make_restore(tmp_path)
    async with service._lock:
        with pytest.raises(RestoreError) as exc:
            await service.restore(tmp_path / "x.db", actor="test")
    assert exc.value.status == 409


async def test_stop_failure_does_not_touch_db(tmp_path: Path) -> None:
    from dafeiyu.supervisor.restore import RestoreError

    db = tmp_path / "dafeiyu.db"
    store = make_db(db)
    add_message(store, "v1")
    store.close()
    supervisor = FakeSupervisor()
    supervisor.fail_stop = True
    backups = BackupManager(db, tmp_path / "backups", clock=FakeClock())
    from dafeiyu.supervisor.restore import RestoreService

    service = RestoreService(db_path=db, backups=backups, supervisor=supervisor)
    upload = tmp_path / "upload.db"
    upload.write_bytes((tmp_path / "backups" / (await backups.create()).filename).read_bytes())

    with pytest.raises(RestoreError) as exc:
        await service.restore(upload, actor="test")

    assert exc.value.stage == "stop"
    assert rows(db) == ["v1"]
