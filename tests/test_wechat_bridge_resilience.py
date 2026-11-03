"""桥的韧性：健康探测超时只降级、重启后对齐窗口能续上停机缺口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dafeiyu.config import WeChatConfig
from dafeiyu.gateway.wechat_bridge.driver import ChatHandle
from dafeiyu.gateway.wechat_bridge.poller import PollingLoop

STEP = 5.0
GROUP = "老友群"
ALIAS = "g1"


def _wechat(tmp_path: Path) -> WeChatConfig:
    return WeChatConfig(
        enabled=True,
        bridge_host="127.0.0.1",
        bridge_port=0,
        bridge_token="t",
        account_nickname="小助手",
        poll_seconds=0.02,
        kill_switch_file=str(tmp_path / "wechat.stop"),
    )


def _panel() -> frozenset[str]:
    """面板白名单非空：只轮询列出的这一个群。"""
    return frozenset({f"wxg:{ALIAS}"})


def _probe(connected: bool) -> dict[str, Any]:
    return {
        "v": 1,
        "type": "health",
        "connected": bool(connected),
        "logged_in": True,
        "desktop": "interactive",
        "wechat_version": "4.1.15.13",
        "language": "zh-CN",
        "integrity": "matching",
        "queue_depth": 0,
        "degraded": False,
        "reason": None,
    }


class _Registry:
    def __init__(self) -> None:
        self._peers = {
            ALIAS: SimpleNamespace(
                alias=ALIAS, display_name=GROUP, live=True, kind="group"
            )
        }

    def chat_id(self, display_name: str, *, kind: str = "group") -> str:
        return f"wxg:{ALIAS}"

    def by_alias(self, alias: str) -> Any | None:
        return self._peers.get(alias)


class FakeDriver:
    """只实现开窗 + 快照；每次快照按脚本推进（超出的轮次停在最后一帧）。"""

    def __init__(self, windows: list[list[tuple[str, str]]]) -> None:
        self.windows = windows
        self._index = 0

    def open_chat(self, name: str) -> ChatHandle:
        return ChatHandle(name=name, window=None)

    def snapshot(self, handle: ChatHandle) -> dict[str, Any]:
        window = self.windows[min(self._index, len(self.windows) - 1)]
        self._index += 1
        return {
            "chat_name": handle.name,
            "messages": [
                {"nickname": nickname, "text": text} for nickname, text in window
            ],
        }


async def _take(source: Any, count: int) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    async def _run() -> None:
        async for frame in source:
            frames.append(frame)
            if len(frames) >= count:
                return

    await asyncio.wait_for(_run(), STEP)
    return frames


async def test_health_probe_timeout_degrades_instead_of_killing_frames(
    tmp_path: Path,
) -> None:
    """发送占住 UIA 线程 → 探测 5s 超时：只能降级，绝不能打死帧源。"""
    calls = {"probe": 0}

    def probe(connected: bool) -> dict[str, Any]:
        calls["probe"] += 1
        if calls["probe"] == 1:
            return _probe(connected)
        raise TimeoutError("uia thread busy with send")

    poller = PollingLoop(
        _wechat(tmp_path),
        FakeDriver([[("甲", "开场")]]),
        probe=probe,
        registry=_Registry(),
        panel=_panel,
        poll_seconds=0.02,
        align_state_path=tmp_path / "align.json",
    )
    frames: list[dict[str, Any]] = []
    source = poller.frames()
    try:
        frames = await _take(source, 6)
    finally:
        await source.aclose()
        poller.stop()

    health = [f for f in frames if f.get("type") == "health"]
    assert len(health) >= 3, frames
    assert health[0]["degraded"] is False
    # 第一次探测成功过，后面的失败帧必须沿用上一次的 desktop，否则 bot
    # 会以为桌面锁了、把发送全拦掉。
    assert all(f["desktop"] == "interactive" for f in health)
    degraded = [f for f in health if f["degraded"]]
    assert degraded, health
    assert all("health_probe_failed" in str(f.get("reason")) for f in degraded)


async def test_align_window_persists_across_restart(tmp_path: Path) -> None:
    """桥崩溃重启后，停机期间的新消息要补出来，而不是被当成基线丢掉。"""
    state = tmp_path / "align.json"

    first = PollingLoop(
        _wechat(tmp_path),
        FakeDriver([[("甲", "开场")]]),
        probe=_probe,
        registry=_Registry(),
        panel=_panel,
        poll_seconds=0.02,
        align_state_path=state,
    )
    source = first.frames()
    try:
        frames = await _take(source, 2)  # 一轮：群 health + 轮末 health
    finally:
        await source.aclose()
        first.stop()

    assert all(f.get("type") == "health" for f in frames), frames
    assert state.exists(), "对齐窗口必须落盘"
    saved = json.loads(state.read_text("utf-8"))
    assert saved[GROUP][0]["text"] == "开场"

    second = PollingLoop(
        _wechat(tmp_path),
        FakeDriver([[("甲", "开场"), ("乙", "停机期间的新消息")]]),
        probe=_probe,
        registry=_Registry(),
        panel=_panel,
        poll_seconds=0.02,
        align_state_path=state,
    )
    source = second.frames()
    try:
        frames = await _take(source, 3)
    finally:
        await source.aclose()
        second.stop()

    messages = [f for f in frames if f.get("type") == "message"]
    assert [f["text"] for f in messages] == ["停机期间的新消息"], frames


async def test_broken_align_state_falls_back_to_baseline(tmp_path: Path) -> None:
    """状态文件损坏只影响一轮对齐，不能让桥起不来。"""
    state = tmp_path / "align.json"
    state.write_text("{not json", "utf-8")

    poller = PollingLoop(
        _wechat(tmp_path),
        FakeDriver([[("甲", "开场")]]),
        probe=_probe,
        registry=_Registry(),
        panel=_panel,
        poll_seconds=0.02,
        align_state_path=state,
    )
    source = poller.frames()
    try:
        frames = await _take(source, 2)
    finally:
        await source.aclose()
        poller.stop()

    assert [f["type"] for f in frames] == ["health", "health"]
    assert state.exists()
