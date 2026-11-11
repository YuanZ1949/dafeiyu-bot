"""备份保留策略（B3-6）：配额必须对**全部**备份收敛，而不是只清 scheduled。

这些用例全部在 ``tmp_path`` 里造真实 SQLite 文件与 manifest，不碰
``data/backups/``；``_remove`` 不 mock，直接看磁盘上的真实结果。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from dafeiyu.memory.store import Store
from dafeiyu.supervisor.backup import BackupManager, BackupManifest

TZ = timezone(timedelta(hours=8))


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now: datetime = start or datetime(2026, 11, 1, 4, 0, 0, tzinfo=TZ)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 86400) -> None:
        self.now += timedelta(seconds=seconds)


def _make_manager(tmp_path: Path) -> tuple[BackupManager, FakeClock]:
    db = tmp_path / "dafeiyu.db"
    Store(db).init_schema()
    clock = FakeClock()
    manager = BackupManager(
        db,
        tmp_path / "backups",
        daily_keep=7,
        weekly_keep=4,
        clock=clock,
    )
    return manager, clock


async def _seed(
    manager: BackupManager, clock: FakeClock, reasons: list[str]
) -> list[BackupManifest]:
    """按 reasons 顺序落盘真实备份，返回 旧 -> 新 的 manifest 列表。"""
    created: list[BackupManifest] = []
    for reason in reasons:
        created.append(await manager.create(reason))
        clock.advance()
    return created


async def test_prune_counts_manual_and_pre_restore(tmp_path: Path) -> None:
    """配额要对 manual / pre-restore 一起收敛，不能只盯着 scheduled。"""
    manager, clock = _make_manager(tmp_path)
    created = await _seed(
        manager,
        clock,
        [
            "pre-restore",  # 0 最旧：永久锚点
            "scheduled",
            "manual",
            "scheduled",
            "manual",
            "pre-restore",  # 5 较新的 pre-restore：可被配额回收
            "manual",
            "scheduled",
            "manual",
            "scheduled",
        ],
    )
    backups = tmp_path / "backups"

    # 硬保护：最近 3 份 + 最旧 1 份 pre-restore。
    protected = {item.id for item in created[-3:]} | {created[0].id}
    assert len(protected) == 4
    protected_bytes = sum(
        item.size_bytes for item in manager.list() if item.id in protected
    )
    assert protected_bytes > 0

    manager.max_total_bytes = protected_bytes
    removed = manager.prune()
    remaining = manager.list()

    assert {item.id for item in remaining} == protected
    assert len(remaining) == 4
    # total 与候选集同源：删到保护下限后配额确实收敛。
    assert sum(item.size_bytes for item in remaining) <= manager.max_total_bytes
    assert set(removed) == {item.id for item in created} - protected

    for item in created:
        assert (backups / item.filename).exists() is (item.id in protected)
        assert (backups / f"{item.id}.json").exists() is (item.id in protected)


async def test_prune_keeps_oldest_pre_restore(tmp_path: Path) -> None:
    """最旧的 pre-restore 是回滚的最后一道保险，配额压力再大也不回收。"""
    manager, clock = _make_manager(tmp_path)
    created = await _seed(
        manager,
        clock,
        [
            "pre-restore",  # 0 最旧：必须保留
            "scheduled",
            "manual",
            "scheduled",
            "manual",
            "pre-restore",  # 5 超出最近 3 份：允许回收
            "manual",
            "scheduled",
            "manual",
            "scheduled",
        ],
    )
    backups = tmp_path / "backups"
    oldest_pre = created[0]
    newer_pre = created[5]
    protected = {item.id for item in created[-3:]} | {oldest_pre.id}
    manager.max_total_bytes = sum(
        item.size_bytes for item in manager.list() if item.id in protected
    )

    removed = manager.prune()
    surviving = {item.id for item in manager.list()}

    assert oldest_pre.id not in removed
    assert oldest_pre.id in surviving
    assert (backups / oldest_pre.filename).is_file()
    assert (backups / f"{oldest_pre.id}.json").is_file()
    # 只有最旧的那份被锚定；较新的 pre-restore 仍参与配额回收。
    assert newer_pre.id not in surviving
    assert not (backups / newer_pre.filename).exists()


async def test_prune_never_deletes_below_min_keep(tmp_path: Path) -> None:
    """即使配额小到装不下，也至少保留最近 3 份（既有硬下限）。"""
    manager, clock = _make_manager(tmp_path)
    created = await _seed(manager, clock, ["manual"] * 4)

    manager.max_total_bytes = 1
    removed = manager.prune()
    remaining = manager.list()

    assert len(remaining) == 3
    assert removed == [created[0].id]
    assert not (tmp_path / "backups" / created[0].filename).exists()
