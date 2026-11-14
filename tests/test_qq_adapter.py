import asyncio
import json
from typing import Any, cast

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
            assert message.chat_id == "qq:1"
            assert message.content == "hi"

            task = asyncio.create_task(adapter.send(ChatType.GROUP, "qq:1", "hi"))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert raw["action"] == "send_group_msg"
            assert raw["params"] == {"group_id": 1, "message": "hi"}
            assert "echo" in raw

            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 9}, "echo": raw["echo"]}))
            await asyncio.wait_for(task, 2)
            assert "9" in adapter.bot_message_ids
    finally:
        await adapter.stop()


async def test_private_send_uses_send_private_msg() -> None:
    adapter = make_adapter()
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            task = asyncio.create_task(adapter.send(ChatType.PRIVATE, "qq:2", "yo"))
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
            task = asyncio.create_task(adapter.send(ChatType.GROUP, "qq:1", "slow"))
            await asyncio.wait_for(ws.recv(), 2)
            with pytest.raises(GatewayError):
                await asyncio.wait_for(task, 2)
    finally:
        await adapter.stop()


async def test_send_without_connection_raises(caplog) -> None:
    adapter = make_adapter()
    with caplog.at_level("WARNING"), pytest.raises(GatewayError):
        await adapter.send(ChatType.GROUP, "qq:1", "hi")
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
                task = asyncio.create_task(adapter.send(ChatType.GROUP, "qq:20001", "hi"))
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
                task = asyncio.create_task(adapter.send(ChatType.GROUP, "qq:1", "hi"))
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
            task = asyncio.create_task(adapter.send(ChatType.GROUP, "qq:20001", "hi"))
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
            task = asyncio.create_task(adapter.send(ChatType.PRIVATE, "qq:2", "yo"))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {}, "echo": raw["echo"]}))
            assert await asyncio.wait_for(task, 2) is None
    finally:
        await adapter.stop()


async def test_reply_to_bot_message_sets_flag() -> None:
    adapter = make_adapter()
    adapter.bot_message_ids.append("9")
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


async def test_send_segments_posts_message_array() -> None:
    """引用回复把 message 换成段数组；纯文本 send 的 params 结构不变。"""
    adapter = make_adapter()
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            segments = [
                {"type": "reply", "data": {"id": "42"}},
                {"type": "text", "data": {"text": "在的"}},
            ]
            task = asyncio.create_task(
                adapter.send_segments(ChatType.GROUP, "qq:1", segments)
            )
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert raw["action"] == "send_group_msg"
            assert raw["params"] == {"group_id": 1, "message": segments}
            await ws.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 77},
                        "echo": raw["echo"],
                    }
                )
            )
            assert await asyncio.wait_for(task, 2) == "77"
            assert "77" in adapter.bot_message_ids
    finally:
        await adapter.stop()


async def test_call_action_returns_data() -> None:
    """通用动作调用走同一条 echo 回执通路，只读查询也能用。"""
    adapter = make_adapter()
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            task = asyncio.create_task(
                adapter.call_action("get_group_info", {"group_id": 1})
            )
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert raw["action"] == "get_group_info"
            assert raw["params"] == {"group_id": 1}
            await ws.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"group_name": "我们一起测试吖"},
                        "echo": raw["echo"],
                    }
                )
            )
            data = await asyncio.wait_for(task, 2)
            assert data["group_name"] == "我们一起测试吖"
            # 查询不算发送：健康快照里的 last_send_at 不该被它推进。
            assert adapter.last_send_at is None
    finally:
        await adapter.stop()


async def test_call_action_without_connection_raises() -> None:
    adapter = make_adapter()
    with pytest.raises(GatewayError):
        await adapter.call_action("get_group_info", {"group_id": 1})



# ---- todo 7：文件上传与合并转发 ---------------------------------------------


class _Recorder:
    def __init__(self, *, fail_actions: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail_actions or set()

    async def __call__(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        if action in self._fail:
            raise GatewayError(f"{action} 失败：retcode=1")
        return {"message_id": 88}


async def test_upload_file_group_defaults_name(monkeypatch) -> None:
    adapter = make_adapter()
    recorder = _Recorder()

    async def fake_request(action, params):
        return await recorder(action, params)

    monkeypatch.setattr(adapter, "_request", fake_request)

    await adapter.upload_file(ChatType.GROUP, "qq:123", "C:/tmp/a.pdf")

    assert recorder.calls == [
        (
            "upload_group_file",
            {"group_id": 123, "file": "C:/tmp/a.pdf", "name": "a.pdf"},
        )
    ]


async def test_upload_file_private_and_explicit_name(monkeypatch) -> None:
    adapter = make_adapter()
    recorder = _Recorder()

    async def fake_request(action, params):
        return await recorder(action, params)

    monkeypatch.setattr(adapter, "_request", fake_request)

    await adapter.upload_file(
        ChatType.PRIVATE, "qq:9", "C:/tmp/data.zip", name="报告.zip"
    )

    assert recorder.calls == [
        (
            "upload_private_file",
            {"user_id": 9, "file": "C:/tmp/data.zip", "name": "报告.zip"},
        )
    ]


async def test_upload_file_propagates_gateway_error(monkeypatch) -> None:
    adapter = make_adapter()
    recorder = _Recorder(fail_actions={"upload_group_file"})

    async def fake_request(action, params):
        return await recorder(action, params)

    monkeypatch.setattr(adapter, "_request", fake_request)

    with pytest.raises(GatewayError):
        await adapter.upload_file(ChatType.GROUP, "qq:123", "C:/tmp/a.pdf")


async def test_send_forward_group_records_message_id(monkeypatch) -> None:
    adapter = make_adapter()
    recorder = _Recorder()
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "n",
                "uin": "1",
                "content": [{"type": "text", "data": {"text": "hi"}}],
            },
        }
    ]

    async def fake_request(action, params):
        return await recorder(action, params)

    monkeypatch.setattr(adapter, "_request", fake_request)

    message_id = await adapter.send_forward(ChatType.GROUP, "qq:123", nodes)

    assert message_id == "88"
    assert "88" in adapter.bot_message_ids
    assert recorder.calls == [
        ("send_group_forward_msg", {"group_id": 123, "messages": nodes})
    ]


async def test_send_forward_private_without_message_id(monkeypatch) -> None:
    adapter = make_adapter()

    async def fake_request(action, params):
        return {}

    monkeypatch.setattr(adapter, "_request", fake_request)

    message_id = await adapter.send_forward(ChatType.PRIVATE, "qq:9", [])

    assert message_id is None
    assert "88" not in adapter.bot_message_ids


async def test_service_uploads_file_before_text(monkeypatch) -> None:
    from dafeiyu.models import Attachment
    from dafeiyu.service import Service

    adapter = make_adapter()
    recorder = _Recorder()

    async def fake_request(action, params):
        return await recorder(action, params)

    monkeypatch.setattr(adapter, "_request", fake_request)
    service = object.__new__(Service)
    service.adapters = {"qq": adapter}

    await service._send_chunk(
        ChatType.GROUP,
        "qq:123",
        "文件在这",
        attachments=(Attachment(kind="file", path="C:/tmp/a.pdf"),),
    )

    assert [action for action, _ in recorder.calls] == [
        "upload_group_file",
        "send_group_msg",
    ]
    assert recorder.calls[1][1]["message"] == "文件在这"


async def test_service_survives_upload_failure(monkeypatch) -> None:
    from dafeiyu.models import Attachment
    from dafeiyu.service import Service

    adapter = make_adapter()
    recorder = _Recorder(fail_actions={"upload_group_file"})

    async def fake_request(action, params):
        return await recorder(action, params)

    monkeypatch.setattr(adapter, "_request", fake_request)
    service = object.__new__(Service)
    service.adapters = {"qq": adapter}

    message_id = await service._send_chunk(
        ChatType.GROUP,
        "qq:123",
        "文件在这",
        attachments=(Attachment(kind="file", path="C:/tmp/a.pdf"),),
    )

    assert message_id == "88"
    assert [action for action, _ in recorder.calls] == [
        "upload_group_file",
        "send_group_msg",
    ]


async def test_set_msg_emoji_like_sends_bare_message_id(monkeypatch) -> None:
    adapter = make_adapter()
    recorder = _Recorder()

    async def fake_request(action, params):
        return await recorder(action, params)

    monkeypatch.setattr(adapter, "_request", fake_request)

    ok = await adapter.set_msg_emoji_like(ChatType.GROUP, "qq:123", "qq:88", "128")

    assert ok is True
    assert recorder.calls == [
        ("set_msg_emoji_like", {"message_id": 88, "emoji_id": "128"})
    ]


async def test_set_msg_emoji_like_failure_is_swallowed(monkeypatch) -> None:
    adapter = make_adapter()
    recorder = _Recorder(fail_actions={"set_msg_emoji_like"})

    async def fake_request(action, params):
        return await recorder(action, params)

    monkeypatch.setattr(adapter, "_request", fake_request)

    assert await adapter.set_msg_emoji_like(ChatType.GROUP, "qq:123", "88", "128") is False


async def test_set_msg_emoji_like_skips_empty_ids() -> None:
    adapter = make_adapter()

    assert await adapter.set_msg_emoji_like(ChatType.GROUP, "qq:123", "", "128") is False
    assert await adapter.set_msg_emoji_like(ChatType.GROUP, "qq:123", "88", "") is False


async def test_service_sends_forward_and_skips_empty_text(monkeypatch) -> None:
    from dafeiyu.models import Attachment
    from dafeiyu.service import Service

    adapter = make_adapter()
    recorder = _Recorder()
    texts: list[str] = []

    async def fake_request(action, params):
        return await recorder(action, params)

    async def fake_send(chat_type, chat_id, text):
        texts.append(text)
        return "99"

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(adapter, "send", fake_send)
    service = object.__new__(Service)
    service.adapters = {"qq": adapter}
    item = Attachment(kind="forward", nodes=({"name": "阿明", "content": "原话"},))

    message_id = await service._send_chunk(
        ChatType.GROUP, "qq:123", "", attachments=(item,)
    )

    assert message_id == "88"
    assert recorder.calls[0][0] == "send_group_forward_msg"
    assert recorder.calls[0][1]["group_id"] == 123
    assert recorder.calls[0][1]["messages"] == [
        {
            "type": "node",
            "data": {
                "name": "阿明",
                "content": [{"type": "text", "data": {"text": "原话"}}],
            },
        }
    ]
    # 只有转发时不再补一条空文本。
    assert texts == []


async def test_service_degrades_forward_failure_to_text(monkeypatch) -> None:
    from dafeiyu.models import Attachment
    from dafeiyu.service import Service

    adapter = make_adapter()
    recorder = _Recorder(fail_actions={"send_group_forward_msg"})

    async def fake_request(action, params):
        return await recorder(action, params)

    monkeypatch.setattr(adapter, "_request", fake_request)
    service = object.__new__(Service)
    service.adapters = {"qq": adapter}
    item = Attachment(kind="forward", nodes=({"name": "阿明", "content": "原话"},))

    await service._send_chunk(ChatType.GROUP, "qq:123", "", attachments=(item,))

    # 转发失败后退化成一行占位文本，仍然走普通发送。
    assert [action for action, _ in recorder.calls] == [
        "send_group_forward_msg",
        "send_group_msg",
    ]
    assert "合并转发 1 条" in recorder.calls[1][1]["message"]


async def test_maybe_emoji_reaction_respects_config(monkeypatch) -> None:
    from types import SimpleNamespace

    from dafeiyu.service import Service

    adapter = make_adapter()
    calls: list[tuple[object, ...]] = []

    async def fake_react(chat_type, chat_id, message_id, emoji_id):
        calls.append((chat_type, chat_id, message_id, emoji_id))
        return True

    monkeypatch.setattr(adapter, "set_msg_emoji_like", fake_react)
    service = object.__new__(Service)
    service.adapters = {"qq": adapter}
    service.settings = cast(
        Any,
        SimpleNamespace(
            mind=SimpleNamespace(emoji_reactions=True, emoji_reaction_id="128")
        ),
    )

    await service._maybe_emoji_reaction(ChatType.GROUP, "qq:123", "88")
    assert calls == [(ChatType.GROUP, "qq:123", "88", "128")]

    service.settings = cast(
        Any,
        SimpleNamespace(
            mind=SimpleNamespace(emoji_reactions=False, emoji_reaction_id="128")
        ),
    )
    await service._maybe_emoji_reaction(ChatType.GROUP, "qq:123", "88")
    assert calls == [(ChatType.GROUP, "qq:123", "88", "128")]


# --- 只带 id 的合并转发：主动拉取节点 ---

FORWARD_EVENT = {
    "post_type": "message",
    "message_type": "group",
    "group_id": 1,
    "user_id": 2,
    "message_id": 100,
    "sender": {"nickname": "阿明", "card": ""},
    "message": [{"type": "forward", "data": {"id": "fwd-1"}}],
    "time": 0,
    "self_id": 10000,
}

FORWARD_MESSAGES = [
    {"sender": {"nickname": "甲"}, "time": 0, "message": [{"type": "text", "data": {"text": "第一句"}}]},
    {"sender": {"nickname": "乙"}, "time": 0, "message": [{"type": "text", "data": {"text": "第二句"}}]},
]


async def test_forward_id_only_is_fetched_and_dispatched() -> None:
    """只带 id 的合并转发：先发 get_forward_msg，再把节点渲染进 content。"""
    adapter = make_adapter()
    adapter.bot_message_ids.append("9")
    done = asyncio.get_running_loop().create_future()

    async def handler(message):
        if not done.done():
            done.set_result(message)

    adapter.on_message(handler)
    await adapter.start()
    try:
        event = dict(FORWARD_EVENT)
        event["message"] = [
            {"type": "reply", "data": {"id": "9"}},
            {"type": "forward", "data": {"id": "fwd-1"}},
            {"type": "text", "data": {"text": "看这个"}},
        ]
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            await ws.send(json.dumps(event))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert raw["action"] == "get_forward_msg"
            assert raw["params"] == {"id": "fwd-1"}
            await ws.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"messages": FORWARD_MESSAGES},
                        "echo": raw["echo"],
                    }
                )
            )
            message = await asyncio.wait_for(done, 2)
            assert "[聊天记录]" in message.content
            assert "甲: 第一句" in message.content
            assert "乙: 第二句" in message.content
            assert "看这个" in message.content
            # 既有字段不受补拉影响。
            assert message.reply_to_bot is True
            assert message.reply_to_message_id == "9"
    finally:
        await adapter.stop()


async def test_forward_fetch_error_keeps_placeholder() -> None:
    """get_forward_msg 返回 retcode 错误：handler 仍被调用一次，内容保持占位。"""
    adapter = make_adapter()
    done = asyncio.get_running_loop().create_future()
    calls: list[object] = []

    async def handler(message):
        calls.append(message)
        if not done.done():
            done.set_result(message)

    adapter.on_message(handler)
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            await ws.send(json.dumps(FORWARD_EVENT))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert raw["action"] == "get_forward_msg"
            await ws.send(
                json.dumps(
                    {
                        "status": "failed",
                        "retcode": 1200,
                        "data": None,
                        "echo": raw["echo"],
                    }
                )
            )
            message = await asyncio.wait_for(done, 2)
            assert message.content == "[合并转发]"
            assert len(calls) == 1
    finally:
        await adapter.stop()


async def test_forward_fetch_timeout_keeps_placeholder() -> None:
    """拉取超时：只降级成占位，handler 仍被调用。"""
    adapter = make_adapter(send_timeout=0.05)
    done = asyncio.get_running_loop().create_future()

    async def handler(message):
        if not done.done():
            done.set_result(message)

    adapter.on_message(handler)
    await adapter.start()
    try:
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            await ws.send(json.dumps(FORWARD_EVENT))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert raw["action"] == "get_forward_msg"
            # 故意不回包，等 send_timeout 到期。
            message = await asyncio.wait_for(done, 2)
            assert message.content == "[合并转发]"
    finally:
        await adapter.stop()


async def test_forward_with_content_is_not_fetched() -> None:
    """带内联节点的转发直接渲染，不触发额外请求。"""
    adapter = make_adapter()
    done = asyncio.get_running_loop().create_future()

    async def handler(message):
        if not done.done():
            done.set_result(message)

    adapter.on_message(handler)
    await adapter.start()
    try:
        event: dict[str, Any] = dict(FORWARD_EVENT)
        event["message"] = [
            {
                "type": "forward",
                "data": {"content": FORWARD_MESSAGES},
            }
        ]
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            await ws.send(json.dumps(event))
            message = await asyncio.wait_for(done, 2)
            assert "甲: 第一句" in message.content
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.2)
    finally:
        await adapter.stop()


async def test_nested_id_only_forward_is_not_fetched_recursively() -> None:
    """拉回来的节点里再有 only-id 转发时只保留占位，不再发请求。"""
    adapter = make_adapter()
    done = asyncio.get_running_loop().create_future()

    async def handler(message):
        if not done.done():
            done.set_result(message)

    adapter.on_message(handler)
    await adapter.start()
    try:
        nested = [
            {
                "sender": {"nickname": "甲"},
                "time": 0,
                "message": [{"type": "forward", "data": {"id": "inner"}}],
            }
        ]
        uri = f"ws://127.0.0.1:{adapter.port}"
        async with websockets.connect(uri, additional_headers={"Authorization": "Bearer secret"}) as ws:
            await ws.send(json.dumps(FORWARD_EVENT))
            raw = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert raw["params"] == {"id": "fwd-1"}
            await ws.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"messages": nested},
                        "echo": raw["echo"],
                    }
                )
            )
            message = await asyncio.wait_for(done, 2)
            assert "[聊天记录]" in message.content
            assert "甲: [合并转发]" in message.content
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.2)
    finally:
        await adapter.stop()

async def _until(predicate, message: str, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.01)


async def test_old_connection_disconnect_keeps_new_connection_send_alive() -> None:
    """旧 handler 晚退出时只失败本代请求：新连接上的 send 不能被误杀。"""
    adapter = make_adapter()
    await adapter.start()
    uri = f"ws://127.0.0.1:{adapter.port}"
    headers = {"Authorization": "Bearer secret"}
    try:
        async with websockets.connect(uri, additional_headers=headers) as old:
            old_task = asyncio.create_task(
                adapter.send(ChatType.GROUP, "qq:1", "旧连接")
            )
            old_echo = json.loads(await asyncio.wait_for(old.recv(), 2))["echo"]

            async with websockets.connect(uri, additional_headers=headers) as new:
                await _until(lambda: adapter._generation >= 2, "新连接未被接管")
                new_task = asyncio.create_task(
                    adapter.send(ChatType.GROUP, "qq:1", "新连接")
                )
                new_echo = json.loads(await asyncio.wait_for(new.recv(), 2))["echo"]

                await old.close()
                await asyncio.sleep(0.05)

                # 旧连接上的请求必须失败，不能悬挂到超时。
                with pytest.raises(GatewayError):
                    await asyncio.wait_for(old_task, 2)
                # 新连接上的请求不受旧 handler 影响。
                assert not new_task.done()

                await new.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 77},
                            "echo": new_echo,
                        }
                    )
                )
                assert await asyncio.wait_for(new_task, 2) == "77"
                assert "77" in adapter.bot_message_ids
    finally:
        await adapter.stop()
