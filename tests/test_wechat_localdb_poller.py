"""本地消息库接入轮询循环的验收：跳过开窗、失败回落 UIA、门控旁路。

全部注入假驱动 / 假帧源，不碰微信库、不碰 UIA 的真实桌面、不读进程内存。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import dafeiyu.gateway.wechat_bridge.localdb.helper_parsers as helper_parsers
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

class PrivateLocalDb:
    """只暴露一个好友会话的假本地库。"""

    available = True

    def __init__(self) -> None:
        self.messages: dict[str, list[dict[str, Any]]] = {
            "好友甲": [{"nickname": "好友甲", "text": "在么"}]
        }

    def group_names(self) -> list[str]:
        return []

    def friend_names(self) -> list[str]:
        return ["好友甲"]

    def resolve_username(self, name: str) -> str | None:
        return "wxid_f1" if name == "好友甲" else None

    def peer_kind(self, name: str) -> str | None:
        return "friend" if name == "好友甲" else None

    def snapshot(self, name: str) -> dict[str, Any] | None:
        return {"messages": list(self.messages.get(name, []))}


async def test_private_frames_use_private_chat_type(started: Any) -> None:
    driver = FakeDriver()
    localdb = PrivateLocalDb()
    loop = started(driver, localdb, private_enabled=True)
    await loop.start()

    first = await _drain_round(loop)
    assert [f for f in first if f.get("type") == "message"] == []

    localdb.messages["好友甲"].append({"nickname": "好友甲", "text": "睡了吗"})
    second = await _drain_round(loop)

    messages = [f for f in second if f.get("type") == "message"]
    assert [f["text"] for f in messages] == ["睡了吗"]
    assert messages[0]["chat_type"] == "private"
    assert messages[0]["chat_id"] == "wxg:好友甲"
    assert driver.opened == []


async def test_private_disabled_never_polls_friends(started: Any) -> None:
    driver = FakeDriver()
    localdb = PrivateLocalDb()
    loop = started(driver, localdb)
    await loop.start()

    assert await loop._targets() == ()
    assert driver.opened == []


# ------------------------------------- 好友私聊：source -> aligner 信封透传
def test_private_source_snapshot_feeds_aligner(tmp_path: Any) -> None:
    """LocalDbSource(好友) 的 entries 喂给 private aligner：kind 与 media 原样进信封。

    群/好友两类的 localdb 快照解析必须一致：好友同样走 resolve_any ->
    recent(...,50)，媒体元数据只透传 dict，不读字节。
    """
    from dafeiyu.gateway.wechat_bridge.align import SessionAligner
    from dafeiyu.gateway.wechat_bridge.localdb.source import LocalDbSource
    from tests.test_wechat_localdb_repository import (
        FILE_TITLE,
        FRIEND_NAME,
        IMG_PAYLOAD,
        QUOTE_REPLY,
        QUOTED_ORIGINAL,
        build_friend_media_store,
    )

    store, _root = build_friend_media_store(tmp_path)
    source = LocalDbSource(mirror_dir=tmp_path / "mirror")
    source._store = store

    snapshot = source.snapshot(FRIEND_NAME)

    assert snapshot is not None
    assert snapshot["chat_name"] == FRIEND_NAME
    assert [m["kind"] for m in snapshot["messages"]] == [
        "text", "image", "voice", "file", "quote",
    ]
    assert snapshot["messages"][0]["nickname"] == FRIEND_NAME
    assert snapshot["messages"][1]["media"]["kind"] == "image"
    assert snapshot["messages"][1]["media"]["size"] == len(IMG_PAYLOAD)

    aligner = SessionAligner(
        "wxg:friend1",
        FRIEND_NAME,
        account_nickname="小菜鱼",
        chat_type="private",
        clock=lambda: 1.0,
    )
    # 上一轮窗口只有第一条文字：这样本帧里 4 种非文本消息都是「新」的。
    aligner.seed([(FRIEND_NAME, QUOTED_ORIGINAL)])

    result = aligner.poll(snapshot["messages"])

    messages = result.messages
    assert [(m["kind"], m["text"]) for m in messages] == [
        ("image", "[图片]"),
        ("voice", "[语音]"),
        ("file", f"[文件] {FILE_TITLE}"),
        ("quote", QUOTE_REPLY),
    ]
    assert all(m["nickname"] == FRIEND_NAME for m in messages)
    assert all(
        m["chat_type"] == "private" and m["chat_id"] == "wxg:friend1" for m in messages
    )
    assert messages[0]["media"]["kind"] == "image"
    assert messages[0]["media"]["size"] == len(IMG_PAYLOAD)
    assert messages[1]["media"]["kind"] == "voice"
    # 文件消息没有媒体元数据（现有实现的 appmsg/link 映射）；
    # 信封恒有 media 键：无媒体时是空 dict（不是 repository 层的直接省略）。
    assert messages[2]["media"] == {}
    # 引用 bot 的消息：作者 == account_nickname 才算回复机器人。
    assert messages[3]["reply_to_bot"] is True
    assert messages[3]["reply_to_text"] == QUOTED_ORIGINAL


# ------------------------------------- 断档补读：snapshot(limit=...) 透传
def test_source_snapshot_passes_backfill_limit(tmp_path: Any) -> None:
    from dafeiyu.gateway.wechat_bridge.localdb.source import LocalDbSource

    class _Store:
        def __init__(self) -> None:
            self.limits: list[int] = []

        def resolve_any(self, _name: str) -> object:
            return object()

        def recent(self, _ref: object, limit: int = 50) -> list[dict[str, Any]]:
            self.limits.append(limit)
            return []

    source = LocalDbSource(mirror_dir=tmp_path / "mirror")
    store = _Store()
    # 跳过真实的建库/解密：直接换上只记录 limit 的假 store。
    source.__dict__["_store"] = store

    assert source.snapshot("测试群") == {"chat_name": "测试群", "messages": []}
    assert source.snapshot("测试群", limit=200) == {
        "chat_name": "测试群",
        "messages": [],
    }

    assert store.limits == [50, 200]


# ================================ todo 13: 事件桥接（撤回 / 群系统 / 好友申请）
GROUP_NAME_EVENT = "老友群"
EVENT_USERNAME = "111@chatroom"


class EventLocalDb:
    """快照 + 事件行（10000/10002/37）都能读的假本地库；解析在测试里打桩。"""

    available = True

    def __init__(self) -> None:
        self.snapshots: dict[str, list[dict[str, Any]]] = {GROUP_NAME_EVENT: []}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.event_calls: list[tuple[str, tuple[int, ...], int]] = []
        self.event_error: BaseException | None = None

    def group_names(self) -> list[str]:
        return list(self.snapshots)

    def resolve_username(self, name: str) -> str | None:
        return EVENT_USERNAME if name in self.snapshots else None

    def peer_kind(self, name: str) -> str | None:
        return "group" if name in self.snapshots else None

    def self_username(self) -> str:
        return "self_wxid"

    def snapshot(self, name: str, *, limit: int = 50) -> dict[str, Any]:
        return {"chat_name": name, "messages": list(self.snapshots.get(name, []))}

    def event_rows(
        self,
        username: str,
        *,
        types: tuple[int, ...],
        since_local_id: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self.event_calls.append((username, types, since_local_id))
        if self.event_error is not None:
            raise self.event_error
        rows = self.events.get(username, [])
        return [
            dict(row)
            for row in rows
            if row["local_id"] > since_local_id and row["local_type"] in types
        ]


def _fake_group_system(content: str, msg_type: int, room_id: str, self_id: str = "") -> tuple[dict[str, str], ...]:
    if msg_type != 10000 or not room_id.endswith("@chatroom"):
        return ()
    if content == "<join>阿猫</join>":
        return (
            {
                "kind": "join",
                "room_id": room_id,
                "member_id": "wxid_cat",
                "nickname": "阿猫",
                "source": "system_structured",
            },
        )
    if content == "<leave>阿狗</leave>":
        return (
            {
                "kind": "leave",
                "room_id": room_id,
                "member_id": "",
                "nickname": "阿狗",
                "source": "system_text",
            },
        )
    return ()


def _fake_recall(content: str, chat_id: str) -> dict[str, str] | None:
    """内容形如 ``revoke|<session>|<newmsgid>``；故意不按 session 过滤，
    让轮询侧的「不匹配就跳过」逻辑被真实执行。"""
    prefix, sep, tail = content.partition("|")
    if not sep or prefix != "revoke":
        return None
    session, sep2, newmsgid = tail.partition("|")
    if not sep2 or not newmsgid:
        return None
    return {"message_id": newmsgid, "chat_id": session, "notice": ""}


def _fake_friend_request(content: str) -> dict[str, str | int] | None:
    """内容形如 ``req:<opcode>:<nickname>:<verification>``。"""
    parts = content.split(":", 3)
    if len(parts) != 4 or parts[0] != "req":
        return None
    try:
        opcode = int(parts[1])
    except ValueError:
        return None
    return {
        "sender_id": "wxid_new",
        "nickname": parts[2],
        "verification": parts[3],
        "scene": 14,
        "opcode": opcode,
    }


@pytest.fixture
def fake_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper_parsers, "parse_group_system_message", _fake_group_system)
    monkeypatch.setattr(helper_parsers, "parse_recall", _fake_recall)
    monkeypatch.setattr(helper_parsers, "parse_friend_request", _fake_friend_request)


def _event_loop(
    tmp_path: Path,
    localdb: Any,
    **overrides: Any,
) -> PollingLoop:
    defaults: dict[str, Any] = {
        "probe": _probe,
        "registry": _Registry(),
        "localdb": localdb,
        "panel": _all_groups_panel,
        "clock": lambda: 1.0,
        "align_state_path": tmp_path / "align-state.json",
        "event_state_path": tmp_path / "event_state.json",
    }
    defaults.update(overrides)
    return PollingLoop(
        _wechat(tmp_path),
        FakeDriver(),
        **defaults,
    )


def _event_rows() -> list[dict[str, Any]]:
    """撤回（newmsgid=99，与消息 server_id 对上）+ 进群 + 退群 + 畸形行。"""
    return [
        {"local_id": 10, "local_type": 10000, "real_sender_id": 3,
         "create_time": 1, "server_id": 0, "content": "<join>阿猫</join>"},
        {"local_id": 11, "local_type": 10002, "real_sender_id": 3,
         "create_time": 2, "server_id": 0, "content": f"revoke|{EVENT_USERNAME}|99"},
        {"local_id": 12, "local_type": 10000, "real_sender_id": 3,
         "create_time": 3, "server_id": 0, "content": "<leave>阿狗</leave>"},
    ]


def _fmessage_rows() -> list[dict[str, Any]]:
    """一条 opcode=2 的申请 + 一条其他 opcode + 一条解析不出来的垃圾行。"""
    return [
        {"local_id": 20, "local_type": 37, "real_sender_id": 1,
         "create_time": 4, "server_id": 0, "content": "req:2:阿新:我是阿新"},
        {"local_id": 21, "local_type": 37, "real_sender_id": 1,
         "create_time": 5, "server_id": 0, "content": "req:3:阿回执:系统"},
        {"local_id": 22, "local_type": 37, "real_sender_id": 1,
         "create_time": 6, "server_id": 0, "content": "not-a-request"},
    ]


def _events_of(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in frames if f.get("type") in ("notice", "recall")]


async def test_events_emitted_once_and_persisted(
    tmp_path: Path, fake_parsers: None
) -> None:
    """第一轮建基线；第二轮撤回 + 进/退群 + 好友申请；第三轮与重启后都不再发。"""
    db = EventLocalDb()
    state_path = tmp_path / "event_state.json"
    loop = _event_loop(tmp_path, db, event_state_path=state_path)
    await loop.start()
    try:
        # 对齐器需要非空基线：首轮快照里放一条「开场」，事件行仍为空。
        db.snapshots[GROUP_NAME_EVENT].append(
            {"nickname": "甲", "text": "开场", "server_id": 0}
        )
        # 轮 1：基线 —— 没有任何事件行，只建水位（0）。
        first = await _drain_round(loop)
        assert _events_of(first) == []
        assert (EVENT_USERNAME, (10000, 10002), 0) in db.event_calls
        assert ("fmessage", (37,), 0) in db.event_calls

        # 轮 2：消息（server_id=99）+ 三类事件帧，撤回要关联到消息帧的 message_id。
        db.snapshots[GROUP_NAME_EVENT].append(
            {"nickname": "乙", "text": "要被撤回的", "server_id": 99}
        )
        db.events[EVENT_USERNAME] = _event_rows()
        db.events["fmessage"] = _fmessage_rows()
        second = await _drain_round(loop)
        messages = [f for f in second if f.get("type") == "message"]
        assert len(messages) == 1
        message_id = messages[0]["message_id"]
        assert messages[0]["server_id"] == 99

        events = _events_of(second)
        assert [f["type"] for f in events] == [
            "notice", "recall", "notice", "notice",
        ]
        increase, recall, decrease, request = events
        assert increase["kind"] == "group_increase"
        assert increase["chat_id"] == f"wxg:{GROUP_NAME_EVENT}"
        assert increase["user_id"] == "wxid_cat"
        assert increase["nickname"] == "阿猫"
        assert increase["event_id"] == "wechat:sys:10"
        assert recall["message_id"] == message_id, "撤回应通过 server_id=99 关联"
        assert recall["chat_id"] == f"wxg:{GROUP_NAME_EVENT}"
        assert recall["user_id"] == ""
        assert decrease["kind"] == "group_decrease"
        assert decrease["event_id"] == "wechat:sys:12"
        assert request["kind"] == "friend_request"
        assert request["chat_id"] == "wxg:fmessage"
        assert request["nickname"] == "阿新"
        assert request["comment"] == "我是阿新"
        assert request["event_id"] == "fmessage:20"

        # 轮 2 结束后水位落盘：schema + server_id 映射。
        assert state_path.exists()
        state = json.loads(state_path.read_text("utf-8"))
        assert state["version"] == 1
        assert state["watermarks"][EVENT_USERNAME] == 12
        assert state["watermarks"]["fmessage"] == 22
        assert state["server_ids"] == {"99": message_id}

        # 轮 3：没有新行 → 一个事件帧都不发（水位去重）。
        third = await _drain_round(loop)
        assert _events_of(third) == []
    finally:
        await asyncio.to_thread(loop.stop)

    # 重启：从落盘的水位继续，老事件不重放。
    fresh = _event_loop(tmp_path, db, event_state_path=state_path)
    assert fresh._event_watermarks[EVENT_USERNAME] == 12
    assert fresh._event_server_ids == {"99": message_id}
    await fresh.start()
    try:
        again = await _drain_round(fresh)
        assert _events_of(again) == []
        assert fresh._event_watermarks[EVENT_USERNAME] == 12
    finally:
        await asyncio.to_thread(fresh.stop)


async def test_baseline_observation_emits_nothing(
    tmp_path: Path, fake_parsers: None
) -> None:
    """首次观察（水位尚未建立）只建水位：积压事件不补发。"""
    db = EventLocalDb()
    db.events[EVENT_USERNAME] = _event_rows()
    db.events["fmessage"] = _fmessage_rows()
    loop = _event_loop(tmp_path, db)
    await loop.start()
    try:
        first = await _drain_round(loop)
        assert _events_of(first) == []
        assert loop._event_watermarks[EVENT_USERNAME] == 12
        assert loop._event_watermarks["fmessage"] == 22
    finally:
        await asyncio.to_thread(loop.stop)


async def test_event_rows_failure_is_skipped_not_fatal(
    tmp_path: Path, fake_parsers: None
) -> None:
    """event_rows 抛错：该会话跳过本轮事件，轮询本身不炸。"""
    db = EventLocalDb()
    db.event_error = RuntimeError("镜像被占用")
    loop = _event_loop(tmp_path, db)
    await loop.start()
    try:
        frames = await asyncio.wait_for(_drain_round(loop), 5.0)
        assert _events_of(frames) == []
        assert any(f["type"] == "health" for f in frames)
    finally:
        await asyncio.to_thread(loop.stop)


async def test_malformed_and_foreign_rows_advance_watermark_silently(
    tmp_path: Path, fake_parsers: None
) -> None:
    """畸形系统消息 / 别的会话的撤回 / 未映射撤回：跳过或兜底，水位照常推进。"""
    db = EventLocalDb()
    loop = _event_loop(tmp_path, db)
    await loop.start()
    try:
        # 轮 1 建基线。
        db.events[EVENT_USERNAME] = []
        await _drain_round(loop)

        db.events[EVENT_USERNAME] = [
            {"local_id": 30, "local_type": 10000, "real_sender_id": 3,
             "create_time": 1, "server_id": 0, "content": "<broken"},
            {"local_id": 31, "local_type": 10002, "real_sender_id": 3,
             "create_time": 2, "server_id": 0, "content": "revoke|999@chatroom|5"},
            {"local_id": 32, "local_type": 10002, "real_sender_id": 3,
             "create_time": 3, "server_id": 0, "content": f"revoke|{EVENT_USERNAME}|777"},
        ]
        second = await _drain_round(loop)
        recalls = [f for f in second if f.get("type") == "recall"]
        assert [f["message_id"] for f in recalls] == ["wechat:777"], (
            "别的会话的撤回要跳过；未映射的撤回退回合成 id",
        )
        assert all(f.get("kind") != "group_increase" for f in _events_of(second))
        # 水位推过整批（含被跳过的行），下一轮不重放。
        assert loop._event_watermarks[EVENT_USERNAME] == 32
        third = await _drain_round(loop)
        assert _events_of(third) == []
    finally:
        await asyncio.to_thread(loop.stop)


def test_server_id_map_evicts_oldest_beyond_limit(tmp_path: Path) -> None:
    """server_id 映射 5000 条 FIFO 上限：最旧的先被淘汰。"""
    loop = _event_loop(tmp_path, EventLocalDb())
    for sid in range(6000):
        loop._remember_server_id(sid, f"m{sid}")

    assert len(loop._event_server_ids) == 5000
    assert "0" not in loop._event_server_ids
    assert "999" not in loop._event_server_ids
    assert loop._event_server_ids["5999"] == "m5999"


def test_corrupt_event_state_starts_empty(tmp_path: Path) -> None:
    """损坏的事件水位文件：debug 记一条、按空状态启动、绝不抛错。"""
    path = tmp_path / "event_state.json"
    path.write_text("{not json", encoding="utf-8")

    loop = _event_loop(tmp_path, EventLocalDb(), event_state_path=path)

    assert loop._event_watermarks == {}
    assert loop._event_server_ids == {}


def test_store_recent_still_drops_event_rows(tmp_path: Path) -> None:
    """回归：事件行（10000/10002/37）仍然不进 store.recent() 的对齐序列。"""
    from tests.test_wechat_localdb_repository import (
        GROUP_NAME,
        build_event_store,
    )

    store = build_event_store(tmp_path)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None

    entries = store.recent(ref)

    assert all(entry.get("server_id") != 7002 for entry in entries)
    assert all(entry.get("server_id") != 7003 for entry in entries)
    assert all(entry.get("server_id") != 7004 for entry in entries)
