from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

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

#: 工作区快照的硬上限：超过就不打 zip / 拒绝恢复（R20）。
#: 没有上限时，工作区膨胀会拖慢备份，也给了 zip 炸弹一个入口。
MAX_WORKSPACE_FILES = 20000
MAX_WORKSPACE_BYTES = 512 * 1024 * 1024


def _tmp_path(directory: Path, suffix: str) -> Path:
    """唯一临时文件名：同进程并发备份不会写坏同一个 tmp（R20）。"""
    return directory / f".{uuid4().hex}{suffix}"


def _atomic_copy(source: Path, target: Path) -> None:
    """镜像复制：先写同目录唯一 tmp，再 os.replace，避免半截文件。"""
    tmp = _tmp_path(target.parent, f"-{target.name}.tmp")
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


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


@dataclass(frozen=True)
class WorkspaceRestore:
    """一次工作区恢复的结果；``previous`` 用于启动失败时回滚。"""

    target: Path
    previous: Path | None
    restored_files: int
    restored_bytes: int


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
        mirror_dir: Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)
        # 长期记忆工作区：None 时不产 zip，行为与旧版本完全一致。
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None
        # 异地镜像：None/空时完全关闭，不影响主备份路径。
        self.mirror_dir = Path(mirror_dir) if mirror_dir else None
        self.daily_keep = max(0, int(daily_keep))
        self.weekly_keep = max(0, int(weekly_keep))
        self.max_total_bytes = max(1, int(max_total_gb)) * 1024**3
        self.audit = audit
        self._clock = clock or now_shanghai
        self._running = False
        self._last_success_at: str | None = None
        self._last_error_at: str | None = None
        self._last_error = ""
        # 镜像失败独立记录：绝不污染主备份的 last_error/成功状态。
        self._mirror_last_error_at: str | None = None
        self._mirror_last_error = ""
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
        tmp = _tmp_path(self.backup_dir, f"-{reason}.tmp")
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
            # 配额按 manifest.size_bytes 求和（prune），workspace zip 必须一起算，
            # 否则备份总量可以悄悄超过 max_total_gb（R20）。
            workspace_bytes = 0
            if workspace_filename:
                with contextlib.suppress(OSError):
                    workspace_bytes = (
                        self.backup_dir / workspace_filename
                    ).stat().st_size
            manifest = BackupManifest(
                id=Path(filename).stem,
                filename=filename,
                created_at=now.isoformat(timespec="seconds"),
                reason=reason,
                size_bytes=target.stat().st_size + workspace_bytes,
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
            # 主备份已成功落盘：镜像只是附加动作，任何失败都不影响本次成功。
            self._mirror(manifest)
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

    def _record_mirror_error(self, message: str) -> None:
        self._mirror_last_error = message
        self._mirror_last_error_at = self._clock().isoformat(timespec="seconds")
        log.warning("备份镜像失败（主备份不受影响）：%s", message)

    def _mirror(self, manifest: BackupManifest) -> None:
        """把刚写好的 DB/manifest（以及存在的 workspace zip）复制到异地目录。

        这是主备份成功之后的附加动作：任何异常都在这里吞掉，只写独立的
        镜像错误字段，绝不改变主备份结果、也绝不向调用方抛出。
        """
        mirror = self.mirror_dir
        if mirror is None:
            return
        try:
            # 镜像目录若与主目录相同，复制等于就地覆盖自己，没有意义。
            if mirror.resolve() == self.backup_dir.resolve():
                log.debug("backup.mirror_dir 与 backup.dir 相同，跳过镜像")
                return
            mirror.mkdir(parents=True, exist_ok=True)
            sources = [
                self.backup_dir / manifest.filename,
                self.backup_dir / f"{manifest.id}.json",
            ]
            if manifest.workspace_filename:
                workspace = self.backup_dir / manifest.workspace_filename
                if workspace.is_file():
                    sources.append(workspace)
            for source in sources:
                _atomic_copy(source, mirror / source.name)
        except Exception as exc:  # noqa: BLE001 - 镜像失败必须被隔离
            self._record_mirror_error(str(exc))
        else:
            self._mirror_last_error = ""

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
        # 上限只影响「要不要打 zip」，不影响数据库备份本身。
        if len(files) > MAX_WORKSPACE_FILES:
            raise OSError(
                f"工作区文件过多（{len(files)} > {MAX_WORKSPACE_FILES}），本次只备份数据库"
            )
        total_bytes = sum(path.stat().st_size for path in files)
        if total_bytes > MAX_WORKSPACE_BYTES:
            raise OSError(
                f"工作区过大（{total_bytes} > {MAX_WORKSPACE_BYTES} 字节），本次只备份数据库"
            )
        name = f"{db_backup.stem}-workspace.zip"
        target = self.backup_dir / name
        tmp = _tmp_path(self.backup_dir, "-workspace.tmp")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in files:
                    zf.write(path, path.relative_to(workspace).as_posix())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return name, _sha256(target)

    # ------------------------------------------------------------- 工作区恢复
    def restore_workspace(
        self, backup_id: str, target_dir: Path | None = None
    ) -> WorkspaceRestore:
        """把某份备份里的 ``-workspace.zip`` 解压回工作区（R18）。

        流程：校验 ID/清单 → SHA256 校验 → zip 成员安全检查 → 解压到同级
        临时目录 → 目录交换（旧的改名为 ``<name>.pre-restore-<ts>``）。
        任何一步失败都不动现有工作区。
        """
        if not backup_id or not _NAME_RE.match(backup_id):
            raise BackupError("非法备份 ID")
        manifest = next(
            (item for item in self.list() if item.id == backup_id), None
        )
        if manifest is None:
            raise BackupError("备份不存在")
        if not manifest.workspace_filename:
            raise BackupError("该备份没有工作区快照")
        workspace = Path(target_dir) if target_dir is not None else self.workspace_dir
        if workspace is None:
            raise BackupError("未启用工作区（workspace.dir 为空）")
        archive = self.backup_dir / manifest.workspace_filename
        if not archive.is_file():
            raise BackupError("工作区快照文件缺失")
        if manifest.workspace_sha256:
            digest = _sha256(archive)
            if digest != manifest.workspace_sha256:
                raise BackupError("工作区快照校验失败（SHA256 不匹配）")

        workspace = Path(workspace)
        try:
            workspace.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupError(f"工作区父目录不可用：{exc}") from exc

        staging = workspace.with_name(f".{workspace.name}-restore-{os.getpid()}")
        self._remove_tree(staging)
        files = 0
        size = 0
        try:
            with zipfile.ZipFile(archive) as zf:
                files, size = _validate_zip_members(zf)
                try:
                    zf.extractall(staging)
                except (OSError, zipfile.BadZipFile) as exc:
                    raise BackupError(f"工作区解压失败：{exc}") from exc
            if not staging.is_dir():
                raise BackupError("工作区解压后为空")
            previous = self._swap_workspace(staging, workspace)
        except BackupError:
            self._remove_tree(staging)
            raise
        except OSError as exc:
            self._remove_tree(staging)
            raise BackupError(f"工作区替换失败：{exc}") from exc

        self._prune_previous_workspaces(workspace.parent, workspace.name)
        if self.audit is not None:
            self.audit.write(
                "backup.restore_workspace",
                target=manifest.id,
                detail={"files": files, "bytes": size},
            )
        return WorkspaceRestore(
            target=workspace,
            previous=previous,
            restored_files=files,
            restored_bytes=size,
        )

    def rollback_workspace(self, restored: WorkspaceRestore) -> None:
        """启动失败时把工作区换回恢复前的目录；失败只记日志。"""
        target = restored.target
        try:
            if target.exists():
                failed = target.with_name(
                    f"{target.name}.failed-{datetime.now():%Y%m%d-%H%M%S}"
                )
                os.replace(target, failed)
            if restored.previous is not None and restored.previous.exists():
                os.replace(restored.previous, target)
        except OSError as exc:
            log.exception("工作区回滚失败：%s", exc)
        finally:
            self._prune_previous_workspaces(target.parent, target.name)

    def _swap_workspace(self, staging: Path, target: Path) -> Path | None:
        """把 ``staging`` 换到 ``target``，返回旧目录（没有旧目录时为 None）。"""
        previous: Path | None = None
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            previous = target.with_name(f"{target.name}.pre-restore-{stamp}")
            os.replace(target, previous)
        try:
            os.replace(staging, target)
        except OSError:
            if previous is not None and not target.exists():
                with contextlib.suppress(OSError):
                    os.replace(previous, target)
            raise
        return previous

    def _prune_previous_workspaces(self, parent: Path, name: str) -> None:
        """只保留最近一份 ``<name>.pre-restore-*``/``.failed-*``，避免越攒越大。"""
        try:
            candidates = sorted(
                parent.glob(f"{name}.pre-restore-*"), key=lambda p: p.name
            ) + sorted(parent.glob(f"{name}.failed-*"), key=lambda p: p.name)
        except OSError:
            return
        for old in candidates[:-1]:
            self._remove_tree(old)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        try:
            if path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            log.debug("清理目录失败：%s", path, exc_info=True)

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
        manifests = self.list()
        return {
            "last_success_at": self._last_success_at,
            "last_error_at": self._last_error_at,
            "last_error": self._last_error,
            "count": len(manifests),
            # size_bytes 现在含 workspace zip；total_bytes 与 prune 的配额口径一致。
            "total_bytes": sum(item.size_bytes for item in manifests),
            "max_total_bytes": self.max_total_bytes,
            "running": self._running,
            "mirror_dir": str(self.mirror_dir) if self.mirror_dir is not None else "",
            "mirror_last_error_at": self._mirror_last_error_at,
            "mirror_last_error": self._mirror_last_error,
        }

    def last_scheduled_date(self) -> str | None:
        for item in self.list():
            if item.reason == "scheduled":
                return item.created_at[:10]
        return None


def _validate_zip_members(zf: zipfile.ZipFile) -> tuple[int, int]:
    """检查 zip 成员是否可安全解压，返回 ``(文件数, 解压后总字节数)``。

    拒绝：绝对路径、``..`` 越界、盘符、符号链接、超大/超多条目。
    这些检查必须在 ``extractall`` 之前做完，不能边解边判。
    """
    files = 0
    total = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or any(part == ".." for part in path.parts)
            or (path.parts and path.parts[0].endswith(":"))
        ):
            raise BackupError(f"工作区快照包含非法路径：{info.filename}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise BackupError(f"工作区快照包含符号链接：{info.filename}")
        if info.is_dir():
            continue
        files += 1
        total += max(0, int(info.file_size))
        if files > MAX_WORKSPACE_FILES:
            raise BackupError(f"工作区快照文件过多（> {MAX_WORKSPACE_FILES}）")
        if total > MAX_WORKSPACE_BYTES:
            raise BackupError(
                f"工作区快照解压后过大（> {MAX_WORKSPACE_BYTES} 字节）"
            )
    if files == 0:
        raise BackupError("工作区快照没有文件")
    return files, total


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
