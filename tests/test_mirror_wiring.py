"""T4：MirroredAdapter 出站代理的包装、能力保持与观察语义。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pytest

from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    MirrorConfig,
    MirrorPairConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.gateway.base import GatewayError
from dafeiyu.memory.store import Store
from dafeiyu.mirror import MirrorItem, MirrorRouter, MirroredAdapter
from dafeiyu.models import Attachment, ChatType, Message, Segment
from tests.fakes import FakeChannelAdapter

QQ = "qq:1001"
WXG = "wxg:group"


class RecordingRouter:
    """只记录 observe_outbound 调用：不启动 worker、不碰 store。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def observe_outbound(
        self,
        *,
        target_chat_id: str,
        text: str = "",
        message_id: str | None = None,
        attachments: Sequence[Attachment] = (),
        segments: Sequence[Segment] = (),
        kind: str = "text",
        nickname: str = "",
    ) -> None:
        self.calls.append(
            {
                "target_chat_id": target_chat_id,
                "text": text,
                "message_id": message_id,
                "attachments": list(attachments),
                "segments": list(segments),
                "kind": kind,
                "nickname": nickname,
            }
        )


class RecordingSender:
    """可注入返回值/异常的内层文本发送器（含 kwargs 记录）。"""

    def __init__(self, *, result: Any = "m-1", error: Exception | None = None) -> None:
        self.bot_id = "qq:999"
        self.connected = True
        self.degraded = False
        self.result = result
        self.error = error
        self.calls: list[tuple[ChatType, str, str, dict[str, Any]]] = []

    async def send(
        self, chat_type: ChatType, chat_id: str, text: str, **kwargs: Any
    ) -> Any:
        self.calls.append((chat_type, chat_id, text, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class NoneIdAdapter:
    """模拟 UIA 默认后端：发送成功但拿不到 message_id。"""

    def __init__(self) -> None:
        self.bot_id = "wxg:self"
        self.connected = True
        self.degraded = False
        self.sent: list[tuple[ChatType, str, str]] = []

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> None:
        self.sent.append((chat_type, chat_id, text))


class WeChatishAdapter:
    """同时具备 upload_file 与 send_media 的微信形态内层 adapter。"""

    def __init__(
        self,
        *,
        media_ok: bool = True,
        media_error: Exception | None = None,
    ) -> None:
        self.bot_id = "wxg:self"
        self.connected = True
        self.degraded = False
        self.media_ok = media_ok
        self.media_error = media_error
        self.media_calls: list[tuple[ChatType, str, str, str, str]] = []
        self.file_calls: list[tuple[ChatType, str, str, str]] = []

    async def send_media(
        self, chat_type: ChatType, chat_id: str, kind: str, path: str, name: str = ""
    ) -> bool:
        self.media_calls.append((chat_type, chat_id, kind, path, name))
        if self.media_error is not None:
            raise self.media_error
        return self.media_ok

    async def upload_file(
        self, chat_type: ChatType, chat_id: str, path: str, *, name: str = ""
    ) -> None:
        self.file_calls.append((chat_type, chat_id, path, name))


def make_settings() -> Settings:
    return Settings(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        bot_qq=999,
        ws_host="127.0.0.1",
        ws_port=8080,
        ws_token="",
        db_path=":memory:",
        persona=PersonaConfig("溟月", ("溟月",), 120, 300, 0.05),
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        mirror=MirrorConfig(
            enabled=True,
            pairs=(MirrorPairConfig(qq=QQ, wechat=WXG),),
        ),
    )


def make_router(tmp_path: Path) -> MirrorRouter:
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    return MirrorRouter(make_settings(), store, {})


# --------------------------------------------------------------- 文本 send
async def test_proxy_send_mirrors_once_with_returned_id() -> None:
    inner = RecordingSender(result="m-1")
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    result = await proxy.send(ChatType.GROUP, WXG, "你好")

    assert result == "m-1"
    assert inner.calls == [(ChatType.GROUP, WXG, "你好", {})]
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["target_chat_id"] == WXG
    assert call["text"] == "你好"
    assert call["message_id"] == "m-1"
    assert call["attachments"] == []


async def test_proxy_send_none_id_still_mirrors_once() -> None:
    inner = RecordingSender(result=None)
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    result = await proxy.send(ChatType.GROUP, WXG, "在吗")

    assert result is None
    assert len(router.calls) == 1
    assert router.calls[0]["message_id"] is None


async def test_proxy_send_forwards_kwargs_without_leaking_to_observe() -> None:
    inner = RecordingSender(result="m-2")
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    await proxy.send(ChatType.GROUP, WXG, "分片", dedupe_key="dk")

    assert inner.calls[0][3] == {"dedupe_key": "dk"}
    assert "dedupe_key" not in router.calls[0]


# --------------------------------------------------------------- 消息段
async def test_proxy_segments_text_and_image_preserve_text(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    inner = FakeChannelAdapter(segments=True)
    proxy = MirroredAdapter(inner, router)

    await proxy.send_segments(
        ChatType.GROUP,
        QQ,
        [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "http://x/a.png"}},
        ],
    )

    item = router._queues["qq"].get_nowait()
    assert isinstance(item, MirrorItem)
    # T3 的 QQ 渲染：有附件时正文只认 text 段，必须显式回传才不丢字。
    assert router._render(item) == ["看这个 [图片]"]
    assert item.text_lines == ("看这个",)


async def test_proxy_segments_drops_at_and_reply_and_uses_segment_model() -> None:
    inner = FakeChannelAdapter(segments=True)
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    await proxy.send_segments(
        ChatType.GROUP,
        QQ,
        [
            {"type": "text", "data": {"text": "你好"}},
            {"type": "at", "data": {"qq": "123"}},
            {"type": "reply", "data": {"id": "9"}},
            {"type": "image", "data": {"url": "http://x/a.png", "name": "a.png"}},
            {"type": "face", "data": {"id": "1"}},
        ],
    )

    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["text"] == "你好[表情]"
    assert call["kind"] == "text"
    assert [att.kind for att in call["attachments"]] == ["image"]
    assert call["attachments"][0].source == "http://x/a.png"
    assert all(isinstance(seg, Segment) for seg in call["segments"])
    assert list(call["segments"]) == [Segment(type="text", data={"text": "你好[表情]"})]


# --------------------------------------------------------------- 媒体
async def test_proxy_media_true_enqueues_once() -> None:
    inner = FakeChannelAdapter(media=True)
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    result = await proxy.send_media(
        ChatType.GROUP, WXG, "image", "/tmp/a.png", name="a.png"
    )

    assert result is True
    assert inner.media == [(ChatType.GROUP, WXG, "image", "/tmp/a.png", "a.png")]
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["target_chat_id"] == WXG
    assert call["kind"] == "image"
    assert call["attachments"] == [
        Attachment(kind="image", path="/tmp/a.png", name="a.png")
    ]


async def test_proxy_media_false_does_not_enqueue() -> None:
    inner = FakeChannelAdapter(media=True, media_ok=False)
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    result = await proxy.send_media(ChatType.GROUP, WXG, "image", "/tmp/a.png")

    assert result is False
    assert router.calls == []


async def test_proxy_media_upload_file_prefers_send_media() -> None:
    inner = WeChatishAdapter()
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    result = await proxy.upload_file(
        ChatType.GROUP, WXG, "/tmp/doc.pdf", name="doc.pdf"
    )

    assert result is True
    assert inner.media_calls == [
        (ChatType.GROUP, WXG, "file", "/tmp/doc.pdf", "doc.pdf")
    ]
    assert inner.file_calls == []
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["target_chat_id"] == WXG
    assert call["kind"] == "file"
    assert call["attachments"] == [
        Attachment(kind="file", path="/tmp/doc.pdf", name="doc.pdf")
    ]


async def test_proxy_media_upload_file_false_does_not_enqueue() -> None:
    inner = WeChatishAdapter(media_ok=False)
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    result = await proxy.upload_file(ChatType.GROUP, WXG, "/tmp/doc.pdf")

    assert result is False
    assert router.calls == []


# --------------------------------------------------------------- 异常
async def test_proxy_error_send_propagates_without_enqueue() -> None:
    error = GatewayError("boom")
    router = RecordingRouter()
    proxy = MirroredAdapter(RecordingSender(error=error), router)

    with pytest.raises(GatewayError) as excinfo:
        await proxy.send(ChatType.GROUP, QQ, "hi")

    assert excinfo.value is error
    assert router.calls == []


async def test_proxy_error_segments_propagates_without_enqueue() -> None:
    error = GatewayError("boom")
    router = RecordingRouter()
    proxy = MirroredAdapter(
        FakeChannelAdapter(segments=True, segments_error=error), router
    )

    with pytest.raises(GatewayError):
        await proxy.send_segments(
            ChatType.GROUP, QQ, [{"type": "text", "data": {"text": "hi"}}]
        )

    assert router.calls == []


async def test_proxy_error_media_propagates_without_enqueue() -> None:
    error = GatewayError("boom")
    router = RecordingRouter()
    proxy = MirroredAdapter(FakeChannelAdapter(media=True, media_error=error), router)

    with pytest.raises(GatewayError):
        await proxy.send_media(ChatType.GROUP, WXG, "image", "/tmp/a.png")

    assert router.calls == []


async def test_proxy_error_upload_file_propagates_without_enqueue() -> None:
    error = GatewayError("boom")
    router = RecordingRouter()
    proxy = MirroredAdapter(WeChatishAdapter(media_error=error), router)

    with pytest.raises(GatewayError):
        await proxy.upload_file(ChatType.GROUP, WXG, "/tmp/doc.pdf")

    assert router.calls == []


# --------------------------------------------------------------- 能力保持
def test_proxy_capability_only_exposes_inner_methods() -> None:
    inner = FakeChannelAdapter(segments=False, media=False)
    proxy = MirroredAdapter(inner, None)

    for name in (
        "send_segments",
        "send_media",
        "send_at",
        "send_forward",
        "upload_file",
    ):
        assert not hasattr(proxy, name)
    assert callable(getattr(proxy, "send"))


async def test_proxy_capability_attributes_and_lifecycle_delegate() -> None:
    inner = FakeChannelAdapter()
    proxy = MirroredAdapter(inner, None)

    assert proxy.connected is inner.connected
    assert proxy.bot_id == inner.bot_id
    assert proxy.degraded is inner.degraded
    assert proxy.snapshot() == inner.snapshot()
    assert proxy.supports_dedupe_key is inner.supports_dedupe_key

    seen: list[Message] = []

    async def handler(message: Message) -> None:
        seen.append(message)

    proxy.on_message(handler)
    await proxy.start()
    assert inner.started is True

    message = Message(
        chat_type=ChatType.GROUP,
        chat_id=WXG,
        user_id="wxg:user1",
        nickname="阿猫",
        content="hi",
        message_id="m1",
    )
    await inner.emit(message)
    assert seen == [message]

    await proxy.stop()
    assert inner.stopped is True


async def test_proxy_none_id_without_router_still_calls_inner() -> None:
    inner = NoneIdAdapter()
    proxy = MirroredAdapter(inner, None)

    result = await proxy.send(ChatType.GROUP, WXG, "在吗")

    assert result is None
    assert inner.sent == [(ChatType.GROUP, WXG, "在吗")]
