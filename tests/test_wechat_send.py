"""桥发送器契约：串行、闸门顺序、节流与送达确认（假时钟 + 假驱动）。

不装 pyweixin、不真的 sleep、不碰桌面：平台 API 通过 monkeypatch 替换成固定
返回值，时间由 `FakeClock` 推进，所有断言都是确定性的。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import types
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dafeiyu.gateway.wechat_bridge import driver as driver_module
from dafeiyu.gateway.wechat_bridge import sender
from dafeiyu.gateway.wechat_bridge.driver import ChatHandle, DriverError
from dafeiyu.gateway.wechat_bridge.sender import Sender

WINDOW = 4242
ACCOUNT = "小助手"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = float(now)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeDriver:
    """只实现 sender 需要的三个驱动函数，并记录每次调用的时间/线程。"""

    def __init__(self, clock: FakeClock, *, observe: bool = True) -> None:
        self.clock = clock
        self.observe = observe
        self.window = WINDOW
        self.messages: list[dict] = []
        self.calls: list[dict] = []
        self.threads: set[str] = set()
        self.fail_send: Exception | None = None

    def _record(self, action: str, **fields) -> None:
        fields["action"] = action
        fields["at"] = self.clock()
        self.calls.append(fields)
        self.threads.add(threading.current_thread().name)

    def open_chat(self, name: str) -> ChatHandle:
        self._record("open_chat", name=name)
        return ChatHandle(name=name, window=self.window)

    def send_text(self, name: str, text: str, delay: float) -> None:
        self._record("send_text", name=name, text=text, delay=delay)
        if self.fail_send is not None:
            raise self.fail_send
        if self.observe:
            self.messages.append({"nickname": ACCOUNT, "text": text, "kind": "text"})

    def snapshot(self, handle: ChatHandle) -> dict:
        self._record("snapshot", name=handle.name)
        return {
            "chat_name": handle.name,
            "messages": list(self.messages),
            "counts": {"total": len(self.messages)},
        }

    def actions(self) -> list[str]:
        return [call["action"] for call in self.calls]

    def send_times(self) -> list[float]:
        return [call["at"] for call in self.calls if call["action"] == "send_text"]


def make_config(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = {
        "kill_switch_file": str(tmp_path / "wechat.stop"),
        "send_min_interval_seconds": 20.0,
        "send_max_per_hour": 20,
        "send_max_per_day": 60,
        "quiet_hours": ("00:00", "00:00"),
        "typing_delay_seconds": (0.6, 1.2),
        "require_foreground_idle": True,
        "account_nickname": ACCOUNT,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def fake_desktop(monkeypatch):
    """默认桌面可交互、无输入、前台就是 FakeDriver 的窗口。"""
    monkeypatch.setattr(sender, "get_foreground_window", lambda: WINDOW)
    monkeypatch.setattr(sender, "last_input_age", lambda: 99.0)
    monkeypatch.setattr(sender, "desktop_state", lambda: "interactive")


@dataclass
class Harness:
    sender: Sender
    driver: FakeDriver
    clock: FakeClock


@pytest.fixture
def harness(tmp_path: Path):
    created: list[Sender] = []

    def make(
        *,
        now: float = 1_000_000.0,
        observe: bool = True,
        driver: FakeDriver | None = None,
        health: object | None = None,
        **overrides,
    ) -> Harness:
        clock = FakeClock(now)
        fake = driver if driver is not None else FakeDriver(clock, observe=observe)
        instance = Sender(
            make_config(tmp_path, **overrides),
            driver=fake,
            clock=clock,
            sleep=clock.sleep,
            health=health,
            uniform=lambda low, high: high,
            session_epoch=7,
        )
        created.append(instance)
        return Harness(instance, fake, clock)

    yield make
    for instance in created:
        instance.close(timeout=2.0)


# ------------------------------------------------------------------ 节奏 / 频率
def test_pacing_of_five_sends_is_at_least_min_interval(harness) -> None:
    h = harness(send_min_interval_seconds=20.0)

    results = [h.sender.submit("测试群", f"第{i}条") for i in range(5)]

    assert all(result.ok and result.delivered == "observed" for result in results)
    times = h.driver.send_times()
    assert len(times) == 5
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert all(gap >= 20.0 - 1e-9 for gap in gaps), gaps
    # 打字延迟取注入的 uniform 上界，并真的传给了驱动。
    delays = [call["delay"] for call in h.driver.calls if call["action"] == "send_text"]
    assert delays == [1.2] * 5


def test_twenty_first_send_is_rejected_and_alerts(harness) -> None:
    h = harness(
        send_min_interval_seconds=0.0,
        send_max_per_hour=20,
        send_max_per_day=60,
    )

    for index in range(20):
        result = h.sender.submit("测试群", f"第{index}条")
        assert result.ok, result

    rejected = h.sender.submit("测试群", "第20条")

    assert rejected.ok is False
    assert rejected.error_code == "rate_limited"
    assert rejected.delivered == "failed"
    assert len(h.driver.send_times()) == 20
    assert [code for code, _ in h.sender.alerts] == ["rate_limited"]
    assert h.sender.alerts[0][1]["hour_count"] == 20


def test_rate_limit_emits_on_alert_callback(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeDriver(clock)
    alerts: list[tuple[str, dict]] = []
    instance = Sender(
        make_config(tmp_path, send_min_interval_seconds=0.0, send_max_per_hour=1),
        driver=fake,
        clock=clock,
        sleep=clock.sleep,
        uniform=lambda low, high: high,
        on_alert=lambda code, payload: alerts.append((code, payload)),
        session_epoch=7,
    )
    try:
        assert instance.submit("测试群", "一").ok
        assert instance.submit("测试群", "二").error_code == "rate_limited"
    finally:
        instance.close(timeout=2.0)

    assert alerts and alerts[0][0] == "rate_limited"


# --------------------------------------------------------------------- 闸门
def test_kill_switch_never_touches_driver_and_reports_disconnected(
    harness, tmp_path: Path
) -> None:
    h = harness()
    (tmp_path / "wechat.stop").write_text("stop", encoding="utf-8")

    result = h.sender.submit("测试群", "在吗")

    assert result.ok is False
    assert result.error_code == "kill_switch"
    assert result.delivered == "failed"
    assert h.driver.calls == []
    assert h.sender.connected is False
    assert h.sender.health_snapshot()["connected"] is False


def test_foreground_conflict_opens_chat_first_but_never_sends(
    harness, monkeypatch
) -> None:
    monkeypatch.setattr(sender, "get_foreground_window", lambda: 111)
    h = harness()

    result = h.sender.submit("测试群", "在吗")

    assert result.ok is False
    assert result.error_code == "foreground_conflict"
    assert result.delivered == "failed"
    # 顺序证据：先 open_chat，再做前台断言；send_text 一次都没调。
    assert h.driver.actions()[0] == "open_chat"
    assert "send_text" not in h.driver.actions()


def test_foreground_busy_rejects_recent_user_input(harness, monkeypatch) -> None:
    monkeypatch.setattr(sender, "last_input_age", lambda: 0.2)
    h = harness(require_foreground_idle=True)

    result = h.sender.submit("测试群", "在吗")

    assert result.error_code == "foreground_busy"
    assert "send_text" not in h.driver.actions()


def test_desktop_unavailable_rejects(harness, monkeypatch) -> None:
    monkeypatch.setattr(sender, "desktop_state", lambda: "locked")
    h = harness()

    result = h.sender.submit("测试群", "在吗")

    assert result.error_code == "desktop_unavailable"
    assert "send_text" not in h.driver.actions()


def test_quiet_hours_wraps_midnight(harness) -> None:
    moment = datetime(2026, 11, 1, 23, 45).timestamp()
    h = harness(now=moment, quiet_hours=("23:30", "08:00"))

    result = h.sender.submit("测试群", "在吗")

    assert result.error_code == "quiet_hours"
    assert "send_text" not in h.driver.actions()

    daytime = datetime(2026, 11, 1, 12, 0).timestamp()
    h2 = harness(now=daytime, quiet_hours=("23:30", "08:00"))
    assert h2.sender.submit("测试群", "中午好").ok


def test_health_provider_degraded_rejects(harness) -> None:
    class Health:
        desktop = "interactive"
        degraded = True

        def may_send(self) -> bool:
            return True

    h = harness(health=Health())

    assert h.sender.submit("测试群", "在吗").error_code == "degraded"
    assert "send_text" not in h.driver.actions()


# --------------------------------------------------------------------- 并发
def test_concurrent_submits_never_interleave_driver_calls(harness) -> None:
    h = harness(
        send_min_interval_seconds=0.0,
        send_max_per_hour=100,
        send_max_per_day=100,
    )
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(text: str) -> None:
        barrier.wait(timeout=5)
        results[text] = h.sender.submit("测试群", text)

    threads = [
        threading.Thread(target=worker, args=(text,), name=f"submit-{text}")
        for text in ("甲", "乙")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(result.ok for result in results.values())
    actions = h.driver.actions()
    assert len(actions) == 6
    # 每个任务独占 open_chat -> send_text -> snapshot 三元组，不存在交叉。
    for start in (0, 3):
        assert actions[start : start + 3] == ["open_chat", "send_text", "snapshot"]
    # 所有驱动调用都在同一个发送线程里（不是两个提交线程）。
    assert len(h.driver.threads) == 1


# --------------------------------------------------------------------- 失败路径
def test_missing_bubble_returns_failed_without_retry(harness) -> None:
    h = harness(observe=False)

    result = h.sender.submit("测试群", "可能没发出去")

    assert result.ok is False
    assert result.delivered == "failed"
    assert result.error_code == "not_observed"
    assert len(h.driver.send_times()) == 1


def test_driver_send_error_surfaces_and_does_not_retry(harness) -> None:
    h = harness()
    h.driver.fail_send = DriverError("send_failed", "boom")

    result = h.sender.submit("测试群", "在吗")

    assert result.ok is False
    assert result.error_code == "send_failed"
    assert result.delivered == "failed"
    assert len(h.driver.send_times()) == 1


# --------------------------------------------------------------- 启动 / 导入约束
def test_startup_forces_close_weixin_and_maximize_false(
    monkeypatch, tmp_path: Path
) -> None:
    fake_global = SimpleNamespace(close_weixin=True, is_maximize=True)
    module = types.ModuleType("pyweixin")
    module.GlobalConfig = fake_global
    monkeypatch.setitem(sys.modules, "pyweixin", module)

    instance = Sender(
        make_config(tmp_path),
        driver=driver_module,
        clock=lambda: 1_000_000.0,
        sleep=lambda _seconds: None,
    )
    try:
        # 默认是 True，必须被启动逻辑显式翻回 False。
        assert fake_global.close_weixin is False
        assert fake_global.is_maximize is False
    finally:
        instance.close(timeout=2.0)


def test_sender_imports_without_pyweixin() -> None:
    src = Path(sender.__file__).resolve().parents[3]
    env = dict(os.environ, PYTHONPATH=str(src))
    code = (
        "import importlib.abc, sys\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'pyweixin' or name.startswith('pyweixin.'):\n"
        "            raise ImportError('blocked for laziness test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import dafeiyu.gateway.wechat_bridge.sender as s\n"
        "print('ok', s.__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "ok dafeiyu.gateway.wechat_bridge.sender" in result.stdout
