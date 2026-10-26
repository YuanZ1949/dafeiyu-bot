from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

import aiohttp

from dafeiyu.config import AlertsConfig
from dafeiyu.supervisor.audit import AuditLog
from dafeiyu.timeutil import SHANGHAI

log = logging.getLogger(__name__)

MAX_ALERTS_PER_CHECK = 5
RESOLVED_RETENTION_SECONDS = 24 * 3600
RESTART_WINDOW_SECONDS = 600
RESTART_BURST_COUNT = 3
DISK_REPEAT_SECONDS = 12 * 3600


@dataclass
class Incident:
    id: str
    rule: str
    severity: str
    status: str
    fingerprint: str
    title: str
    detail: str
    first_seen_at: str
    last_seen_at: str
    resolved_at: str | None = None
    notify_count: int = 0
    last_notified_at: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Condition:
    active: bool
    severity: str
    title: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


class AlertChannelError(Exception):
    pass


class AlertChannel(Protocol):
    async def send(self, incident: Incident, event: str) -> None: ...


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), SHANGHAI).isoformat(timespec="seconds")


def _parse_ts(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


_BUDGET_SCOPES = frozenset({"per_chat", "global_daily", "monthly", "cost"})
_BUDGET_SCOPE_LABELS = {
    "per_chat": "会话日额度",
    "global_daily": "全局日额度",
    "monthly": "月度额度",
    "cost": "月度费用",
}


def _ratio_text(value: Any) -> str:
    """指纹里的比例保持 0.8 / 1.0 这种稳定写法。"""
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return str(value)


def _format_text(incident: Incident, event: str) -> str:
    prefix = {
        "firing": "【大肥鱼告警】",
        "resolved": "【大肥鱼恢复】",
        "test": "【大肥鱼测试】",
    }.get(event, "【大肥鱼告警】")
    text = f"{prefix}{incident.title}"
    if incident.detail:
        text = f"{text}\n{incident.detail}"
    return text[:1000]


class AlertManager:
    """8 条规则 → pending/firing/resolved 状态机；通道失败不影响状态。"""

    def __init__(
        self,
        cfg: AlertsConfig,
        state_path: str | Path,
        channels: list[AlertChannel],
        *,
        audit: AuditLog | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.cfg = cfg
        self.state_path = Path(state_path)
        self.channels = list(channels)
        self.audit = audit
        self._clock = clock or time.time
        self._incidents: dict[str, Incident] = {}
        self._since: dict[str, float] = {}
        self._recover: dict[str, int] = {}
        self._restart_marks: dict[str, tuple[float, int]] = {}
        self._restart_events: dict[str, list[tuple[float, int]]] = {}
        self._load()

    # ---- 状态读写 -------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        names = {item.name for item in fields(Incident)}
        incidents = raw.get("incidents")
        if isinstance(incidents, dict):
            for key, item in incidents.items():
                if not isinstance(item, dict):
                    continue
                try:
                    self._incidents[str(key)] = Incident(
                        **{name: item[name] for name in names if name in item}
                    )
                except TypeError:
                    continue
        since = raw.get("since")
        if isinstance(since, dict):
            for key, value in since.items():
                try:
                    self._since[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        recover = raw.get("recover")
        if isinstance(recover, dict):
            for key, value in recover.items():
                try:
                    self._recover[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue

    def _save(self) -> None:
        payload = {
            "incidents": {key: asdict(item) for key, item in self._incidents.items()},
            "since": self._since,
            "recover": self._recover,
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("告警状态写入失败：%s", exc)

    # ---- 规则 -----------------------------------------------------
    @staticmethod
    def _process_status(processes: dict[str, Any], name: str) -> str:
        item = processes.get(name) or {}
        return str(item.get("status") or "stopped")

    def _track_restarts(self, processes: dict[str, Any], now: float) -> int:
        for name, item in processes.items():
            try:
                count = int((item or {}).get("restart_count") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            previous = self._restart_marks.get(name)
            if previous is not None and count > previous[1]:
                self._restart_events.setdefault(name, []).append(
                    (now, count - previous[1])
                )
            self._restart_marks[name] = (now, count)
        total = 0
        for name, events in list(self._restart_events.items()):
            kept = [(ts, n) for ts, n in events if now - ts <= RESTART_WINDOW_SECONDS]
            self._restart_events[name] = kept
            total += sum(n for _ts, n in kept)
        return total

    def _conditions(
        self, snapshot: dict[str, Any], now: float
    ) -> dict[str, Condition]:
        processes = snapshot.get("processes") or {}
        if not isinstance(processes, dict):
            processes = {}
        bot = snapshot.get("bot") or {}
        if not isinstance(bot, dict):
            bot = {}
        rules = self.cfg.rules
        conditions: dict[str, Condition] = {}

        burst = self._track_restarts(processes, now)
        failed = [
            name
            for name in processes
            if self._process_status(processes, name) == "failed"
        ]
        if failed or burst >= RESTART_BURST_COUNT:
            detail_parts = []
            if failed:
                detail_parts.append("失败进程：" + "、".join(sorted(failed)))
            if burst >= RESTART_BURST_COUNT:
                detail_parts.append(
                    f"10 分钟内累计重启 {burst} 次"
                )
            conditions["process_failed"] = Condition(
                True, "critical", "进程异常", "；".join(detail_parts)
            )

        bot_status = self._process_status(processes, "bot")
        bot_ok = bot.get("ok") is True
        if bot_status != "running" or not bot_ok:
            detail = (
                f"bot 进程状态：{bot_status}"
                if bot_status != "running"
                else "bot 健康检查连续失败"
            )
            conditions["bot_down"] = Condition(True, "critical", "bot 不在线", detail)

        opencode_status = self._process_status(processes, "opencode")
        if opencode_status != "running":
            conditions["opencode_down"] = Condition(
                True,
                "critical",
                "opencode 不在线",
                f"opencode 进程状态：{opencode_status}",
            )

        gateway = bot.get("gateway") or {}
        if not isinstance(gateway, dict):
            gateway = {}
        napcat_status = self._process_status(processes, "napcat")
        disconnected = gateway.get("connected") is False
        if napcat_status != "running" or disconnected:
            detail = (
                f"napcat 进程状态：{napcat_status}"
                if napcat_status != "running"
                else "QQ 网关连接已断开"
            )
            conditions["napcat_down"] = Condition(
                True, "critical", "NapCat 不在线", detail
            )

        quota = bot.get("quota") or {}
        if not isinstance(quota, dict):
            quota = {}
        paused_until = quota.get("paused_until")
        try:
            quota_active = bool(paused_until) and float(paused_until) > now
        except (TypeError, ValueError):
            quota_active = False
        if quota_active and rules.quota_pause:
            conditions["quota_pause"] = Condition(
                True, "warning", "额度熔断中", "记忆写入暂停，等待额度恢复"
            )

        errors = bot.get("errors") or {}
        if not isinstance(errors, dict):
            errors = {}
        try:
            recent_errors = int(errors.get("recent_count") or 0)
        except (TypeError, ValueError):
            recent_errors = 0
        if recent_errors >= rules.reply_error_count:
            conditions["reply_errors"] = Condition(
                True,
                "warning",
                "回复错误偏多",
                f"{rules.reply_error_window_seconds} 秒内 {recent_errors} 次错误",
            )

        disk = snapshot.get("disk") or {}
        if not isinstance(disk, dict):
            disk = {}
        free_percent = disk.get("free_percent")
        try:
            disk_low = (
                free_percent is not None
                and float(free_percent) < rules.disk_free_percent
            )
        except (TypeError, ValueError):
            disk_low = False
        if disk_low:
            conditions["disk_low"] = Condition(
                True,
                "warning",
                "磁盘空间不足",
                f"可用空间 {free_percent}%，低于阈值 {rules.disk_free_percent}%",
            )

        backups = snapshot.get("backups") or {}
        if not isinstance(backups, dict):
            backups = {}
        last_error = backups.get("last_error_at")
        last_success = backups.get("last_success_at")
        backup_failed = bool(last_error) and (
            not last_success or str(last_error) > str(last_success)
        )
        if backup_failed:
            conditions["backup_failed"] = Condition(
                True, "warning", "备份失败", "最近一次备份未成功"
            )

        budget = snapshot.get("budget") or {}
        if rules.budget_warning and isinstance(budget, dict):
            warnings = budget.get("warnings") or []
            if isinstance(warnings, list):
                for warning in warnings:
                    if not isinstance(warning, dict):
                        continue
                    scope = str(warning.get("scope") or "").strip()
                    if scope not in _BUDGET_SCOPES:
                        continue
                    try:
                        ratio = float(warning.get("ratio", 0))
                    except (TypeError, ValueError):
                        continue
                    ratio_text = _ratio_text(ratio)
                    calls = warning.get("calls", 0)
                    limit = warning.get("limit", 0)
                    detail = (
                        f"{_BUDGET_SCOPE_LABELS[scope]}达到 {ratio_text}"
                        f"（{calls}/{limit}）"
                    )
                    conditions[f"budget_warning:{scope}:{ratio_text}"] = Condition(
                        True,
                        "warning",
                        "预算预警",
                        detail,
                        {"calls": calls, "limit": limit},
                    )
        return conditions

    def _pending_seconds(self, rule: str) -> int:
        rules = self.cfg.rules
        return {
            "bot_down": rules.bot_down_seconds,
            "opencode_down": rules.opencode_down_seconds,
            "napcat_down": rules.napcat_down_seconds,
        }.get(rule, 0)

    def _repeat_seconds(self, rule: str) -> int:
        if rule.startswith("budget_warning:"):
            return 86400
        if rule == "disk_low":
            return max(DISK_REPEAT_SECONDS, int(self.cfg.repeat_seconds))
        return int(self.cfg.repeat_seconds)

    # ---- 状态机 ---------------------------------------------------
    async def evaluate(self, snapshot: dict[str, Any]) -> list[Incident]:
        now = self._clock()
        conditions = self._conditions(snapshot, now)
        outgoing: list[tuple[Incident, str]] = []

        for fingerprint, incident in list(self._incidents.items()):
            condition = conditions.get(fingerprint)
            if condition is None:
                if incident.status == "pending":
                    self._incidents.pop(fingerprint, None)
                    self._since.pop(fingerprint, None)
                elif incident.status == "firing":
                    confirmations = self._recover.get(fingerprint, 0) + 1
                    self._recover[fingerprint] = confirmations
                    if confirmations >= 2:
                        incident.status = "resolved"
                        incident.resolved_at = _iso(now)
                        incident.last_seen_at = _iso(now)
                        self._recover.pop(fingerprint, None)
                        outgoing.append((incident, "resolved"))
                continue

            if incident.status == "resolved":
                # 已恢复的规则再次触发：开始一轮新的 pending/firing。
                self._incidents.pop(fingerprint, None)
                continue

            self._recover.pop(fingerprint, None)
            incident.severity = condition.severity
            incident.title = condition.title
            incident.detail = condition.detail
            incident.data = dict(condition.data)
            incident.last_seen_at = _iso(now)

            if incident.status == "pending":
                started = self._since.get(fingerprint, now)
                if now - started >= self._pending_seconds(incident.rule):
                    incident.status = "firing"
                    incident.first_seen_at = _iso(started)
                    incident.notify_count += 1
                    incident.last_notified_at = _iso(now)
                    self._since.pop(fingerprint, None)
                    outgoing.append((incident, "firing"))
                continue

            if now - _parse_ts(incident.last_notified_at) >= self._repeat_seconds(
                incident.rule
            ):
                incident.notify_count += 1
                incident.last_notified_at = _iso(now)
                outgoing.append((incident, "firing"))

        for fingerprint, condition in conditions.items():
            if fingerprint in self._incidents:
                continue
            started = self._since.setdefault(fingerprint, now)
            incident = Incident(
                id=fingerprint,
                rule=fingerprint,
                severity=condition.severity,
                status="pending",
                fingerprint=fingerprint,
                title=condition.title,
                detail=condition.detail,
                first_seen_at=_iso(started),
                last_seen_at=_iso(now),
                data=dict(condition.data),
            )
            if now - started >= self._pending_seconds(fingerprint):
                incident.status = "firing"
                incident.notify_count = 1
                incident.last_notified_at = _iso(now)
                self._since.pop(fingerprint, None)
                outgoing.append((incident, "firing"))
            self._incidents[fingerprint] = incident

        for fingerprint, incident in list(self._incidents.items()):
            if (
                incident.status == "resolved"
                and now - _parse_ts(incident.resolved_at) > RESOLVED_RETENTION_SECONDS
            ):
                self._incidents.pop(fingerprint, None)

        self._save()
        await self._dispatch(outgoing)
        return [incident for incident, _event in outgoing]

    async def _dispatch(self, outgoing: list[tuple[Incident, str]]) -> None:
        if len(outgoing) > MAX_ALERTS_PER_CHECK:
            log.warning("单轮告警 %d 条，已合并为 alert_flood", len(outgoing))
            stamp = _iso(self._clock())
            flood = Incident(
                id="alert_flood",
                rule="alert_flood",
                severity="warning",
                status="firing",
                fingerprint="alert_flood",
                title="告警风暴",
                detail=f"本轮 {len(outgoing)} 条告警已合并，请检查系统状态",
                first_seen_at=stamp,
                last_seen_at=stamp,
                notify_count=1,
                last_notified_at=stamp,
            )
            outgoing = outgoing[: MAX_ALERTS_PER_CHECK - 1] + [(flood, "firing")]

        for incident, event in outgoing:
            for channel in self.channels:
                try:
                    await channel.send(incident, event)
                except Exception as exc:  # noqa: BLE001 - 通道失败不触发新告警
                    log.warning(
                        "告警通道发送失败（rule=%s event=%s）：%s",
                        incident.rule,
                        event,
                        exc,
                    )
            if self.audit is not None:
                self.audit.write(
                    f"alert.{event}",
                    actor="system",
                    target=incident.fingerprint,
                    detail={
                        "rule": incident.rule,
                        "severity": incident.severity,
                    },
                )
                if event == "firing" and incident.rule.startswith("budget_warning:"):
                    # 预算审计只记用量与上限，绝不带消息正文。
                    self.audit.write(
                        "budget.warning",
                        actor="system",
                        target=incident.rule.split(":", 1)[1],
                        detail={
                            "calls": incident.data.get("calls", 0),
                            "limit": incident.data.get("limit", 0),
                        },
                    )

    # ---- 查询 -----------------------------------------------------
    def events(self, include_resolved_hours: int = 24) -> list[dict[str, Any]]:
        cutoff = self._clock() - max(0, int(include_resolved_hours)) * 3600
        rows: list[dict[str, Any]] = []
        for incident in self._incidents.values():
            if (
                incident.status == "resolved"
                and _parse_ts(incident.resolved_at) < cutoff
            ):
                continue
            rows.append(asdict(incident))
        rows.sort(key=lambda row: str(row["last_seen_at"]), reverse=True)
        rows.sort(key=lambda row: 0 if row["status"] == "firing" else 1)
        return rows

    def summary(self) -> dict[str, int]:
        now = self._clock()
        firing = sum(
            1 for item in self._incidents.values() if item.status == "firing"
        )
        resolved = sum(
            1
            for item in self._incidents.values()
            if item.status == "resolved"
            and now - _parse_ts(item.resolved_at) <= RESOLVED_RETENTION_SECONDS
        )
        return {"firing": firing, "resolved_24h": resolved}

    async def send_test(self) -> dict[str, int]:
        stamp = _iso(self._clock())
        incident = Incident(
            id="test",
            rule="test",
            severity="info",
            status="firing",
            fingerprint="test",
            title="测试告警",
            detail="这是一条测试告警，无需处理。",
            first_seen_at=stamp,
            last_seen_at=stamp,
            notify_count=1,
            last_notified_at=stamp,
        )
        sent = 0
        failed = 0
        for channel in self.channels:
            try:
                await channel.send(incident, "test")
                sent += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.warning("测试告警发送失败：%s", exc)
        return {"sent": sent, "failed": failed}


class WebhookChannel:
    def __init__(
        self,
        url: str,
        format: str = "generic",
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.url = (url or "").strip()
        self.format = format
        self._session = session
        self.timeout = timeout

    def payload(self, incident: Incident, event: str) -> dict[str, Any]:
        text = _format_text(incident, event)
        base: dict[str, Any] = {
            "version": 1,
            "event": event,
            "service": "dafeiyu",
            "incident": {
                "rule": incident.rule,
                "severity": incident.severity,
                "title": incident.title,
                "detail": incident.detail,
                "first_seen_at": incident.first_seen_at,
                "last_seen_at": incident.last_seen_at,
            },
        }
        if self.format == "ntfy":
            base["text"] = text
            base["title"] = incident.title
        elif self.format in ("wecom", "dingtalk"):
            base["msgtype"] = "text"
            base["text"] = {"content": text}
        return base

    async def send(self, incident: Incident, event: str) -> None:
        if not self.url:
            return
        payload = self.payload(incident, event)
        last_error: Exception | None = None
        for delay in (0.0, 1.0, 3.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                if self._session is not None:
                    await self._post(self._session, payload)
                else:
                    async with aiohttp.ClientSession() as session:
                        await self._post(session, payload)
                return
            except Exception as exc:  # noqa: BLE001 - 统一重试
                last_error = exc
        raise AlertChannelError(f"webhook 发送失败：{last_error}")

    async def _post(
        self, session: aiohttp.ClientSession, payload: dict[str, Any]
    ) -> None:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with session.post(self.url, json=payload, timeout=timeout) as resp:
            if resp.status >= 400:
                raise AlertChannelError(f"webhook 返回 HTTP {resp.status}")


class QQAdminChannel:
    def __init__(
        self,
        bot_url: str,
        token: str = "",
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.bot_url = (bot_url or "").rstrip("/")
        self.token = token
        self._session = session
        self.timeout = timeout

    async def send(self, incident: Incident, event: str) -> None:
        # bot 自己掉线时这条通道必然失败，默认只走 webhook。
        if incident.rule == "bot_down":
            return
        payload = {
            "text": _format_text(incident, event),
            "dedupe_key": incident.fingerprint[:64],
        }
        if self._session is not None:
            await self._post(self._session, payload)
            return
        async with aiohttp.ClientSession() as session:
            await self._post(session, payload)

    async def _post(
        self, session: aiohttp.ClientSession, payload: dict[str, Any]
    ) -> None:
        url = f"{self.bot_url}/api/notify/admin"
        headers = {"X-Control-Token": self.token} if self.token else {}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with session.post(
            url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            if resp.status >= 400:
                raise AlertChannelError(f"bot 通知返回 HTTP {resp.status}")


__all__ = [
    "AlertChannel",
    "AlertChannelError",
    "AlertManager",
    "Condition",
    "Incident",
    "QQAdminChannel",
    "WebhookChannel",
]
