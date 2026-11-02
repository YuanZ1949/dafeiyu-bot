"""生产发送接线（P0 #34）：run() -> 内置 send_text -> Sender -> UiThread。

覆盖：
- 真 `run()` + 真 WebSocket + 真 `Sender` + 真 `UiThread`，只有驱动是假的；
- 驱动调用全部发生在 STA/UIA 线程，显示名（不是 alias）传给驱动；
- 未知 alias / 非 allowlist 群 / 非空 `at` 都在碰驱动之前失败；
- 注入 `frame_source` 时没有 Sender，发送按 `chat_name_unresolved` 拒绝；
- `SendResult` -> 回执映射（含 `not_observed` -> typed）；
- 服务器按动作选超时：发送 120s（测试注入 0.5s），其余仍 5s（测试注入 0.05s）；
- `_UiThreadDriver` 的死锁护栏 / 停机拒绝。

不装 pyweixin、不碰真桌面：`sender` 模块的窗口/输入/桌面函数都被 monkeypatch。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.asyncio.client import connect

import dafeiyu.gateway.wechat_bridge.__main__ as bridge_main
import dafeiyu.gateway.wechat_bridge.server as server_module
from dafeiyu.config import WeChatConfig
from dafeiyu.gateway.wechat_bridge import sender as sender_module
from dafeiyu.gateway.wechat_bridge.driver import DriverError
from dafeiyu.gateway.wechat_bridge.identity import PeerRegistry
from dafeiyu.gateway.wechat_bridge.poller import UiThread
from dafeiyu.gateway.wechat_bridge.sender import Sender, SendResult
from dafeiyu.gateway.wechat_bridge.server import BridgeServer

TOKEN = "task34-wiring-token"
ACCOUNT = "小助手"
WINDOW = 4242
ALLOWED = "老友群"
STEP = 5.0

HEALTH_FRAME = {
    "v": 1,
    "type": "health",
    "channel": "wechat",
    "connected": True,
    "logged_in": True,
    "desktop": "interactive",
    "queue_depth": 0,
}


# --------------------------------------------------------------- 小工具
def _wechat(tmp_path: Path, **overrides: Any) -> WeChatConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 0,
        "bridge_token": TOKEN,
        "account_nickname": ACCOUNT,
        "group_allowlist": frozenset({ALLOWED}),
        "max_groups": 3,
        "poll_seconds": 1.0,
        # 关闭节流/频率/静默时段，避免测试依赖真实时钟与本地时区。
        "send_min_interval_seconds": 0.0,
        "send_max_per_hour": 0,
        "send_max_per_day": 0,
        "quiet_hours": ("00:00", "00:00"),
        "typing_delay_seconds": (0.0, 0.0),
        "kill_switch_file": str(tmp_path / "wechat.stop"),
        "require_foreground_idle": True,
    }
    values.update(overrides)
    return WeChatConfig(**values)


def _sender_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        kill_switch_file=str(tmp_path / "wechat.stop"),
        send_min_interval_seconds=0.0,
        send_max_per_hour=0,
        send_max_per_day=0,
        quiet_hours=("00:00", "00:00"),
        typing_delay_seconds=(0.0, 0.0),
        require_foreground_idle=True,
        account_nickname=ACCOUNT,
    )


def _uri(port: int) -> str:
    return f"ws://127.0.0.1:{port}/wechat-bridge"


async def _wait_for_port(holder: dict[str, Any], *, timeout: float = STEP) -> BridgeServer:
    deadline = time.monotonic() + timeout
    while True:
        server = holder.get("server")
        if server is not None and server.port is not None:
            return server
        if time.monotonic() > deadline:
            raise AssertionError("微信桥没有在超时前绑定端口")
        await asyncio.sleep(0.01)


async def _recv_until(ws: Any, predicate: Any, *, limit: int = 40) -> dict[str, Any]:
    for _ in range(limit):
        frame = json.loads(await asyncio.wait_for(ws.recv(), STEP))
        if predicate(frame):
            return frame
    raise AssertionError(f"等待的目标帧没有出现（上限 {limit} 帧）")


async def _command(
    harness: Any, action: str, params: dict[str, Any], command_id: str
) -> dict[str, Any]:
    await asyncio.wait_for(
        harness.ws.send(
            json.dumps(
                {"v": 1, "id": command_id, "action": action, "params": params}
            )
        ),
        STEP,
    )
    return await _recv_until(harness.ws, lambda frame: frame.get("id") == command_id)


@pytest.fixture(autouse=True)
def fake_desktop(monkeypatch):
    """Sender 的闸门只看这三个平台函数；测试固定为「可交互、无输入、目标前台」。"""
    monkeypatch.setattr(sender_module, "get_foreground_window", lambda: WINDOW)
    monkeypatch.setattr(sender_module, "last_input_age", lambda: 99.0)
    monkeypatch.setattr(sender_module, "desktop_state", lambda: "interactive")


# --------------------------------------------------------------- 假驱动 / 假轮询
class WiringDriver:
    """记录调用顺序、显示名与执行线程；快照里回显已发出的消息。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.messages: list[dict[str, Any]] = []
        self.probe_threads: list[int] = []

    def is_running(self) -> bool:
        self.probe_threads.append(threading.get_ident())
        return False

    def about(self) -> Any:
        self.probe_threads.append(threading.get_ident())
        return None

    def language(self) -> Any:
        self.probe_threads.append(threading.get_ident())
        return None

    def open_chat(self, name: str) -> Any:
        self.calls.append(("open_chat", name, threading.get_ident()))
        return SimpleNamespace(name=name, window=WINDOW)

    def send_text(self, name: str, text: str, delay: float) -> None:
        self.calls.append(("send_text", name, threading.get_ident()))
        self.messages.append({"nickname": ACCOUNT, "text": text, "kind": "text"})

    def snapshot(self, handle: Any) -> dict[str, Any]:
        self.calls.append(
            ("snapshot", str(getattr(handle, "name", "")), threading.get_ident())
        )
        return {
            "chat_name": str(getattr(handle, "name", "")),
            "messages": list(self.messages),
            "counts": {"total": len(self.messages)},
        }

    def actions(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakePoller:
    """只实现 run() 用到的轮询接缝；真正执行 UI op 的仍是 `UiThread`。"""

    def __init__(self, tmp_path: Path, driver: Any) -> None:
        self.ui = UiThread(name="task34-wiring-uia")
        self.registry = PeerRegistry(tmp_path / "peers.json")
        self.driver = driver
        self.stopped = False
        self.abort_calls = 0

    async def start(self) -> None:
        self.ui.start()

    def abort_sends(self) -> int:
        self.abort_calls += 1
        self.stopped = True
        return self.ui.cancel_pending("send")

    def stop(self, *, timeout: float = STEP) -> bool:
        self.stopped = True
        return self.ui.stop(timeout=timeout)

    def reset_for_resume(self) -> bool:
        self.stopped = False
        return True

    def enqueue_send(self, fn: Any) -> Any:
        return self.ui.submit(fn, kind="send")

    async def request_send(self, fn: Any) -> Any:
        return await asyncio.wrap_future(self.enqueue_send(fn))

    async def frames(self):
        while not self.stopped:
            yield dict(HEALTH_FRAME)
            await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def _bridge(
    tmp_path: Path,
    *,
    peers: tuple[str, ...] = (),
    frame_source: Any = None,
):
    driver = WiringDriver()
    wechat = _wechat(tmp_path)
    holder: dict[str, Any] = {}
    pollers: list[FakePoller] = []

    def server_factory(*args: Any, **kwargs: Any) -> BridgeServer:
        server = BridgeServer(*args, **kwargs)
        holder["server"] = server
        return server

    def poller_factory(_wechat: Any, raw: Any) -> FakePoller:
        poller = FakePoller(tmp_path, raw)
        poller.registry.resolve(ALLOWED, kind="group")
        for name in peers:
            poller.registry.resolve(name, kind="group")
        pollers.append(poller)
        return poller

    run_task = asyncio.create_task(
        bridge_main.run(
            settings=SimpleNamespace(wechat=wechat),
            driver_factory=lambda: driver,
            server_factory=server_factory,
            frame_source=frame_source,
            poller_factory=None if frame_source is not None else poller_factory,
        )
    )
    ws = None
    try:
        server = await _wait_for_port(holder)
        ws = await asyncio.wait_for(
            connect(
                _uri(server.port),
                additional_headers={"Authorization": f"Bearer {TOKEN}"},
            ),
            STEP,
        )
        await _recv_until(ws, lambda frame: frame.get("type") == "health")
        yield SimpleNamespace(
            ws=ws,
            server=server,
            driver=driver,
            pollers=pollers,
            poller=pollers[0] if pollers else None,
            run_task=run_task,
        )
    finally:
        if ws is not None and not run_task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    ws.send(
                        json.dumps(
                            {"v": 1, "id": "__stop__", "action": "stop", "params": {}}
                        )
                    ),
                    STEP,
                )
        if not run_task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(run_task), STEP)
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(run_task, STEP)
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()


# --------------------------------------------------------------- (1) 端到端发送
async def test_run_send_text_reaches_driver_on_sta_thread(tmp_path: Path) -> None:
    async with _bridge(tmp_path) as harness:
        alias = harness.poller.registry.live_aliases(ALLOWED)[0]
        receipt = await _command(
            harness,
            "send_text",
            {"chat_id": f"wxg:{alias}", "text": "你好"},
            "m1",
        )

        assert receipt["ok"] is True
        assert receipt["error_code"] is None
        assert receipt["delivered"] == "observed"
        assert isinstance(receipt["message_id"], str)
        assert receipt["message_id"]

        # 驱动看到的是显示名（alias 只用于查注册表）。
        assert harness.driver.actions() == ["open_chat", "send_text", "snapshot"]
        assert [call[1] for call in harness.driver.calls] == [ALLOWED] * 3
        # 所有 UIA 调用都在 UiThread 上，绝不在事件循环/Sender worker 上。
        ui_ident = harness.poller.ui.thread_ident
        assert ui_ident is not None
        assert {call[2] for call in harness.driver.calls} == {ui_ident}
        assert threading.get_ident() not in {call[2] for call in harness.driver.calls}


async def test_startup_health_probe_runs_on_ui_thread_with_injected_driver(
    tmp_path: Path,
) -> None:
    """启动探测必须投到 UIA 线程，并用 run() 注入的 driver（不是模块全局）。"""
    async with _bridge(tmp_path) as harness:
        assert harness.poller is not None
        ui_ident = harness.poller.ui.thread_ident
        assert ui_ident is not None
        assert harness.driver.probe_threads
        assert set(harness.driver.probe_threads) == {ui_ident}
        assert threading.get_ident() not in harness.driver.probe_threads


async def test_unknown_alias_and_not_allowlisted_never_touch_driver(
    tmp_path: Path,
) -> None:
    async with _bridge(tmp_path, peers=("隔壁群",)) as harness:
        receipt = await _command(
            harness,
            "send_text",
            {"chat_id": "wxg:nobody", "text": "hi"},
            "u1",
        )
        assert receipt["ok"] is False
        assert receipt["error_code"] == "chat_name_unresolved"

        other = harness.poller.registry.live_aliases("隔壁群")[0]
        receipt = await _command(
            harness,
            "send_text",
            {"chat_id": f"wxg:{other}", "text": "hi"},
            "u2",
        )
        assert receipt["ok"] is False
        assert receipt["error_code"] == "chat_not_allowed"

        assert harness.driver.calls == []


async def test_send_at_non_empty_at_is_unsupported_and_empty_at_sends(
    tmp_path: Path,
) -> None:
    async with _bridge(tmp_path) as harness:
        alias = harness.poller.registry.live_aliases(ALLOWED)[0]

        receipt = await _command(
            harness,
            "send_at",
            {"chat_id": f"wxg:{alias}", "text": "hi", "at": ["某人"]},
            "a1",
        )
        assert receipt["ok"] is False
        assert receipt["error_code"] == "unsupported_at"
        assert harness.driver.calls == []

        # 空 at 等价于普通 send_text。
        receipt = await _command(
            harness,
            "send_at",
            {"chat_id": f"wxg:{alias}", "text": "hi", "at": []},
            "a2",
        )
        assert receipt["ok"] is True
        assert receipt["delivered"] == "observed"


async def test_frame_source_only_bridge_has_no_sender(tmp_path: Path) -> None:
    async with _bridge(tmp_path, frame_source=[dict(HEALTH_FRAME)]) as harness:
        receipt = await _command(
            harness,
            "send_text",
            {"chat_id": "wxg:any", "text": "hi"},
            "f1",
        )
        assert receipt["ok"] is False
        assert receipt["error_code"] == "chat_name_unresolved"
        assert harness.driver.calls == []


# --------------------------------------------------------------- (2) 回执映射
def test_receipt_mapping_preserves_delivery_state() -> None:
    observed = bridge_main._receipt_from_result(
        SendResult(True, None, "observed", "7:1")
    )
    assert observed == {
        "ok": True,
        "error_code": None,
        "delivered": "observed",
        "message_id": "7:1",
    }

    # 已经打进输入框、只是回轮询没看到气泡：按 typed 成功回执，避免重复发送。
    typed = bridge_main._receipt_from_result(
        SendResult(False, "not_observed", "failed")
    )
    assert typed == {
        "ok": True,
        "error_code": "not_observed",
        "delivered": "typed",
        "message_id": None,
    }

    unresolved = bridge_main._receipt_from_result(
        SendResult(False, "not_friend", "failed")
    )
    assert unresolved["ok"] is False
    assert unresolved["error_code"] == "chat_name_unresolved"

    limited = bridge_main._receipt_from_result(
        SendResult(False, "rate_limited", "failed")
    )
    assert limited["ok"] is False
    assert limited["error_code"] == "rate_limited"


# --------------------------------------------------------------- (3) 服务器超时
async def test_server_uses_send_timeout_for_send_actions_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "_AWAIT_TIMEOUT", 0.05)

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {
            "ok": True,
            "error_code": None,
            "delivered": "typed",
            "message_id": None,
        }

    server = BridgeServer(command_handler=handler, send_timeout=0.5)
    try:
        sent = await server._dispatch_command("send_text", {}, "s1")
        assert sent["ok"] is True
        assert sent["id"] == "s1"

        other = await server._dispatch_command("resume", {}, "r1")
        assert other["ok"] is False
        assert other["error_code"] == "timeout"
        assert other["id"] == "r1"
    finally:
        with contextlib.suppress(Exception):
            await server.stop()


# --------------------------------------------------------------- (4) UiThreadDriver
def test_ui_thread_driver_routes_all_calls_to_sta_thread(tmp_path: Path) -> None:
    ui = UiThread(name="task34-proxy-uia")
    ui.start()
    driver = WiringDriver()
    proxy = bridge_main._UiThreadDriver(
        SimpleNamespace(stopped=False, ui=ui), driver
    )
    instance = Sender(
        _sender_config(tmp_path),
        driver=proxy,
        clock=lambda: 1_000_000.0,
        sleep=lambda _seconds: None,
        uniform=lambda _low, high: high,
        session_epoch=7,
    )
    try:
        result = instance.submit(ALLOWED, "你好")
        assert result.ok is True
        assert result.delivered == "observed"

        ident = ui.thread_ident
        assert ident is not None
        assert driver.actions() == ["open_chat", "send_text", "snapshot"]
        assert {call[2] for call in driver.calls} == {ident}
        assert threading.get_ident() not in {call[2] for call in driver.calls}
    finally:
        instance.close(timeout=STEP)
        ui.stop(timeout=STEP)


def test_ui_thread_driver_deadlock_guard() -> None:
    ui = UiThread(name="task34-guard-uia")
    ui.start()
    driver = WiringDriver()
    proxy = bridge_main._UiThreadDriver(
        SimpleNamespace(stopped=False, ui=ui), driver
    )
    try:
        future = ui.submit(lambda: proxy.open_chat("x"), kind="send")
        with pytest.raises(DriverError) as info:
            future.result(timeout=STEP)
        assert info.value.code == "deadlock_guard"
        assert driver.calls == []
    finally:
        ui.stop(timeout=STEP)


def test_ui_thread_driver_rejects_when_bridge_is_stopped() -> None:
    ui = UiThread(name="task34-stopped-uia")
    ui.start()
    driver = WiringDriver()
    proxy = bridge_main._UiThreadDriver(
        SimpleNamespace(stopped=True, ui=ui), driver
    )
    try:
        with pytest.raises(DriverError) as info:
            proxy.open_chat("x")
        assert info.value.code == "aborted"
        assert driver.calls == []
    finally:
        ui.stop(timeout=STEP)
