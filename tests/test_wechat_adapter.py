"""微信桥线协议契约测试：真 socket 假桥（seam A）。

不 mock WebSocket、不联网：`websockets.asyncio.server.serve` 在 `port=0` 起真服务端，
`WeChatAdapter` 作为真客户端连入，逐条断言信封 <-> 内部模型的映射与发送回执关联。
协议见 `docs/superpowers/specs/2026-11-01-wechat-bridge-protocol.md`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Self

import pytest
from websockets.asyncio.server import serve

import dafeiyu.gateway.wechat as wechat_module
from dafeiyu.config import ConfigError, WeChatConfig
from dafeiyu.gateway.base import GatewayError
from dafeiyu.gateway.wechat import WeChatAdapter
from dafeiyu.models import ChatType, Message, RecallEvent

GROUP_CHAT = "wxg:abc123"
EPOCH = 3


# --------------------------------------------------------------------- 假桥
class FakeBridge:
    """真 websocket 服务端：记录指令帧，按配置回执。"""

    def __init__(self) -> None:
        self.server = None
        self.port = 0
        self.commands: list[dict] = []
        self.ws = None
        self.ready = asyncio.Event()
        self.receipt: dict = {}
        self.receipt_id_override: str | None = None
        self.silent = False

    async def __aenter__(self) -> Self:
        self.server = await serve(self._handle, "127.0.0.1", 0)
        # 端口由内核分配，测试里绝不硬编码。
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, ws) -> None:
        self.ws = ws
        self.ready.set()
        with contextlib.suppress(Exception):
            async for raw in ws:
                data = json.loads(raw)
                self.commands.append(data)
                if self.silent or data.get("action") == "stop":
                    continue
                receipt = {
                    "v": 1,
                    "id": self.receipt_id_override or data["id"],
                    "ok": True,
                    "error_code": None,
                    "delivered": "observed",
                    "message_id": "3:99",
                }
                receipt.update(self.receipt)
                await ws.send(json.dumps(receipt, ensure_ascii=False))

    async def push(self, envelope: dict) -> None:
        await self.ready.wait()
        await self.ws.send(json.dumps(envelope, ensure_ascii=False))

    async def close_from_bridge(self) -> None:
        await self.ready.wait()
        await self.ws.close()


def message_envelope(**overrides: object) -> dict:
    envelope = {
        "v": 1,
        "type": "message",
        "channel": "wechat",
        "chat_type": "group",
        "chat_id": GROUP_CHAT,
        "chat_name": "测试群",
        "user_id": "wxg:user1",
        "nickname": "阿猫",
        "text": "你好",
        "at_bot": False,
        "reply_to_bot": False,
        "reply_to_text": None,
        "ts": 1761888000,
        "message_id": f"{EPOCH}:1",
        "is_bot": False,
        "session_epoch": EPOCH,
    }
    envelope.update(overrides)
    return envelope


def make_config(port: int, **overrides: object) -> WeChatConfig:
    base: dict = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": port,
        "bridge_token": "",
        # 合法配置：空 nickname 会让 start() 抛 ConfigError。
        "account_nickname": "小助手",
        "poll_seconds": 0.2,
        "echo_window_seconds": 5.0,
    }
    base.update(overrides)
    return WeChatConfig(**base)


async def wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件超时")


@contextlib.asynccontextmanager
async def running(bridge: FakeBridge, **overrides: object):
    adapter = WeChatAdapter(make_config(bridge.port, **overrides))
    await adapter.start()
    try:
        await wait_for(lambda: adapter.connected)
        yield adapter
    finally:
        await adapter.stop()


# ---------------------------------------------------------------- 用例
async def test_bot_id_and_handler_assignment() -> None:
    """等价于被删掉的 test_wechat.py 的 3 个断言：可构造、可赋值、有稳定身份。"""
    adapter = WeChatAdapter(make_config(1))
    received: list[Message] = []

    async def handler(message: Message) -> None:
        received.append(message)

    adapter.on_message(handler)

    assert adapter.bot_id == "wxg:self"
    assert adapter.connected is False

    # 没有 socket 也能走公开 seam：handler 确实被登记并调用。
    await adapter.dispatch_envelope(message_envelope())
    assert [m.message_id for m in received] == ["3:1"]


async def test_message_envelope_maps_every_field() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        received: list[Message] = []

        async def on_message(message: Message) -> None:
            received.append(message)

        adapter.on_message(on_message)
        await bridge.push(
            message_envelope(
                text="在吗",
                at_bot=True,
                reply_to_bot=True,
                reply_to_text="上一条",
            )
        )
        await wait_for(lambda: received)

        message = received[0]
        assert message.chat_type is ChatType.GROUP
        assert message.chat_id == GROUP_CHAT
        assert message.user_id == "wxg:user1"
        assert message.nickname == "阿猫"
        assert message.content == "在吗"
        assert message.message_id == "3:1"
        assert message.timestamp == 1761888000
        assert message.at_bot is True
        assert message.reply_to_bot is True
        assert message.reply_to_text == "上一条"


async def test_recall_envelope_becomes_recall_event() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        recalls: list[RecallEvent] = []

        async def on_recall(event: RecallEvent) -> None:
            recalls.append(event)

        adapter.on_recall(on_recall)
        await bridge.push(
            {
                "v": 1,
                "type": "recall",
                "channel": "wechat",
                "chat_type": "group",
                "chat_id": GROUP_CHAT,
                "chat_name": "测试群",
                "user_id": "wxg:user1",
                "nickname": "阿猫",
                "ts": 1761888001,
                "message_id": "3:1",
                "session_epoch": EPOCH,
            }
        )
        await wait_for(lambda: recalls)

        event = recalls[0]
        assert event.chat_type is ChatType.GROUP
        assert event.chat_id == GROUP_CHAT
        assert event.message_id == "3:1"
        assert event.operator_id == "wxg:user1"


async def test_command_frame_carries_id_and_receipt_correlates() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        message_id = await adapter.send(ChatType.GROUP, GROUP_CHAT, "机器人回复")

        assert message_id == "3:99"
        command = bridge.commands[-1]
        assert command["v"] == 1
        assert command["action"] == "send_text"
        assert command["params"] == {"chat_id": GROUP_CHAT, "text": "机器人回复"}
        assert command["id"]  # 回执靠它关联，绝不能缺


async def test_receipt_with_wrong_id_times_out(monkeypatch) -> None:
    """回执 id 对不上就永远等不到：证明关联真的在起作用，不是照着最后一个回执返回。"""
    monkeypatch.setattr(wechat_module, "_SEND_TIMEOUT", 0.3)
    async with FakeBridge() as bridge:
        bridge.receipt_id_override = "not-this-command"
        async with running(bridge) as adapter:
            with pytest.raises(GatewayError, match="超时"):
                await adapter.send(ChatType.GROUP, GROUP_CHAT, "等不到")


async def test_failed_receipt_raises_and_records_no_liveness() -> None:
    async with FakeBridge() as bridge:
        bridge.receipt = {
            "ok": False,
            "error_code": "not_focused",
            "delivered": "failed",
            "message_id": None,
        }
        async with running(bridge) as adapter:
            with pytest.raises(GatewayError, match="not_focused"):
                await adapter.send(ChatType.GROUP, GROUP_CHAT, "发不出去")

            # 失败不是存活的证据：F3 依赖这个字段。
            assert adapter.last_send_at is None
            assert len(adapter._sent_echo) == 0


async def test_typed_receipt_returns_id_and_warns(caplog) -> None:
    async with FakeBridge() as bridge:
        bridge.receipt = {"delivered": "typed"}
        async with running(bridge) as adapter:
            with caplog.at_level("WARNING", logger="dafeiyu.gateway.wechat"):
                message_id = await adapter.send(ChatType.GROUP, GROUP_CHAT, "打下去了")

            assert message_id == "3:99"
            assert "typed" in caplog.text
            assert adapter.last_send_at is not None


async def test_is_bot_event_is_dropped() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        received: list[Message] = []

        async def on_message(message: Message) -> None:
            received.append(message)

        adapter.on_message(on_message)
        await bridge.push(message_envelope(is_bot=True))
        # 给它足够的机会误分发
        await asyncio.sleep(0.15)

        assert received == []
        assert adapter.last_event_at is None


async def test_echo_of_sent_text_is_dropped_and_resync_clears() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        received: list[Message] = []

        async def on_message(message: Message) -> None:
            received.append(message)

        adapter.on_message(on_message)
        await adapter.send(ChatType.GROUP, GROUP_CHAT, "机器人回复")

        # 桥漏标 is_bot 时，同 chat + 同文本 + 窗口内也不许二次入库。
        await bridge.push(
            message_envelope(text="机器人回复", message_id=f"{EPOCH}:2")
        )
        await asyncio.sleep(0.15)
        assert received == []

        # 同文本、不同 chat 不是回声。
        await bridge.push(
            message_envelope(
                text="机器人回复", message_id=f"{EPOCH}:3", chat_id="wxg:other"
            )
        )
        await wait_for(lambda: received)
        assert received[0].chat_id == "wxg:other"

        # resync 只清回声表，不回溯补发。
        await bridge.push({"v": 1, "type": "resync", "reason": "reconnect"})
        await asyncio.sleep(0.1)
        assert len(received) == 1

        await bridge.push(
            message_envelope(text="机器人回复", message_id=f"{EPOCH}:4")
        )
        await wait_for(lambda: len(received) == 2)
        assert received[1].message_id == f"{EPOCH}:4"


async def test_health_connected_false_reaches_adapter_and_blocks_send() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        await bridge.push(
            {
                "v": 1,
                "type": "health",
                "connected": False,
                "logged_in": True,
                "desktop": "interactive",
                "queue_depth": 0,
            }
        )
        await wait_for(lambda: adapter.connected is False)

        frames_before = len(bridge.commands)
        with pytest.raises(GatewayError, match="未连接"):
            await adapter.send(ChatType.GROUP, GROUP_CHAT, "不该发出")
        assert len(bridge.commands) == frames_before


async def test_stale_health_expires_connected() -> None:
    async with FakeBridge() as bridge, running(bridge, poll_seconds=0.1) as adapter:
        assert adapter.connected is True
        # 新鲜度预算 3 × 0.1 = 0.3s；期间没有任何健康帧。
        await asyncio.sleep(0.5)
        assert adapter.connected is False
        with pytest.raises(GatewayError, match="未连接"):
            await adapter.send(ChatType.GROUP, GROUP_CHAT, "过期不发")


async def test_stop_sends_stop_action_and_closes_socket() -> None:
    async with FakeBridge() as bridge:
        adapter = WeChatAdapter(make_config(bridge.port))
        await adapter.start()
        await wait_for(lambda: adapter.connected)

        await adapter.stop()

        assert bridge.commands[-1]["action"] == "stop"
        assert bridge.commands[-1]["params"] == {}
        assert adapter.connected is False
        assert adapter._ws is None


async def test_start_rejects_incomplete_wechat_config() -> None:
    cases = [
        (WeChatConfig(enabled=True), "account_nickname"),
    ]
    for config, key in cases:
        with pytest.raises(ConfigError, match=key):
            await WeChatAdapter(config).start()


# ------------------------------------------------------ 消息类型 v2（todo 3）
async def test_message_kind_image_builds_placeholder_attachment() -> None:
    envelope = message_envelope(text="[图片]", kind="image")
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert message.kind == "image"
    assert message.content == "[图片]"
    assert len(message.attachments) == 1
    assert message.attachments[0].kind == "image"
    assert message.attachments[0].name == "[图片]"


async def test_message_kind_voice_maps_attachment_name() -> None:
    envelope = message_envelope(text="[语音]", kind="voice")
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert message.kind == "voice"
    assert len(message.attachments) == 1
    assert message.attachments[0].name == "[语音]"


async def test_message_unknown_kind_falls_back_to_text() -> None:
    envelope = message_envelope(kind="999")
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert message.kind == "text"
    assert message.attachments == []


async def test_message_without_kind_key_matches_legacy_behaviour() -> None:
    envelope = message_envelope()
    assert "kind" not in envelope
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert message.kind == "text"
    assert message.attachments == []


async def test_message_non_text_kind_with_empty_text_has_no_placeholder() -> None:
    envelope = message_envelope(text="", kind="image")
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert message.kind == "image"
    assert message.attachments == []


@pytest.mark.parametrize("raw_kind", [None, [], "  ", 7])
async def test_message_kind_normalizes_without_raising(raw_kind: object) -> None:
    envelope = message_envelope(kind=raw_kind)
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert message.kind == "text"


async def test_message_kind_uppercase_and_spaces_normalize_to_known_kind() -> None:
    envelope = message_envelope(text="[图片]", kind=" Image ")
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert message.kind == "image"
    assert len(message.attachments) == 1
    assert message.attachments[0].kind == "image"


# ---- todo 8：send_at / send_media -------------------------------------------


async def test_send_at_sends_one_frame_and_records_echo() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        message_id = await adapter.send_at(
            ChatType.GROUP, GROUP_CHAT, "你好", ["阿强", "小明"]
        )

        assert message_id == "3:99"
        assert len(bridge.commands) == 1
        frame = bridge.commands[0]
        assert frame["action"] == "send_at"
        assert frame["params"] == {
            "chat_id": GROUP_CHAT,
            "text": "你好",
            "at": ["阿强", "小明"],
        }
        assert list(adapter._sent_echo)[-1][:2] == (GROUP_CHAT, "你好")


async def test_send_at_empty_at_degrades_to_send_text() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        await adapter.send_at(ChatType.GROUP, GROUP_CHAT, "你好", [])

        assert len(bridge.commands) == 1
        assert bridge.commands[0]["action"] == "send_text"
        assert bridge.commands[0]["params"] == {"chat_id": GROUP_CHAT, "text": "你好"}


async def test_send_media_unsupported_returns_false_without_echo() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        bridge.receipt = {
            "ok": False,
            "error_code": "unsupported_media",
            "delivered": "failed",
        }

        ok = await adapter.send_media(
            ChatType.GROUP, GROUP_CHAT, "image", "C:/tmp/a.png"
        )

        assert ok is False
        assert len(bridge.commands) == 1
        assert bridge.commands[0]["action"] == "send_media"
        assert len(adapter._sent_echo) == 0
        assert adapter.last_send_at is None


async def test_send_media_other_failure_raises() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        bridge.receipt = {
            "ok": False,
            "error_code": "internal_error",
            "delivered": "failed",
        }

        with pytest.raises(GatewayError):
            await adapter.send_media(
                ChatType.GROUP, GROUP_CHAT, "image", "C:/tmp/a.png"
            )


# ---- todo 12: 本地库媒体 -> Attachment.path --------------------------------
async def test_message_media_dict_becomes_attachment_with_path() -> None:
    envelope = message_envelope(
        text="[图片]",
        kind="image",
        media={
            "kind": "image",
            "path": "C:/wx/media/a.dat",
            "name": "a.png",
            "size": 123,
            "duration": 0,
        },
    )
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert message.kind == "image"
    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.kind == "image"
    assert attachment.path == "C:/wx/media/a.dat"
    assert attachment.name == "a.png"
    assert attachment.size == 123
    assert attachment.duration == 0


async def test_message_media_without_path_keeps_placeholder_attachment() -> None:
    envelope = message_envelope(text="[图片]", kind="image", media={"name": "a.png"})
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert len(message.attachments) == 1
    assert message.attachments[0].path == ""
    assert message.attachments[0].name == "[图片]"


async def test_message_media_non_dict_is_ignored() -> None:
    envelope = message_envelope(text="[图片]", kind="image", media="oops")
    message = WeChatAdapter(make_config(1))._parse_message(envelope)

    assert message is not None
    assert len(message.attachments) == 1
    assert message.attachments[0].path == ""
    assert message.attachments[0].name == "[图片]"


# ---- 死锁回归：消息处理转后台任务，收帧循环永不被 handler 阻塞 ----
async def test_handler_send_returns_while_still_pending() -> None:
    """handler 里 await send 必须能等到回执：收帧循环不能被 handler 卡死。

    2026-11-05 事故：dispatch 内联 await handler → 唯一读循环被 send 的
    回执等待占住 → 回执永远读不进来 → 150s 超时 → 重试 → 用户收到重复回复。
    """
    async with FakeBridge() as bridge, running(bridge) as adapter:
        handler_done = asyncio.Event()
        send_results: list[str | None] = []

        async def on_message(message: Message) -> None:
            message_id = await adapter.send(
                ChatType.PRIVATE, "wxg:private1", "x"
            )
            send_results.append(message_id)
            handler_done.set()

        adapter.on_message(on_message)
        await bridge.push(
            message_envelope(chat_type="private", chat_id="wxg:private1")
        )

        await asyncio.wait_for(handler_done.wait(), timeout=2.0)
        assert send_results == ["3:99"]


async def test_same_chat_messages_run_in_arrival_order() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        order: list[str] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def on_message(message: Message) -> None:
            order.append(message.message_id)
            if message.message_id == f"{EPOCH}:1":
                first_started.set()
                await release_first.wait()

        adapter.on_message(on_message)
        await bridge.push(message_envelope())
        await first_started.wait()

        # 同 chat 第二条不许在第一条挂着时插队。
        await bridge.push(message_envelope(message_id=f"{EPOCH}:2"))
        await asyncio.sleep(0.1)
        assert order == [f"{EPOCH}:1"]

        release_first.set()
        await wait_for(lambda: order == [f"{EPOCH}:1", f"{EPOCH}:2"])


async def test_different_chats_not_serialized_behind_each_other() -> None:
    async with FakeBridge() as bridge, running(bridge) as adapter:
        processed: list[str] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def on_message(message: Message) -> None:
            if message.chat_id == GROUP_CHAT:
                first_started.set()
                await release_first.wait()
            processed.append(message.chat_id)

        adapter.on_message(on_message)
        await bridge.push(message_envelope())
        await first_started.wait()

        # 第一条还在挂起：别的 chat 不许被它串行堵住。
        await bridge.push(
            message_envelope(chat_id="wxg:second", message_id=f"{EPOCH}:2")
        )
        await wait_for(lambda: "wxg:second" in processed)
        assert GROUP_CHAT not in processed

        release_first.set()
        await wait_for(lambda: GROUP_CHAT in processed)


async def test_stop_cancels_pending_inbound_tasks() -> None:
    async with FakeBridge() as bridge:
        adapter = WeChatAdapter(make_config(bridge.port))
        await adapter.start()
        await wait_for(lambda: adapter.connected)

        started = asyncio.Event()
        held = asyncio.Event()

        async def on_message(message: Message) -> None:
            started.set()
            await held.wait()

        adapter.on_message(on_message)
        await bridge.push(message_envelope())
        await started.wait()
        assert adapter._inbound_tasks

        await asyncio.wait_for(adapter.stop(), timeout=2.0)

        assert adapter._inbound_tasks == set()


async def test_transient_disconnect_lets_handler_finish_and_reconnect() -> None:
    """桥重启抖动不许变成静默丢工作：handler 要收场并进服务重试队列。

    2026-11-05 事故修复的补丁修正：断线 teardown 只 fail 在途回执，
    绝不取消后台任务；取消只属于 stop()（彻底停机）。
    """
    async with FakeBridge() as bridge:
        adapter = WeChatAdapter(make_config(bridge.port))
        await adapter.start()
        await wait_for(lambda: adapter.connected)

        outcomes: list[str] = []
        first_started = asyncio.Event()

        async def on_message(message: Message) -> None:
            if message.message_id == f"{EPOCH}:1":
                first_started.set()
                try:
                    await adapter.send(ChatType.PRIVATE, "wxg:private1", "x")
                    outcomes.append("send_ok")
                except GatewayError:
                    # send 失败由服务层捕获并入重试队列；CancelledError 反而
                    # 意味着 handler 被掐死，回复会静默丢失。
                    outcomes.append("gateway_error")
            else:
                outcomes.append(message.message_id)

        adapter.on_message(on_message)
        await bridge.push(message_envelope())
        await first_started.wait()

        # 桥侧断开：此时 handler 正卡在 send 等回执。
        stale_ws = bridge.ws
        await bridge.close_from_bridge()

        await wait_for(lambda: outcomes == ["gateway_error"])

        # 重连后新消息仍被处理，且排在第一个 handler 正常收场之后。
        await wait_for(lambda: adapter.connected, timeout=5.0)
        await wait_for(lambda: bridge.ws is not stale_ws, timeout=5.0)
        await bridge.push(message_envelope(message_id=f"{EPOCH}:2"))
        await wait_for(lambda: outcomes == ["gateway_error", f"{EPOCH}:2"])

        await adapter.stop()
        assert adapter._inbound_tasks == set()
