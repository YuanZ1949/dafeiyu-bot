import asyncio
import json

import pytest
import websockets
from websockets.exceptions import InvalidStatus

from dafeiyu.gateway.base import GatewayError
from dafeiyu.gateway.qq import QQAdapter
from dafeiyu.models import ChatType

GROUP_EVENT = {
    "post_type": "message",
    "message_type": "group",
    "group_id": 1,
    "user_id": 2,
    "message_id": 100,
    "raw_message": "hi",
    "sender": {"nickname": "阿明", "card": ""},
    "message": [{"type": "text", "data": {"text": "hi"}}],
    "time": 0,
    "self_id": 10000,
}

PRIVATE_EVENT = {
    "post_type": "message",
    "message_type": "private",
    "user_id": 2,
    "message_id": 101,
    "sender": {"nickname": "阿明"},
    "message": "hello",
    "time": 0,
    "self_id": 10000,
}


def make_adapter(**kwargs) -> QQAdapter:
    return QQAdapter(host="127.0.0.1", port=0, token="secret", bot_qq=10000, **kwargs)


async def test_event_reaches_handler_and_send_emits_action() -> None:
    adapter = make_adapter()
    received = []
    done = asyncio.get_running_loop().create_future()

    async def handler(message):
        received.append(message)
        if not done.done():
            done.set_result(message)

    adapter.on_message(handler)
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            await ws.send(json.dumps(GROUP_EVENT))
            message = await asyncio.wait_for(done, 2)
            assert message.chat_type is ChatType.GROUP
            assert message.chat_id == 1
            assert message.content == "hi"

            task = asyncio.create_task(adapter.send(ChatType.GROUP, 1, "hi"))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert raw["action"] == "send_group_msg"
            assert raw["params"] == {"group_id": 1, "message": "hi"}
            assert "echo" in raw

            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 9}, "echo": raw["echo"]}))
            await asyncio.wait_for(task, 2)
            assert 9 in adapter.bot_message_ids
    finally:
        await adapter.stop()


async def test_private_send_uses_send_private_msg() -> None:
    adapter = make_adapter()
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            task = asyncio.create_task(adapter.send(ChatType.PRIVATE, 2, "yo"))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert raw["action"] == "send_private_msg"
            assert raw["params"] == {"user_id": 2, "message": "yo"}
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 10}, "echo": raw["echo"]}))
            await asyncio.wait_for(task, 2)
    finally:
        await adapter.stop()


async def test_bad_token_is_rejected_with_403() -> None:
    adapter = make_adapter()
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        with pytest.raises(InvalidStatus) as exc:
            async with websockets.connect(uri, additional_headers={"Authorization": "Bearer wrong"}):
                pass
        assert exc.value.response.status_code == 403
    finally:
        await adapter.stop()


async def test_empty_token_skips_auth() -> None:
    adapter = QQAdapter(host="127.0.0.1", port=0, token="", bot_qq=10000)
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps(PRIVATE_EVENT))
            await asyncio.sleep(0.05)
        assert adapter.connected
    finally:
        await adapter.stop()


async def test_stop_releases_port() -> None:
    adapter = make_adapter()
    await adapter.start()
    port = adapter.port
    await adapter.stop()
    with pytest.raises(OSError):
        async with websockets.connect(f"ws://127.0.0.1:{port}"):
            pass


async def test_send_timeout_raises_gateway_error() -> None:
    adapter = make_adapter(send_timeout=0.05)
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            task = asyncio.create_task(adapter.send(ChatType.GROUP, 1, "slow"))
            await asyncio.wait_for(ws.recv(), 2)
            with pytest.raises(GatewayError):
                await asyncio.wait_for(task, 2)
    finally:
        await adapter.stop()


async def test_send_without_connection_raises(caplog) -> None:
    adapter = make_adapter()
    with caplog.at_level("WARNING"), pytest.raises(GatewayError):
        await adapter.send(ChatType.GROUP, 1, "hi")
    assert "QQ 网关未连接" in caplog.text
    assert "send_group_msg" in caplog.text


async def test_send_logs_delivery_receipt_with_real_message_id(caplog) -> None:
    """送达留痕：成功时必须留 INFO，带 NapCat 返回的真实 message_id。"""
    adapter = make_adapter()
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            with caplog.at_level("INFO", logger="dafeiyu.gateway.qq"):
                task = asyncio.create_task(adapter.send(ChatType.GROUP, 20001, "hi"))
                raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 2098681382},
                            "echo": raw["echo"],
                        }
                    )
                )
                await asyncio.wait_for(task, 2)
            assert "msg_id=2098681382" in caplog.text
            assert "send_group_msg" in caplog.text
            assert "group/20001" in caplog.text
    finally:
        await adapter.stop()


async def test_send_failure_logs_retcode(caplog) -> None:
    adapter = make_adapter()
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            with caplog.at_level("WARNING", logger="dafeiyu.gateway.qq"):
                task = asyncio.create_task(adapter.send(ChatType.GROUP, 1, "hi"))
                raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
                await ws.send(
                    json.dumps(
                        {"status": "failed", "retcode": 1200, "data": None, "echo": raw["echo"]}
                    )
                )
                with pytest.raises(GatewayError):
                    await asyncio.wait_for(task, 2)
            assert "retcode=1200" in caplog.text
    finally:
        await adapter.stop()


async def test_send_returns_real_message_id() -> None:
    """返回值必须是 NapCat 的真实 message_id —— 上层靠它入库对账。"""
    adapter = make_adapter()
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            task = asyncio.create_task(adapter.send(ChatType.GROUP, 20001, "hi"))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            await ws.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 1388905241},
                        "echo": raw["echo"],
                    }
                )
            )
            assert await asyncio.wait_for(task, 2) == "1388905241"
    finally:
        await adapter.stop()


async def test_send_returns_none_without_message_id() -> None:
    """网关没给 message_id 时返回 None，由调用方决定回退策略。"""
    adapter = make_adapter()
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            task = asyncio.create_task(adapter.send(ChatType.PRIVATE, 2, "yo"))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {}, "echo": raw["echo"]}))
            assert await asyncio.wait_for(task, 2) is None
    finally:
        await adapter.stop()


async def test_reply_to_bot_message_sets_flag() -> None:
    adapter = make_adapter()
    adapter.bot_message_ids.append(9)
    received = []
    done = asyncio.get_running_loop().create_future()

    async def handler(message):
        received.append(message)
        if not done.done():
            done.set_result(message)

    adapter.on_message(handler)
    await adapter.start()
    try:
        event = dict(GROUP_EVENT)
        event["message"] = [{"type": "reply", "data": {"id": "9"}}, {"type": "text", "data": {"text": "在吗"}}]
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            await ws.send(json.dumps(event))
            message = await asyncio.wait_for(done, 2)
            assert message.reply_to_bot is True
            assert message.content == "在吗"
    finally:
        await adapter.stop()
