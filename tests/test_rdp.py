"""RDP + tscon 会话交接：探测、计划、执行与 CLI。

不真的调用 tscon、不真的等待、不碰当前桌面：平台探测与 runner 全部注入。
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from dafeiyu import rdp


def _state(**overrides: Any) -> rdp.SessionState:
    values: dict[str, Any] = {
        "platform": "win32",
        "session_name": "RDP-Tcp#1",
        "session_id": 2,
        "protocol": 2,
        "is_rdp": True,
        "is_admin": True,
    }
    values.update(overrides)
    return rdp.SessionState(**values)


class FakeCompleted:
    def __init__(
        self, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Runner:
    def __init__(self, outcome: Any = None) -> None:
        self._outcome = outcome if outcome is not None else FakeCompleted()
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> FakeCompleted:
        self.calls.append((list(command), kwargs))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ------------------------------------------------------------------ detect
def test_detect_uses_wts_protocol_over_session_name(monkeypatch) -> None:
    monkeypatch.setattr(rdp, "current_session_id", lambda: 2)
    monkeypatch.setattr(rdp, "client_protocol_type", lambda sid=None: 2)
    monkeypatch.setattr(rdp, "is_admin", lambda: True)

    state = rdp.detect(env={"SESSIONNAME": "Console"}, platform="win32")

    assert state.is_rdp is True
    assert state.session_id == 2
    assert state.protocol == 2
    assert state.is_admin is True


def test_detect_falls_back_to_session_name(monkeypatch) -> None:
    monkeypatch.setattr(rdp, "current_session_id", lambda: 3)
    monkeypatch.setattr(rdp, "client_protocol_type", lambda sid=None: None)
    monkeypatch.setattr(rdp, "is_admin", lambda: False)

    rdp_state = rdp.detect(env={"SESSIONNAME": "RDP-Tcp#7"}, platform="win32")
    console_state = rdp.detect(env={"SESSIONNAME": "Console"}, platform="win32")

    assert rdp_state.is_rdp is True
    assert console_state.is_rdp is False
    assert console_state.protocol is None


def test_detect_non_windows_is_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(
        rdp, "current_session_id", lambda: pytest.fail("非 Windows 不应探测")
    )
    monkeypatch.setattr(
        rdp, "client_protocol_type", lambda sid=None: pytest.fail("非 Windows 不应探测")
    )

    state = rdp.detect(env={"SESSIONNAME": "RDP-Tcp#1"}, platform="linux")

    assert state.supported is False
    assert state.is_rdp is False
    assert state.session_id is None


# ------------------------------------------------------------------ plan
def test_plan_noop_on_console() -> None:
    plan = rdp.plan_handoff(_state(is_rdp=False, protocol=0, session_name="Console"))

    assert plan.action == "noop"
    assert plan.command == ()
    assert "无需交接" in plan.reason


def test_plan_handoff_when_rdp_and_admin() -> None:
    plan = rdp.plan_handoff(_state())

    assert plan.action == "handoff"
    assert plan.command == ("tscon", "2", "/dest:console")


def test_plan_needs_admin_still_shows_command() -> None:
    plan = rdp.plan_handoff(_state(is_admin=False))

    assert plan.action == "needs_admin"
    assert "管理员" in plan.reason
    assert plan.command == ("tscon", "2", "/dest:console")


def test_plan_unknown_session_and_unsupported() -> None:
    unknown = rdp.plan_handoff(_state(session_id=None))
    unsupported = rdp.plan_handoff(_state(platform="linux", is_rdp=False))

    assert unknown.action == "unknown_session"
    assert unknown.command == ()
    assert unsupported.action == "unsupported"
    assert "仅 Windows" in unsupported.reason


# ------------------------------------------------------------------ handoff
def test_handoff_noop_does_not_run_tscon() -> None:
    runner = Runner()

    result = rdp.handoff(
        state=_state(is_rdp=False, protocol=0, session_name="Console"),
        runner=runner,
    )

    assert result.ok is True
    assert result.verified is True
    assert runner.calls == []


def test_handoff_needs_admin_does_not_run_tscon() -> None:
    runner = Runner()

    result = rdp.handoff(state=_state(is_admin=False), runner=runner)

    assert result.ok is False
    assert result.error_code == "needs_admin"
    assert runner.calls == []


def test_handoff_runs_tscon_and_verifies_protocol(monkeypatch) -> None:
    runner = Runner(FakeCompleted(0))
    sleeps = Sleeps()
    protocols = iter([2, 0])
    monkeypatch.setattr(
        rdp, "client_protocol_type", lambda sid=None: next(protocols, 0)
    )

    result = rdp.handoff(
        state=_state(),
        runner=runner,
        sleep=sleeps,
        timeout=7.5,
    )

    assert result.ok is True
    assert result.verified is True
    assert result.error_code is None
    assert runner.calls == [
        (
            ["tscon", "2", "/dest:console"],
            {"capture_output": True, "text": True, "timeout": 7.5, "check": False},
        )
    ]
    assert sleeps.calls == [rdp._VERIFY_INTERVAL]


def test_handoff_unverified_is_still_a_success(monkeypatch) -> None:
    sleeps = Sleeps()
    monkeypatch.setattr(rdp, "client_protocol_type", lambda sid=None: 2)

    result = rdp.handoff(
        state=_state(),
        runner=Runner(FakeCompleted(0)),
        sleep=sleeps,
        verify_attempts=3,
        verify_interval=0.1,
    )

    assert result.ok is True
    assert result.verified is False
    assert "未能确认" in result.detail
    assert sleeps.calls == [0.1, 0.1]


def test_handoff_tscon_nonzero_exit_reports_stderr() -> None:
    result = rdp.handoff(
        state=_state(),
        runner=Runner(FakeCompleted(5, stderr="Access is denied.")),
    )

    assert result.ok is False
    assert result.error_code == "tscon_failed"
    assert "5" in result.detail
    assert "Access is denied" in result.detail


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (FileNotFoundError("tscon"), "tscon_missing"),
        (subprocess.TimeoutExpired("tscon", 15), "tscon_timeout"),
        (OSError("boom"), "tscon_failed"),
    ],
)
def test_handoff_runner_failures(outcome: Exception, code: str) -> None:
    result = rdp.handoff(state=_state(), runner=Runner(outcome), timeout=3)

    assert result.ok is False
    assert result.error_code == code


# ------------------------------------------------------------------ CLI
def test_cli_status_json(monkeypatch, capsys) -> None:
    state = _state()
    monkeypatch.setattr(rdp, "detect", lambda **kwargs: state)

    code = rdp.main(["status", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"]["is_rdp"] is True
    assert payload["session"]["session_id"] == 2
    assert payload["plan"]["action"] == "handoff"
    assert payload["command"] == ["tscon", "2", "/dest:console"]


def test_cli_status_text_mentions_reason(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        rdp, "detect", lambda **kwargs: _state(is_rdp=False, protocol=0)
    )

    assert rdp.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "连接协议：控制台" in out
    assert "无需交接" in out


def test_cli_handoff_needs_admin_returns_3(monkeypatch, capsys) -> None:
    monkeypatch.setattr(rdp, "detect", lambda **kwargs: _state(is_admin=False))

    assert rdp.main(["handoff"]) == 3
    assert "管理员" in capsys.readouterr().out


def test_cli_handoff_success_returns_0(monkeypatch, capsys) -> None:
    monkeypatch.setattr(rdp, "detect", lambda **kwargs: _state())
    monkeypatch.setattr(
        rdp,
        "handoff",
        lambda **kwargs: rdp.HandoffResult(
            True, None, "ok", rdp.plan_handoff(_state())
        ),
    )

    assert rdp.main(["handoff", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
