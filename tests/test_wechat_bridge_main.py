"""微信桥 PART A 验收：命令行入口 + WebSocket 服务器生命周期。

覆盖：
- ``python -m dafeiyu.gateway.wechat_bridge --help`` 在没装 pyweixin 时退出 0；
- ``__main__.run()`` 的完整生命周期（假驱动 + 假帧源 + 真 websockets 客户端）；
- 握手 token（对/错/缺）与客户端 ``wechat.py`` 的 header 形态一致；
- 未知动作按 §6 规则 5 回 ``bad_action``，且 ``id`` 原样回填；
- ``_startup_checks`` 的 fail-closed 拒绝（enabled / allowlist / nickname / max_groups）；
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
        "group_allowlist": frozenset({"老友群"}),
        "max_groups": 3,
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


# --------------------------------------------------- (e) 启动 fail-closed 拒绝
def test_startup_refuses_too_many_groups() -> None:
    wechat = _wechat(
        max_groups=3,
        group_allowlist=frozenset({"群一", "群二", "群三", "群四"}),
    )
    with pytest.raises(ConfigError) as excinfo:
        bridge_main._startup_checks(wechat)
    message = str(excinfo.value)
    assert "max_groups" in message
    assert "group_allowlist" in message
    assert "群一" not in message  # 不回显配置值


def test_startup_refuses_empty_allowlist() -> None:
    with pytest.raises(ConfigError) as excinfo:
        bridge_main._startup_checks(_wechat(group_allowlist=frozenset()))
    assert "group_allowlist" in str(excinfo.value)


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

    wechat = _wechat(group_allowlist=frozenset({"a", "b", "c", "d"}), max_groups=3)
    with pytest.raises(ConfigError) as excinfo:
        await asyncio.wait_for(
            bridge_main.run(
                settings=SimpleNamespace(wechat=wechat),
                driver_factory=exploding_driver_factory,
            ),
            STEP,
        )
    assert "max_groups" in str(excinfo.value)


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
