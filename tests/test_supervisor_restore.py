from __future__ import annotations

import os
from pathlib import Path

import pytest

from dafeiyu.supervisor import restore as restore_mod
from dafeiyu.supervisor.backup import BackupManager
from dafeiyu.supervisor.restore import (
    RestoreService,
    _same_volume,
    _stage_for_replace,
)


class FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def stop(self, name: str) -> None:
        self.calls.append(("stop", name))

    async def start(self, name: str) -> None:
        self.calls.append(("start", name))


def _service(db_path: Path, tmp_path: Path) -> RestoreService:
    return RestoreService(
        db_path=db_path,
        backups=BackupManager(db_path, tmp_path / "backups"),
        supervisor=FakeSupervisor(),
    )


def _not_same_volume(left: Path, right: Path) -> bool:
    """强制按跨卷处理（Windows 上无法稳定造真实跨卷）。"""
    return False


def _write(path: Path, data: bytes) -> None:
    _ = path.write_bytes(data)


def test_same_volume_for_sibling_paths(tmp_path: Path) -> None:
    assert _same_volume(tmp_path / "a.db", tmp_path / "data" / "b.db") is True


def test_stage_for_replace_keeps_upload_on_same_volume(tmp_path: Path) -> None:
    upload = tmp_path / "upload.db"
    _write(upload, b"payload")
    db = tmp_path / "data" / "dafeiyu.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    assert _stage_for_replace(upload, db) == upload


def test_stage_for_replace_copies_across_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = tmp_path / "upload.db"
    _write(upload, b"payload")
    db = tmp_path / "data" / "dafeiyu.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(restore_mod, "_same_volume", _not_same_volume)

    staged = _stage_for_replace(upload, db)

    try:
        assert staged != upload
        assert staged.parent == db.parent
        assert staged.read_bytes() == b"payload"
    finally:
        staged.unlink(missing_ok=True)


def test_restore_uses_temp_on_target_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨卷恢复：先在同一卷造好临时文件（确认可替换），再删 sidecar、replace。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "dafeiyu.db"
    _write(db, b"old")
    upload = tmp_path / "upload.db"
    _write(upload, b"new")
    service = _service(db, tmp_path)

    events: list[str] = []
    removals: list[tuple[Path | None, bytes]] = []
    real_replace = os.replace

    def fake_remove_sidecars() -> None:
        staged_now = [
            path
            for path in data_dir.iterdir()
            if path.name.startswith("dafeiyu-restore-staged-")
        ]
        staged = staged_now[0] if staged_now else None
        removals.append((staged, staged.read_bytes() if staged else b""))
        events.append("remove_sidecars")

    def recording_replace(
        src: str | os.PathLike[str], dst: str | os.PathLike[str]
    ) -> None:
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(restore_mod, "_same_volume", _not_same_volume)
    monkeypatch.setattr(service, "_remove_sidecars", fake_remove_sidecars)
    monkeypatch.setattr(os, "replace", recording_replace)

    service._replace(upload)

    assert events == ["remove_sidecars", "replace"]
    staged_at_removal, staged_bytes = removals[0]
    assert staged_at_removal is not None
    assert staged_at_removal.parent == db.parent
    assert staged_bytes == b"new"
    assert db.read_bytes() == b"new"
    assert not upload.exists()
