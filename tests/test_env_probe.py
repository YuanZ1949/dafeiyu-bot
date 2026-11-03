"""运行环境自检：完整性 SID 映射、temp 不可写、报告与退出码。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dafeiyu import env_probe
from dafeiyu.config import WeChatConfig

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

# ---- 微信前置检查 --------------------------------------------------------


def _wechat_config(**overrides) -> WeChatConfig:
    values = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 8790,
        "bridge_token": "",
        "account_nickname": "小号",
        "version_min": "4.1.6",
        "language": "zh-CN",
    }
    values.update(overrides)
    return WeChatConfig(**values)


def _patch_probes(monkeypatch, **overrides) -> None:
    defaults = {
        "pids": [1234],
        "about": {"version": "4.1.6.14"},
        "language": "zh-CN",
        "desktop": "interactive",
        "integrity": "matching",
        "session": 1,
        "driver": True,
        "port_free": True,
    }
    defaults.update(overrides)
    monkeypatch.setattr(env_probe, "_wechat_pids", lambda: list(defaults["pids"]))
    monkeypatch.setattr(env_probe, "_wechat_about", lambda: defaults["about"])
    monkeypatch.setattr(env_probe, "_wechat_language", lambda: defaults["language"])
    monkeypatch.setattr(env_probe, "_wechat_desktop", lambda: defaults["desktop"])
    monkeypatch.setattr(
        env_probe, "_wechat_integrity_state", lambda running: defaults["integrity"]
    )
    monkeypatch.setattr(env_probe, "_current_session_id", lambda: defaults["session"])
    monkeypatch.setattr(
        env_probe, "_pyweixin_available", lambda: defaults["driver"]
    )
    monkeypatch.setattr(
        env_probe,
        "_port_free",
        lambda port, host="127.0.0.1": defaults["port_free"],
    )


def _by_id(checks):
    return {check["id"]: check for check in checks}


def test_probe_wechat_disabled_returns_eight_skips(monkeypatch) -> None:
    # 禁用时不允许触发任何真实探测。
    monkeypatch.setattr(
        env_probe, "_wechat_pids", lambda: pytest.fail("不应探测进程")
    )

    checks = env_probe.probe_wechat(_wechat_config(enabled=False))

    assert [check["id"] for check in checks] == list(env_probe.WECHAT_CHECK_IDS)
    assert len(checks) == 8
    assert all(check["status"] == "SKIP" for check in checks)
    assert all("gateway.wechat.enabled=false" in check["detail"] for check in checks)


def test_probe_wechat_none_is_disabled() -> None:
    checks = env_probe.probe_wechat(None)

    assert len(checks) == 8
    assert all(check["status"] == "SKIP" for check in checks)


def test_probe_wechat_accepts_settings(monkeypatch) -> None:
    _patch_probes(monkeypatch)

    checks = env_probe.probe_wechat(SimpleNamespace(wechat=_wechat_config()))

    assert len(checks) == 8
    assert env_probe.wechat_ok(checks) is True


def test_probe_wechat_all_pass(monkeypatch) -> None:
    _patch_probes(monkeypatch)

    checks = env_probe.probe_wechat(_wechat_config())

    assert [check["id"] for check in checks] == list(env_probe.WECHAT_CHECK_IDS)
    assert [check["status"] for check in checks] == ["PASS"] * 8
    assert env_probe.wechat_ok(checks) is True


def test_probe_wechat_missing_nickname_fails_config_ready(monkeypatch) -> None:
    _patch_probes(monkeypatch)

    checks = env_probe.probe_wechat(_wechat_config(account_nickname="  "))

    config_ready = _by_id(checks)["wechat.config_ready"]
    assert config_ready["status"] == "FAIL"
    assert "account_nickname" in config_ready["detail"]


def test_probe_wechat_config_ready_pass(monkeypatch) -> None:
    _patch_probes(monkeypatch)

    checks = env_probe.probe_wechat(_wechat_config())

    config_ready = _by_id(checks)["wechat.config_ready"]
    assert config_ready["status"] == "PASS"
    assert "小号" in config_ready["detail"]


def test_probe_wechat_version_too_old_fails(monkeypatch) -> None:
    _patch_probes(monkeypatch, about={"version": "4.0.0.1"})

    checks = env_probe.probe_wechat(_wechat_config())

    version = _by_id(checks)["wechat.version_ok"]
    assert version["status"] == "FAIL"
    assert "4.0.0.1" in version["detail"]


def test_probe_wechat_language_mismatch_fails(monkeypatch) -> None:
    _patch_probes(monkeypatch, language="en-US")

    checks = env_probe.probe_wechat(_wechat_config())

    language = _by_id(checks)["wechat.language_ok"]
    assert language["status"] == "FAIL"
    assert "en-US" in language["detail"]


def test_probe_wechat_integrity_mismatch_fails(monkeypatch) -> None:
    _patch_probes(monkeypatch, integrity="mismatched")

    checks = env_probe.probe_wechat(_wechat_config())

    assert _by_id(checks)["wechat.integrity_match"]["status"] == "FAIL"


def test_probe_wechat_integrity_unknown_is_skip(monkeypatch) -> None:
    _patch_probes(monkeypatch, integrity="unknown")

    checks = env_probe.probe_wechat(_wechat_config())

    assert _by_id(checks)["wechat.integrity_match"]["status"] == "SKIP"


def test_probe_wechat_locked_desktop_fails(monkeypatch) -> None:
    _patch_probes(monkeypatch, desktop="locked")

    checks = env_probe.probe_wechat(_wechat_config())

    assert _by_id(checks)["wechat.desktop_interactive"]["status"] == "FAIL"


def test_probe_wechat_session_zero_fails(monkeypatch) -> None:
    _patch_probes(monkeypatch, session=0)

    checks = env_probe.probe_wechat(_wechat_config())

    desktop = _by_id(checks)["wechat.desktop_interactive"]
    assert desktop["status"] == "FAIL"
    assert "Session 0" in desktop["detail"]


def test_probe_wechat_rdp_session_hints_handoff(monkeypatch) -> None:
    _patch_probes(monkeypatch)
    monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#7")

    checks = env_probe.probe_wechat(_wechat_config())

    desktop = _by_id(checks)["wechat.desktop_interactive"]
    assert desktop["status"] == "PASS"
    assert "RDP" in desktop["detail"]
    assert "dafeiyu.rdp handoff" in desktop["detail"]


def test_probe_wechat_console_session_has_no_rdp_hint(monkeypatch) -> None:
    _patch_probes(monkeypatch)
    monkeypatch.setenv("SESSIONNAME", "Console")

    checks = env_probe.probe_wechat(_wechat_config())

    desktop = _by_id(checks)["wechat.desktop_interactive"]
    assert desktop["status"] == "PASS"
    assert "dafeiyu.rdp" not in desktop["detail"]


def test_probe_wechat_missing_pyweixin(monkeypatch) -> None:
    _patch_probes(monkeypatch, driver=False)

    checks = env_probe.probe_wechat(_wechat_config())
    by_id = _by_id(checks)

    assert by_id["wechat.driver_importable"]["status"] == "FAIL"
    assert by_id["wechat.version_ok"]["status"] == "SKIP"
    assert by_id["wechat.language_ok"]["status"] == "SKIP"


def test_probe_wechat_busy_port_fails(monkeypatch) -> None:
    _patch_probes(monkeypatch, port_free=False)

    checks = env_probe.probe_wechat(_wechat_config())

    port = _by_id(checks)["wechat.bridge_port_free"]
    assert port["status"] == "FAIL"
    assert "已被占用" in port["detail"]


def test_probe_wechat_not_running_skips_dependent_checks(monkeypatch) -> None:
    _patch_probes(monkeypatch, pids=[])

    checks = env_probe.probe_wechat(_wechat_config())
    by_id = _by_id(checks)

    assert by_id["wechat.process_running"]["status"] == "FAIL"
    assert by_id["wechat.version_ok"]["status"] == "SKIP"
    assert by_id["wechat.language_ok"]["status"] == "SKIP"
    assert by_id["wechat.integrity_match"]["status"] == "SKIP"
    assert by_id["wechat.desktop_interactive"]["status"] == "SKIP"


def test_wechat_ok_semantics() -> None:
    pass_check = {"id": "a", "status": "PASS", "detail": ""}
    skip_check = {"id": "b", "status": "SKIP", "detail": ""}
    fail_check = {"id": "c", "status": "FAIL", "detail": ""}

    assert env_probe.wechat_ok([pass_check, skip_check]) is True
    assert env_probe.wechat_ok([pass_check, fail_check]) is False
    assert env_probe.wechat_ok([]) is True


def test_format_wechat_config_hides_token_value() -> None:
    cfg = _wechat_config(bridge_token="super-secret-token")

    text = env_probe.format_wechat_config(cfg)

    assert "super-secret-token" not in text
    assert "已配置（不显示）" in text
    assert "bridge_token：" in text


def test_format_wechat_config_empty_token_says_unset() -> None:
    text = env_probe.format_wechat_config(_wechat_config())

    assert "未配置" in text
    assert "bridge_host：127.0.0.1" in text
    assert "bridge_port：8790" in text
    assert "version_min：4.1.6" in text
    assert "language：zh-CN" in text
    assert "kill_switch_file：data/wechat.stop" in text
    assert "allow_test_inject：false" in text


def test_format_wechat_report_lists_eight_lines_and_conclusion() -> None:
    checks = [
        {"id": check_id, "status": "PASS", "detail": "ok"}
        for check_id in env_probe.WECHAT_CHECK_IDS
    ]

    report = env_probe.format_wechat_report(checks)

    assert "微信前置检查（8 项）" in report
    for check_id in env_probe.WECHAT_CHECK_IDS:
        assert f"[PASS] {check_id}" in report
    assert "结论：微信前置检查通过（8 项全部 PASS）" in report


def test_format_wechat_report_reports_failures() -> None:
    checks = [
        {"id": check_id, "status": "PASS", "detail": "ok"}
        for check_id in env_probe.WECHAT_CHECK_IDS
    ]
    checks[0] = {"id": checks[0]["id"], "status": "FAIL", "detail": "坏了"}

    report = env_probe.format_wechat_report(checks)

    assert "[FAIL] wechat.process_running：坏了" in report
    assert "结论：微信前置检查失败（1 项 FAIL）" in report
