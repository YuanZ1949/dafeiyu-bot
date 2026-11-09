"""Sender 闸门回归：去节流后的默认值不再限速，但旧上限仍然生效。

直接驱动 `Sender._execute`，用假传输 + 假时钟，不碰桌面、不真的 sleep。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from dafeiyu.gateway.wechat_bridge.sender import Prepared, SendResult, Sender

CHAT = "测试群"


class FakeTransport:
    """最小传输层：prepare 恒成功，send 直接回成功回执。"""

    requires_desktop = False

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, float]] = []

    def prepare(self, chat_name: str) -> Prepared:
        return Prepared(chat_name=chat_name)

    def send(self, prepared: Prepared, text: str, delay: float) -> SendResult:
        self.sent.append((prepared.chat_name, text, delay))
        return SendResult(
            True,
            "typed",
            "observed",
            chat_name=prepared.chat_name,
            message_id="m",
        )


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = float(now)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_config(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = {
        "kill_switch_file": str(tmp_path / "wechat.stop"),
        "send_min_interval_seconds": 0.0,
        "send_max_per_hour": 0,
        "send_max_per_day": 0,
        "quiet_hours": ("00:00", "00:00"),
        "typing_delay_seconds": (0.6, 1.2),
        "require_foreground_idle": False,
        "account_nickname": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_sender(
    tmp_path: Path,
    *,
    clock=None,
    **overrides,
) -> tuple[Sender, FakeTransport, FakeClock | None]:
    transport = FakeTransport()
    fake_clock = clock
    if fake_clock is None:
        instance = Sender(
            make_config(tmp_path, **overrides),
            transport=transport,
            sleep=lambda _seconds: None,
            uniform=lambda _low, _high: 0.0,
            session_epoch=1,
        )
        return instance, transport, None
    instance = Sender(
        make_config(tmp_path, **overrides),
        transport=transport,
        clock=fake_clock,
        sleep=fake_clock.sleep,
        uniform=lambda _low, _high: 0.0,
        session_epoch=1,
    )
    return instance, transport, fake_clock


def test_zero_caps_and_no_quiet_hours_never_sleep_or_reject(tmp_path: Path) -> None:
    instance, transport, _ = make_sender(tmp_path)
    try:
        started = time.monotonic()
        results = [instance._execute(CHAT, f"第{i}条") for i in range(5)]
        elapsed = time.monotonic() - started
    finally:
        instance.close(timeout=2.0)

    assert elapsed < 0.2, f"elapsed={elapsed:.4f}s"
    assert all(result.ok for result in results), results
    assert all(
        result.error_code not in ("rate_limited", "quiet_hours") for result in results
    )
    assert len(transport.sent) == 5


def test_legacy_quiet_hours_still_rejects(tmp_path: Path) -> None:
    moment = datetime(2026, 11, 1, 23, 45).timestamp()
    instance, transport, clock = make_sender(
        tmp_path,
        clock=FakeClock(moment),
        send_min_interval_seconds=20.0,
        send_max_per_hour=20,
        send_max_per_day=60,
        quiet_hours=("23:30", "08:00"),
    )
    try:
        result = instance._execute(CHAT, "在吗")
    finally:
        instance.close(timeout=2.0)

    assert result.error_code == "quiet_hours"
    assert transport.sent == []
    assert clock is not None and clock.sleeps == []


def test_legacy_rate_cap_and_interval_still_reject(tmp_path: Path) -> None:
    moment = datetime(2026, 11, 1, 12, 0).timestamp()
    instance, transport, clock = make_sender(
        tmp_path,
        clock=FakeClock(moment),
        send_min_interval_seconds=20.0,
        send_max_per_hour=20,
        send_max_per_day=60,
        quiet_hours=("23:30", "08:00"),
    )
    try:
        results = [instance._execute(CHAT, f"第{i}条") for i in range(20)]
        rejected = instance._execute(CHAT, "第20条")
    finally:
        instance.close(timeout=2.0)

    assert all(result.ok for result in results), results
    assert rejected.error_code == "rate_limited"
    assert len(transport.sent) == 20
    # 20s 间隔仍然生效：后 19 次发送前都等了 20s。
    assert clock is not None
    assert clock.sleeps and all(seconds == 20.0 for seconds in clock.sleeps)
