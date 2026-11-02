"""运行环境自检：完整性 SID 映射、temp 不可写、报告与退出码。"""

from __future__ import annotations

import json

import pytest

from dafeiyu import env_probe

INTEGRITIES = {"low", "medium", "high", "system", "unknown"}


@pytest.mark.parametrize(
    ("sid", "expected"),
    [
        ("S-1-16-4096", "low"),
        ("S-1-16-8192", "medium"),
        ("S-1-16-12288", "high"),
        ("S-1-16-16384", "system"),
        # 大小写 / 首尾空白不影响判断。
        (" s-1-16-4096 ", "low"),
        # 未知的完整性 subauthority（如 8448）与普通用户 SID 都算 unknown。
        ("S-1-16-8448", "unknown"),
        ("S-1-5-21-1001", "unknown"),
        ("", "unknown"),
    ],
)
def test_integrity_from_sid_maps_known_levels(sid: str, expected: str) -> None:
    assert env_probe._integrity_from_sid(sid) == expected


def test_probe_environment_reports_unwritable_temp(monkeypatch) -> None:
    """沙箱降权时 tempfile.mkdtemp 会抛 OSError；必须翻成 temp_writable=False。"""

    def boom(*args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(env_probe.tempfile, "mkdtemp", boom)

    probe = env_probe.probe_environment()

    assert probe.temp_writable is False
    assert probe.integrity in INTEGRITIES
    assert "临时目录写入失败" in probe.detail
    assert "Permission denied" in probe.detail


def test_probe_environment_returns_dataclass_fields() -> None:
    probe = env_probe.probe_environment()

    assert probe.integrity in INTEGRITIES
    assert isinstance(probe.temp_writable, bool)
    assert isinstance(probe.detail, str) and probe.detail


def test_format_report_normal_says_ok() -> None:
    report = env_probe.format_report(
        env_probe.EnvProbe("medium", True, "完整性 SID：S-1-16-8192；临时目录可写")
    )

    assert "运行环境自检" in report
    assert "medium" in report
    assert "环境正常" in report


def test_format_report_problem_points_to_p4_p5_not_antivirus() -> None:
    report = env_probe.format_report(
        env_probe.EnvProbe("low", False, "完整性 SID：S-1-16-4096；临时目录写入失败：EPERM")
    )

    assert "low" in report
    assert "docs/deployment-notes-windows.md" in report
    assert "P4" in report and "P5" in report
    assert "别怀疑杀软" in report


@pytest.mark.parametrize(
    ("probe", "expected_code"),
    [
        (env_probe.EnvProbe("medium", True, "临时目录可写"), 0),
        (env_probe.EnvProbe("high", True, "临时目录可写"), 0),
        (env_probe.EnvProbe("unknown", True, "非 Windows"), 0),
        (env_probe.EnvProbe("low", True, "完整性 SID：S-1-16-4096"), 1),
        (env_probe.EnvProbe("medium", False, "临时目录写入失败：EPERM"), 1),
    ],
)
def test_main_exit_code(monkeypatch, capsys, probe, expected_code: int) -> None:
    monkeypatch.setattr(env_probe, "probe_environment", lambda: probe)

    assert env_probe.main([]) == expected_code
    assert "运行环境自检" in capsys.readouterr().out


def test_main_json_output(monkeypatch, capsys) -> None:
    probe = env_probe.EnvProbe("low", False, "完整性 SID：S-1-16-4096")
    monkeypatch.setattr(env_probe, "probe_environment", lambda: probe)

    assert env_probe.main(["--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "integrity": "low",
        "temp_writable": False,
        "detail": "完整性 SID：S-1-16-4096",
    }
