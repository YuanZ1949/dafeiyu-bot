from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dafeiyu.config import AlertRulesConfig, AlertsConfig
from dafeiyu.supervisor.alerts import (
    AlertChannelError,
    AlertManager,
    Incident,
    QQAdminChannel,
    WebhookChannel,
)
from dafeiyu.supervisor.audit import AuditLog


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[Incident, str]] = []

    async def send(self, incident: Incident, event: str) -> None:
        self.sent.append((incident, event))


class ExplodingChannel:
    async def send(self, incident: Incident, event: str) -> None:
        raise RuntimeError("channel down")


def base_snapshot(**overrides):
    snapshot = {
        "ok": True,
        "processes": {
            "bot": {"status": "running", "restart_count": 0},
            "opencode": {"status": "running", "restart_count": 0},
            "napcat": {"status": "running", "restart_count": 0},
        },
        "bot": {
            "ok": True,
            "gateway": {"connected": True},
            "quota": {"paused_until": None},
            "errors": {"recent_count": 0},
        },
        "napcat": {"logged_in": True},
        "disk": {"free_percent": 50.0},
        "backups": {"last_success_at": "2026-10-26T03:00:00+08:00", "last_error_at": None},
    }
    snapshot.update(overrides)
    return snapshot

def down_snapshot():
    snapshot = base_snapshot()
    snapshot["bot"]["ok"] = False
    return snapshot


def wechat_snapshot(*, bridge: str = "running", wechat: dict | None = None):
    snapshot = base_snapshot()
    snapshot["processes"]["wechat_bridge"] = {
        "status": bridge,
        "restart_count": 0,
    }
    snapshot["bot"]["channels"] = {
        "wechat": wechat
        if wechat is not None
        else {
            "connected": True,
            "degraded": False,
            "logged_in": True,
            "desktop": "interactive",
        }
    }
    return snapshot


def manager(tmp_path: Path, clock: Clock, channel, *, cfg: AlertsConfig | None = None):
    return AlertManager(
        cfg or AlertsConfig(),
        tmp_path / "alert-state.json",
        [channel],
        clock=clock,
    )


async def fire_bot_down(mgr: AlertManager, clock: Clock):
    await mgr.evaluate(down_snapshot())
    clock.advance(121)
    await mgr.evaluate(down_snapshot())


async def test_pending_then_firing(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)

    await mgr.evaluate(down_snapshot())
    assert channel.sent == []
    assert mgr.summary()["firing"] == 0

    clock.advance(121)
    await mgr.evaluate(down_snapshot())
    assert channel.sent[0][1] == "firing"
    assert channel.sent[0][0].rule == "bot_down"
    assert mgr.summary()["firing"] == 1


async def test_pending_clears_when_condition_disappears(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)

    await mgr.evaluate(down_snapshot())
    await mgr.evaluate(base_snapshot())
    clock.advance(600)
    await mgr.evaluate(down_snapshot())

    assert channel.sent == []
    assert mgr.summary()["firing"] == 0


async def test_recovery_needs_two_checks(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    await fire_bot_down(mgr, clock)
    assert mgr.summary()["firing"] == 1

    await mgr.evaluate(base_snapshot())
    assert mgr.summary()["firing"] == 1

    await mgr.evaluate(base_snapshot())
    assert channel.sent[-1][1] == "resolved"
    assert mgr.summary()["firing"] == 0
    assert mgr.summary()["resolved_24h"] == 1


async def test_repeat_is_deduped(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    await fire_bot_down(mgr, clock)

    clock.advance(21600 - 1)
    await mgr.evaluate(down_snapshot())
    assert [event for _, event in channel.sent] == ["firing"]

    clock.advance(1)
    await mgr.evaluate(down_snapshot())
    assert [event for _, event in channel.sent] == ["firing", "firing"]
    assert mgr.summary()["firing"] == 1


async def test_channel_failure_does_not_raise(tmp_path: Path) -> None:
    clock = Clock()
    mgr = AlertManager(
        AlertsConfig(),
        tmp_path / "alert-state.json",
        [ExplodingChannel()],
        clock=clock,
    )
    snapshot = base_snapshot()
    snapshot["processes"]["bot"]["status"] = "failed"

    await mgr.evaluate(snapshot)

    assert mgr.summary()["firing"] == 1
    assert (tmp_path / "alert-state.json").exists()


async def test_all_rules_activate(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    snapshot = base_snapshot()
    snapshot["processes"]["bot"]["status"] = "failed"
    snapshot["processes"]["opencode"]["status"] = "failed"
    snapshot["processes"]["napcat"]["status"] = "failed"
    snapshot["bot"]["gateway"]["connected"] = False
    snapshot["bot"]["quota"]["paused_until"] = clock.now + 60
    snapshot["bot"]["errors"]["recent_count"] = 3
    snapshot["disk"]["free_percent"] = 5.0
    snapshot["napcat"]["offline"] = True
    snapshot["backups"] = {
        "last_success_at": "2026-10-26T03:00:00+08:00",
        "last_error_at": "2026-10-26T04:00:00+08:00",
    }

    await mgr.evaluate(snapshot)
    clock.advance(200)
    await mgr.evaluate(snapshot)

    rules = {incident.rule for incident, _event in channel.sent}
    assert rules == {
        "process_failed",
        "bot_down",
        "opencode_down",
        "napcat_down",
        "napcat_offline",
        "quota_pause",
        "reply_errors",
        "disk_low",
        "backup_failed",
    }


async def test_wechat_down_fires_after_threshold(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    snapshot = wechat_snapshot(
        wechat={"connected": False, "logged_in": True, "desktop": "interactive"}
    )

    await mgr.evaluate(snapshot)
    assert channel.sent == []

    clock.advance(179)
    await mgr.evaluate(snapshot)
    assert channel.sent == []

    clock.advance(1)
    await mgr.evaluate(snapshot)
    assert len(channel.sent) == 1
    incident, event = channel.sent[0]
    assert event == "firing"
    assert incident.rule == "wechat_down"
    assert incident.severity == "critical"
    assert incident.title == "微信通道不在线"
    assert incident.detail == "微信桥连接已断开"


@pytest.mark.parametrize(
    ("snapshot_kwargs", "expected"),
    [
        ({"bridge": "failed"}, "微信桥进程不在线（状态：failed）"),
        (
            {
                "wechat": {
                    "connected": True,
                    "logged_in": True,
                    "desktop": "locked",
                }
            },
            "桌面已锁定，微信无法操作",
        ),
        (
            {
                "wechat": {
                    "connected": True,
                    "logged_in": False,
                    "desktop": "interactive",
                }
            },
            "微信未登录",
        ),
    ],
)
async def test_wechat_down_detail_matches_failure_mode(
    tmp_path: Path, snapshot_kwargs: dict, expected: str
) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    snapshot = wechat_snapshot(**snapshot_kwargs)

    await mgr.evaluate(snapshot)
    clock.advance(181)
    await mgr.evaluate(snapshot)

    fired = [
        incident
        for incident, event in channel.sent
        if incident.rule == "wechat_down" and event == "firing"
    ]
    assert len(fired) == 1
    incident = fired[0]
    assert incident.detail == expected
    assert "napcat" not in incident.detail.casefold()


async def test_wechat_down_skipped_when_bridge_disabled(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    snapshot = wechat_snapshot(
        bridge="disabled",
        wechat={"connected": False, "logged_in": False, "desktop": "locked"},
    )

    await mgr.evaluate(snapshot)
    clock.advance(181)
    await mgr.evaluate(snapshot)

    assert channel.sent == []
    assert mgr.summary()["firing"] == 0


async def test_wechat_down_not_raised_for_qq_only(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    snapshot = base_snapshot()
    snapshot["bot"]["channels"] = {"qq": {"connected": True, "degraded": False}}

    await mgr.evaluate(snapshot)
    clock.advance(181)
    await mgr.evaluate(snapshot)

    assert all(incident.rule != "wechat_down" for incident, _event in channel.sent)


async def test_half_offline_alerts_after_pending(tmp_path: Path) -> None:
    """进程在、网关连、isLogin=true，只有 isOffline=true：也要告警。"""
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    snapshot = base_snapshot()
    snapshot["napcat"]["offline"] = True

    await mgr.evaluate(snapshot)
    assert channel.sent == []

    clock.advance(181)
    await mgr.evaluate(snapshot)
    assert channel.sent[0][0].rule == "napcat_offline"
    assert channel.sent[0][0].severity == "critical"


async def test_restart_burst_triggers_process_failed(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)

    await mgr.evaluate(base_snapshot())
    snapshot = base_snapshot()
    snapshot["processes"]["bot"]["restart_count"] = 2
    await mgr.evaluate(snapshot)
    assert channel.sent == []

    snapshot["processes"]["bot"]["restart_count"] = 3
    await mgr.evaluate(snapshot)
    assert channel.sent[0][0].rule == "process_failed"


async def test_events_and_retention(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    await fire_bot_down(mgr, clock)
    await mgr.evaluate(base_snapshot())
    await mgr.evaluate(base_snapshot())

    events = mgr.events()
    assert events[0]["status"] == "resolved"
    clock.advance(1)
    assert mgr.events(include_resolved_hours=0) == []

    clock.advance(24 * 3600 + 1)
    await mgr.evaluate(base_snapshot())
    assert mgr.events() == []


async def test_state_is_persisted_and_reloaded(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    path = tmp_path / "alert-state.json"
    mgr = manager(tmp_path, clock, channel)
    await fire_bot_down(mgr, clock)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"incidents", "since", "recover"}
    assert "bot_down" in raw["incidents"]

    second_channel = FakeChannel()
    restored = AlertManager(AlertsConfig(), path, [second_channel], clock=clock)
    assert restored.summary()["firing"] == 1
    await restored.evaluate(down_snapshot())
    assert second_channel.sent == []


async def test_send_test_does_not_persist(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)

    result = await mgr.send_test()

    assert result == {"sent": 1, "failed": 0}
    assert channel.sent[0][1] == "test"
    assert mgr.summary()["firing"] == 0
    assert mgr.events() == []


async def test_budget_warning_alerts_and_repeats_after_24h(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    snapshot = base_snapshot(
        budget={
            "warnings": [
                {"scope": "global_daily", "ratio": 0.8, "calls": 8, "limit": 10}
            ]
        }
    )

    await mgr.evaluate(snapshot)
    assert len(channel.sent) == 1
    incident, event = channel.sent[0]
    assert event == "firing"
    assert incident.rule == "budget_warning:global_daily:0.8"
    assert incident.fingerprint == "budget_warning:global_daily:0.8"
    assert incident.severity == "warning"
    assert incident.data == {"calls": 8, "limit": 10}

    clock.advance(86400 - 1)
    await mgr.evaluate(snapshot)
    assert len(channel.sent) == 1

    clock.advance(1)
    await mgr.evaluate(snapshot)
    assert len(channel.sent) == 2
    assert channel.sent[1][1] == "firing"
    assert channel.sent[1][0].rule == "budget_warning:global_daily:0.8"


async def test_budget_warning_resolved_rerise_suppressed_for_24h(
    tmp_path: Path,
) -> None:
    clock = Clock()
    channel = FakeChannel()
    mgr = manager(tmp_path, clock, channel)
    snapshot = base_snapshot(
        budget={
            "warnings": [
                {"scope": "monthly", "ratio": 0.8, "calls": 8, "limit": 10}
            ]
        }
    )

    await mgr.evaluate(snapshot)
    assert channel.sent[0][0].rule == "budget_warning:monthly:0.8"

    # 回落 → 两次巡检确认恢复
    await mgr.evaluate(base_snapshot())
    await mgr.evaluate(base_snapshot())
    assert [event for _, event in channel.sent] == ["firing", "resolved"]

    # 24h 内再次超过同一阈值：不得重复告警
    clock.advance(3600)
    await mgr.evaluate(snapshot)
    assert [event for _, event in channel.sent] == ["firing", "resolved"]
    assert mgr.summary()["firing"] == 0

    # 距上次通知满 24h 后才重新告警
    clock.advance(86400 - 3600)
    await mgr.evaluate(snapshot)
    assert [event for _, event in channel.sent] == ["firing", "resolved", "firing"]
    assert channel.sent[-1][0].rule == "budget_warning:monthly:0.8"


async def test_budget_warning_audit_has_only_calls_and_limit(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "audit.jsonl")
    mgr = AlertManager(
        AlertsConfig(),
        tmp_path / "alert-state.json",
        [channel],
        audit=audit,
        clock=clock,
    )
    snapshot = base_snapshot(
        budget={
            "warnings": [
                {"scope": "global_daily", "ratio": 0.8, "calls": 8, "limit": 10}
            ]
        }
    )

    await mgr.evaluate(snapshot)

    rows = [row for row in audit.query(limit=20) if row["action"] == "budget.warning"]
    assert len(rows) == 1
    assert rows[0]["target"] == "global_daily:0.8"
    assert rows[0]["detail"] == {"calls": 8, "limit": 10}


async def test_budget_warning_can_be_disabled(tmp_path: Path) -> None:
    clock = Clock()
    channel = FakeChannel()
    cfg = replace(
        AlertsConfig(),
        rules=replace(AlertRulesConfig(), budget_warning=False),
    )
    mgr = manager(tmp_path, clock, channel, cfg=cfg)
    snapshot = base_snapshot(
        budget={
            "warnings": [
                {"scope": "global_daily", "ratio": 0.8, "calls": 8, "limit": 10}
            ]
        }
    )

    await mgr.evaluate(snapshot)

    assert channel.sent == []
    assert mgr.events() == []


def test_webhook_payload_formats() -> None:
    incident = Incident(
        id="bot_down",
        rule="bot_down",
        severity="critical",
        status="firing",
        fingerprint="bot_down",
        title="bot 不在线",
        detail="连续 120 秒健康检查失败",
        first_seen_at="2026-10-26T12:00:00+08:00",
        last_seen_at="2026-10-26T12:02:00+08:00",
    )
    generic = WebhookChannel("https://example.com/hook").payload(incident, "firing")
    assert generic["version"] == 1
    assert generic["event"] == "firing"
    assert generic["service"] == "dafeiyu"
    assert generic["incident"]["rule"] == "bot_down"

    ntfy = WebhookChannel("https://example.com", format="ntfy").payload(
        incident, "firing"
    )
    assert "bot 不在线" in ntfy["text"]

    wecom = WebhookChannel("https://example.com", format="wecom").payload(
        incident, "firing"
    )
    assert wecom["msgtype"] == "text"
    assert "bot 不在线" in wecom["text"]["content"]


class StubResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class StubSession:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return StubResponse(self.status)


async def test_qq_admin_channel_skips_bot_down() -> None:
    session = StubSession()
    channel = QQAdminChannel("http://127.0.0.1:1", "t", session=session)
    incident = Incident(
        id="bot_down",
        rule="bot_down",
        severity="critical",
        status="firing",
        fingerprint="bot_down",
        title="bot 不在线",
        detail="",
        first_seen_at="",
        last_seen_at="",
    )

    await channel.send(incident, "firing")
    assert session.calls == []


async def test_qq_admin_channel_posts_notify() -> None:
    session = StubSession()
    channel = QQAdminChannel("http://127.0.0.1:1/", "secret", session=session)
    incident = Incident(
        id="disk_low",
        rule="disk_low",
        severity="warning",
        status="firing",
        fingerprint="disk_low",
        title="磁盘空间不足",
        detail="剩余 5%",
        first_seen_at="",
        last_seen_at="",
    )

    await channel.send(incident, "firing")

    url, kwargs = session.calls[0]
    assert url == "http://127.0.0.1:1/api/notify/admin"
    assert kwargs["headers"]["X-Control-Token"] == "secret"
    assert "磁盘空间不足" in kwargs["json"]["text"]
    assert kwargs["json"]["dedupe_key"] == "disk_low"


async def test_qq_admin_channel_raises_on_error_status() -> None:
    channel = QQAdminChannel("http://127.0.0.1:1", session=StubSession(status=503))
    incident = Incident(
        id="disk_low",
        rule="disk_low",
        severity="warning",
        status="firing",
        fingerprint="disk_low",
        title="磁盘空间不足",
        detail="",
        first_seen_at="",
        last_seen_at="",
    )
    with pytest.raises(AlertChannelError):
        await channel.send(incident, "firing")


async def test_qq_admin_channel_resolves_callable_token_at_send_time() -> None:
    current = {"value": "old-token"}
    session = StubSession()
    channel = QQAdminChannel(
        "http://127.0.0.1:1", lambda: current["value"], session=session
    )
    incident = Incident(
        id="disk_low",
        rule="disk_low",
        severity="warning",
        status="firing",
        fingerprint="disk_low",
        title="磁盘空间不足",
        detail="",
        first_seen_at="",
        last_seen_at="",
    )

    await channel.send(incident, "firing")
    current["value"] = "new-token"
    await channel.send(incident, "firing")

    assert session.calls[0][1]["headers"]["X-Control-Token"] == "old-token"
    assert session.calls[1][1]["headers"]["X-Control-Token"] == "new-token"
