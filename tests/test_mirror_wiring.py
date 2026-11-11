"""T4：MirroredAdapter 出站代理的包装、能力保持与观察语义。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from dafeiyu import __main__ as bot_main
from dafeiyu.brain.client import BrainReply
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
    WorkspaceConfig,
)
from dafeiyu.gateway.base import GatewayError
from dafeiyu.memory.store import Store
from dafeiyu.mirror import MirrorItem, MirrorRouter, MirroredAdapter
from dafeiyu.models import Attachment, ChatType, Message, Segment
from dafeiyu.router import Router
from dafeiyu.service import Service
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


class AtForwardSender:
    """记录 send_at / send_forward 的内层 adapter（QQ 形态）。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.bot_id = "qq:999"
        self.connected = True
        self.degraded = False
        self.error = error
        self.at_calls: list[tuple[ChatType, str, str, list[str]]] = []
        self.forward_calls: list[tuple[ChatType, str, list[dict[str, Any]]]] = []

    async def send_at(
        self,
        chat_type: ChatType,
        chat_id: str,
        text: str,
        at: Sequence[str],
        **kwargs: Any,
    ) -> Any:
        self.at_calls.append((chat_type, chat_id, text, list(at)))
        if self.error is not None:
            raise self.error
        return "m-at"

    async def send_forward(
        self,
        chat_type: ChatType,
        chat_id: str,
        nodes: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        self.forward_calls.append((chat_type, chat_id, list(nodes)))
        if self.error is not None:
            raise self.error
        return "m-fwd"


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

    item = router._queues[WXG].get_nowait()
    assert isinstance(item, MirrorItem)
    # T3 的 QQ 渲染：有附件时正文只认 text 段，必须显式回传才不丢字。
    assert router._render(item) == ["看这个 [图片]"]
    assert item.text_lines == ("看这个",)


async def test_proxy_segments_degrades_at_and_reply_to_text() -> None:
    """引用/@ 跨平台没有对应关系：降级成文本线索，不再整段丢弃。"""
    inner = FakeChannelAdapter(segments=True)
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    await proxy.send_segments(
        ChatType.GROUP,
        QQ,
        [
            {"type": "reply", "data": {"id": "9"}},
            {"type": "at", "data": {"qq": "123"}},
            {"type": "text", "data": {"text": "你好"}},
            {"type": "image", "data": {"url": "http://x/a.png", "name": "a.png"}},
            {"type": "face", "data": {"id": "1"}},
        ],
    )

    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["text"] == "[回复] @群友 你好[表情]"
    assert call["kind"] == "text"
    assert [att.kind for att in call["attachments"]] == ["image"]
    assert call["attachments"][0].source == "http://x/a.png"
    assert all(isinstance(seg, Segment) for seg in call["segments"])
    assert list(call["segments"]) == [
        Segment(type="text", data={"text": "[回复] @群友 你好[表情]"})
    ]


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


# --------------------------------------------------------------- @ / 转发
async def test_proxy_send_at_mirrors_text_only() -> None:
    """@ 关系不跨平台：只镜像正文，at 列表不泄漏给观察者。"""
    inner = AtForwardSender()
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    result = await proxy.send_at(ChatType.GROUP, WXG, "看这个", ["123", "456"])

    assert result == "m-at"
    assert inner.at_calls == [(ChatType.GROUP, WXG, "看这个", ["123", "456"])]
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["target_chat_id"] == WXG
    assert call["text"] == "看这个"
    assert call["attachments"] == []
    assert call["kind"] == "text"


async def test_proxy_send_at_error_propagates_without_enqueue() -> None:
    router = RecordingRouter()
    proxy = MirroredAdapter(AtForwardSender(error=GatewayError("boom")), router)

    with pytest.raises(GatewayError):
        await proxy.send_at(ChatType.GROUP, WXG, "看这个", ["123"])

    assert router.calls == []


async def test_proxy_send_forward_mirrors_node_count() -> None:
    """合并转发不搬节点结构，只回传「N 条」占位并标成 forward。"""
    nodes = [
        {"type": "node", "data": {"name": "a", "content": "hi"}},
        {"type": "node", "data": {"name": "b", "content": "yo"}},
    ]
    inner = AtForwardSender()
    router = RecordingRouter()
    proxy = MirroredAdapter(inner, router)

    result = await proxy.send_forward(ChatType.GROUP, WXG, nodes)

    assert result == "m-fwd"
    assert len(inner.forward_calls) == 1
    assert inner.forward_calls[0][2] == nodes
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["target_chat_id"] == WXG
    assert call["text"] == "[合并转发 2 条]"
    assert call["kind"] == "forward"
    assert call["attachments"] == []


async def test_proxy_send_forward_error_propagates_without_enqueue() -> None:
    router = RecordingRouter()
    proxy = MirroredAdapter(AtForwardSender(error=GatewayError("boom")), router)

    with pytest.raises(GatewayError):
        await proxy.send_forward(ChatType.GROUP, WXG, [{"type": "node"}])

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


# =====================================================================
# T5：Service 入站钩子、生命周期顺序与 __main__ 代理装配
# =====================================================================

SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)


class SilentBrain:
    """最小脑替身：不发言，只需要 Service 构造与停机所需的方法。"""

    async def decide(self, system, messages, max_tokens=400) -> BrainReply:
        return BrainReply(False, "", "test", 0, 0)

    async def aclose(self) -> None:
        pass


class RecordingMirror:
    """只记录 observe_inbound/生命周期；不启动 worker、不碰 store。"""

    def __init__(self) -> None:
        self.inbound: list[str] = []
        self.raise_on_inbound = False
        self.started = 0
        self.stopped = 0
        # Service.__init__ 会把微信 .dat 解码器绑到这里。
        self.decode: Any = None

    def observe_inbound(self, msg: Message) -> None:
        if self.raise_on_inbound:
            raise RuntimeError("mirror boom")
        self.inbound.append(msg.message_id)

    def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


def make_service(
    tmp_path: Path,
    *,
    adapters: dict[str, Any] | None = None,
    mirror: Any = None,
    brain: Any = None,
) -> tuple[Service, Store]:
    store = Store(tmp_path / "service.db")
    store.init_schema()
    settings = make_settings()
    adapters = adapters or {"qq": FakeChannelAdapter(bot_id="qq:999")}
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(
        settings,
        store,
        brain or SilentBrain(),
        adapters=adapters,
        router=router,
        persona_text="test",
        mirror=mirror,
    )
    return service, store


def make_msg(
    message_id: str = "m1",
    content: str = "@溟月 在吗",
    *,
    is_bot: bool = False,
    kind: str = "text",
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=QQ,
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        is_bot=is_bot,
        kind=kind,
    )


def _noop_materializers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_materialize(msg, settings, annotate=True) -> None:
        return None

    async def fake_attachments(msg, settings, decode=None, annotate=True) -> None:
        return None

    monkeypatch.setattr("dafeiyu.service.materialize", fake_materialize)
    monkeypatch.setattr("dafeiyu.service.materialize_attachments", fake_attachments)


class OrderMirror(RecordingMirror):
    """把 observe_inbound 的调用顺序写进共享列表。"""

    def __init__(self, order: list[tuple[str, str]]) -> None:
        super().__init__()
        self._order = order

    def observe_inbound(self, msg: Message) -> None:
        self._order.append(("enqueue", msg.message_id))


async def test_service_handle_enqueues_before_materialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入队必须发生在第一个 await 之前，且人类消息只入队一次。"""
    order: list[tuple[str, str]] = []
    mirror = OrderMirror(order)
    service, store = make_service(tmp_path, mirror=mirror)
    assert service.mirror is mirror
    assert mirror.decode == service._decode_wechat_dat

    async def fake_materialize(msg, settings, annotate=True) -> None:
        order.append(("materialize", msg.message_id))

    async def fake_attachments(msg, settings, decode=None, annotate=True) -> None:
        order.append(("materialize_attachments", msg.message_id))

    monkeypatch.setattr("dafeiyu.service.materialize", fake_materialize)
    monkeypatch.setattr("dafeiyu.service.materialize_attachments", fake_attachments)

    try:
        await service.handle(make_msg("m1"))
    finally:
        store.close()

    assert order[:3] == [
        ("enqueue", "m1"),
        ("materialize", "m1"),
        ("materialize_attachments", "m1"),
    ]
    assert [name for name, _ in order].count("enqueue") == 1


async def test_service_handle_raises_keeps_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """镜像抛错只记 warning，消息照常入库、异常不外泄。"""
    mirror = RecordingMirror()
    mirror.raise_on_inbound = True
    service, store = make_service(tmp_path, mirror=mirror)
    _noop_materializers(monkeypatch)

    try:
        with caplog.at_level(logging.WARNING, logger="dafeiyu.service"):
            await service.handle(make_msg("m1"))
        assert store.message_exists(ChatType.GROUP, QQ, "m1")
    finally:
        store.close()

    assert "镜像入队失败" in caplog.text


async def test_service_handle_ordering_text_image_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """materialize 阻塞时，三条消息仍按到达顺序入队（先入队后 await）。"""
    mirror = RecordingMirror()
    service, store = make_service(tmp_path, mirror=mirror)
    gate = asyncio.Event()

    async def blocking_materialize(msg, settings, annotate=True) -> None:
        await gate.wait()

    async def fake_attachments(msg, settings, decode=None, annotate=True) -> None:
        return None

    monkeypatch.setattr("dafeiyu.service.materialize", blocking_materialize)
    monkeypatch.setattr("dafeiyu.service.materialize_attachments", fake_attachments)

    messages = [
        make_msg("m1", "你好"),
        make_msg("m2", "[图片]", kind="image"),
        make_msg("m3", "再见"),
    ]
    tasks = [asyncio.create_task(service.handle(msg)) for msg in messages]
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        # 三个 handle 都停在 materialize 的 gate 上；此时入队已全部完成。
        assert mirror.inbound == ["m1", "m2", "m3"]
    finally:
        gate.set()
        await asyncio.gather(*tasks)
        store.close()


async def test_service_handle_is_bot_never_enqueues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = RecordingMirror()
    service, store = make_service(tmp_path, mirror=mirror)
    _noop_materializers(monkeypatch)

    try:
        await service.handle(make_msg("b1", is_bot=True))
    finally:
        store.close()

    assert mirror.inbound == []


class LifecycleAdapter(FakeChannelAdapter):
    """在 fake 适配器上补一条 start/stop 事件序列。"""

    def __init__(self, name: str, events: list[str]) -> None:
        super().__init__(bot_id="qq:999" if name == "qq" else "wxg:self")
        # Keepalive 需要一个非 qq:0 的 bot_qq 才会创建后台任务。
        self.bot_qq = "qq:999"
        self._name = name
        self._events = events

    async def start(self) -> None:
        self._events.append(f"start:{self._name}")
        await super().start()

    async def stop(self) -> None:
        self._events.append(f"stop:{self._name}")
        await super().stop()


class LifecycleMirror:
    """记录 start/stop 事件，并在 stop 时断言后台任务已先取消。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.service: Service | None = None
        self.cancelled_flags: tuple[bool, bool, bool] | None = None

    def start(self) -> None:
        self.events.append("mirror_start")

    async def stop(self) -> None:
        assert self.service is not None
        self.cancelled_flags = (
            self.service._outbox_task is None,
            self.service._keepalive_task is None,
            self.service._scheduler_task is None,
        )
        self.events.append("mirror_stop")


async def test_service_lifecycle_start_order_and_stop_order(tmp_path: Path) -> None:
    events: list[str] = []
    qq = LifecycleAdapter("qq", events)
    wechat = LifecycleAdapter("wechat", events)
    mirror = LifecycleMirror(events)
    service, store = make_service(
        tmp_path, adapters={"qq": qq, "wechat": wechat}, mirror=mirror
    )
    mirror.service = service

    try:
        await service.start()
        assert events == ["start:qq", "start:wechat", "mirror_start"]

        await service.stop()
    finally:
        store.close()

    assert events == [
        "start:qq",
        "start:wechat",
        "mirror_start",
        "mirror_stop",
        "stop:qq",
        "stop:wechat",
    ]
    # mirror 停止时 outbox/keepalive/scheduler 的取消已经完成。
    assert mirror.cancelled_flags == (True, True, True)


class CrashingRouter(MirrorRouter):
    """首个迭代就崩溃的 worker，用来验证 done-callback 的可观测性。"""

    async def _worker(self, queue: asyncio.Queue[Any]) -> None:
        raise RuntimeError("worker boom")


async def test_mirror_worker_exit_surfaces_and_stops(tmp_path: Path, caplog) -> None:
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    router = CrashingRouter(make_settings(), store, {})

    try:
        with caplog.at_level(logging.ERROR, logger="dafeiyu.mirror"):
            router.start()
            for _ in range(5):
                await asyncio.sleep(0)
            last_error = router.stats()["last_error"]
            assert isinstance(last_error, str)
            assert last_error.startswith("worker_exit:RuntimeError")
            assert "镜像 worker 异常退出" in caplog.text
            # 已崩溃的 worker 不能让 stop 把异常再抛出来。
            await router.stop()
        assert router._tasks == {}
    finally:
        store.close()


def _patch_main(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    inner_adapters: dict[str, Any],
) -> dict[str, Any]:
    """把 main() 里除装配逻辑外的重对象全部替换成记录替身。"""
    captured: dict[str, Any] = {}

    class ServiceSpy:
        def __init__(
            self,
            settings_arg,
            store_arg,
            brain_arg,
            adapters,
            router,
            persona_text,
            **kwargs,
        ) -> None:
            self.adapters = adapters
            self.brain = brain_arg
            self.mirror = kwargs.get("mirror")
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    async def fake_start_control_server(
        settings_arg, store_arg, service_arg, adapter_arg, **kwargs
    ):
        captured["service"] = service_arg
        captured["control_adapter"] = adapter_arg

        class Runner:
            async def cleanup(self) -> None:
                captured["cleanup"] = True

        return Runner()

    def fake_log_active_channels(settings_arg, adapters_arg, provider_arg) -> None:
        captured["log_adapters"] = adapters_arg

    async def noop(*args, **kwargs) -> None:
        return None

    class StopEvent:
        async def wait(self) -> None:
            raise SystemExit("stop")

    monkeypatch.setattr(bot_main, "load_settings", lambda: settings)
    monkeypatch.setattr(bot_main, "setup_logging", lambda settings_arg: None)
    monkeypatch.setattr(bot_main, "load_persona", lambda path: "persona")
    monkeypatch.setattr(
        bot_main, "current_provider", lambda settings_arg, runtime: "token"
    )
    monkeypatch.setattr(bot_main, "load_runtime", lambda: object())
    monkeypatch.setattr(bot_main, "stop_server", noop)
    monkeypatch.setattr(bot_main, "build_brain", lambda *args, **kwargs: SilentBrain())
    monkeypatch.setattr(
        bot_main, "build_adapters", lambda settings_arg: dict(inner_adapters)
    )
    monkeypatch.setattr(bot_main, "AuditLog", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(bot_main, "Service", ServiceSpy)
    monkeypatch.setattr(
        bot_main, "build_workers", lambda *args, **kwargs: bot_main.Workers()
    )
    for name in (
        "checkpoint_loop",
        "retention_loop",
        "reminder_loop",
        "followup_loop",
        "observer_loop",
        "reflector_loop",
        "outcome_loop",
        "summary_loop",
    ):
        monkeypatch.setattr(bot_main, name, noop)
    monkeypatch.setattr(bot_main, "start_control_server", fake_start_control_server)
    monkeypatch.setattr(bot_main, "log_active_channels", fake_log_active_channels)
    monkeypatch.setattr(asyncio, "Event", StopEvent)
    return captured


async def test_control_build_adapters_wires_proxies_and_inner_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(make_settings(), workspace=WorkspaceConfig(dir=""))
    inner_qq = FakeChannelAdapter(bot_id="qq:999", chat_id=QQ)
    inner_wechat = FakeChannelAdapter()
    captured = _patch_main(
        monkeypatch, settings, {"qq": inner_qq, "wechat": inner_wechat}
    )

    await bot_main.main()

    service = captured["service"]
    router = service.mirror
    assert isinstance(router, MirrorRouter)
    proxy_qq = service.adapters["qq"]

    assert isinstance(proxy_qq, MirroredAdapter)
    # router 只拿 inner，control 口拿同一个代理对象。
    assert router.adapters["qq"] is inner_qq
    assert router.adapters["wechat"] is inner_wechat
    assert not isinstance(router.adapters["qq"], MirroredAdapter)
    assert captured["control_adapter"] is proxy_qq
    assert captured["log_adapters"]["qq"] is proxy_qq
    assert service.started is True
    assert service.stopped is True


async def test_control_mirror_disabled_keeps_inner_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(
        make_settings(),
        mirror=MirrorConfig(enabled=False),
        workspace=WorkspaceConfig(dir=""),
    )
    inner_qq = FakeChannelAdapter(bot_id="qq:999", chat_id=QQ)
    captured = _patch_main(monkeypatch, settings, {"qq": inner_qq})

    await bot_main.main()

    service = captured["service"]
    assert service.mirror is None
    assert service.adapters["qq"] is inner_qq
    assert not isinstance(service.adapters["qq"], MirroredAdapter)
    assert captured["control_adapter"] is inner_qq

