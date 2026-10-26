from __future__ import annotations

from pathlib import Path

from dafeiyu.supervisor.audit import AuditLog
from dafeiyu.supervisor.logs import LogHub


def test_loghub_redacts_ring_and_file(tmp_path: Path) -> None:
    """环与落盘共用同一份脱敏文本。"""
    hub = LogHub(tmp_path / "logs")
    hub.write("bot", "Authorization: Bearer abc")

    ring = hub.tail("bot")
    assert len(ring) == 1
    assert "abc" not in ring[0]
    assert "Authorization: ***" in ring[0]

    disk = (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8")
    assert "abc" not in disk
    assert "Authorization: ***" in disk


def test_loghub_redacts_each_line_of_multiline(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs")
    hub.write("supervisor", "line1\nDEEPSEEK_API_KEY=sk-live-1\nline3")

    ring = hub.tail("supervisor")
    assert len(ring) == 3
    assert "sk-live-1" not in "\n".join(ring)
    disk = (tmp_path / "logs" / "supervisor.log").read_text(encoding="utf-8")
    assert "sk-live-1" not in disk
    assert "line1" in disk and "line3" in disk


def test_loghub_keeps_plain_token_usage_text(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs")
    hub.write("bot", "今日 token 用量 1234")

    assert "今日 token 用量 1234" in hub.tail("bot")[0]


def test_loghub_redacts_json_event_line(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs")
    hub.write("opencode", '{"event": "call", "api_key": "sk-live-9"}')

    line = hub.tail("opencode")[0]
    assert "sk-live-9" not in line
    assert '"api_key": "***"' in line
    disk = (tmp_path / "logs" / "opencode.log").read_text(encoding="utf-8")
    assert "sk-live-9" not in disk


def test_audit_detail_is_redacted_recursively(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.write(
        "ops.test",
        detail={
            "token": "abc",
            "note": "token 用量",
            "nested": {"Authorization": "Bearer abc", "items": ["api_key: sk-1", 3]},
        },
    )

    detail = audit.query()[0]["detail"]
    assert detail["token"] == "***"
    assert detail["note"] == "token 用量"
    assert detail["nested"]["Authorization"] == "***"
    assert detail["nested"]["items"] == ["api_key: ***", 3]


def test_audit_sanitize_runs_before_redaction(tmp_path: Path) -> None:
    audit = AuditLog(
        tmp_path / "audit.jsonl",
        sanitize=lambda detail: {"copied": detail.get("payload", "")},
    )
    audit.write("ops.test", detail={"payload": "password=hunter2"})

    assert audit.query()[0]["detail"] == {"copied": "password=***"}
