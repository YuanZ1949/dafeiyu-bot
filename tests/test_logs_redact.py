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


def test_loghub_redacts_sse_events(tmp_path: Path) -> None:
    """SSE 订阅队列拿到的也是同一份脱敏文本。"""
    hub = LogHub(tmp_path / "logs")
    queue = hub.subscribe("bot")

    hub.write("bot", "Authorization: Bearer abc")

    seq, entry = queue.get_nowait()
    assert seq == 1
    assert "Authorization: ***" in entry
    assert "abc" not in entry


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


def test_audit_sensitive_key_object_is_masked_whole(tmp_path: Path) -> None:
    """敏感键的值即使是嵌套对象也整体打码（fail-closed，宁可丢结构）。"""
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.write("ops.test", detail={"token": {"sub": "abc", "exp": 1}})

    assert audit.query()[0]["detail"] == {"token": "***"}


def test_audit_prefixed_and_compound_keys_are_masked(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.write(
        "ops.test",
        detail={
            "DEEPSEEK_API_KEY": "sk-live-1",
            "napcat_token": "abc",
            "secret_key": "abc",
            "private_key": "abc",
            "access_key": "abc",
        },
    )

    detail = audit.query()[0]["detail"]
    assert set(detail.values()) == {"***"}


def test_audit_token_usage_keys_are_not_masked(tmp_path: Path) -> None:
    """描述性键不带凭据语义，保持原样。"""
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.write(
        "ops.test",
        detail={"token_usage": 1234, "note": "token 用量", "token_usage_note": "abc"},
    )

    detail = audit.query()[0]["detail"]
    assert detail == {
        "token_usage": 1234,
        "note": "token 用量",
        "token_usage_note": "abc",
    }


def test_audit_sanitize_runs_before_redaction(tmp_path: Path) -> None:
    audit = AuditLog(
        tmp_path / "audit.jsonl",
        sanitize=lambda detail: {"copied": detail.get("payload", "")},
    )
    audit.write("ops.test", detail={"payload": "password=hunter2"})

    assert audit.query()[0]["detail"] == {"copied": "password=***"}
