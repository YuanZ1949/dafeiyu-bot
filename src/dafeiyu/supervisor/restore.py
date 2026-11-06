from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .backup import BackupError, BackupManager, validate_database

log = logging.getLogger(__name__)


class RestoreError(Exception):
    def __init__(self, stage: str, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.stage = stage
        self.status = status


class RestoreService:
    """停 bot → 校验 → 预备份 → 原子替换 → 启动；失败回滚。"""

    def __init__(
        self,
        *,
        db_path: Path,
        backups: BackupManager,
        supervisor: Any,
        audit: AuditLog | None = None,
        bot_name: str = "bot",
        ready_check: Callable[[], Awaitable[bool]] | None = None,
        ready_timeout: float = 30.0,
        workspace_dir: Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backups = backups
        self.supervisor = supervisor
        self.audit = audit
        self.bot_name = bot_name
        self.ready_check = ready_check
        self.ready_timeout = ready_timeout
        # 工作区恢复（R18）：None = 未启用工作区，只允许数据库恢复。
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def restore(
        self,
        upload_path: Path,
        *,
        actor: str,
        ip: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        if self._lock.locked():
            raise RestoreError("locked", "已有恢复正在进行", 409)
        async with self._lock:
            started = time.monotonic()
            upload = Path(upload_path)
            ok, reason = validate_database(upload)
            if not ok:
                raise RestoreError("validate", f"文件校验失败：{reason}", 400)

            try:
                pre = await self.backups.create("pre-restore")
            except BackupError as exc:
                raise RestoreError("backup", f"恢复前备份失败：{exc}", 500) from exc

            try:
                await self.supervisor.stop(self.bot_name)
            except Exception as exc:
                raise RestoreError(
                    "stop", f"无法停止 bot：{exc.__class__.__name__}", 500
                ) from exc

            try:
                self._replace(upload)
            except OSError as exc:
                await self._rollback(pre.filename, started, actor, ip, request_id)
                raise RestoreError("replace", f"替换数据库失败：{exc}", 500) from exc

            try:
                await self.supervisor.start(self.bot_name)
                await self._wait_ready()
            except Exception as exc:
                await self._rollback(pre.filename, started, actor, ip, request_id)
                raise RestoreError(
                    "health", f"恢复后启动失败：{exc.__class__.__name__}", 500
                ) from exc

            result = {
                "ok": True,
                "restored_from": upload.name,
                "pre_restore_backup": pre.filename,
                "bot_started": True,
                "duration_seconds": round(time.monotonic() - started, 1),
            }
            self._audit(
                actor,
                ip,
                request_id,
                upload.name,
                "ok",
                {"stage": "done", "duration_seconds": result["duration_seconds"]},
            )
            return result

    async def _wait_ready(self) -> None:
        if self.ready_check is None:
            return
        ready = await asyncio.wait_for(self.ready_check(), self.ready_timeout)
        if not ready:
            raise RuntimeError("健康检查未通过")

    async def restore_workspace(
        self,
        backup_id: str,
        *,
        actor: str,
        ip: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        """把某份备份里的工作区快照解压回 ``workspace.dir``（R18）。

        与数据库恢复同样的顺序：停 bot → 校验并换目录 → 启动 → 健康检查。
        启动失败会把工作区换回恢复前的目录，再尝试把 bot 拉起来。
        """
        if self.workspace_dir is None:
            raise RestoreError("unsupported", "未启用工作区，无法恢复", 400)
        if self._lock.locked():
            raise RestoreError("locked", "已有恢复正在进行", 409)
        async with self._lock:
            started = time.monotonic()
            try:
                await self.supervisor.stop(self.bot_name)
            except Exception as exc:
                raise RestoreError(
                    "stop", f"无法停止 bot：{exc.__class__.__name__}", 500
                ) from exc

            try:
                restored = await asyncio.to_thread(
                    self.backups.restore_workspace, backup_id, self.workspace_dir
                )
            except BackupError as exc:
                await self._restart_quietly()
                raise RestoreError("replace", str(exc), 400) from exc
            except Exception as exc:  # noqa: BLE001 - 兜底成可读错误
                await self._restart_quietly()
                raise RestoreError(
                    "replace", f"工作区恢复失败：{exc.__class__.__name__}", 500
                ) from exc

            try:
                await self.supervisor.start(self.bot_name)
                await self._wait_ready()
            except Exception as exc:
                self.backups.rollback_workspace(restored)
                await self._restart_quietly()
                raise RestoreError(
                    "health", f"恢复后启动失败：{exc.__class__.__name__}", 500
                ) from exc

            result = {
                "ok": True,
                "backup_id": backup_id,
                "restored_files": restored.restored_files,
                "restored_bytes": restored.restored_bytes,
                "bot_started": True,
                "duration_seconds": round(time.monotonic() - started, 1),
            }
            self._audit(
                actor,
                ip,
                request_id,
                backup_id,
                "ok",
                {"stage": "done", "duration_seconds": result["duration_seconds"]},
                action="backup.restore_workspace",
            )
            return result

    async def _restart_quietly(self) -> None:
        with contextlib.suppress(Exception):
            await self.supervisor.start(self.bot_name)

    def _replace(self, upload: Path) -> None:
        self._remove_sidecars()
        os.replace(upload, self.db_path)

    def _remove_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            with_sidecar = Path(f"{self.db_path}{suffix}")
            try:
                with_sidecar.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("删除 %s 失败：%s", with_sidecar, exc)

    async def _rollback(
        self,
        pre_filename: str,
        started: float,
        actor: str,
        ip: str,
        request_id: str,
    ) -> None:
        pre_path = self.backups.backup_dir / pre_filename
        try:
            with contextlib.suppress(Exception):
                await self.supervisor.stop(self.bot_name)
            self._remove_sidecars()
            if pre_path.exists():
                shutil.copy2(pre_path, self.db_path)
            else:
                raise OSError("pre-restore 备份文件缺失")
            with contextlib.suppress(Exception):
                await self.supervisor.start(self.bot_name)
            result = "rolled_back"
            detail = {"stage": "rollback", "ok": True}
        except Exception as exc:
            log.exception("恢复回滚失败")
            result = "rollback_failed"
            detail = {"stage": "rollback", "ok": False, "error": str(exc)}
        self._audit(
            actor,
            ip,
            request_id,
            pre_filename,
            result,
            {**detail, "duration_seconds": round(time.monotonic() - started, 1)},
        )

    def _audit(
        self,
        actor: str,
        ip: str,
        request_id: str,
        target: str,
        result: str,
        detail: dict[str, Any],
        *,
        action: str = "backup.restore",
    ) -> None:
        if self.audit is None:
            return
        self.audit.write(
            action,
            actor=actor,
            target=target,
            result=result,
            detail=detail,
            request_id=request_id,
            ip=ip,
        )
