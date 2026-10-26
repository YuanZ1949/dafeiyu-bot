from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dafeiyu.supervisor.audit import AuditLog

TZ = timezone(timedelta(hours=8))


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=TZ)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def test_append_and_query_newest_first(tmp_path: Path) -> None:
    clock = FakeClock()
    log = AuditLog(tmp_path / "audit.jsonl", clock=clock)
    log.write("backup.create", actor="dashboard:admin@127.0.0.1", target="a.db")
    clock.advance(1)
    log.write("backup.delete", target="b.db")

    rows = log.query(limit=10)
    assert [row["action"] for row in rows] == ["backup.delete", "backup.create"]
    assert set(rows[0]) == {
        "id",
        "ts",
        "actor",
        "action",
        "target",
        "result",
        "detail",
        "request_id",
        "ip",
    }
    assert rows[0]["ts"] == "2026-10-26T12:00:01+08:00"


def test_bad_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps({"action": "ok", "ts": "2026-10-26T12:00:00+08:00"})
        + "\nnot-json\n",
        encoding="utf-8",
    )

    rows = AuditLog(path).query()
    assert [row["action"] for row in rows] == ["ok"]


def test_query_before_and_limit(tmp_path: Path) -> None:
    clock = FakeClock()
    log = AuditLog(tmp_path / "audit.jsonl", clock=clock)
    for index in range(5):
        log.write(f"e{index}")
        clock.advance(1)

    rows = log.query(limit=2, before="2026-10-26T12:00:03+08:00")
    assert [row["action"] for row in rows] == ["e2", "e1"]


def test_rotation_keeps_requested_backups(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=300, backups=2)
    for index in range(20):
        log.write("x.y", detail={"i": index})

    assert (tmp_path / "audit.jsonl").exists()
    rotated = sorted(tmp_path.glob("audit.jsonl.*"))
    assert len(rotated) <= 2
    assert log.query(limit=100)


def test_sanitize_hook_is_applied(tmp_path: Path) -> None:
    log = AuditLog(
        tmp_path / "audit.jsonl", sanitize=lambda detail: {"ok": True}
    )
    log.write("x", detail={"token": "secret"})

    assert log.query()[0]["detail"] == {"ok": True}


def test_write_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", boom)
    log.write("x")  # 不抛
