"""IS-7 允许差异 (3)：schema_version 必须能在这两个面上被断言到 2。

- `/api/health/detail` 的 `memory.schema_version`
- 备份 manifest 的 `schema_version`

`/api/health` 只允许回 `{"ok": true}`，不得泄漏 schema_version。
"""

from __future__ import annotations

from pathlib import Path

from test_control_api import make_control, running

from dafeiyu.memory.store import Store
from dafeiyu.supervisor.backup import BackupManager


async def test_health_detail_reports_schema_version_two(tmp_path: Path) -> None:
    _control, _service, app = make_control(tmp_path)

    async with running(app) as client:
        detail = await client.get("/api/health/detail")
        assert detail.status == 200
        body = await detail.json()
        assert body["memory"]["schema_version"] == 2

        plain = await client.get("/api/health")
        assert plain.status == 200
        assert await plain.json() == {"ok": True}


async def test_backup_manifest_records_schema_version_two(tmp_path: Path) -> None:
    db_path = tmp_path / "source.db"
    store = Store(db_path)
    store.init_schema()

    manager = BackupManager(db_path, tmp_path / "backups")
    manifest = await manager.create("manual")
    assert manifest.schema_version == 2

    reread = manager.list()
    assert reread
    assert reread[0].schema_version == 2
