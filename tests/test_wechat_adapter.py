"""微信桥线协议契约测试：真 socket 假桥（seam A）。

不 mock WebSocket、不联网：`websockets.asyncio.server.serve` 在 `port=0` 起真服务端，
`WeChatAdapter` 作为真客户端连入，逐条断言信封 <-> 内部模型的映射与发送回执关联。
协议见 `docs/superpowers/specs/2026-11-01-wechat-bridge-protocol.md`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

import dafeiyu.gateway.wechat as wechat_module
from dafeiyu.config import ConfigError, WeChatConfig
from dafeiyu.gateway.base import GatewayError
from dafeiyu.gateway.wechat import WeChatAdapter
from dafeiyu.models import ChatType, Message, RecallEvent
from websockets.asyncio.server import serve

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

    async def __aenter__(self) -> FakeBridge:
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
        # 合法配置：空 allowlist / 空 nickname 会让 start() 抛 ConfigError。
        "account_nickname": "小助手",
        "group_allowlist": frozenset({GROUP_CHAT}),
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
    async with FakeBridge() as bridge:
        async with running(bridge, poll_seconds=0.1) as adapter:
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
    for config, key in [
        (
            WeChatConfig(enabled=True, account_nickname="小助手"),
            "group_allowlist",
        ),
        (
            WeChatConfig(
                enabled=True, group_allowlist=frozenset({GROUP_CHAT})
            ),
            "account_nickname",
        ),
    ]:
        with pytest.raises(ConfigError, match=key):
            await WeChatAdapter(config).start()
