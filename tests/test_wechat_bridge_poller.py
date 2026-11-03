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
        "poll_seconds": 0.02,
        "kill_switch_file": str(tmp_path / "wechat.stop"),
    }
    defaults.update(overrides)
    return WeChatConfig(**defaults)


ALL_GROUPS = tuple(sorted(GROUPS))


class _GroupNamesLocalDb:
    """只提供群名枚举的本地库：不声明 available，轮询仍走 UIA 开窗。"""

    available = False

    def __init__(self, names: tuple[str, ...]) -> None:
        self._names = names

    def group_names(self) -> tuple[str, ...]:
        return self._names


def _all_groups_panel() -> frozenset[str]:
    """面板白名单为空 = 不限制；群范围由本地库枚举。"""
    return frozenset()


def _loop(
    wechat: WeChatConfig,
    driver: Any,
    *,
    groups: tuple[str, ...] = GROUPS,
    **kwargs: Any,
) -> PollingLoop:
    """按「面板空白名单 + 本地库枚举」构造轮询循环（群范围=groups）。"""
    kwargs.setdefault("poll_seconds", 0.02)
    return PollingLoop(
        wechat,
        driver,
        probe=_probe,
        registry=_Registry(),
        localdb=_GroupNamesLocalDb(groups),
        panel=_all_groups_panel,
        **kwargs,
    )


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
        return _loop(config, drv)

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
    poller = _loop(wechat, driver)
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


# ------------------------------------------------ (c) 群范围边界
async def test_scope_comes_from_localdb_and_list_is_not_truncated(
    tmp_path: Path,
) -> None:
    """面板空白名单 = 不限制：群范围 = 本地库枚举，稳定顺序、绝不截断。"""
    wechat = _wechat(tmp_path)
    groups = ("群甲", "群乙")
    driver = FakeDriver(
        {"群甲": [[("甲", "开场")]], "群乙": [[("乙", "开场")]], "外部群": [[]]}
    )
    poller = _loop(wechat, driver, groups=groups)
    assert poller._scope() == tuple(sorted(groups))
    assert len(poller._scope()) == len(groups)

    frames: list[dict[str, Any]] = []
    source = poller.frames()
    try:
        async def _one_round() -> None:
            # 每群之后都有一帧 health，所以「一轮」= 群数帧心跳。
            health_seen = 0
            while health_seen < len(groups):
                frame = await _next_frame(source)
                frames.append(frame)
                if frame.get("type") == "health":
                    health_seen += 1

        await asyncio.wait_for(_one_round(), STEP)
    finally:
        poller.stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(source.aclose(), STEP)

    assert set(driver.opened) == set(groups)
    assert all(name in groups for _, name in driver.calls)
    assert not any(name == "外部群" for _, name in driver.calls)


async def test_poll_round_emits_health_after_each_group(tmp_path: Path) -> None:
    """每群之后一帧心跳：多群串行轮询的最大帧间隔 = 单群耗时，而不是整轮耗时。

    帧泵的空转看门狗按「单群耗时」量级设值；若只在轮末发心跳，3 个群一轮
    15s+ 会把健康的多群轮询误判成卡死（正是入站静默停摆的根因）。
    """
    wechat = _wechat(tmp_path)
    driver = FakeDriver()
    poller = _loop(wechat, driver)
    await poller.start()
    try:
        async def _collect() -> list[str]:
            seen: list[str] = []
            async for frame in poller._poll_round():
                seen.append(frame["type"])
                if frame["type"] == "health":
                    # 第 k 帧心跳必须在第 k 个群开窗之后立刻到达：证明心跳是按群
                    # 发出的，而不是整轮结束才补一帧。
                    assert len(driver.opened) == seen.count("health")
            return seen

        types = await asyncio.wait_for(_collect(), STEP)
    finally:
        poller.stop()

    assert types.count("health") == len(GROUPS)
    assert types[-1] == "health", "群循环必须以心跳收尾"


# ------------------------------------------------ (d) 杀开关
async def test_kill_switch_stops_polling_and_deleting_file_does_not_resume(
    tmp_path: Path,
) -> None:
    wechat = _wechat(tmp_path)
    driver = FakeDriver()
    poller = _loop(wechat, driver)
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
    wechat = _wechat(tmp_path)
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

    poller = _loop(wechat, SlowOpenDriver())
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
        return _loop(config, drv)

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
    poller = _loop(wechat, driver)
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
    poller = _loop(wechat, driver, poll_seconds=seconds)
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
    poller = _loop(wechat, FakeDriver())
    kill_path = Path(wechat.kill_switch_file)
    kill_path.write_text("stop\n", encoding="utf-8")

    poller.abort_sends()
    assert poller.stopped is True
    assert poller.reset_for_resume() is False
    assert poller.stopped is True

    kill_path.unlink()
    assert poller.reset_for_resume() is True
    assert poller.stopped is False


def test_reset_for_resume_resets_sweep_state(tmp_path: Path) -> None:
    """急停期间可能积压消息：恢复后第一轮必须全量，而不是继续吃旧的红点缓存。"""
    poller = _loop(_wechat(tmp_path), FakeDriver())
    poller._last_full_sweep = 42.0
    poller._skip_scan_until = 99.0

    assert poller.reset_for_resume() is True
    assert poller._last_full_sweep == 0.0
    assert poller._skip_scan_until == 0.0


# ------------------------------------------------ (i) 未读门控
class _Clock:
    """可控时钟：门控测试不能靠真实时间等 full_sweep_seconds。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class UnreadDriver(FakeDriver):
    """在 FakeDriver 上补未读探测面，并记录探测/扫描次数。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.unread_total_value: Any = 0
        self.unread_sessions_value: Any = {}
        self.unread_calls = 0
        self.scan_calls = 0
        self.probe_error: BaseException | None = None
        self.scan_error: BaseException | None = None

    def unread_total(self) -> Any:
        self.unread_calls += 1
        if self.probe_error is not None:
            raise self.probe_error
        return self.unread_total_value

    def unread_sessions(self) -> Any:
        self.scan_calls += 1
        if self.scan_error is not None:
            raise self.scan_error
        return self.unread_sessions_value


@contextlib.asynccontextmanager
async def _running_poller(wechat: WeChatConfig, driver: Any, clock: Any):
    poller = _loop(wechat, driver, clock=clock)
    await poller.start()
    try:
        yield poller
    finally:
        poller.stop()


async def test_unread_gate_skips_round_without_unread(tmp_path: Path) -> None:
    clock = _Clock()
    driver = UnreadDriver()
    wechat = _wechat(tmp_path, unread_gate=True, full_sweep_seconds=120.0)

    async with _running_poller(wechat, driver, clock) as poller:
        poller._last_full_sweep = clock.now  # 预热那一轮已经全量过
        assert await poller._targets() == ()

    assert driver.opened == []
    assert driver.unread_calls == 1
    assert driver.scan_calls == 0


async def test_unread_gate_opens_only_groups_with_unread(tmp_path: Path) -> None:
    clock = _Clock()
    driver = UnreadDriver()
    driver.unread_total_value = 3
    driver.unread_sessions_value = {"同事群": 2, "白名单外的群": 5}
    wechat = _wechat(tmp_path, unread_gate=True, full_sweep_seconds=120.0)

    async with _running_poller(wechat, driver, clock) as poller:
        poller._last_full_sweep = clock.now
        assert await poller._targets() == ("同事群",)


async def test_unread_gate_falls_back_when_probe_fails(tmp_path: Path) -> None:
    clock = _Clock()
    driver = UnreadDriver()
    driver.probe_error = RuntimeError("红点读不到")
    wechat = _wechat(tmp_path, unread_gate=True, full_sweep_seconds=120.0)

    async with _running_poller(wechat, driver, clock) as poller:
        poller._last_full_sweep = clock.now
        assert await poller._targets() == ALL_GROUPS


async def test_unread_gate_falls_back_when_scan_fails(tmp_path: Path) -> None:
    clock = _Clock()
    driver = UnreadDriver()
    driver.unread_total_value = 1
    driver.scan_error = RuntimeError("会话列表读不到")
    wechat = _wechat(tmp_path, unread_gate=True, full_sweep_seconds=120.0)

    async with _running_poller(wechat, driver, clock) as poller:
        poller._last_full_sweep = clock.now
        assert await poller._targets() == ALL_GROUPS


async def test_unread_outside_allowlist_is_not_rescanned_every_tick(
    tmp_path: Path,
) -> None:
    """免打扰/白名单外的未读清不掉红点，不能每个 tick 都去翻会话列表。"""
    clock = _Clock()
    driver = UnreadDriver()
    driver.unread_total_value = 1
    driver.unread_sessions_value = {"白名单外的群": 2}
    wechat = _wechat(tmp_path, unread_gate=True, full_sweep_seconds=120.0)

    async with _running_poller(wechat, driver, clock) as poller:
        poller._last_full_sweep = clock.now
        assert await poller._targets() == ()
        assert await poller._targets() == ()

        assert driver.unread_calls == 1
        assert driver.scan_calls == 1

        # 到点仍要全量兜底：红点可能永久清不掉。
        clock.now += poller.full_sweep_seconds
        assert await poller._targets() == ALL_GROUPS


async def test_full_sweep_runs_even_without_unread(tmp_path: Path) -> None:
    clock = _Clock()
    driver = UnreadDriver()
    wechat = _wechat(tmp_path, unread_gate=True, full_sweep_seconds=60.0)

    async with _running_poller(wechat, driver, clock) as poller:
        poller._last_full_sweep = clock.now
        clock.now += poller.full_sweep_seconds

        assert await poller._targets() == ALL_GROUPS
    # 全量兜底不需要再看红点。
    assert driver.unread_calls == 0


async def test_unread_gate_disabled_polls_all_groups(tmp_path: Path) -> None:
    clock = _Clock()
    driver = UnreadDriver()
    wechat = _wechat(tmp_path, unread_gate=False)

    async with _running_poller(wechat, driver, clock) as poller:
        assert await poller._targets() == ALL_GROUPS
    assert driver.unread_calls == 0


async def test_driver_without_unread_probe_keeps_full_polling(tmp_path: Path) -> None:
    """旧驱动/替身没有未读探测面时，行为必须和以前完全一致。"""
    clock = _Clock()
    driver = FakeDriver()
    wechat = _wechat(tmp_path, unread_gate=True, full_sweep_seconds=120.0)

    async with _running_poller(wechat, driver, clock) as poller:
        poller._last_full_sweep = clock.now
        assert await poller._targets() == ALL_GROUPS


async def test_gated_round_still_emits_health_frames(tmp_path: Path) -> None:
    """门控只跳过开窗，不能把帧源饿死：桩泵超时靠这些 health 帧判断存活。"""
    clock = _Clock()
    driver = UnreadDriver()
    wechat = _wechat(tmp_path, unread_gate=True, full_sweep_seconds=120.0)
    poller = _loop(wechat, driver, poll_seconds=0.01, clock=clock)
    poller._last_full_sweep = clock.now
    await poller.start()
    source = poller.frames()
    frames: list[dict[str, Any]] = []
    try:
        while len(frames) < 3:
            frames.append(await asyncio.wait_for(source.__anext__(), STEP))
    finally:
        poller.stop()
        with contextlib.suppress(BaseException):
            await source.aclose()

    assert [f.get("type") for f in frames] == ["health", "health", "health"]
    assert driver.opened == []
