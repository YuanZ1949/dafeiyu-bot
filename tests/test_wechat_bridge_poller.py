"""微信桥 PART B 验收：轮询循环、专用 UIA 线程、杀开关、发送抢占、优雅停机。

全部用注入的假驱动/probe/registry 驱动，不碰 pyweixin / pywinauto / UIA。
反挂死：所有 await 都在 ``asyncio.wait_for``（<= 5s）里；每个线程 join 都有
超时；整个模块远低于 60s。
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
from dafeiyu.config import WeChatConfig
from dafeiyu.gateway.wechat_bridge.driver import ChatHandle
from dafeiyu.gateway.wechat_bridge.poller import PollingLoop
from dafeiyu.gateway.wechat_bridge.server import BridgeServer

STEP = 5.0
TOKEN = "task18b-token"
GROUPS = ("老友群", "同事群")
UI_THREAD_NAME = "wechat-uia"

# 两轮窗口：第一轮只建立对齐基线（0 条事件），第二轮新增 2 + 1 = 3 条消息。
WINDOWS: dict[str, list[list[tuple[str, str]]]] = {
    "老友群": [
        [("甲", "开场")],
        [("甲", "开场"), ("乙", "一"), ("甲", "二")],
    ],
    "同事群": [
        [("丙", "开场")],
        [("丙", "开场"), ("丁", "三")],
    ],
}


# --------------------------------------------------------------- 小工具
def _wechat(tmp_path: Path, **overrides: Any) -> WeChatConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 0,
        "bridge_token": TOKEN,
        "account_nickname": "小助手",
        "group_allowlist": frozenset(GROUPS),
        "max_groups": 3,
        "poll_seconds": 0.02,
        "kill_switch_file": str(tmp_path / "wechat.stop"),
    }
    defaults.update(overrides)
    return WeChatConfig(**defaults)


def _probe(connected: bool) -> dict[str, Any]:
    """假健康探测：不碰驱动/桌面，只回协议 §5 形状。"""
    return {
        "v": 1,
        "type": "health",
        "connected": bool(connected),
        "logged_in": True,
        "desktop": "interactive",
        "wechat_version": "4.1.6.14",
        "language": "zh-CN",
        "integrity": "matching",
        "queue_depth": 0,
        "last_event_at": None,
        "last_send_at": None,
        "degraded": False,
        "reason": None,
    }


class _Registry:
    """假身份表：显示名 -> 稳定 `wxg:<alias>`（不落盘）。"""

    def __init__(self) -> None:
        self._aliases: dict[str, str] = {}

    def chat_id(self, display_name: str, *, kind: str = "group") -> str:
        alias = self._aliases.setdefault(display_name, f"g{len(self._aliases) + 1}")
        return f"wxg:{alias}"


class FakeDriver:
    """假驱动门面：记录调用线程/并发度/顺序，按脚本返回窗口快照。"""

    def __init__(
        self,
        windows: dict[str, list[list[tuple[str, str]]]] | None = None,
        *,
        gate: threading.Event | None = None,
        delay: float = 0.001,
    ) -> None:
        self.windows = windows if windows is not None else WINDOWS
        self.gate = gate
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self.opened: list[str] = []
        self.thread_ids: set[int] = set()
        self.max_concurrent = 0
        self._active = 0
        self._snapshots: dict[str, int] = {}
        self._lock = threading.Lock()

    def _begin(self, op: str, name: str) -> None:
        with self._lock:
            self.calls.append((op, name))
            self.thread_ids.add(threading.get_ident())
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        if self.delay:
            time.sleep(self.delay)

    def _end(self) -> None:
        with self._lock:
            self._active -= 1

    def open_chat(self, name: str) -> ChatHandle:
        self._begin("open_chat", name)
        try:
            if self.gate is not None:
                self.gate.wait(STEP)
            if name not in self.windows:
                raise AssertionError(f"打开了 allowlist 之外的会话：{name!r}")
            self.opened.append(name)
            return ChatHandle(name=name, window=None)
        finally:
            self._end()

    def snapshot(self, handle: ChatHandle) -> dict[str, Any]:
        self._begin("snapshot", handle.name)
        try:
            with self._lock:
                index = self._snapshots.get(handle.name, 0)
                self._snapshots[handle.name] = index + 1
            windows = self.windows.get(handle.name) or [[]]
            window = windows[min(index, len(windows) - 1)]
            return {
                "chat_name": handle.name,
                "messages": [
                    {"nickname": nickname, "text": text} for nickname, text in window
                ],
                "counts": {"total": len(window)},
            }
        finally:
            self._end()


async def _wait_until(predicate: Any, *, timeout: float = STEP) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


async def _next_frame(source: Any) -> dict[str, Any]:
    return await asyncio.wait_for(source.__anext__(), STEP)


async def _recv_json(ws: Any) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), STEP)
    return json.loads(raw)


async def _recv_until(ws: Any, predicate: Any, *, limit: int = 30) -> dict[str, Any]:
    for _ in range(limit):
        frame = await _recv_json(ws)
        if predicate(frame):
            return frame
    raise AssertionError(f"等待的目标帧没有出现（上限 {limit} 帧）")


def _uri(port: int) -> str:
    return f"ws://127.0.0.1:{port}/wechat-bridge"


async def _wait_for_port(holder: dict[str, Any], *, timeout: float = STEP) -> BridgeServer:
    async def _poll() -> BridgeServer:
        while True:
            server = holder.get("server")
            if server is not None and server.port:
                return server
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(_poll(), timeout)


def _ui_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == UI_THREAD_NAME]


async def _collect_messages(
    source: Any, frames: list[dict[str, Any]], *, count: int, limit: int = 80
) -> list[dict[str, Any]]:
    async def _run() -> list[dict[str, Any]]:
        while True:
            frame = await _next_frame(source)
            frames.append(frame)
            messages = [f for f in frames if f.get("type") == "message"]
            if len(messages) >= count:
                return messages
            if len(frames) >= limit:
                raise AssertionError(f"上限 {limit} 帧里只收到 {len(messages)} 条消息")

    return await asyncio.wait_for(_run(), STEP)


# ------------------------------------------------ (a) run() 全生命周期 + 3 条消息
async def test_run_lifecycle_three_messages_then_stop(tmp_path: Path) -> None:
    wechat = _wechat(tmp_path)
    gate = threading.Event()
    driver = FakeDriver(gate=gate)
    holder: dict[str, Any] = {}

    def server_factory(*args: Any, **kwargs: Any) -> BridgeServer:
        server = BridgeServer(*args, **kwargs)
        holder["server"] = server
        return server

    def poller_factory(config: WeChatConfig, drv: Any) -> PollingLoop:
        return PollingLoop(
            config,
            drv,
            probe=_probe,
            registry=_Registry(),
            poll_seconds=0.02,
        )

    tasks_before = asyncio.all_tasks()
    threads_before = threading.active_count()
    run_task = asyncio.create_task(
        bridge_main.run(
            settings=SimpleNamespace(wechat=wechat),
            driver_factory=lambda: driver,
            server_factory=server_factory,
            poller_factory=poller_factory,
        )
    )
    try:
        server = await _wait_for_port(holder)
        ws = await asyncio.wait_for(
            connect(
                _uri(server.port),
                additional_headers={"Authorization": f"Bearer {TOKEN}"},
            ),
            STEP,
        )
        async with ws:
            # 客户端挂上之后再放行第一轮轮询：消息不会在没人听的时候丢掉。
            gate.set()
            messages = await _collect_messages(_drive(ws), [], count=3)
            assert sorted(frame["text"] for frame in messages) == ["一", "三", "二"]
            assert {frame["chat_id"] for frame in messages} == {"wxg:g1", "wxg:g2"}
            assert all(frame["v"] == 1 and frame["channel"] == "wechat" for frame in messages)
            assert all(frame["type"] == "message" for frame in messages)

            await asyncio.wait_for(
                ws.send(json.dumps({"v": 1, "id": "bye", "action": "stop", "params": {}})),
                STEP,
            )
            receipt = await _recv_until(ws, lambda f: f.get("id") == "bye")
            assert receipt["ok"] is True
            final = await _recv_until(
                ws, lambda f: f.get("type") == "health" and f.get("connected") is False
            )
            assert final["v"] == 1

        assert await asyncio.wait_for(run_task, STEP) == 0
        assert server.is_running is False
        assert server.client_count() == 0
        assert threading.active_count() == threads_before
        assert _ui_threads() == []
        assert asyncio.all_tasks() == tasks_before
    finally:
        gate.set()
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(run_task, STEP)


async def _drive(ws: Any) -> Any:
    """把客户端套接字包装成「帧源」，让 `_collect_messages` 一帧帧读。"""
    while True:
        yield await _recv_json(ws)


# ------------------------------------------------ (b) UIA 线程纪律
async def test_all_driver_calls_run_on_one_non_loop_thread(tmp_path: Path) -> None:
    wechat = _wechat(tmp_path)
    driver = FakeDriver()
    poller = PollingLoop(
        wechat,
        driver,
        probe=_probe,
        registry=_Registry(),
        poll_seconds=0.02,
    )
    loop_thread = threading.get_ident()
    threads_before = threading.active_count()
    frames: list[dict[str, Any]] = []
    source = poller.frames()
    try:
        await _collect_messages(source, frames, count=3)
    finally:
        poller.stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(source.aclose(), STEP)

    assert driver.max_concurrent == 1, "UIA 线程里同时跑了两个驱动调用"
    assert len(driver.thread_ids) == 1, f"驱动被多个线程调用：{driver.thread_ids}"
    driver_tid = next(iter(driver.thread_ids))
    assert driver_tid != loop_thread, "驱动调用跑在事件循环线程上"
    assert poller.ui.thread_ident == driver_tid
    assert poller.ui.running is False
    assert threading.active_count() == threads_before


# ------------------------------------------------ (c) allowlist 边界
async def test_only_allowlisted_groups_are_polled_and_list_is_not_truncated(
    tmp_path: Path,
) -> None:
    wechat = _wechat(tmp_path, group_allowlist=frozenset({"群甲", "群乙"}), max_groups=5)
    driver = FakeDriver(
        {"群甲": [[("甲", "开场")]], "群乙": [[("乙", "开场")]], "外部群": [[]]}
    )
    poller = PollingLoop(
        wechat,
        driver,
        probe=_probe,
        registry=_Registry(),
        poll_seconds=0.02,
    )
    # 稳定顺序 + 绝不截断：allowlist 是什么就轮询什么，不做「只取前 N 个」。
    assert poller.allowlist == tuple(sorted(wechat.group_allowlist))
    assert set(poller.allowlist) == set(wechat.group_allowlist)
    assert len(poller.allowlist) == len(wechat.group_allowlist)

    frames: list[dict[str, Any]] = []
    source = poller.frames()
    try:
        async def _one_round() -> None:
            while True:
                frame = await _next_frame(source)
                frames.append(frame)
                if frame.get("type") == "health":
                    return

        await asyncio.wait_for(_one_round(), STEP)
    finally:
        poller.stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(source.aclose(), STEP)

    assert set(driver.opened) == {"群甲", "群乙"}
    assert all(name in wechat.group_allowlist for _, name in driver.calls)
    assert not any(name == "外部群" for _, name in driver.calls)


# ------------------------------------------------ (d) 杀开关
async def test_kill_switch_stops_polling_and_deleting_file_does_not_resume(
    tmp_path: Path,
) -> None:
    wechat = _wechat(tmp_path)
    driver = FakeDriver()
    poller = PollingLoop(
        wechat,
        driver,
        probe=_probe,
        registry=_Registry(),
        poll_seconds=0.02,
    )
    threads_before = threading.active_count()
    frames: list[dict[str, Any]] = []
    source = poller.frames()

    async def _consume() -> None:
        async for frame in source:
            frames.append(frame)

    task = asyncio.create_task(_consume())
    kill_path = Path(wechat.kill_switch_file)
    try:
        await _wait_until(lambda: len(driver.calls) >= 2)
        assert not kill_path.exists()
        kill_path.write_text("stop", encoding="utf-8")
        await asyncio.wait_for(task, STEP)

        frozen = len(driver.calls)
        assert frozen >= 2
        health = [f for f in frames if f.get("type") == "health"][-1]
        assert health["connected"] is False
        assert health["degraded"] is True
        assert "kill_switch" in (health["reason"] or "")

        # 删掉文件也不复活：轮询循环已经单向结束（要恢复只能重启桥进程）。
        kill_path.unlink()
        await asyncio.wait_for(asyncio.sleep(0.1), STEP)
        assert len(driver.calls) == frozen
        assert task.done()
        assert poller.stopped is True
    finally:
        poller.stop()
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, STEP)

    assert threading.active_count() == threads_before


# ------------------------------------------------ (e) 发送抢占
async def test_queued_send_runs_before_next_poll_op(tmp_path: Path) -> None:
    wechat = _wechat(tmp_path, group_allowlist=frozenset({"老友群"}))
    sequence: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    class SlowOpenDriver:
        def open_chat(self, name: str) -> ChatHandle:
            sequence.append("open_chat")
            entered.set()
            release.wait(STEP)
            return ChatHandle(name=name, window=None)

        def snapshot(self, handle: ChatHandle) -> dict[str, Any]:
            sequence.append("snapshot")
            return {"chat_name": handle.name, "messages": [], "counts": {"total": 0}}

    poller = PollingLoop(
        wechat,
        SlowOpenDriver(),
        probe=_probe,
        registry=_Registry(),
        poll_seconds=0.02,
    )
    frames: list[dict[str, Any]] = []
    source = poller.frames()

    async def _consume() -> None:
        async for frame in source:
            frames.append(frame)

    task = asyncio.create_task(_consume())
    try:
        await _wait_until(entered.is_set)
        future = poller.enqueue_send(lambda: sequence.append("send"))
        release.set()
        await _wait_until(lambda: "snapshot" in sequence)
        assert sequence[:3] == ["open_chat", "send", "snapshot"], sequence
        await asyncio.wait_for(asyncio.wrap_future(future), STEP)
    finally:
        release.set()
        poller.stop()
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, STEP)


# ------------------------------------------------ (f) SIGINT 路径收摊
async def test_request_stop_shutdown_joins_ui_thread(tmp_path: Path) -> None:
    wechat = _wechat(tmp_path)
    driver = FakeDriver()
    holder: dict[str, Any] = {}

    def server_factory(*args: Any, **kwargs: Any) -> BridgeServer:
        server = BridgeServer(*args, **kwargs)
        holder["server"] = server
        return server

    def poller_factory(config: WeChatConfig, drv: Any) -> PollingLoop:
        return PollingLoop(
            config, drv, probe=_probe, registry=_Registry(), poll_seconds=0.02
        )

    threads_before = threading.active_count()
    non_daemon_before = sum(1 for t in threading.enumerate() if not t.daemon)
    run_task = asyncio.create_task(
        bridge_main.run(
            settings=SimpleNamespace(wechat=wechat),
            driver_factory=lambda: driver,
            server_factory=server_factory,
            poller_factory=poller_factory,
        )
    )
    try:
        server = await _wait_for_port(holder)
        # SIGINT hook 最终也是让这个 future 完成：request_stop 是同一条收摊路径。
        await _wait_until(lambda: bool(_ui_threads()))
        # 启动健康帧走完（poller.start() 之后的 UIA 线程探测）再停：否则
        # request_stop 的 abort_sends 会取消仍在等待的探测 future，run() 收到
        # CancelledError 而无法走到正常收摊。
        await _wait_until(lambda: server.latest_health is not None)
        server.request_stop()
        assert await asyncio.wait_for(run_task, STEP) == 0
    finally:
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(run_task, STEP)

    assert _ui_threads() == []
    assert threading.active_count() == threads_before
    assert sum(1 for t in threading.enumerate() if not t.daemon) == non_daemon_before


# ------------------------------------------------ (g) 杀开关时序：轮次开关先查
async def test_kill_switch_checked_before_any_driver_call(tmp_path: Path) -> None:
    wechat = _wechat(tmp_path)
    Path(wechat.kill_switch_file).write_text("stop", encoding="utf-8")
    driver = FakeDriver()
    poller = PollingLoop(
        wechat,
        driver,
        probe=_probe,
        registry=_Registry(),
        poll_seconds=0.02,
    )
    source = poller.frames()
    frames: list[dict[str, Any]] = []
    try:
        async def _drain() -> None:
            async for frame in source:
                frames.append(frame)

        await asyncio.wait_for(_drain(), STEP)
    finally:
        poller.stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(source.aclose(), STEP)

    assert driver.calls == []
    assert frames[-1]["type"] == "health"
    assert frames[-1]["connected"] is False
    assert frames[-1]["degraded"] is True


@pytest.mark.parametrize("seconds", [0.0, 0.02])
async def test_poller_never_blocks_event_loop(tmp_path: Path, seconds: float) -> None:
    """回归：UIA 线程里 sleep 不会让事件循环停摆（每次 await 都有上限）。"""
    wechat = _wechat(tmp_path, poll_seconds=seconds)
    driver = FakeDriver(delay=0.005)
    poller = PollingLoop(
        wechat,
        driver,
        probe=_probe,
        registry=_Registry(),
        poll_seconds=seconds,
    )
    ticks = 0
    source = poller.frames()
    try:
        async def _consume() -> None:
            nonlocal ticks
            async for _frame in source:
                ticks += 1
                if ticks >= 12:
                    return

        await asyncio.wait_for(_consume(), STEP)
    finally:
        poller.stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(source.aclose(), STEP)
    assert ticks >= 12


# ------------------------------------------------ (h) resume 只在删文件后生效
def test_reset_for_resume_only_after_kill_file_removed(tmp_path: Path) -> None:
    wechat = _wechat(tmp_path)
    poller = PollingLoop(
        wechat,
        FakeDriver(),
        probe=_probe,
        registry=_Registry(),
        poll_seconds=0.02,
    )
    kill_path = Path(wechat.kill_switch_file)
    kill_path.write_text("stop\n", encoding="utf-8")

    poller.abort_sends()
    assert poller.stopped is True
    assert poller.reset_for_resume() is False
    assert poller.stopped is True

    kill_path.unlink()
    assert poller.reset_for_resume() is True
    assert poller.stopped is False
