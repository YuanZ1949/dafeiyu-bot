"""微信桥 PART A 验收：命令行入口 + WebSocket 服务器生命周期。

覆盖：
- ``python -m dafeiyu.gateway.wechat_bridge --help`` 在没装 pyweixin 时退出 0；
- ``__main__.run()`` 的完整生命周期（假驱动 + 假帧源 + 真 websockets 客户端）；
- 握手 token（对/错/缺）与客户端 ``wechat.py`` 的 header 形态一致；
- 未知动作按 §6 规则 5 回 ``bad_action``，且 ``id`` 原样回填；
- ``_startup_checks`` 的 fail-closed 拒绝（enabled / nickname / 出站后端）；
- ``BridgeServer.broadcast`` / ``pump_frames`` 的推帧通路。

反挂死：所有 await 都包在 ``asyncio.wait_for``（<= 5s）里；整个模块远低于 60s。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

import dafeiyu.gateway.wechat_bridge.__main__ as bridge_main
import dafeiyu.gateway.wechat_bridge.server as bridge_server
from dafeiyu.config import ConfigError, WeChatConfig
from dafeiyu.gateway.wechat_bridge.server import BridgeServer, pump_frames

WORKTREE = Path(__file__).resolve().parents[1]
SRC = WORKTREE / "src"
STEP = 5.0  # 单次 await 的上限（秒）

HEALTH_FRAME = {
    "v": 1,
    "type": "health",
    "connected": True,
    "logged_in": True,
    "desktop": "interactive",
    "queue_depth": 0,
    "last_event_at": None,
    "last_send_at": None,
    "degraded": False,
}

TOKEN = "task18-test-token"


# --------------------------------------------------------------- 小工具
def _wechat(**overrides: Any) -> WeChatConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 0,
        "bridge_token": TOKEN,
        "account_nickname": "小助手",
        "poll_seconds": 1.0,
    }
    defaults.update(overrides)
    return WeChatConfig(**defaults)


async def _recv_json(ws: Any) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), STEP)
    return json.loads(raw)


async def _recv_until(ws: Any, predicate: Any, *, limit: int = 10) -> dict[str, Any]:
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


# ------------------------------------------------------- (a) --help 退出 0
def test_help_exits_zero_without_pyweixin(tmp_path: Path) -> None:
    """pyweixin 缺席时 `--help` 也必须退出 0（惰性 import 的硬约束）。"""
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    # 假的 pyweixin：一旦被 import 就炸；--help 路径绝不该碰到它。
    (blocker / "pyweixin.py").write_text(
        "raise ImportError('pyweixin is blocked in this test')\n", encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(blocker), str(SRC)])
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.run(
        [sys.executable, "-m", "dafeiyu.gateway.wechat_bridge", "--help"],
        cwd=str(WORKTREE),
        check=False,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout.casefold()
    assert "wechat_bridge" in proc.stdout
    assert "Traceback" not in proc.stderr


# -------------------------------------------- (b) run() 全生命周期 + 客户端
async def test_run_lifecycle_with_real_client() -> None:
    wechat = _wechat()
    holder: dict[str, Any] = {}
    seen: list[tuple[str, dict[str, Any], str]] = []

    class FakeDriver:
        """假驱动门面：PART A 只验证注入接缝，不碰 pyweixin。"""

    def server_factory(*args: Any, **kwargs: Any) -> BridgeServer:
        server = BridgeServer(*args, **kwargs)
        holder["server"] = server
        return server

    async def command_handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        seen.append((action, params, command_id))
        return {
            "ok": True,
            "error_code": None,
            "delivered": "typed",
            "message_id": "1:7",
        }

    tasks_before = asyncio.all_tasks()
    threads_before = threading.active_count()
    run_task = asyncio.create_task(
        bridge_main.run(
            settings=SimpleNamespace(wechat=wechat),
            driver_factory=FakeDriver,
            server_factory=server_factory,
            command_handler=command_handler,
            frame_source=[HEALTH_FRAME],
        )
    )
    try:
        server = await _wait_for_port(holder)
        ws = await asyncio.wait_for(
            connect(_uri(server.port), additional_headers={"Authorization": f"Bearer {TOKEN}"}),
            STEP,
        )
        async with ws:
            # 初始健康帧：新客户端接入即收到（快照或启动推帧，二者等价）。
            health = await _recv_until(ws, lambda f: f.get("type") == "health")
            assert health["v"] == 1
            assert health["channel"] == "wechat"

            # 指令 -> 同 id 回执。
            await asyncio.wait_for(
                ws.send(
                    json.dumps(
                        {
                            "v": 1,
                            "id": "abc",
                            "action": "send_text",
                            "params": {"chat_id": "wxg:laoyouqun", "text": "你好"},
                        }
                    )
                ),
                STEP,
            )
            receipt = await _recv_until(ws, lambda f: f.get("id") == "abc")
            assert receipt["v"] == 1
            assert receipt["ok"] is True
            assert "error_code" in receipt
            assert receipt["error_code"] is None
            assert receipt["delivered"] == "typed"
            assert receipt["message_id"] == "1:7"
            assert seen == [
                ("send_text", {"chat_id": "wxg:laoyouqun", "text": "你好"}, "abc")
            ]

            # stop 必须先收到回执，再优雅停机（回执不许被停机抢掉）。
            await asyncio.wait_for(
                ws.send(json.dumps({"v": 1, "id": "bye", "action": "stop", "params": {}})),
                STEP,
            )
            stop_receipt = await _recv_until(ws, lambda f: f.get("id") == "bye")
            assert stop_receipt["ok"] is True

            # 停机前最后再推一帧 connected:false 的健康帧。
            final_health = await _recv_until(
                ws, lambda f: f.get("type") == "health" and f.get("connected") is False
            )
            assert final_health["v"] == 1

        assert await asyncio.wait_for(run_task, STEP) == 0
        assert server.is_running is False
        assert server.client_count() == 0

        # 监听口已关：再连必须被拒。
        with pytest.raises(OSError):
            await asyncio.wait_for(connect(_uri(server.port)), STEP)

        # 没有遗留任务 / 线程。
        assert asyncio.all_tasks() == tasks_before
        assert threading.active_count() == threads_before
    finally:
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(run_task, STEP)


# ------------------ (b2) 纯消息帧源也要有无条件的初始 / 停机 health 帧
async def test_run_emits_health_even_with_message_only_source() -> None:
    """帧源只产消息帧时，初始 health 与停机 health 仍必须发出。

    PART A 的复核缺陷：旧实现只在 `latest_health` 非空时才发停机帧，于是
    「只推消息的帧源」既没有初始健康快照、也没有 connected:false 的停机通知，
    客户端 `_health_connected` 会一直是 False。
    """
    wechat = _wechat()
    holder: dict[str, Any] = {}

    class FakeDriver:
        """本用例不触碰驱动。"""

    def server_factory(*args: Any, **kwargs: Any) -> BridgeServer:
        server = BridgeServer(*args, **kwargs)
        holder["server"] = server
        return server

    messages: list[dict[str, Any]] = [
        {
            "v": 1,
            "type": "message",
            "chat_id": "wxg:g1",
            "message_id": f"1:{index}",
            "text": f"m{index}",
        }
        for index in range(3)
    ]
    run_task = asyncio.create_task(
        bridge_main.run(
            settings=SimpleNamespace(wechat=wechat),
            driver_factory=FakeDriver,
            server_factory=server_factory,
            frame_source=messages,
        )
    )
    try:
        server = await _wait_for_port(holder)
        async with connect(
            _uri(server.port), additional_headers={"Authorization": f"Bearer {TOKEN}"}
        ) as ws:
            health = await _recv_until(ws, lambda f: f.get("type") == "health")
            assert health["v"] == 1
            assert health["channel"] == "wechat"
            # 协议 §5 的必填字段必须在场（探测降级时也一样）。
            for key in ("connected", "logged_in", "desktop", "queue_depth"):
                assert key in health

            await asyncio.wait_for(
                ws.send(
                    json.dumps({"v": 1, "id": "bye", "action": "stop", "params": {}})
                ),
                STEP,
            )
            assert (await _recv_until(ws, lambda f: f.get("id") == "bye"))["ok"] is True
            final = await _recv_until(
                ws,
                lambda f: f.get("type") == "health" and f.get("connected") is False,
            )
            assert final["v"] == 1
            assert final["channel"] == "wechat"

        assert await asyncio.wait_for(run_task, STEP) == 0
        assert server.is_running is False
    finally:
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(run_task, STEP)


# ------------------------------------------------ (c) token 缺失/错误 -> 拒绝
async def test_wrong_or_missing_token_is_rejected() -> None:
    server = BridgeServer(bridge_token=TOKEN)
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        for headers in ({"Authorization": "Bearer wrong-token"}, None):
            with pytest.raises(InvalidStatus) as excinfo:
                async with connect(_uri(port), additional_headers=headers):
                    pass
            assert excinfo.value.response.status_code == 401
        # 正确的 token 仍然能连上。
        async with connect(
            _uri(port), additional_headers={"Authorization": f"Bearer {TOKEN}"}
        ) as ws:
            await asyncio.wait_for(server.broadcast({"type": "health", **HEALTH_FRAME}), STEP)
            assert (await _recv_until(ws, lambda f: f.get("type") == "health"))["v"] == 1
    finally:
        await asyncio.wait_for(server.stop(), STEP)


# ------------------------------------------------ (d) 未知动作 -> bad_action
async def test_unknown_action_receipt() -> None:
    server = BridgeServer()
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            await asyncio.wait_for(
                ws.send(
                    json.dumps(
                        {"v": 1, "id": "u-1", "action": "frobnicate", "params": {}}
                    )
                ),
                STEP,
            )
            receipt = await _recv_until(ws, lambda f: f.get("id") == "u-1")
            assert receipt["v"] == 1
            assert receipt["id"] == "u-1"
            assert receipt["ok"] is False
            # 线协议 v1 的冻结码：§6 规则 5 明确写的是 bad_action。
            assert receipt["error_code"] == "bad_action"
            assert receipt["delivered"] == "failed"
    finally:
        await asyncio.wait_for(server.stop(), STEP)


async def test_health_frame_reports_inbound_gap_after_overflow() -> None:
    """未确认帧被挤掉后，健康帧必须暴露 ``inbound_gap_from``（G6）。"""
    server = BridgeServer(bridge_token=TOKEN)
    for index in range(bridge_server.UNACKED_MAX + 1):
        await asyncio.wait_for(
            server.broadcast(
                {
                    "type": "message",
                    "chat_id": "wxg:g1",
                    "message_id": f"1:{index}",
                    "text": "x",
                }
            ),
            STEP,
        )
    await asyncio.wait_for(server.broadcast({"type": "health", **HEALTH_FRAME}), STEP)

    health = server.latest_health
    assert health is not None
    assert health["inbound_seq"] == bridge_server.UNACKED_MAX + 1
    assert health["unacked_count"] == bridge_server.UNACKED_MAX
    # 第一条被挤掉的帧 seq=1：从它起再也补不回来。
    assert health["inbound_gap_from"] == 1


async def test_health_frame_without_overflow_has_no_gap() -> None:
    server = BridgeServer(bridge_token=TOKEN)
    await asyncio.wait_for(server.broadcast({"type": "health", **HEALTH_FRAME}), STEP)

    health = server.latest_health
    assert health is not None
    assert health["inbound_gap_from"] is None
    assert health["inbound_seq"] == 0


# --------------------------------------------------- (e) 启动 fail-closed 拒绝
def test_startup_refuses_empty_nickname() -> None:
    with pytest.raises(ConfigError) as excinfo:
        bridge_main._startup_checks(_wechat(account_nickname=""))
    assert "account_nickname" in str(excinfo.value)


def test_startup_refuses_disabled() -> None:
    with pytest.raises(ConfigError) as excinfo:
        bridge_main._startup_checks(_wechat(enabled=False))
    assert "enabled" in str(excinfo.value)


async def test_run_refuses_before_binding() -> None:
    """run() 里也走同一套检查：配置不全不绑端口，不加载驱动。"""

    def exploding_driver_factory() -> Any:  # pragma: no cover - 不该被调用
        raise AssertionError("启动检查失败时不得加载驱动")

    wechat = _wechat(account_nickname="")
    with pytest.raises(ConfigError) as excinfo:
        await asyncio.wait_for(
            bridge_main.run(
                settings=SimpleNamespace(wechat=wechat),
                driver_factory=exploding_driver_factory,
            ),
            STEP,
        )
    assert "account_nickname" in str(excinfo.value)


# --------------------------------------------- (f) broadcast / pump_frames
async def test_broadcast_pushes_frame_to_client() -> None:
    server = BridgeServer()
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            await asyncio.wait_for(
                server.broadcast(
                    {
                        "type": "message",
                        "chat_id": "wxg:laoyouqun",
                        "text": "收到",
                        "message_id": "1:1",
                    }
                ),
                STEP,
            )
            frame = await _recv_until(ws, lambda f: f.get("type") == "message")
            assert frame["v"] == 1
            assert frame["channel"] == "wechat"
            assert frame["chat_id"] == "wxg:laoyouqun"
            assert frame["message_id"] == "1:1"
    finally:
        await asyncio.wait_for(server.stop(), STEP)


async def test_pump_frames_feeds_broadcast() -> None:
    server = BridgeServer()
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            frames = [
                {"type": "health", **HEALTH_FRAME},
                {"type": "message", "chat_id": "wxg:laoyouqun", "text": "第二帧"},
            ]
            await asyncio.wait_for(pump_frames(server, frames), STEP)
            first = await _recv_until(ws, lambda f: f.get("type") == "health")
            second = await _recv_until(ws, lambda f: f.get("type") == "message")
            assert first["v"] == 1
            assert second["text"] == "第二帧"
            assert server.latest_health is not None
            assert server.latest_health["type"] == "health"
    finally:
        await asyncio.wait_for(server.stop(), STEP)


# ------------------------------- (f2) 灌帧空转上限必须覆盖一个轮询间隔
class _RecordingServer:
    """只记录 broadcast 的假服务器：灌帧测试不需要真端口。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.latest_health: dict[str, Any] | None = None

    async def broadcast(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)
        if frame.get("type") == "health":
            self.latest_health = dict(frame)


async def test_pump_survives_poll_seconds_gap() -> None:
    """poll_seconds=8 时泵必须活过 6s 无事件的窗口（旧的 5s 默认会误停泵）。

    真等 8s 太慢：按 1/50 缩放——8s -> 0.16s、6s -> 0.12s、16s 超时 -> 0.32s。
    断言的关系（超时 > 轮询间隔 > 6s > 旧默认 5s）与真实时间一一对应。
    """
    scale = 1 / 50
    poll_seconds = 8.0
    gap = 6.0 * scale
    idle = bridge_main._pump_idle_timeout(poll_seconds) * scale

    # 生产配置下的算术关系：16s 上限 > 8s 轮询间隔 > 6s 空窗 > 旧 5s 默认。
    old_default = 5.0
    empty_window = 6.0
    assert bridge_main._pump_idle_timeout(8.0) == 16.0
    assert old_default < empty_window < poll_seconds < bridge_main._pump_idle_timeout(
        poll_seconds
    )
    assert idle > gap

    async def source() -> Any:
        for index in range(3):
            if index:
                # 每个间隔 6s（缩放后 0.12s）：比旧的 5s 默认长，比 16s 上限短。
                await asyncio.sleep(gap)
            yield {"type": "health", **HEALTH_FRAME}

    server = _RecordingServer()
    await asyncio.wait_for(pump_frames(server, source(), idle_timeout=idle), STEP)
    assert len(server.frames) == 3, "泵在 6s 空窗里被误停"
    assert server.latest_health is not None

    # 反证：把上限压到空窗以下，泵确实会按超时停机（接缝没有变成「永不超时」）。
    slow = _RecordingServer()
    await asyncio.wait_for(pump_frames(slow, source(), idle_timeout=gap / 2), STEP)
    assert len(slow.frames) == 1


async def test_pump_stall_limit_tolerates_transient_gap() -> None:
    """stall_limit>0：单次空转只记警告，慢轮询不该让帧泵退出。"""
    server = _RecordingServer()

    async def source() -> Any:
        yield {"type": "health", **HEALTH_FRAME}
        # 空窗 = 2.5 个 idle_timeout：连续超时 2 次 < stall_limit 3，随后恢复。
        await asyncio.sleep(0.15)
        yield {"type": "message", "chat_id": "wxg:g", "text": "迟到的帧"}

    await asyncio.wait_for(
        pump_frames(server, source(), idle_timeout=0.06, stall_limit=3), STEP
    )
    assert [frame["type"] for frame in server.frames] == ["health", "message"]


async def test_pump_stall_limit_stops_pump_when_source_is_dead() -> None:
    """连续空转达到 stall_limit 才停泵：真卡死仍会退出，不会无限等。"""
    server = _RecordingServer()

    async def source() -> Any:
        yield {"type": "health", **HEALTH_FRAME}
        await asyncio.Event().wait()  # 之后永不产出

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(
        pump_frames(server, source(), idle_timeout=0.05, stall_limit=2), STEP
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert len(server.frames) == 1
    assert elapsed >= 0.10, "没等满 stall_limit 次空转就停泵了"


# ------------------------------------- (f3) loss-free 回归：闸门 / 持帧 / 重投
def _bridge_msg(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "chat_id": "wxg:laoyouqun",
        "message_id": f"1:{text}",
        "text": text,
    }


class _StaticConn:
    """broadcast 的假快速连接：send 立即成功。"""

    def __init__(self) -> None:
        self.received: list[str] = []

    async def send(self, payload: str) -> None:
        self.received.append(payload)


class _HangingConn:
    """send 永远挂起的假连接：模拟收不动帧的慢客户端。"""

    def __init__(self) -> None:
        self.received: list[str] = []

    async def send(self, payload: str) -> None:
        self.received.append(payload)
        await asyncio.sleep(30)  # 由 broadcast 的逐连接 wait_for 取消


async def _wait_absent(server: Any) -> None:
    """等断线收尾：has_clients 翻转为 False。"""

    async def _poll() -> None:
        while server.has_clients:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), STEP)


async def test_broadcast_counts_clients_and_drops_slow_one() -> None:
    """broadcast 返回送达数；发送超时的连接在广播内被剔除，超时不外泄。"""
    server = BridgeServer(broadcast_timeout=0.05)
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        # 真实客户端：在场信号翻转 + 送达计数。
        async with connect(_uri(port)) as ws:
            assert server.has_clients is True
            assert (
                await asyncio.wait_for(
                    server.broadcast({"type": "message", "text": "ok"}), STEP
                )
                == 1
            )
            frame = await _recv_until(ws, lambda f: f.get("type") == "message")
            assert frame["text"] == "ok"
        # 断开是异步收尾：等在场信号翻转，再走无假连接检查。
        await asyncio.wait_for(_wait_absent(server), STEP)
        assert server.has_clients is False

        # 假连接：一个能收，一个 send 挂死；计数只算成功者，挂死者被剔除。
        fast = _StaticConn()
        slow = _HangingConn()
        server._connections.update({fast, slow})
        assert (
            await asyncio.wait_for(
                server.broadcast({"type": "message", "text": "慢客户端"}), STEP
            )
            == 1
        )
        assert fast in server._connections
        assert slow not in server._connections
        assert fast.received and "慢客户端" in fast.received[-1]

        # 全部掉线：0 个客户端，广播返回 0，不报错。
        server._connections.clear()
        assert await asyncio.wait_for(server.broadcast(_bridge_msg("没人")), STEP) == 0
    finally:
        await asyncio.wait_for(server.stop(), STEP)
    assert server.has_clients is False


class _TimeoutOnceServer:
    """第一次 broadcast 超时且把客户端判掉线的假服务器（事故形态复刻）。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._present = asyncio.Event()
        self._present.set()  # 泵启动时就有一个客户端在场
        self._client = True
        self.fail_once = True

    @property
    def has_clients(self) -> bool:
        return self._client

    def connect(self) -> None:
        self._client = True
        self._present.set()

    async def wait_for_client(self) -> None:
        await self._present.wait()

    async def broadcast(self, frame: dict[str, Any]) -> int:
        if self.fail_once:
            self.fail_once = False
            self._client = False
            self._present.clear()
            raise asyncio.TimeoutError()
        self.frames.append(frame)
        return 1


async def test_pump_survives_broadcast_timeout_and_retries() -> None:
    """broadcast 抛 TimeoutError 不杀泵：同一帧等客户端回来后重投，顺序不变。"""
    server = _TimeoutOnceServer()

    async def source() -> Any:
        yield _bridge_msg("第一帧")
        yield _bridge_msg("第二帧")

    pump = asyncio.create_task(pump_frames(server, source(), idle_timeout=STEP))
    try:
        # 超时只按 delivered=0 处理：泵活着，帧被持有，不外抛也不丢。
        await asyncio.sleep(0.1)
        assert not pump.done()
        assert server.frames == []

        server.connect()
        await asyncio.wait_for(pump, STEP)
        assert [f["text"] for f in server.frames] == ["第一帧", "第二帧"]
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump


async def test_pump_pauses_source_until_client_connects() -> None:
    """无客户端时帧源不被拉动；接入后先送达已有帧，断线期间计数冻结。"""
    server = BridgeServer()
    port = await asyncio.wait_for(server.start(), STEP)
    counter = 0
    step2 = asyncio.Event()
    step3 = asyncio.Event()

    async def source() -> Any:
        nonlocal counter
        counter += 1
        yield _bridge_msg("m1")
        await step2.wait()
        counter += 1
        yield _bridge_msg("m2")
        await step3.wait()
        counter += 1
        yield _bridge_msg("m3")

    pump = asyncio.create_task(pump_frames(server, source(), idle_timeout=STEP))
    try:
        # 阶段 1：无人能收帧——帧源一次都不该被碰。
        await asyncio.sleep(0.15)
        assert counter == 0
        assert not pump.done()

        # 阶段 2：客户端接入 → 第一帧送达，源被拉动一格。
        async with connect(_uri(port)) as ws:
            first = await _recv_until(ws, lambda f: f.get("type") == "message")
            assert first["text"] == "m1"
            step2.set()
            second = await _recv_until(
                ws, lambda f: f.get("type") == "message" and f.get("text") == "m2"
            )
            assert second["v"] == 1
            assert counter == 2

        # 阶段 3：断开期间生产与消费都被闸住，计数冻结。
        await asyncio.sleep(0.15)
        assert counter == 2

        # 阶段 4：客户端回来，闸门放开，最后的帧送达且泵自然收尾。
        async with connect(_uri(port)) as ws:
            step3.set()
            third = await _recv_until(
                ws, lambda f: f.get("type") == "message" and f.get("text") == "m3"
            )
            assert third["text"] == "m3"
            assert counter == 3
        await asyncio.wait_for(pump, STEP)
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await asyncio.wait_for(server.stop(), STEP)


class _DroppingServer:
    """可控「投递窗口掉线」的假服务器：断线前已产出的帧重连后原序补投。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._present = asyncio.Event()
        self._present.set()  # 泵启动时就有一个客户端在场
        self._client = True
        self.drop_next = False

    @property
    def has_clients(self) -> bool:
        return self._client

    def connect(self) -> None:
        self._client = True
        self._present.set()

    async def wait_for_client(self) -> None:
        await self._present.wait()

    async def broadcast(self, frame: dict[str, Any]) -> int:
        if self.drop_next:
            self.drop_next = False
            self._client = False
            self._present.clear()
            return 0  # 客户端恰好在投递窗口断线：无人收到
        self.frames.append(frame)
        return 1


async def test_pump_delivers_frame_from_before_disconnect_after_reconnect() -> None:
    """断线瞬间已产出未送达的帧不丢：重连后先补投它，再按序投积压帧。"""
    server = _DroppingServer()
    ready = asyncio.Event()

    async def source() -> Any:
        yield _bridge_msg("m1")
        await ready.wait()
        yield _bridge_msg("m2")
        yield _bridge_msg("m3")

    pump = asyncio.create_task(pump_frames(server, source(), idle_timeout=STEP))
    try:
        await asyncio.sleep(0.1)
        assert [f["text"] for f in server.frames] == ["m1"]

        # 掉线：m2 已产出但无人收到——被持有，不静默丢弃。
        server.drop_next = True
        ready.set()
        await asyncio.sleep(0.1)
        assert [f["text"] for f in server.frames] == ["m1"]

        # 客户端回来：m2 补投，m3 排在后面 —— 无丢失、顺序保持。
        server.connect()
        await asyncio.wait_for(pump, STEP)
        assert [f["text"] for f in server.frames] == ["m1", "m2", "m3"]
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump


async def test_run_exits_nonzero_when_pump_dies(tmp_path: Path) -> None:
    """帧泵在轮询仍应继续时结束 = 入站静默失效：非 0 退出交给 supervisor 重启。"""
    wechat = _wechat(kill_switch_file=str(tmp_path / "wechat.stop"))
    holder: dict[str, Any] = {}

    class FakeDriver:
        """本用例不触碰驱动。"""

    class DeadPoller:
        """帧源产一帧就结束，且没有 stop / 急停：模拟帧泵意外死亡。"""

        def __init__(self) -> None:
            self.stop_calls = 0

        async def start(self) -> None:
            return None

        async def request_send(self, fn: Any) -> Any:
            return {"v": 1, "type": "health", "connected": True}

        async def frames(self) -> Any:
            yield {"v": 1, "type": "health", "connected": True, "logged_in": True}
            return

        @property
        def stopped(self) -> bool:
            return False

        def abort_sends(self) -> int:
            return 0

        def stop(self, *, timeout: float = STEP) -> bool:
            self.stop_calls += 1
            return True

    poller = DeadPoller()

    def server_factory(*args: Any, **kwargs: Any) -> BridgeServer:
        server = BridgeServer(*args, **kwargs)
        holder["server"] = server
        return server

    async def command_handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any] | None:
        return {"ok": True, "error_code": None, "delivered": "typed", "message_id": None}

    run_task = asyncio.create_task(
        bridge_main.run(
            settings=SimpleNamespace(wechat=wechat),
            driver_factory=FakeDriver,
            server_factory=server_factory,
            command_handler=command_handler,
            poller_factory=lambda _config, _driver: poller,
        )
    )
    try:
        # 帧源只有在有人能收帧时才会被拉动（loss-free 闸门）：接入一个客户端，
        # 让泵把 DeadPoller 的帧源跑到耗尽，才能观测「泵结束 -> 退出 1」的路径。
        server = await _wait_for_port(holder)
        async with connect(
            _uri(server.port), additional_headers={"Authorization": f"Bearer {TOKEN}"}
        ):
            assert await asyncio.wait_for(run_task, STEP) == 1
        assert poller.stop_calls == 1
    finally:
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(run_task, STEP)


async def test_probe_and_bad_version_do_not_crash_loop() -> None:
    server = BridgeServer()
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            await asyncio.wait_for(
                ws.send(json.dumps({"v": 1, "id": "p", "action": "probe", "params": {}})),
                STEP,
            )
            probe = await _recv_until(ws, lambda f: f.get("id") == "p")
            assert probe["ok"] is True

            # 坏 JSON 不该断连接：随后一条 probe 仍然有回执。
            await asyncio.wait_for(ws.send("not json"), STEP)
            await asyncio.wait_for(
                ws.send(json.dumps({"v": 1, "id": "p2", "action": "probe", "params": {}})),
                STEP,
            )
            assert (await _recv_until(ws, lambda f: f.get("id") == "p2"))["ok"] is True

            # §6.6：首条非 v1 的信封回 version_mismatch 再断开。
            await asyncio.wait_for(
                ws.send(json.dumps({"v": 2, "id": "v2", "action": "probe", "params": {}})),
                STEP,
            )
            bad = await _recv_until(ws, lambda f: f.get("id") == "v2")
            assert bad["ok"] is False
            assert bad["error_code"] == "version_mismatch"
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), STEP)
    finally:
        await asyncio.wait_for(server.stop(), STEP)


# ------------------------------------------- (g) resume 指令重建 pump 任务
async def test_run_resume_restarts_pump(tmp_path: Path) -> None:
    """kill 文件存在时 resume 必须拒绝；删文件后必须重建灌帧任务。"""
    wechat = _wechat(kill_switch_file=str(tmp_path / "wechat.stop"))
    holder: dict[str, Any] = {}

    class FakeDriver:
        """本用例不触碰驱动。"""

    class FakePoller:
        def __init__(self) -> None:
            self.frames_calls = 0
            self.resets = 0
            self.abort_calls = 0
            self.stop_calls = 0
            self._stopped = False

        @property
        def stopped(self) -> bool:
            return self._stopped

        async def start(self) -> None:
            return None

        async def frames(self) -> Any:
            self.frames_calls += 1
            yield {
                "v": 1,
                "type": "health",
                "connected": True,
                "logged_in": True,
                "desktop": "interactive",
            }
            # 真实轮询只会在 stop / 急停之后结束：单帧帧源等价于「已被急停」，
            # 所以结束即置 stopped，别让 run() 把它判成「帧泵意外死亡」。
            self._stopped = True

        def reset_for_resume(self) -> bool:
            self.resets += 1
            self._stopped = False
            return True

        def abort_sends(self) -> int:
            self.abort_calls += 1
            return 0

        def stop(self, *, timeout: float = STEP) -> bool:
            self.stop_calls += 1
            return True

    poller = FakePoller()

    def poller_factory(_config: WeChatConfig, _driver: Any) -> FakePoller:
        return poller

    def server_factory(*args: Any, **kwargs: Any) -> BridgeServer:
        server = BridgeServer(*args, **kwargs)
        holder["server"] = server
        return server

    kill_path = Path(wechat.kill_switch_file)
    run_task = asyncio.create_task(
        bridge_main.run(
            settings=SimpleNamespace(wechat=wechat),
            driver_factory=FakeDriver,
            server_factory=server_factory,
            poller_factory=poller_factory,
        )
    )
    try:
        server = await _wait_for_port(holder)
        async with connect(
            _uri(server.port), additional_headers={"Authorization": f"Bearer {TOKEN}"}
        ) as ws:
            # 等第一轮 pump 结束（FakePoller.frames 只产一帧就返回）。
            async def _wait_first_pump() -> None:
                while poller.frames_calls < 1:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(_wait_first_pump(), STEP)

            kill_path.write_text("stop\n", encoding="utf-8")
            await asyncio.wait_for(
                ws.send(
                    json.dumps(
                        {"v": 1, "id": "r0", "action": "resume", "params": {}}
                    )
                ),
                STEP,
            )
            blocked = await _recv_until(ws, lambda f: f.get("id") == "r0")
            assert blocked["ok"] is False
            assert blocked["error_code"] == "kill_switch_active"

            kill_path.unlink()
            await asyncio.wait_for(
                ws.send(
                    json.dumps(
                        {"v": 1, "id": "r1", "action": "resume", "params": {}}
                    )
                ),
                STEP,
            )
            resumed = await _recv_until(ws, lambda f: f.get("id") == "r1")
            assert resumed["ok"] is True
            assert poller.resets == 1

            async def _wait_second_pump() -> None:
                while poller.frames_calls < 2:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(_wait_second_pump(), STEP)
            assert poller.frames_calls == 2

            await asyncio.wait_for(
                ws.send(
                    json.dumps(
                        {"v": 1, "id": "bye", "action": "stop", "params": {}}
                    )
                ),
                STEP,
            )
            assert (await _recv_until(ws, lambda f: f.get("id") == "bye"))["ok"] is True

        assert await asyncio.wait_for(run_task, STEP) == 0
    finally:
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(run_task, STEP)


def test_ui_thread_driver_exposes_self_input_age_without_ui_call() -> None:
    """前台保护要在 worker 线程里读这个值：纯委托，不投递 UIA op。"""
    raw = SimpleNamespace(last_synthetic_input_age=lambda: 0.4)
    facade = bridge_main._UiThreadDriver(SimpleNamespace(), raw)

    assert facade.last_synthetic_input_age() == 0.4


def test_ui_thread_driver_self_input_age_is_none_without_hook() -> None:
    facade = bridge_main._UiThreadDriver(SimpleNamespace(), SimpleNamespace())

    assert facade.last_synthetic_input_age() is None


# ------------------------------------------------------- keepalive（默认关闭）
def test_hook_health_url_derives_from_send_endpoint() -> None:
    assert (
        bridge_main._hook_health_url("http://127.0.0.1:30011/send_text?x=1")
        == "http://127.0.0.1:30011/health"
    )
    assert bridge_main._hook_health_url("") == ""
    assert bridge_main._hook_health_url("127.0.0.1:30011/send_text") == ""


def test_start_keepalive_is_off_by_default() -> None:
    assert bridge_main._start_keepalive(_wechat()) is None
    assert (
        bridge_main._start_keepalive(
            _wechat(keepalive=True, hook_url="")
        )
        is None
    )


async def test_start_keepalive_creates_cancellable_task() -> None:
    task = bridge_main._start_keepalive(
        _wechat(keepalive=True, hook_url="http://127.0.0.1:1/send_text")
    )
    assert task is not None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_keepalive_loop_survives_probe_errors() -> None:
    calls: list[str] = []
    done = asyncio.Event()

    async def probe(url: str) -> bool:
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("hook is gone")
        done.set()
        return False

    task = asyncio.create_task(
        bridge_main._keepalive_loop("http://x/health", interval=0.01, probe=probe)
    )
    await asyncio.wait_for(done.wait(), STEP)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert calls[:2] == ["http://x/health", "http://x/health"]


def test_default_receipt_verifier_carries_ack_requirement() -> None:
    poller = SimpleNamespace(localdb=object())
    strict = bridge_main._default_receipt_verifier(
        _wechat(send_verify=True, ack_requires_observed=True), poller
    )
    loose = bridge_main._default_receipt_verifier(
        _wechat(send_verify=True, ack_requires_observed=False), poller
    )

    assert strict is not None and loose is not None
    assert strict._required is True
    assert loose._required is False


# --------------------------------------------------- (h) 发送去重（dedupe_key）
_RESULT = {"ok": True, "error_code": None, "delivered": "observed", "message_id": "7:1"}


async def _stopped(server: BridgeServer) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(server.stop(), STEP)


def _freeze_clock(server: BridgeServer, now: float = 1000.0) -> list[float]:
    """冻结入站水位落盘节流的时钟，返回可推进的 ``[now]``。

    落盘节流按「时间间隔 / 帧数」双阈值触发：测试用假时钟推进时间，
    不必真的等 ``INBOUND_SAVE_INTERVAL`` 秒，也不会因机器慢而偶发 flush。
    """
    clock = [now]
    server._clock = lambda: clock[0]
    server._inbound_last_save = clock[0]
    return clock


async def test_dedupe_replays_completed_result_with_current_receipt_id() -> None:
    """同一个 dedupe_key 连发两次：handler 只执行一次，两次回执都 ok 且
    message_id 相同，但 id 各自是当前请求的（回执复用 _receipt 的合并）。"""
    calls: list[str] = []

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        calls.append(command_id)
        return dict(_RESULT)

    server = BridgeServer(command_handler=handler, send_timeout=STEP)
    try:
        params = {"chat_id": "wxg:a", "text": "hi", "dedupe_key": "k1"}
        first = await asyncio.wait_for(
            server._dispatch_command("send_text", params, "r1"), STEP
        )
        second = await asyncio.wait_for(
            server._dispatch_command("send_text", params, "r2"), STEP
        )
        assert calls == ["r1"], "第二次必须直接重放缓存结果，不得重复执行 handler"
        assert first["id"] == "r1" and second["id"] == "r2"
        assert first["ok"] is second["ok"] is True
        assert first["message_id"] == second["message_id"] == "7:1"
        assert second["delivered"] == "observed"
    finally:
        await _stopped(server)


async def test_dedupe_different_key_or_chat_executes_separately() -> None:
    calls: list[str] = []

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        calls.append(command_id)
        return dict(_RESULT)

    server = BridgeServer(command_handler=handler, send_timeout=STEP)
    try:
        base = {"chat_id": "wxg:a", "text": "hi", "dedupe_key": "k1"}
        await asyncio.wait_for(
            server._dispatch_command("send_text", dict(base), "r1"), STEP
        )
        await asyncio.wait_for(
            server._dispatch_command("send_text", {**base, "dedupe_key": "k2"}, "r2"),
            STEP,
        )
        await asyncio.wait_for(
            server._dispatch_command(
                "send_text", {**base, "chat_id": "wxg:b"}, "r3"
            ),
            STEP,
        )
        assert sorted(calls) == ["r1", "r2", "r3"]
    finally:
        await _stopped(server)


async def test_dedupe_in_flight_requests_merge_into_one_execution() -> None:
    """并发两个同 key 请求：卡住 handler，第二个必须合流而不是再执行一次。"""
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        calls.append(command_id)
        started.set()
        await release.wait()
        return dict(_RESULT)

    server = BridgeServer(command_handler=handler, send_timeout=STEP)
    try:
        params = {"chat_id": "wxg:a", "text": "hi", "dedupe_key": "k1"}
        first = asyncio.create_task(server._dispatch_command("send_text", params, "r1"))
        await asyncio.wait_for(started.wait(), STEP)
        second = asyncio.create_task(server._dispatch_command("send_text", params, "r2"))
        release.set()
        one = await asyncio.wait_for(first, STEP)
        two = await asyncio.wait_for(second, STEP)
        assert calls == ["r1"]
        assert one["id"] == "r1" and two["id"] == "r2"
        assert one["ok"] is two["ok"] is True
        assert one["message_id"] == two["message_id"] == "7:1"
    finally:
        await _stopped(server)


async def test_dedupe_caller_cancel_does_not_cancel_shared_execution() -> None:
    """合流用 asyncio.shield：请求方取消只取消它自己，共享执行照常完成。"""
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        calls.append(command_id)
        await release.wait()
        return dict(_RESULT)

    server = BridgeServer(command_handler=handler, send_timeout=STEP)
    try:
        params = {"chat_id": "wxg:a", "text": "hi", "dedupe_key": "k1"}
        first = asyncio.create_task(server._dispatch_command("send_text", params, "r1"))
        while not calls:
            await asyncio.sleep(0.01)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(first, STEP)
        # 共享执行没被掐死：后续同 key 合流到同一个在途执行。
        second = asyncio.create_task(server._dispatch_command("send_text", params, "r2"))
        await asyncio.sleep(0.02)
        assert len(calls) == 1
        release.set()
        receipt = await asyncio.wait_for(second, STEP)
        assert receipt["id"] == "r2"
        assert receipt["ok"] is True
        assert receipt["message_id"] == "7:1"
    finally:
        await _stopped(server)


async def test_dedupe_failure_result_is_not_cached() -> None:
    calls: list[str] = []

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        calls.append(command_id)
        raise RuntimeError("boom")

    server = BridgeServer(command_handler=handler, send_timeout=STEP)
    try:
        params = {"chat_id": "wxg:a", "text": "hi", "dedupe_key": "k1"}
        first = await asyncio.wait_for(
            server._dispatch_command("send_text", params, "r1"), STEP
        )
        assert first["ok"] is False and first["error_code"] == "internal_error"
        second = await asyncio.wait_for(
            server._dispatch_command("send_text", params, "r2"), STEP
        )
        assert second["ok"] is False and second["error_code"] == "internal_error"
        assert second["id"] == "r2"
        assert len(calls) == 2, "失败结局不缓存：第二次要重新执行"
    finally:
        await _stopped(server)


async def test_dedupe_timeout_receipt_and_reexecution() -> None:
    calls: list[str] = []

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        calls.append(command_id)
        await asyncio.sleep(0.5)
        return dict(_RESULT)

    server = BridgeServer(command_handler=handler, send_timeout=0.4)
    try:
        params = {"chat_id": "wxg:a", "text": "hi", "dedupe_key": "k1"}
        first = await asyncio.wait_for(
            server._dispatch_command("send_text", params, "r1"), STEP
        )
        assert first["ok"] is False and first["error_code"] == "timeout"
        second = await asyncio.wait_for(
            server._dispatch_command("send_text", params, "r2"), STEP
        )
        assert second["error_code"] == "timeout"
        assert len(calls) == 2
    finally:
        await _stopped(server)


async def test_dedupe_ttl_expires_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_server, "DEDUPE_TTL_SECONDS", 0.0)
    calls: list[str] = []

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        calls.append(command_id)
        await asyncio.sleep(0.01)
        return dict(_RESULT)

    server = BridgeServer(command_handler=handler, send_timeout=STEP)
    try:
        params = {"chat_id": "wxg:a", "text": "hi", "dedupe_key": "k1"}
        await asyncio.wait_for(server._dispatch_command("send_text", params, "r1"), STEP)
        await asyncio.sleep(0.02)
        await asyncio.wait_for(server._dispatch_command("send_text", params, "r2"), STEP)
        assert len(calls) == 2, "TTL 过期后允许重新执行"
    finally:
        await _stopped(server)


async def test_dedupe_capacity_evicts_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_server, "DEDUPE_MAX_ENTRIES", 1)
    calls: list[str] = []

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        calls.append(command_id)
        await asyncio.sleep(0)
        return dict(_RESULT)

    server = BridgeServer(command_handler=handler, send_timeout=STEP)
    try:
        base = {"chat_id": "wxg:a", "text": "hi"}
        await asyncio.wait_for(
            server._dispatch_command("send_text", {**base, "dedupe_key": "k1"}, "r1"),
            STEP,
        )
        await asyncio.wait_for(
            server._dispatch_command("send_text", {**base, "dedupe_key": "k2"}, "r2"),
            STEP,
        )
        await asyncio.wait_for(
            server._dispatch_command("send_text", {**base, "dedupe_key": "k1"}, "r3"),
            STEP,
        )
        assert len(calls) == 3, "容量到顶按插入时间淘汰最旧，k1 已被 k2 挤出"
    finally:
        await _stopped(server)


async def test_dedupe_absent_key_keeps_legacy_behavior() -> None:
    """无 key 的指令完全不变：不进去重表，连发两次各执行一次。"""
    calls: list[str] = []

    async def handler(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        calls.append(command_id)
        return dict(_RESULT)

    server = BridgeServer(command_handler=handler, send_timeout=STEP)
    try:
        params = {"chat_id": "wxg:a", "text": "hi"}
        await asyncio.wait_for(server._dispatch_command("send_text", params, "r1"), STEP)
        await asyncio.wait_for(server._dispatch_command("send_text", params, "r2"), STEP)
        assert calls == ["r1", "r2"]
        assert server._dedupe == {}
    finally:
        await _stopped(server)


# ------------------------------------- 入站 seq/ack/hello 水位（P3-10 / P3-11）
async def test_inbound_seq_only_on_event_frames_and_monotonic(tmp_path: Path) -> None:
    """seq 只给 message/recall，从 1 起单调递增；health 永不带 seq 但带水位。"""
    server = BridgeServer(state_path=tmp_path / "inbound.json")
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            msg1 = _bridge_msg("m1")
            await asyncio.wait_for(server.broadcast(msg1), STEP)
            first = await _recv_until(ws, lambda f: f.get("type") == "message")
            assert first["seq"] == 1
            # seq 写回帧源对象：帧泵 pending 重试同一 dict 时不会再发号。
            assert msg1["seq"] == 1

            await asyncio.wait_for(server.broadcast(_bridge_msg("m2")), STEP)
            second = await _recv_until(
                ws, lambda f: f.get("type") == "message" and f.get("text") == "m2"
            )
            assert second["seq"] == 2

            await asyncio.wait_for(
                server.broadcast({"type": "health", **HEALTH_FRAME}), STEP
            )
            health = await _recv_until(
                ws, lambda f: f.get("type") == "health" and "inbound_seq" in f
            )
            assert "seq" not in health
            assert health["inbound_seq"] == 2
            assert health["inbound_acked"] == 0
            assert health["unacked_count"] == 2

            # recall 同样带 seq，且在 message 的水位之上。
            await asyncio.wait_for(
                server.broadcast(
                    {"type": "recall", "chat_id": "wxg:g", "message_id": "1:9"}
                ),
                STEP,
            )
            recall = await _recv_until(ws, lambda f: f.get("type") == "recall")
            assert recall["seq"] == 3
    finally:
        await asyncio.wait_for(server.stop(), STEP)


async def test_ack_trims_unacked_and_persists(tmp_path: Path) -> None:
    """ack 后未确认缓冲裁剪 + 显式 flush 后水位落盘（seq / acked 两个键）。"""
    server = BridgeServer(state_path=tmp_path / "state.json")
    _freeze_clock(server)
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            await asyncio.wait_for(server.broadcast(_bridge_msg("m1")), STEP)
            await asyncio.wait_for(server.broadcast(_bridge_msg("m2")), STEP)
            await _recv_until(
                ws, lambda f: f.get("type") == "message" and f.get("seq") == 2
            )
            assert len(server._unacked) == 2

            await asyncio.wait_for(
                ws.send(
                    json.dumps({"v": 1, "id": "a1", "action": "ack", "params": {"seq": 2}})
                ),
                STEP,
            )
            receipt = await _recv_until(ws, lambda f: f.get("id") == "a1")
            assert receipt["ok"] is True
            assert receipt["error_code"] is None
            assert len(server._unacked) == 0
            assert server._inbound_acked == 2
            assert server._inbound_seq == 2

            # 节流：ack 只标脏，窗口内不写盘（写盘由定时器/帧数阈值/停机触发）。
            assert not (tmp_path / "state.json").exists()

            server.flush_inbound_state()
            state = json.loads((tmp_path / "state.json").read_text("utf-8"))
            assert state == {"seq": 2, "acked": 2, "gap_from": None, "frames": []}
    finally:
        await asyncio.wait_for(server.stop(), STEP)


async def test_ack_bad_params_replied_without_crashing(tmp_path: Path) -> None:
    """非法 seq 回 bad_params；连接保持可用，水位置零不动。"""
    server = BridgeServer(state_path=tmp_path / "state.json")
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            await asyncio.wait_for(server.broadcast(_bridge_msg("m1")), STEP)
            await _recv_until(ws, lambda f: f.get("type") == "message")

            for bad in ("x", None, True, -1):
                await asyncio.wait_for(
                    ws.send(
                        json.dumps(
                            {
                                "v": 1,
                                "id": f"bad-{bad}",
                                "action": "ack",
                                "params": {"seq": bad},
                            }
                        )
                    ),
                    STEP,
                )
                receipt = await _recv_until(
                    ws, lambda f: f.get("id") == f"bad-{bad}"
                )
                assert receipt["ok"] is False
                assert receipt["error_code"] == "bad_params"

            # 连接还活着：probe 照常有回执。
            await asyncio.wait_for(
                ws.send(json.dumps({"v": 1, "id": "p1", "action": "probe", "params": {}})),
                STEP,
            )
            assert (await _recv_until(ws, lambda f: f.get("id") == "p1"))["ok"] is True
            assert len(server._unacked) == 1
            assert server._inbound_acked == 0
    finally:
        await asyncio.wait_for(server.stop(), STEP)


async def test_hello_redelivers_higher_seq_only_once_per_connection(
    tmp_path: Path,
) -> None:
    """hello 只重投 > last_seq 的帧（升序带原 seq）；重复 hello 不再重投。"""
    server = BridgeServer(state_path=tmp_path / "state.json")
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            frames = {}
            for text in ("m1", "m2", "m3"):
                frame = _bridge_msg(text)
                frames[text] = frame
                await asyncio.wait_for(server.broadcast(frame), STEP)
                await _recv_until(
                    ws, lambda f: f.get("text") == text, limit=6
                )

            await asyncio.wait_for(
                ws.send(
                    json.dumps({"v": 1, "id": "h1", "action": "hello", "params": {"last_seq": 1}})
                ),
                STEP,
            )
            redelivered = await _recv_json(ws)
            assert redelivered["type"] == "message" and redelivered["seq"] == 2
            assert redelivered["text"] == "m2"
            redelivered = await _recv_json(ws)
            assert redelivered["seq"] == 3
            hello_receipt = await _recv_json(ws)
            assert hello_receipt["id"] == "h1" and hello_receipt["ok"] is True

            # 同一连接的第二个 hello：只更新高水位，不重投——下一帧是自己的回执。
            await asyncio.wait_for(
                ws.send(
                    json.dumps({"v": 1, "id": "h2", "action": "hello", "params": {"last_seq": 0}})
                ),
                STEP,
            )
            # 帧泵 pending 重试同一 dict（已带 seq=2/3）：已重投过的 seq 不再出现。
            await asyncio.wait_for(server.broadcast(frames["m2"]), STEP)
            await asyncio.wait_for(server.broadcast(frames["m3"]), STEP)
            hi_receipt = await _recv_json(ws)
            assert hi_receipt["id"] == "h2" and hi_receipt["ok"] is True
            # 同一帧不得对同一连接出现第二遍：下一条必须是探针回执而非消息帧。
            await asyncio.wait_for(
                ws.send(
                    json.dumps({"v": 1, "id": "p9", "action": "probe", "params": {}})
                ),
                STEP,
            )
            probe_receipt = await _recv_json(ws)
            assert probe_receipt["id"] == "p9", probe_receipt
    finally:
        await asyncio.wait_for(server.stop(), STEP)


async def test_ack_out_of_order_never_lowers_watermark(tmp_path: Path) -> None:
    """ack(3) 后再收 ack(2)：水位不回退，帧不复活，unacked_count 保持 0。"""
    server = BridgeServer(state_path=tmp_path / "state.json")
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            for text in ("m1", "m2", "m3"):
                await asyncio.wait_for(server.broadcast(_bridge_msg(text)), STEP)
            await _recv_until(
                ws, lambda f: f.get("type") == "message" and f.get("seq") == 3, limit=6
            )

            for command_id, seq in (("a3", 3), ("a2", 2)):
                await asyncio.wait_for(
                    ws.send(
                        json.dumps(
                            {
                                "v": 1,
                                "id": command_id,
                                "action": "ack",
                                "params": {"seq": seq},
                            }
                        )
                    ),
                    STEP,
                )
                receipt = await _recv_until(ws, lambda f: f.get("id") == command_id)
                assert receipt["ok"] is True

            assert server._inbound_acked == 3
            assert len(server._unacked) == 0

            await asyncio.wait_for(
                server.broadcast({"type": "health", **HEALTH_FRAME}), STEP
            )
            health = await _recv_until(
                ws, lambda f: f.get("type") == "health" and "unacked_count" in f
            )
            assert health["inbound_seq"] == 3
            assert health["inbound_acked"] == 3
            assert health["unacked_count"] == 0
    finally:
        await asyncio.wait_for(server.stop(), STEP)


async def test_inbound_state_never_regresses_across_restart(tmp_path: Path) -> None:
    """重启后的新实例从水位文件恢复 seq：绝不回退到 0 重新发号。"""
    path = tmp_path / "state.json"
    server = BridgeServer(state_path=path)
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            for text in ("m1", "m2"):
                await asyncio.wait_for(server.broadcast(_bridge_msg(text)), STEP)
            await _recv_until(
                ws, lambda f: f.get("type") == "message" and f.get("seq") == 2, limit=6
            )
            await asyncio.wait_for(
                ws.send(json.dumps({"v": 1, "id": "a1", "action": "ack", "params": {"seq": 2}})),
                STEP,
            )
            assert (await _recv_until(ws, lambda f: f.get("id") == "a1"))["ok"] is True
    finally:
        await asyncio.wait_for(server.stop(), STEP)

    assert json.loads(path.read_text("utf-8")) == {
        "seq": 2,
        "acked": 2,
        "gap_from": None,
        "frames": [],
    }

    server2 = BridgeServer(state_path=path)
    assert server2._inbound_seq == 2
    assert server2._inbound_acked == 2
    port2 = await asyncio.wait_for(server2.start(), STEP)
    try:
        async with connect(_uri(port2)) as ws:
            await asyncio.wait_for(server2.broadcast(_bridge_msg("m3")), STEP)
            third = await _recv_until(ws, lambda f: f.get("type") == "message")
            assert third["seq"] == 3, "重启后 seq 必须接着水位继续，而不是回到 1"
    finally:
        await asyncio.wait_for(server2.stop(), STEP)
    # 未确认帧现在随水位每帧落盘：seq=3 的帧在重启回放窗口内，不会被回退覆盖。
    state = json.loads(path.read_text("utf-8"))
    assert state["seq"] == 3
    assert state["acked"] == 2
    assert [item["seq"] for item in state["frames"]] == [3]


async def test_corrupt_state_file_treated_as_zero_without_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """损坏的水位文件按 0 处理并告警：构造、启动、发号全程不崩。"""
    path = tmp_path / "state.json"
    path.write_text("{not json at all", encoding="utf-8")

    server = BridgeServer(state_path=path)
    assert server._inbound_seq == 0
    assert server._inbound_acked == 0
    with caplog.at_level("WARNING", logger="dafeiyu.gateway.wechat_bridge.server"):
        port = await asyncio.wait_for(server.start(), STEP)
    try:
        assert await asyncio.wait_for(server.broadcast(_bridge_msg("m1")), STEP) == 0
        assert server._inbound_seq == 1
        assert "水位" in caplog.text or "watermark" in caplog.text
    finally:
        await asyncio.wait_for(server.stop(), STEP)


# ------------------------------------- 未确认帧持久化 / 硬杀重放（P3-11）
async def test_unacked_frames_survive_hard_kill_and_replay_on_hello(
    tmp_path: Path,
) -> None:
    """不调用 stop 直接换实例：未确认帧从状态文件恢复，hello(0) 原样重投。"""
    path = tmp_path / "state.json"
    server = BridgeServer(state_path=path)
    _freeze_clock(server)
    port = await asyncio.wait_for(server.start(), STEP)
    originals: dict[str, dict[str, Any]] = {}
    ws = await connect(_uri(port))
    try:
        for text in ("m1", "m2"):
            frame = _bridge_msg(text)
            originals[text] = frame
            await asyncio.wait_for(server.broadcast(frame), STEP)
            await _recv_until(
                ws, lambda f, t=text: f.get("type") == "message" and f.get("text") == t
            )
    finally:
        await ws.close()

    # 模拟进程被硬杀：不调用 stop。节流窗口内的帧此刻还没写盘——这正是
    # 用「脏标记 + 定时 flush」换掉每帧写盘的代价，必须显式证明边界。
    assert not path.exists()

    # 生产环境由 1s / 20 帧的定时器在窗口结束时 flush；这里显式模拟该时刻，
    # 证明「最后一帧已落盘」后硬杀，重启仍能原样重投。
    server.flush_inbound_state()
    state = json.loads(path.read_text("utf-8"))
    assert [item["seq"] for item in state["frames"]] == [1, 2]
    # 状态文件是本地数据文件，允许包含帧正文（用于重投）。
    assert [item["frame"]["text"] for item in state["frames"]] == ["m1", "m2"]

    server2 = BridgeServer(state_path=path)
    assert [seq for seq, _ in server2._unacked] == [1, 2]
    port2 = await asyncio.wait_for(server2.start(), STEP)
    try:
        async with connect(_uri(port2)) as ws2:
            await asyncio.wait_for(
                ws2.send(
                    json.dumps(
                        {"v": 1, "id": "h1", "action": "hello", "params": {"last_seq": 0}}
                    )
                ),
                STEP,
            )
            first = await _recv_json(ws2)
            assert first["type"] == "message"
            assert first["text"] == "m1"
            assert first["seq"] == originals["m1"]["seq"] == 1
            second = await _recv_json(ws2)
            assert second["type"] == "message"
            assert second["text"] == "m2"
            assert second["seq"] == originals["m2"]["seq"] == 2
            receipt = await _recv_json(ws2)
            assert receipt["id"] == "h1" and receipt["ok"] is True
    finally:
        await _stopped(server2)
        await _stopped(server)


async def test_inbound_state_is_not_written_until_flush(tmp_path: Path) -> None:
    """节流核心：窗口内的帧只标脏，显式 flush 前状态文件根本不存在。"""
    path = tmp_path / "state.json"
    server = BridgeServer(state_path=path)
    _freeze_clock(server)
    try:
        for text in ("m1", "m2", "m3"):
            server._stamp_inbound({}, _bridge_msg(text))
        assert server._inbound_dirty is True
        assert not path.exists()

        server.flush_inbound_state()
        assert server._inbound_dirty is False
        state = json.loads(path.read_text("utf-8"))
        assert state["seq"] == 3
        assert [item["seq"] for item in state["frames"]] == [1, 2, 3]

        # 干净时再 flush 是 no-op：不产生额外写盘（mtime 不变）。
        before = path.stat().st_mtime_ns
        server.flush_inbound_state()
        assert path.stat().st_mtime_ns == before
    finally:
        await _stopped(server)


async def test_inbound_state_flushes_when_interval_elapses(tmp_path: Path) -> None:
    """时间阈值：距上次落盘满 INBOUND_SAVE_INTERVAL 后，下一帧触发写盘。"""
    path = tmp_path / "state.json"
    server = BridgeServer(state_path=path)
    clock = _freeze_clock(server)
    try:
        server._stamp_inbound({}, _bridge_msg("m1"))
        assert not path.exists()

        clock[0] += bridge_server.INBOUND_SAVE_INTERVAL
        server._stamp_inbound({}, _bridge_msg("m2"))
        state = json.loads(path.read_text("utf-8"))
        assert [item["seq"] for item in state["frames"]] == [1, 2]
    finally:
        await _stopped(server)


async def test_inbound_state_flushes_at_frame_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """帧数阈值：忙群里不等到时间间隔，累计到 INBOUND_SAVE_FRAMES 帧立刻落盘。"""
    monkeypatch.setattr(bridge_server, "INBOUND_SAVE_FRAMES", 3)
    path = tmp_path / "state.json"
    server = BridgeServer(state_path=path)
    _freeze_clock(server)
    try:
        for text in ("m1", "m2"):
            server._stamp_inbound({}, _bridge_msg(text))
        assert not path.exists()

        server._stamp_inbound({}, _bridge_msg("m3"))
        state = json.loads(path.read_text("utf-8"))
        assert [item["seq"] for item in state["frames"]] == [1, 2, 3]
    finally:
        await _stopped(server)


async def test_stop_flushes_pending_inbound_state(tmp_path: Path) -> None:
    """正常停机兜底：窗口内没来得及写盘的水位在 stop() 里强制落盘，重启可恢复。"""
    path = tmp_path / "state.json"
    server = BridgeServer(state_path=path)
    _freeze_clock(server)
    for text in ("m1", "m2"):
        server._stamp_inbound({}, _bridge_msg(text))
    assert not path.exists()

    await _stopped(server)

    state = json.loads(path.read_text("utf-8"))
    assert state["seq"] == 2
    assert [item["seq"] for item in state["frames"]] == [1, 2]

    restored = BridgeServer(state_path=path)
    assert [seq for seq, _ in restored._unacked] == [1, 2]
    await _stopped(restored)


async def test_acked_frames_not_replayed_after_restart(tmp_path: Path) -> None:
    """ack(1) 后重建：只剩 seq=2 在文件里，hello(0) 只重投它。"""
    path = tmp_path / "state.json"
    server = BridgeServer(state_path=path)
    _freeze_clock(server)
    port = await asyncio.wait_for(server.start(), STEP)
    ws = await connect(_uri(port))
    try:
        for text in ("m1", "m2"):
            await asyncio.wait_for(server.broadcast(_bridge_msg(text)), STEP)
            await _recv_until(
                ws, lambda f, t=text: f.get("type") == "message" and f.get("text") == t
            )
        await asyncio.wait_for(
            ws.send(json.dumps({"v": 1, "id": "a1", "action": "ack", "params": {"seq": 1}})),
            STEP,
        )
        assert (await _recv_until(ws, lambda f: f.get("id") == "a1"))["ok"] is True
    finally:
        await ws.close()

    # 节流窗口内不自动落盘；模拟窗口结束时的定时 flush。
    assert not path.exists()
    server.flush_inbound_state()
    state = json.loads(path.read_text("utf-8"))
    assert state["acked"] == 1
    assert [item["seq"] for item in state["frames"]] == [2]

    server2 = BridgeServer(state_path=path)
    assert [seq for seq, _ in server2._unacked] == [2]
    port2 = await asyncio.wait_for(server2.start(), STEP)
    try:
        async with connect(_uri(port2)) as ws2:
            await asyncio.wait_for(
                ws2.send(
                    json.dumps(
                        {"v": 1, "id": "h1", "action": "hello", "params": {"last_seq": 0}}
                    )
                ),
                STEP,
            )
            only = await _recv_json(ws2)
            assert only["type"] == "message" and only["seq"] == 2
            assert only["text"] == "m2"
            receipt = await _recv_json(ws2)
            assert receipt["id"] == "h1" and receipt["ok"] is True
    finally:
        await _stopped(server2)
        await _stopped(server)


async def test_legacy_state_without_frames_starts_empty(tmp_path: Path) -> None:
    """旧格式只有 seq/acked：启动不崩、缓冲为空，后续从水位 +1 继续发号。"""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"seq": 3, "acked": 0}), encoding="utf-8")

    server = BridgeServer(state_path=path)
    assert server._inbound_seq == 3
    assert server._inbound_acked == 0
    assert len(server._unacked) == 0
    try:
        assert await asyncio.wait_for(server.broadcast(_bridge_msg("m4")), STEP) == 0
        assert server._inbound_seq == 4
    finally:
        await _stopped(server)
    assert json.loads(path.read_text("utf-8"))["seq"] == 4


async def test_restore_skips_bad_frame_entries(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """坏条目（非 int/0 seq、非 dict frame、越界、跨帧 seq 不符、重复）逐条跳过。"""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "seq": 5,
                "acked": 0,
                "frames": [
                    # 先放 seq=5 再放 seq=1：恢复后必须按 seq 升序。
                    {"seq": 5, "frame": {"v": 1, "type": "message", "text": "backfill"}},
                    {"seq": 1, "frame": {"v": 1, "type": "message", "text": "keep-1", "seq": 1}},
                    {"seq": "x", "frame": {"v": 1, "type": "message", "seq": 1}},
                    {"seq": 0, "frame": {"v": 1, "type": "message", "seq": 0}},
                    {"seq": 4, "frame": "not-a-dict"},
                    {"seq": 2, "frame": {"v": 1, "type": "message", "seq": 9}},
                    {"seq": 6, "frame": {"v": 1, "type": "message", "seq": 6}},
                    {"seq": 1, "frame": {"v": 1, "type": "message", "text": "dup", "seq": 1}},
                ],
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="dafeiyu.gateway.wechat_bridge.server"):
        server = BridgeServer(state_path=path)
    assert [seq for seq, _ in server._unacked] == [1, 5]
    first_seq, first_frame = server._unacked[0]
    assert first_frame["text"] == "keep-1", "重复 seq 只保留第一条"
    _, last_frame = server._unacked[1]
    assert last_frame["seq"] == 5, "缺 seq 的帧回填顶层 seq"
    assert caplog.text.count("非法条目") == 1, "坏条目只汇总告警一次，不刷屏"
    await _stopped(server)


async def test_unacked_overflow_records_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """缓冲写满再入帧：被挤掉的最旧 seq 记入 gap_from、告警、随水位落盘。

    用 monkeypatch 把 UNACKED_MAX 临时改小（deque maxlen 在 __init__ 读取），
    避免构造 200 条 envelope；被挤掉的帧重启后不再重投，必须留下账目。
    """
    monkeypatch.setattr(bridge_server, "UNACKED_MAX", 3)
    path = tmp_path / "state.json"
    server = BridgeServer(state_path=path)
    try:
        for i in range(1, 4):
            server._stamp_inbound({}, _bridge_msg(f"m{i}"))
        assert [seq for seq, _ in server._unacked] == [1, 2, 3]
        assert server._inbound_gap_from is None

        with caplog.at_level("WARNING", logger="dafeiyu.gateway.wechat_bridge.server"):
            server._stamp_inbound({}, _bridge_msg("m4"))
        assert server._inbound_gap_from == 1, "首次溢出记下被挤掉的最旧 seq"
        assert [seq for seq, _ in server._unacked] == [2, 3, 4]
        assert "挤掉" in caplog.text and "seq=1" in caplog.text
        # 再溢出：保留最早的 gap_from，不被后续覆盖。
        server._stamp_inbound({}, _bridge_msg("m5"))
        assert server._inbound_gap_from == 1

        # 节流窗口内不写盘；显式 flush 后账目（gap_from + 帧窗口）才可见。
        assert not path.exists()
        server.flush_inbound_state()
        state = json.loads(path.read_text("utf-8"))
        assert state["gap_from"] == 1
        assert [item["seq"] for item in state["frames"]] == [3, 4, 5]
    finally:
        await _stopped(server)

    server2 = BridgeServer(state_path=path)
    assert server2._inbound_gap_from == 1, "gap_from 随水位恢复"
    await _stopped(server2)

    # gap_from <= acked = 已被 ack 追平，视为已恢复：置 None，不再报丢帧。
    path2 = tmp_path / "state2.json"
    path2.write_text(
        json.dumps({"seq": 4, "acked": 4, "gap_from": 2, "frames": []}),
        encoding="utf-8",
    )
    server3 = BridgeServer(state_path=path2)
    assert server3._inbound_gap_from is None
    await _stopped(server3)


async def test_no_state_path_means_no_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 state_path = 纯内存水位：进程帧绝不写进默认生产文件（测试污染回归）。"""
    monkeypatch.chdir(tmp_path)
    server = BridgeServer()
    assert server.state_path is None
    port = await asyncio.wait_for(server.start(), STEP)
    try:
        async with connect(_uri(port)) as ws:
            await asyncio.wait_for(server.broadcast(_bridge_msg("m1")), STEP)
            await _recv_until(ws, lambda f: f.get("type") == "message")
            await asyncio.wait_for(
                ws.send(
                    json.dumps({"v": 1, "id": "a1", "action": "ack", "params": {"seq": 1}})
                ),
                STEP,
            )
            receipt = await _recv_until(ws, lambda f: f.get("id") == "a1")
            assert receipt["ok"] is True
    finally:
        await _stopped(server)
    assert not (tmp_path / bridge_server.DEFAULT_INBOUND_STATE).exists()


def test_main_entry_enables_state_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有真实进程入口开启水位持久化：main() -> run(state_path=默认路径)。"""
    captured: dict[str, Any] = {}

    async def fake_run(
        argv: list[str] | None = None,
        *,
        state_path: str | Path | None = None,
        **kwargs: Any,
    ) -> int:
        captured["state_path"] = state_path
        return 0

    monkeypatch.setattr(bridge_main, "run", fake_run)
    assert bridge_main.main([]) == 0
    assert captured["state_path"] == bridge_server.DEFAULT_INBOUND_STATE
    assert bridge_main.DEFAULT_INBOUND_STATE == bridge_server.DEFAULT_INBOUND_STATE


async def test_run_forwards_state_path_to_server_factory() -> None:
    """run() 把显式 state_path 原样交给 BridgeServer（默认 None 不落盘）。"""
    recorded: dict[str, Any] = {}

    class _Stop(Exception):
        pass

    def server_factory(*args: Any, **kwargs: Any) -> Any:
        recorded.update(kwargs)
        raise _Stop()

    with pytest.raises(_Stop):
        await bridge_main.run(
            settings=SimpleNamespace(wechat=_wechat()),
            driver_factory=type("FakeDriver", (), {}),
            server_factory=server_factory,
            frame_source=[],
            state_path="data/custom-state.json",
        )
    assert recorded["state_path"] == "data/custom-state.json"
