"""本地消息库接入轮询循环的验收：跳过开窗、失败回落 UIA、门控旁路。

全部注入假驱动 / 假帧源，不碰微信库、不碰 UIA 的真实桌面、不读进程内存。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dafeiyu.config import WeChatConfig
from dafeiyu.gateway.wechat_bridge.driver import ChatHandle
from dafeiyu.gateway.wechat_bridge.poller import PollingLoop

STEP = 5.0
GROUPS = ("老友群", "同事群")


class _Registry:
    def chat_id(self, display_name: str, *, kind: str = "group") -> str:
        return f"wxg:{display_name}"


def _wechat(tmp_path: Any, **overrides: Any) -> WeChatConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 0,
        "account_nickname": "小助手",
        "poll_seconds": 0.01,
        "kill_switch_file": str(tmp_path / "wechat.stop"),
    }
    defaults.update(overrides)
    return WeChatConfig(**defaults)


def _all_groups_panel() -> frozenset[str]:
    """面板空白名单 = 不限制：群范围由本地库枚举。"""
    return frozenset()


def _probe(connected: bool) -> dict[str, Any]:
    return {"v": 1, "type": "health", "connected": bool(connected), "degraded": False}


class FakeDriver:
    """只记录 UIA 侧调用；本地库生效时它应该完全不被触碰。"""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.unread_calls = 0

    def open_chat(self, name: str) -> ChatHandle:
        self.opened.append(name)
        return ChatHandle(name=name, window=None)

    def snapshot(self, handle: ChatHandle) -> dict[str, Any]:
        return {"chat_name": handle.name, "messages": [{"nickname": "甲", "text": "UIA"}]}

    def unread_total(self) -> int:
        self.unread_calls += 1
        return 0

    def unread_sessions(self) -> list[str]:
        return []


class FakeLocalDb:
    def __init__(self, snapshots: dict[str, Any]) -> None:
        self.snapshots = snapshots
        self.calls: list[str] = []
        self.available = True

    def snapshot(self, name: str) -> dict[str, Any] | None:
        self.calls.append(name)
        return self.snapshots.get(name)

    def group_names(self) -> list[str]:
        """本地库能枚举到的群：面板空白名单时这就是轮询范围。"""
        return list(self.snapshots)


def _loop(tmp_path: Any, driver: Any, localdb: Any, **overrides: Any) -> PollingLoop:
    return PollingLoop(
        _wechat(tmp_path, **overrides),
        driver,
        registry=_Registry(),
        probe=_probe,
        localdb=localdb,
        panel=_all_groups_panel,
        clock=lambda: 1.0,
    )


async def _drain_round(loop: PollingLoop) -> list[dict[str, Any]]:
    return [frame async for frame in loop._poll_round(await loop._targets())]


@pytest.fixture
async def started(tmp_path: Any):
    """按真实生命周期启停轮询循环（UIA 线程只跑假驱动，不做桌面操作）。"""

    loops: list[PollingLoop] = []

    def make(driver: Any, localdb: Any, **overrides: Any) -> PollingLoop:
        loop = _loop(tmp_path, driver, localdb, **overrides)
        loops.append(loop)
        return loop

    await _noop()
    yield make
    for loop in loops:
        await asyncio.to_thread(loop.stop)


async def _noop() -> None:
    return None


async def test_localdb_ready_skips_uia_and_emits_new_messages(started: Any) -> None:
    driver = FakeDriver()
    localdb = FakeLocalDb(
        {
            "老友群": {"messages": [{"nickname": "甲", "text": "本地库一"}]},
            "同事群": {"messages": []},
        }
    )
    loop = started(driver, localdb)
    await loop.start()

    first = await _drain_round(loop)
    # 首轮只建立基线（不补发历史），且绝不能开窗读屏。
    assert [f for f in first if f.get("type") == "message"] == []
    assert driver.opened == []
    assert driver.unread_calls == 0
    assert localdb.calls == sorted(GROUPS)

    # 第二轮多一条：应只发出这一条。
    localdb.snapshots["老友群"] = {
        "messages": [
            {"nickname": "甲", "text": "本地库一"},
            {"nickname": "乙", "text": "本地库二"},
        ]
    }
    second = await _drain_round(loop)
    messages = [f for f in second if f.get("type") == "message"]
    assert [f["text"] for f in messages] == ["本地库二"]
    assert driver.opened == []


async def test_localdb_targets_ignore_unread_gate(started: Any) -> None:
    driver = FakeDriver()
    loop = started(driver, FakeLocalDb(dict.fromkeys(GROUPS)))

    targets = await loop._targets()

    assert targets == tuple(sorted(GROUPS))
    assert driver.unread_calls == 0


async def test_localdb_returns_none_falls_back_to_uia(started: Any) -> None:
    driver = FakeDriver()
    loop = started(driver, FakeLocalDb(dict.fromkeys(GROUPS)))
    await loop.start()

    await _drain_round(loop)

    assert driver.opened == sorted(GROUPS)


async def test_localdb_raises_falls_back_to_uia(started: Any) -> None:
    class Broken:
        available = True

        def group_names(self) -> list[str]:
            return list(GROUPS)

        def snapshot(self, name: str) -> Any:
            raise RuntimeError("镜像坏了")

    driver = FakeDriver()
    loop = started(driver, Broken())
    await loop.start()

    await _drain_round(loop)

    assert driver.opened == sorted(GROUPS)


async def test_localdb_not_available_keeps_unread_gate(started: Any) -> None:
    driver = FakeDriver()
    localdb = FakeLocalDb({})
    localdb.available = False
    loop = started(driver, localdb)
    await loop.start()

    targets = await loop._targets()

    # 红点=0 且未到全量兜底 → 本轮不开任何群。
    assert targets == ()
    assert driver.unread_calls == 1
    assert localdb.calls == []


async def test_uses_localdb_tolerates_broken_attr(started: Any) -> None:
    class Weird:
        @property
        def available(self) -> bool:
            raise RuntimeError("boom")

    loop = started(FakeDriver(), Weird())

    assert loop._uses_localdb() is False


async def test_localdb_snapshot_timeout_falls_back(started: Any) -> None:
    class Slow:
        available = True

        def group_names(self) -> list[str]:
            return ["老友群"]

        def snapshot(self, name: str) -> Any:
            import time

            time.sleep(STEP * 2)
            return {"messages": []}

    driver = FakeDriver()
    loop = started(driver, Slow())
    await loop.start()

    await asyncio.wait_for(_drain_round(loop), STEP * 2)

    assert driver.opened == ["老友群"]


async def test_localdb_targets_skip_groups_without_resolvable_username(
    started: Any,
) -> None:
    """枚举到但解析不出 @chatroom 的群不开窗：UIA 兜底只会白等一次超时。"""

    class _Partial(FakeLocalDb):
        def resolve_username(self, name: str) -> str | None:
            return None if name == "垃圾桶" else f"{name}@chatroom"

    driver = FakeDriver()
    localdb = _Partial({"垃圾桶": {"messages": []}, "老友群": {"messages": []}})
    loop = started(driver, localdb)

    assert await loop._targets() == ("老友群",)
    assert driver.opened == []
