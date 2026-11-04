"""微信桥健康探测：桌面/完整性/版本/语言与发送闸门。

所有平台 API 与 pyweixin 驱动都在这里 monkeypatch，不触发任何真实 Windows
调用、不启动微信、不碰网络。
"""

from __future__ import annotations

import inspect
import sys

import pytest

from dafeiyu.config import WeChatConfig
from dafeiyu.gateway.wechat_bridge import health

REQUIRED_KEYS = {
    "v",
    "type",
    "connected",
    "logged_in",
    "desktop",
    "wechat_version",
    "language",
    "integrity",
    "queue_depth",
    "last_event_at",
    "last_send_at",
    "degraded",
    "reason",
}


def make_config(**overrides: object) -> WeChatConfig:
    values: dict[str, object] = {
        "version_min": "4.1.6",
        "language": "zh-CN",
        "poll_seconds": 8.0,
    }
    values.update(overrides)
    return WeChatConfig(**values)


@pytest.fixture
def healthy(monkeypatch):
    """默认的一台「可交互、版本/语言/完整性都正常」的机器。"""
    monkeypatch.setattr(health.driver, "is_running", lambda: True)
    monkeypatch.setattr(
        health.driver,
        "about",
        lambda: {
            "version": "4.1.6.14",
            "nickname": "小助手",
            "platform": "windows",
        },
    )
    monkeypatch.setattr(health.driver, "language", lambda: "zh-CN")
    monkeypatch.setattr(health, "_detect_desktop", lambda: health.DESKTOP_INTERACTIVE)
    monkeypatch.setattr(health, "_own_integrity_sid", lambda: "S-1-16-8192")
    monkeypatch.setattr(health, "_wechat_pids", lambda: [4242])
    monkeypatch.setattr(
        health, "_process_integrity_sid", lambda pid: "S-1-16-8192"
    )


def test_probe_returns_every_required_key(healthy) -> None:
    frame = health.probe(make_config())

    assert set(frame) == REQUIRED_KEYS
    assert frame["v"] == 1
    assert frame["type"] == "health"
    assert frame["connected"] is True
    assert frame["logged_in"] is True
    assert frame["desktop"] == "interactive"
    assert frame["wechat_version"] == "4.1.6.14"
    assert frame["language"] == "zh-CN"
    assert frame["integrity"] == "matching"
    assert frame["queue_depth"] == 0
    assert frame["last_event_at"] is None
    assert frame["last_send_at"] is None
    assert frame["degraded"] is False
    assert frame["reason"] is None


def test_health_probe_gate_is_open_when_healthy(healthy) -> None:
    probe = health.HealthProbe(make_config())

    assert probe.may_send() == (True, None)
    assert probe.degraded is False
    assert probe.desktop == "interactive"


def test_locked_desktop_refuses_sending(healthy, monkeypatch) -> None:
    monkeypatch.setattr(health, "_detect_desktop", lambda: health.DESKTOP_LOCKED)
    probe = health.HealthProbe(make_config())

    frame = probe.probe()
    assert frame["desktop"] == "locked"
    assert frame["degraded"] is True
    assert "desktop_locked" in frame["reason"]

    allowed, reason = probe.may_send()
    assert allowed is False
    assert reason == "desktop_locked"


def test_version_below_min_refuses_sending(healthy, monkeypatch) -> None:
    monkeypatch.setattr(
        health.driver,
        "about",
        lambda: {"version": "4.1.5.9", "nickname": "小助手"},
    )
    probe = health.HealthProbe(make_config(version_min="4.1.6"))

    frame = probe.probe()
    assert frame["wechat_version"] == "4.1.5.9"
    assert frame["degraded"] is True
    assert "version_too_old" in frame["reason"]

    allowed, reason = probe.may_send()
    assert allowed is False
    assert reason == "version_too_old"


def test_version_equal_with_extra_segments_is_accepted(healthy, monkeypatch) -> None:
    monkeypatch.setattr(
        health.driver,
        "about",
        lambda: {"version": "4.1.6.0", "nickname": "小助手"},
    )

    frame = health.probe(make_config(version_min="4.1.6"))

    assert frame["degraded"] is False


def test_language_mismatch_degrades(healthy, monkeypatch) -> None:
    monkeypatch.setattr(health.driver, "language", lambda: "en-US")
    probe = health.HealthProbe(make_config(language="zh-CN"))

    frame = probe.probe()
    assert frame["language"] == "en-US"
    assert frame["degraded"] is True
    assert frame["reason"] == "language_mismatch"

    allowed, reason = probe.may_send()
    assert allowed is False
    assert reason == "language_mismatch"


def test_language_read_failure_degrades(healthy, monkeypatch) -> None:
    def boom() -> str:
        raise health.driver.DriverError("language_unknown", "读不到语言")

    monkeypatch.setattr(health.driver, "language", boom)

    frame = health.probe(make_config())

    assert frame["language"] is None
    assert frame["degraded"] is True
    assert "language_unknown" in frame["reason"]


def test_integrity_mismatch_refuses_sending(healthy, monkeypatch) -> None:
    monkeypatch.setattr(
        health, "_process_integrity_sid", lambda pid: "S-1-16-12288"
    )
    probe = health.HealthProbe(make_config())

    frame = probe.probe()
    assert frame["integrity"] == "mismatched"
    assert frame["degraded"] is True
    assert "integrity_mismatch" in frame["reason"]

    assert probe.may_send() == (False, "integrity_mismatch")


def test_integrity_unknown_fails_closed(healthy, monkeypatch) -> None:
    monkeypatch.setattr(health, "_process_integrity_sid", lambda pid: "")

    frame = health.probe(make_config())

    assert frame["integrity"] == "unknown"
    assert frame["degraded"] is True
    assert "integrity_unknown" in frame["reason"]


def test_wechat_not_running_degrades_to_no_wechat(healthy, monkeypatch) -> None:
    monkeypatch.setattr(health.driver, "is_running", lambda: False)
    probe = health.HealthProbe(make_config())

    frame = probe.probe()
    assert frame["desktop"] == "no_wechat"
    assert frame["logged_in"] is False
    assert frame["wechat_version"] is None
    assert frame["language"] is None
    assert frame["integrity"] == "unknown"
    assert frame["degraded"] is True
    assert frame["reason"] == "no_wechat"

    assert probe.may_send() == (False, "no_wechat")


def test_unknown_desktop_detection_falls_back_to_locked(monkeypatch) -> None:
    # OpenInputDesktop 打不开（锁屏 / 权限不足）就是 locked，绝不猜 interactive。
    monkeypatch.setattr(health, "_open_input_desktop", lambda: 0)

    assert health._detect_desktop() == "locked"


def test_probe_treats_desktop_exception_as_locked(healthy, monkeypatch) -> None:
    def boom() -> str:
        raise RuntimeError("desktop probe blew up")

    monkeypatch.setattr(health, "_detect_desktop", boom)

    frame = health.probe(make_config())

    assert frame["desktop"] == "locked"
    assert "desktop_probe_failed" in frame["reason"]
    assert frame["degraded"] is True


def test_desktop_detection_exception_falls_back_to_locked(monkeypatch) -> None:
    def boom() -> int:
        raise OSError("desktop unavailable")

    monkeypatch.setattr(health, "_open_input_desktop", boom)

    assert health._detect_desktop() == "locked"


def test_desktop_name_not_default_is_locked(monkeypatch) -> None:
    monkeypatch.setattr(health, "_open_input_desktop", lambda: 123)
    monkeypatch.setattr(health, "_desktop_name", lambda handle: "Winlogon")
    monkeypatch.setattr(health, "_foreground_window", lambda: 999)
    monkeypatch.setattr(health, "_close_desktop", lambda handle: None)

    assert health._detect_desktop() == "locked"


def test_missing_foreground_window_is_locked(monkeypatch) -> None:
    monkeypatch.setattr(health, "_open_input_desktop", lambda: 123)
    monkeypatch.setattr(health, "_desktop_name", lambda handle: "Default")
    monkeypatch.setattr(health, "_foreground_window", lambda: 0)
    monkeypatch.setattr(health, "_close_desktop", lambda handle: None)

    assert health._detect_desktop() == "locked"


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop APIs are Windows-only")
def test_default_desktop_with_foreground_is_interactive(monkeypatch) -> None:
    monkeypatch.setattr(health, "_open_input_desktop", lambda: 123)
    monkeypatch.setattr(health, "_desktop_name", lambda handle: "Default")
    monkeypatch.setattr(health, "_foreground_window", lambda: 999)
    monkeypatch.setattr(health, "_close_desktop", lambda handle: None)
    # 无害前台 + 没有可见锁屏窗口，锁屏补充检查必须放行。
    monkeypatch.setattr(health, "_foreground_pid", lambda hwnd: 0)
    monkeypatch.setattr(health, "_visible_lock_ui_window", lambda: False)

    assert health._detect_desktop() == "interactive"


# ---- 锁屏假前台（#36）----------------------------------------------------
def _patch_unlocked_desktop(monkeypatch) -> None:
    """搭好「Default 桌面 + 有前台窗口，但锁屏探针待定」的底座。"""
    monkeypatch.setattr(health, "_open_input_desktop", lambda: 123)
    monkeypatch.setattr(health, "_desktop_name", lambda handle: "Default")
    monkeypatch.setattr(health, "_foreground_window", lambda: 999)
    monkeypatch.setattr(health, "_close_desktop", lambda handle: None)


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop APIs are Windows-only")
def test_lockapp_foreground_is_locked(monkeypatch) -> None:
    """回归 F3：锁屏会话里 LockApp 抢走前台，不能因此判成 interactive。"""
    _patch_unlocked_desktop(monkeypatch)
    monkeypatch.setattr(health, "_foreground_pid", lambda hwnd: 4242)
    monkeypatch.setattr(
        health,
        "_process_image_name",
        lambda pid: "C:\\Windows\\SystemApps\\Microsoft.Windows.StartMenuExperienceHost_cw5n1h2txyewy\\LockApp.exe",
    )
    monkeypatch.setattr(health, "_process_names", list)

    assert health._detect_desktop() == "locked"


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop APIs are Windows-only")
def test_visible_lock_ui_window_is_locked(monkeypatch) -> None:
    _patch_unlocked_desktop(monkeypatch)
    monkeypatch.setattr(health, "_foreground_pid", lambda hwnd: 4242)
    monkeypatch.setattr(
        health, "_process_image_name", lambda pid: "C:\\Apps\\Weixin.exe"
    )
    monkeypatch.setattr(health, "_visible_lock_ui_window", lambda: True)

    assert health._detect_desktop() == "locked"


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop APIs are Windows-only")
def test_suspended_lockapp_leftover_is_not_locked(monkeypatch) -> None:
    """解锁后 LockApp 可能以挂起状态残留；进程存在 ≠ 正在锁屏。"""
    _patch_unlocked_desktop(monkeypatch)
    monkeypatch.setattr(health, "_foreground_pid", lambda hwnd: 4242)
    monkeypatch.setattr(
        health, "_process_image_name", lambda pid: "C:\\Apps\\Weixin.exe"
    )
    monkeypatch.setattr(health, "_visible_lock_ui_window", lambda: False)
    # 仅进程名命中，没有可见窗口：不能据此永久锁死发送。
    monkeypatch.setattr(
        health, "_process_names", lambda: ["explorer.exe", "LockApp.exe"]
    )

    assert health._detect_desktop() == "interactive"


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop APIs are Windows-only")
def test_foreground_pid_unavailable_falls_back_to_interactive(monkeypatch) -> None:
    """新探针读不到前台 pid 时不算「正面检出锁屏」，沿用旧结论（interactive）。"""
    _patch_unlocked_desktop(monkeypatch)
    monkeypatch.setattr(health, "_foreground_pid", lambda hwnd: 0)
    monkeypatch.setattr(
        health,
        "_process_image_name",
        lambda pid: pytest.fail("pid 为 0 时不应读进程镜像"),
    )
    monkeypatch.setattr(health, "_visible_lock_ui_window", lambda: False)

    assert health._detect_desktop() == "interactive"


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop APIs are Windows-only")
def test_lock_ui_probe_exception_keeps_previous_conclusion(monkeypatch) -> None:
    """探针 API 抛错 ≠ 检出锁屏：不能把已确认 interactive 的会话翻转成 locked。"""
    _patch_unlocked_desktop(monkeypatch)

    def boom(hwnd: int) -> int:
        raise OSError("GetWindowThreadProcessId unavailable")

    monkeypatch.setattr(health, "_foreground_pid", boom)
    monkeypatch.setattr(
        health,
        "_process_image_name",
        lambda pid: pytest.fail("探针失败后不应再读镜像"),
    )
    monkeypatch.setattr(health, "_visible_lock_ui_window", lambda: False)

    assert health._detect_desktop() == "interactive"


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop APIs are Windows-only")
def test_harmless_foreground_process_is_interactive(monkeypatch) -> None:
    """前台是微信自己（Weixin.exe）这类无害进程时保持 interactive。"""
    _patch_unlocked_desktop(monkeypatch)
    monkeypatch.setattr(health, "_foreground_pid", lambda hwnd: 4242)
    monkeypatch.setattr(
        health, "_process_image_name", lambda pid: "C:\\Apps\\Weixin.exe"
    )
    monkeypatch.setattr(health, "_visible_lock_ui_window", lambda: False)

    assert health._detect_desktop() == "interactive"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("LockApp.exe", True),
        ("LogonUI.exe", True),
        ("lockapp.exe", True),
        ("LOGONUI.EXE", True),
        ("C:\\Windows\\SystemApps\\...\\LockApp.exe", True),
        ("explorer.exe", False),
        ("", False),
        (None, False),
    ],
)
def test_is_lock_ui_exe(name: str, expected: bool) -> None:
    assert health._is_lock_ui_exe(name) is expected


def test_locked_to_interactive_does_not_resend_refused_messages(
    healthy, monkeypatch
) -> None:
    """锁屏期间的拒绝是纯函数，不留队列：恢复后 must not 自动补发任何旧内容。"""
    monkeypatch.setattr(health, "_detect_desktop", lambda: health.DESKTOP_LOCKED)
    probe = health.HealthProbe(make_config())

    # 发送器被拒绝时只会拿到 (False, reason)，没有可提交的消息载体。
    assert list(inspect.signature(health.HealthProbe.may_send).parameters) == ["self"]
    assert probe.may_send() == (False, "desktop_locked")
    assert probe.queue_depth == 0
    assert probe.last_send_at is None

    # 恢复交互后闸门打开，但没有任何「补发」动作，也没有保留的队列。
    monkeypatch.setattr(
        health, "_detect_desktop", lambda: health.DESKTOP_INTERACTIVE
    )
    assert probe.may_send() == (True, None)
    assert probe.queue_depth == 0
    assert probe.last_send_at is None
    assert not hasattr(probe, "_pending")
    assert not hasattr(probe, "_queue")


def test_health_probe_tracks_timestamps(healthy) -> None:
    probe = health.HealthProbe(make_config())
    probe.mark_event(1761888100)
    probe.mark_send(1761888113)

    frame = probe.probe()

    assert frame["last_event_at"] == 1761888100
    assert frame["last_send_at"] == 1761888113


def test_frames_are_emitted_every_poll_seconds(healthy) -> None:
    sleeps: list[float] = []
    probe = health.HealthProbe(
        make_config(poll_seconds=2.5), sleep=sleeps.append
    )

    frames = list(probe.frames(iterations=3))

    assert len(frames) == 3
    assert all(frame["type"] == "health" for frame in frames)
    assert all(frame["degraded"] is False for frame in frames)
    assert sleeps == [2.5, 2.5]


def test_emit_alias_yields_frames(healthy) -> None:
    probe = health.HealthProbe(make_config(), sleep=lambda seconds: None)

    frames = list(probe.emit(iterations=1))

    assert len(frames) == 1
    assert frames[0]["v"] == 1


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("4.1.6.14", "4.1.6", 1),
        ("4.1.6", "4.1.6.0", 0),
        ("4.1", "4.1.6", -1),
        ("v4.1.6", "4.1.6", 0),
    ],
)
def test_compare_versions(left: str, right: str, expected: int) -> None:
    assert health._compare_versions(left, right) == expected


def test_compare_versions_rejects_non_numeric() -> None:
    assert health._compare_versions("unknown", "4.1.6") is None


# ---- 微信进程名（#35：4.x 改名 Weixin.exe）--------------------------------
def test_is_wechat_exe_matches_both_names_case_insensitively() -> None:
    assert health.WECHAT_EXE == "wechat.exe"  # 旧单名常量保持不变
    assert health._is_wechat_exe("Weixin.exe") is True
    assert health._is_wechat_exe("WEIXIN.EXE") is True
    assert health._is_wechat_exe("WeChat.exe") is True
    assert health._is_wechat_exe("wechat.exe ") is True  # 首尾空白不影响
    assert health._is_wechat_exe("explorer.exe") is False
    assert health._is_wechat_exe("Weibo.exe") is False
    assert health._is_wechat_exe("") is False
    assert health._is_wechat_exe(None) is False


def test_wechat_pids_match_weixin_and_wechat_names(monkeypatch) -> None:
    entries = [
        ("Weixin.exe", 101),
        ("weixin.exe", 102),
        ("WeChat.exe", 103),
        ("explorer.exe", 104),
        ("Weixin-updater.exe", 105),
    ]
    monkeypatch.setattr(health, "_snap_processes", lambda: iter(entries))

    assert health._wechat_pids() == [101, 102, 103]


def test_process_names_lists_snapshot_names(monkeypatch) -> None:
    monkeypatch.setattr(
        health,
        "_snap_processes",
        lambda: iter([("explorer.exe", 1), ("Weixin.exe", 2)]),
    )

    assert health._process_names() == ["explorer.exe", "Weixin.exe"]


# ------------------------------------------------------------------ hook 出站（非 UIA）
def test_hook_outbound_skips_uia_probe_while_locked(healthy, monkeypatch) -> None:
    def boom(*args: object, **kwargs: object):
        raise AssertionError("outbound=hook 不允许探测 UIA")

    monkeypatch.setattr(health.driver, "about", boom)
    monkeypatch.setattr(health.driver, "language", boom)
    monkeypatch.setattr(health, "_detect_desktop", lambda: health.DESKTOP_LOCKED)

    frame = health.probe(make_config(outbound="hook"))

    assert frame["logged_in"] is True
    assert frame["desktop"] == health.DESKTOP_LOCKED
    assert frame["degraded"] is False
    assert frame["reason"] is None
    assert frame["integrity"] == health.INTEGRITY_UNKNOWN


def test_hook_outbound_may_send_while_locked(healthy, monkeypatch) -> None:
    monkeypatch.setattr(health, "_detect_desktop", lambda: health.DESKTOP_LOCKED)
    probe = health.HealthProbe(make_config(outbound="hook"))

    assert probe.may_send() == (True, None)


def test_hook_outbound_still_fails_when_wechat_not_running(healthy, monkeypatch) -> None:
    monkeypatch.setattr(health.driver, "is_running", lambda: False)

    frame = health.probe(make_config(outbound="hook"))

    assert frame["logged_in"] is False
    assert frame["desktop"] == "no_wechat"
    assert frame["degraded"] is True
    assert frame["reason"] == "no_wechat"
