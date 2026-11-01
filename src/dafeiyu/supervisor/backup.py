from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..timeutil import now_shanghai
from .audit import AuditLog

log = logging.getLogger(__name__)

REQUIRED_TABLES = (
    "messages",
    "impressions",
    "summaries",
    "usage",
    "opencode_sessions",
    "brain_sessions",
    "session_summaries",
    "delivered_cursors",
    "delivered_messages",
    "memory_backoff",
    "memory_rate_limit",
)

REQUIRED_COLUMNS = {
    "messages": ("chat_type", "chat_id", "message_id", "user_id", "content", "timestamp"),
    "usage": ("date", "purpose", "prompt_tokens", "completion_tokens"),
    "brain_sessions": ("provider", "chat_type", "chat_id", "session_id"),
}

REASONS = ("scheduled", "manual", "pre-restore")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupManifest:
    id: str
    filename: str
    created_at: str
    reason: str
    size_bytes: int
    sha256: str
    schema_version: int
    integrity_ok: bool
    app_version: str
    # 旧 manifest 没有这两个键，带默认值保证反序列化兼容。
    workspace_filename: str = ""
    workspace_sha256: str = ""


def validate_database(
    path: Path, max_schema_version: int | None = None
) -> tuple[bool, str]:
    """校验备份文件能否安全回灌；返回 (ok, reason) 而不是抛异常。

    ``max_schema_version`` 缺省跟随 ``Store.SCHEMA_VERSION``（单一真源）：
    schema 升版后仍接受新版库，不再需要调用方手动跟改。
    """
    if max_schema_version is None:
        from dafeiyu.memory.store import Store

        max_schema_version = Store.SCHEMA_VERSION
    path = Path(path)
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        return False, f"无法读取：{exc}"
    if header != b"SQLite format 3\x00":
        return False, "不是 SQLite 数据库"

    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"无法打开：{exc}"
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            return False, "完整性检查失败"
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > int(max_schema_version):
            return False, f"数据库版本 {version} 高于支持版本 {max_schema_version}"
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        if missing:
            return False, f"缺少表：{', '.join(missing)}"
        for table, columns in REQUIRED_COLUMNS.items():
            present = {
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
            }
            lacking = [name for name in columns if name not in present]
            if lacking:
                return False, f"{table} 缺少列：{', '.join(lacking)}"
    except sqlite3.Error as exc:
        return False, f"读取失败：{exc}"
    finally:
        conn.close()
    return True, "ok"


class BackupManager:
    def __init__(
        self,
        db_path: Path,
        backup_dir: Path,
        *,
        daily_keep: int = 7,
        weekly_keep: int = 4,
        max_total_gb: int = 20,
        audit: AuditLog | None = None,
        clock: Callable[[], datetime] | None = None,
        workspace_dir: Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)
        # 长期记忆工作区：None 时不产 zip，行为与旧版本完全一致。
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None
        self.daily_keep = max(0, int(daily_keep))
        self.weekly_keep = max(0, int(weekly_keep))
        self.max_total_bytes = max(1, int(max_total_gb)) * 1024**3
        self.audit = audit
        self._clock = clock or now_shanghai
        self._running = False
        self._last_success_at: str | None = None
        self._last_error_at: str | None = None
        self._last_error = ""
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    async def create(self, reason: str = "manual") -> BackupManifest:
        if reason not in REASONS:
            raise BackupError(f"未知备份原因：{reason}")
        return await asyncio.to_thread(self._create_sync, reason)

    def _create_sync(self, reason: str) -> BackupManifest:
        if not self.db_path.exists():
            self._record_error("源数据库不存在")
            raise BackupError(f"源数据库不存在：{self.db_path}")
        self._running = True
        tmp = self.backup_dir / f".{os.getpid()}-{reason}.tmp"
        try:
            source = sqlite3.connect(
                f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=5
            )
            dest = sqlite3.connect(str(tmp))
            try:
                source.backup(dest)
            finally:
                dest.close()
                source.close()
            with tmp.open("rb+") as fh:
                os.fsync(fh.fileno())
            digest = _sha256(tmp)
            now = self._clock()
            stamp = now.strftime("%Y%m%d-%H%M%S")
            filename = self._unique_name(f"dafeiyu-{stamp}-{reason}.db")
            target = self.backup_dir / filename
            os.replace(tmp, target)
            ok, reason_text = validate_database(target)
            if not ok:
                target.unlink(missing_ok=True)
                raise BackupError(f"备份校验失败：{reason_text}")
            schema_version = _schema_version(target)
            try:
                workspace_filename, workspace_sha256 = self._backup_workspace(target)
            except OSError as exc:
                log.warning("工作区打包失败，本次只备份数据库：%s", exc)
                workspace_filename, workspace_sha256 = "", ""
            manifest = BackupManifest(
                id=Path(filename).stem,
                filename=filename,
                created_at=now.isoformat(timespec="seconds"),
                reason=reason,
                size_bytes=target.stat().st_size,
                sha256=digest,
                schema_version=schema_version,
                integrity_ok=True,
                app_version=__version__,
                workspace_filename=workspace_filename,
                workspace_sha256=workspace_sha256,
            )
            _write_manifest(target.with_suffix(".json"), manifest)
            self._last_success_at = manifest.created_at
            self._last_error = ""
            if self.audit is not None:
                self.audit.write(
                    "backup.create",
                    target=filename,
                    detail={
                        "reason": reason,
                        "size_bytes": manifest.size_bytes,
                        "sha256": digest,
                    },
                )
            self.prune()
            return manifest
        except BackupError:
            self._record_error("备份失败")
            raise
        except (OSError, sqlite3.Error) as exc:
            self._record_error(str(exc))
            raise BackupError(f"备份失败：{exc}") from exc
        finally:
            self._running = False
            tmp.unlink(missing_ok=True)

    def _record_error(self, message: str) -> None:
        self._last_error = message
        self._last_error_at = self._clock().isoformat(timespec="seconds")
        log.warning("备份失败：%s", message)

    def _backup_workspace(self, db_backup: Path) -> tuple[str, str]:
        """把工作区打平成 `<db stem>-workspace.zip`；目录缺失或为空则不产文件。"""
        workspace = self.workspace_dir
        if workspace is None or not workspace.is_dir():
            return "", ""
        files = sorted(
            path
            for path in workspace.rglob("*")
            if path.is_file() and not path.is_relative_to(self.backup_dir)
        )
        if not files:
            return "", ""
        name = f"{db_backup.stem}-workspace.zip"
        target = self.backup_dir / name
        tmp = target.with_name(f".{os.getpid()}-workspace.tmp")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in files:
                    zf.write(path, path.relative_to(workspace).as_posix())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return name, _sha256(target)

    def _unique_name(self, name: str) -> str:
        candidate = name
        index = 1
        while (self.backup_dir / candidate).exists():
            stem = Path(name).stem
            candidate = f"{stem}-{index}.db"
            index += 1
        return candidate

    def list(self) -> list[BackupManifest]:
        manifests: list[BackupManifest] = []
        for path in self.backup_dir.glob("*.json"):
            manifest = _read_manifest(path)
            if manifest is None:
                continue
            if not (self.backup_dir / manifest.filename).exists():
                continue
            manifests.append(manifest)
        manifests.sort(key=lambda item: item.created_at, reverse=True)
        return manifests

    def delete(self, backup_id: str) -> None:
        if not backup_id or not _NAME_RE.match(backup_id):
            raise BackupError("非法备份 ID")
        manifest = next(
            (item for item in self.list() if item.id == backup_id), None
        )
        if manifest is None:
            raise BackupError("备份不存在")
        self._remove(manifest)
        if self.audit is not None:
            self.audit.write("backup.delete", target=manifest.filename)

    def prune(self) -> list[str]:
        manifests = self.list()
        scheduled = [item for item in manifests if item.reason == "scheduled"]
        keep: set[str] = set()

        for item in scheduled[: self.daily_keep]:
            keep.add(item.id)
        weekly = [item for item in scheduled if _is_sunday(item.created_at)]
        for item in weekly[: self.weekly_keep]:
            keep.add(item.id)

        removed: list[str] = []
        for item in scheduled:
            if item.id not in keep:
                self._remove(item)
                removed.append(item.id)

        remaining = [
            item for item in self.list() if item.reason == "scheduled"
        ]
        total = sum(item.size_bytes for item in self.list())
        while total > self.max_total_bytes and len(remaining) > 3:
            oldest = remaining.pop()
            total -= oldest.size_bytes
            self._remove(oldest)
            removed.append(oldest.id)
        return removed

    def _remove(self, manifest: BackupManifest) -> None:
        (self.backup_dir / manifest.filename).unlink(missing_ok=True)
        (self.backup_dir / f"{manifest.id}.json").unlink(missing_ok=True)
        if manifest.workspace_filename:
            (self.backup_dir / manifest.workspace_filename).unlink(missing_ok=True)

    def status(self) -> dict[str, Any]:
        return {
            "last_success_at": self._last_success_at,
            "last_error_at": self._last_error_at,
            "last_error": self._last_error,
            "count": len(self.list()),
            "running": self._running,
        }

    def last_scheduled_date(self) -> str | None:
        for item in self.list():
            if item.reason == "scheduled":
                return item.created_at[:10]
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_version(path: Path) -> int:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _write_manifest(path: Path, manifest: BackupManifest) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _read_manifest(path: Path) -> BackupManifest | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return BackupManifest(
            id=str(raw["id"]),
            filename=str(raw["filename"]),
            created_at=str(raw["created_at"]),
            reason=str(raw["reason"]),
            size_bytes=int(raw["size_bytes"]),
            sha256=str(raw["sha256"]),
            schema_version=int(raw["schema_version"]),
            integrity_ok=bool(raw["integrity_ok"]),
            app_version=str(raw["app_version"]),
            # 旧 manifest 无此键，缺省为空串即可。
            workspace_filename=str(raw.get("workspace_filename") or ""),
            workspace_sha256=str(raw.get("workspace_sha256") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _is_sunday(created_at: str) -> bool:
    try:
        return datetime.fromisoformat(created_at).weekday() == 6
    except ValueError:
        return False
