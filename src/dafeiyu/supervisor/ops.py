from __future__ import annotations

from dataclasses import dataclass

from .alerts import AlertManager
from .audit import AuditLog
from .backup import BackupManager
from .health import HealthMonitor
from .restore import RestoreService


@dataclass
class OpsServices:
    """dashboard 与 supervisor 共用的运维服务集合；缺省字段表示未启用。"""

    health: HealthMonitor
    alerts: AlertManager | None = None
    backups: BackupManager | None = None
    restore: RestoreService | None = None
    audit: AuditLog | None = None


__all__ = ["OpsServices"]
