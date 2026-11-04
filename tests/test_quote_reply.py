"""引用回复：首片带 reply 段；适配器不支持或拒收时静默退回纯文本。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from dafeiyu.brain.client import BrainReply
from dafeiyu.config import (
    BudgetConfig,
    ConfigError,
    GroupsConfig,
    InjectionConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
    WorkspaceConfig,
    _build_persona,
)
from dafeiyu.gateway.base import GatewayError
from dafeiyu.memory.store import Store
from dafeiyu.models import Attachment, ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service

GROUP = "qq:1001"


class _SegmentsAdapter:
    bot_id = "qq:999"

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.segments: list[tuple[ChatType, str, list[dict]]] = []
        self._error = error

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_type, chat_id, text))
        return "plain"

    async def send_segments(
        self, chat_type: ChatType, chat_id: str, segments: list[dict]
    ) -> str | None:
        if self._error is not None:
            raise self._error
        self.segments.append((chat_type, chat_id, segments))
        return "quoted"


class _PlainAdapter:
    """没有消息段能力的适配器（微信桥就是这种）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_type, chat_id, text))
        return "plain"


def make_service(adapter):
    service = object.__new__(Service)
    service.adapters = {"qq": adapter}
    return service


async def test_quote_id_sends_reply_segment_first() -> None:
    adapter = _SegmentsAdapter()
    service = make_service(adapter)

    message_id = await service._send_chunk(
        ChatType.GROUP, "qq:1", "在的", quote_id="42"
    )

    assert message_id == "quoted"
    assert adapter.sent == []
    assert adapter.segments == [
        (
            ChatType.GROUP,
            "qq:1",
            [
                {"type": "reply", "data": {"id": "42"}},
                {"type": "text", "data": {"text": "在的"}},
            ],
        )
    ]


async def test_without_quote_id_uses_plain_send() -> None:
    adapter = _SegmentsAdapter()
    service = make_service(adapter)

    message_id = await service._send_chunk(ChatType.GROUP, "qq:1", "哈哈")

    assert message_id == "plain"
    assert adapter.segments == []
    assert adapter.sent == [(ChatType.GROUP, "qq:1", "哈哈")]


async def test_adapter_without_segments_falls_back_to_plain() -> None:
    adapter = _PlainAdapter()
    service = make_service(adapter)

    await service._send_chunk(ChatType.GROUP, "qq:1", "在", quote_id="42")

    assert adapter.sent == [(ChatType.GROUP, "qq:1", "在")]


async def test_quote_failure_falls_back_to_plain() -> None:
    # 老版本 NapCat 不认识 reply 段：引用失败不能把这条回复一起弄丢。
    adapter = _SegmentsAdapter(error=GatewayError("send_group_msg 失败：retcode=100"))
    service = make_service(adapter)

    message_id = await service._send_chunk(
        ChatType.GROUP, "qq:1", "在的", quote_id="42"
    )

    assert message_id == "plain"
    assert adapter.sent == [(ChatType.GROUP, "qq:1", "在的")]


# ---- persona.quote_reply 开关 ---------------------------------------------


def _settings(*, quote_reply: bool) -> Settings:
    return Settings(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        bot_qq=999,
        ws_host="127.0.0.1",
        ws_port=8080,
        ws_token="",
        db_path=":memory:",
        persona=PersonaConfig(
            "溟月", ("大肥鱼",), 120, 300, 0.05, quote_reply=quote_reply
        ),
        triggers=TriggerConfig(
            60, 0.0, ("01:00", "07:00"), ("怎么",), mode="model", batch_seconds=0.0
        ),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({GROUP})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        injection=InjectionConfig(),
    )


def _build_service(tmp_path, *, quote_reply: bool):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = _settings(quote_reply=quote_reply)
    adapter = _SegmentsAdapter()
    router = Router(settings, store)
    service = Service(
        settings,
        store,
        object(),
        {"qq": adapter},
        router,
        "你是溟月",
        injection=InjectionConfig(),
    )
    return service, adapter


def _addressed_msg() -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=GROUP,
        user_id="qq:2002",
        nickname="阿鱼",
        content="@溟月 在吗",
        message_id="m1",
        is_bot=False,
        at_bot=True,
        timestamp=0,
    )


async def test_quote_reply_off_skips_quote_on_batch_path(tmp_path) -> None:
    service, _adapter = _build_service(tmp_path, quote_reply=False)
    seen: list[str | None] = []

    async def fake_send_chunk(chat_type, chat_id, chunk, *, quote_id=None, **_: object):
        seen.append(quote_id)
        return "bot-1"

    service._send_chunk = fake_send_chunk  # type: ignore[method-assign]

    await service._send_batch_reply(
        [_addressed_msg()], BrainReply(True, "在的"), ChatType.GROUP, GROUP
    )

    assert seen == [None]


async def test_quote_reply_on_quotes_first_chunk_on_batch_path(tmp_path) -> None:
    service, _adapter = _build_service(tmp_path, quote_reply=True)
    seen: list[str | None] = []

    async def fake_send_chunk(chat_type, chat_id, chunk, *, quote_id=None, **_: object):
        seen.append(quote_id)
        return "bot-1"

    service._send_chunk = fake_send_chunk  # type: ignore[method-assign]

    await service._send_batch_reply(
        [_addressed_msg()], BrainReply(True, "在的"), ChatType.GROUP, GROUP
    )

    assert seen == ["m1"]


async def test_quote_reply_off_on_single_reply_path(tmp_path, monkeypatch) -> None:
    service, _adapter = _build_service(tmp_path, quote_reply=False)
    seen: list[str | None] = []

    async def fake_ask(msg):
        return BrainReply(True, "在的")

    async def fake_send_chunk(chat_type, chat_id, chunk, *, quote_id=None, **_: object):
        seen.append(quote_id)
        return "bot-1"

    monkeypatch.setattr(service, "_ask_brain", fake_ask)
    monkeypatch.setattr(service, "_send_chunk", fake_send_chunk)

    await service._reply(_addressed_msg())

    assert seen == [None]


def test_quote_reply_config_defaults_true() -> None:
    persona = _build_persona({"aliases": ["大肥鱼"]})
    assert persona.quote_reply is True


def test_quote_reply_config_parses_false() -> None:
    persona = _build_persona({"aliases": ["大肥鱼"], "quote_reply": False})
    assert persona.quote_reply is False


def test_quote_reply_config_rejects_non_bool() -> None:
    with pytest.raises(ConfigError, match="quote_reply"):
        _build_persona({"aliases": ["大肥鱼"], "quote_reply": "yes"})


# ---- todo 6：附件 / @ 与安全降级 --------------------------------------------


class _RichAdapter(_SegmentsAdapter):
    """带文件上传能力的假适配器；计数与异常都可由测试控制。"""

    def __init__(
        self,
        *,
        segment_error: Exception | None = None,
        upload_error: Exception | None = None,
    ) -> None:
        super().__init__(error=segment_error)
        self.uploads: list[tuple[ChatType, str, str, str]] = []
        self._upload_error = upload_error

    async def upload_file(
        self, chat_type: ChatType, chat_id: str, path: str, name: str = ""
    ) -> None:
        if self._upload_error is not None:
            raise self._upload_error
        self.uploads.append((chat_type, chat_id, path, name))


class _AtOnlyAdapter:
    """只有 send_at、没有消息段能力的适配器（微信桥）。"""

    bot_id = "wxg:999"

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.ats: list[tuple[ChatType, str, str, list[str]]] = []
        self._error = error

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_type, chat_id, text))
        return "plain"

    async def send_at(
        self, chat_type: ChatType, chat_id: str, text: str, at: list[str]
    ) -> str | None:
        if self._error is not None:
            raise self._error
        self.ats.append((chat_type, chat_id, text, list(at)))
        return "at-ok"


def _image_attachment(name: str = "a.png") -> Attachment:
    return Attachment(kind="image", path=f"C:/ws/replies/{name}", name=name)


def _rich_service(tmp_path, adapter):
    """真实 Service（store/router/behavior 齐备）+ 可控适配器 + 临时工作区。"""
    service, _ = _build_service(tmp_path, quote_reply=True)
    service.settings = replace(
        service.settings, workspace=WorkspaceConfig(dir=str(tmp_path / "ws"))
    )
    service.adapters["qq"] = adapter
    return service


def _plain_msg(content: str = "今天天气不错") -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=GROUP,
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id="m2",
        is_bot=False,
        timestamp=0,
    )


async def test_image_with_quote_and_at_uses_one_segment_call() -> None:
    adapter = _SegmentsAdapter()
    service = make_service(adapter)

    message_id = await service._send_chunk(
        ChatType.GROUP,
        "qq:1",
        "在的",
        quote_id="42",
        attachments=(_image_attachment(),),
        at_qq_ids=("2002",),
    )

    assert message_id == "quoted"
    assert len(adapter.segments) == 1
    segments = adapter.segments[0][2]
    assert [seg["type"] for seg in segments] == ["reply", "at", "text", "image"]
    assert segments[-1]["data"]["file"] == "C:/ws/replies/a.png"
    assert adapter.sent == []


async def test_image_without_quote_or_at_still_uses_segments() -> None:
    adapter = _SegmentsAdapter()
    service = make_service(adapter)

    await service._send_chunk(
        ChatType.GROUP, "qq:1", "看图", attachments=(_image_attachment(),)
    )

    assert len(adapter.segments) == 1
    assert [seg["type"] for seg in adapter.segments[0][2]] == ["text", "image"]
    assert adapter.sent == []


async def test_segment_failure_with_attachment_falls_back_to_text() -> None:
    adapter = _RichAdapter(segment_error=GatewayError("send_group_msg 失败"))
    service = make_service(adapter)

    message_id = await service._send_chunk(
        ChatType.GROUP, "qq:1", "在的", quote_id="42", attachments=(_image_attachment(),)
    )

    assert message_id == "plain"
    assert adapter.segments == []
    assert adapter.sent == [(ChatType.GROUP, "qq:1", "在的")]


async def test_file_attachment_uploads_before_text() -> None:
    adapter = _RichAdapter()
    service = make_service(adapter)
    file_att = Attachment(kind="file", path="C:/ws/replies/a.pdf", name="")

    await service._send_chunk(
        ChatType.GROUP, "qq:1", "文件在这", attachments=(file_att,)
    )

    assert adapter.uploads == [
        (ChatType.GROUP, "qq:1", "C:/ws/replies/a.pdf", "")
    ]
    assert adapter.segments == []
    assert adapter.sent == [(ChatType.GROUP, "qq:1", "文件在这")]


async def test_upload_failure_still_sends_text() -> None:
    adapter = _RichAdapter(upload_error=GatewayError("upload_group_file 失败"))
    service = make_service(adapter)

    message_id = await service._send_chunk(
        ChatType.GROUP,
        "qq:1",
        "文件在这",
        attachments=(Attachment(kind="file", path="C:/ws/replies/a.pdf"),),
    )

    assert message_id == "plain"
    assert adapter.sent == [(ChatType.GROUP, "qq:1", "文件在这")]


async def test_adapter_with_only_send_at_uses_names() -> None:
    adapter = _AtOnlyAdapter()
    service = make_service(adapter)

    message_id = await service._send_chunk(
        ChatType.GROUP, "qq:1", "你好", at_names=("阿鱼",)
    )

    assert message_id == "at-ok"
    assert adapter.ats == [(ChatType.GROUP, "qq:1", "你好", ["阿鱼"])]
    assert adapter.sent == []


async def test_send_at_failure_falls_back_to_plain() -> None:
    adapter = _AtOnlyAdapter(error=GatewayError("send_at 失败"))
    service = make_service(adapter)

    await service._send_chunk(ChatType.GROUP, "qq:1", "你好", at_names=("阿鱼",))

    assert adapter.sent == [(ChatType.GROUP, "qq:1", "你好")]


async def test_batch_reply_resolves_attachment_and_mention(tmp_path) -> None:
    ws = tmp_path / "ws"
    (ws / "replies").mkdir(parents=True)
    (ws / "replies" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    adapter = _RichAdapter()
    service = _rich_service(tmp_path, adapter)
    reply = BrainReply(
        True,
        "看图",
        attachments=(Attachment(kind="image", path="replies/a.png"),),
        at=("阿鱼",),
    )

    await service._send_batch_reply([_addressed_msg()], reply, ChatType.GROUP, GROUP)

    assert len(adapter.segments) == 1
    segments = adapter.segments[0][2]
    assert [seg["type"] for seg in segments] == ["reply", "at", "text", "image"]
    assert segments[1]["data"]["qq"] == "2002"  # 内部 id 的 qq: 前缀已剥掉
    assert adapter.sent == []


async def test_batch_reply_sticker_becomes_face_segment(tmp_path) -> None:
    adapter = _RichAdapter()
    service = _rich_service(tmp_path, adapter)
    reply = BrainReply(
        True, "哈哈", attachments=(Attachment(kind="sticker", name="178"),)
    )

    await service._send_batch_reply([_plain_msg()], reply, ChatType.GROUP, GROUP)

    assert len(adapter.segments) == 1
    assert [seg["type"] for seg in adapter.segments[0][2]] == ["text", "face"]
    assert adapter.segments[0][2][-1]["data"] == {"id": "178"}
    assert adapter.sent == []
    assert adapter.uploads == []


async def test_batch_reply_sticker_without_name_keeps_text_only(tmp_path) -> None:
    adapter = _RichAdapter()
    service = _rich_service(tmp_path, adapter)
    reply = BrainReply(
        True, "嗯", attachments=(Attachment(kind="sticker", name="  "),)
    )

    await service._send_batch_reply([_plain_msg()], reply, ChatType.GROUP, GROUP)

    assert adapter.segments == []
    assert adapter.sent == [(ChatType.GROUP, GROUP, "嗯")]


async def test_batch_reply_rejected_path_keeps_text_only(tmp_path) -> None:
    adapter = _RichAdapter()
    service = _rich_service(tmp_path, adapter)
    reply = BrainReply(
        True,
        "在的",
        attachments=(Attachment(kind="image", path="../../x.png"),),
    )

    await service._send_batch_reply([_plain_msg()], reply, ChatType.GROUP, GROUP)

    assert adapter.segments == []
    assert adapter.uploads == []
    assert adapter.sent == [(ChatType.GROUP, GROUP, "在的")]


async def test_restriction_drop_sends_and_uploads_nothing(tmp_path, monkeypatch) -> None:
    adapter = _RichAdapter()
    service = _rich_service(tmp_path, adapter)
    resolved: list[int] = []

    def spy_resolve(*_args, **_kwargs):
        resolved.append(1)
        return []

    async def drop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("dafeiyu.service.resolve_attachments", spy_resolve)
    monkeypatch.setattr(service, "_apply_restrictions", drop)
    reply = BrainReply(
        True,
        "禁句",
        attachments=(Attachment(kind="image", path="replies/a.png"),),
        at=("阿鱼",),
    )

    sent = await service._send_batch_reply(
        [_addressed_msg()], reply, ChatType.GROUP, GROUP
    )

    assert sent == []
    assert resolved == []  # 文本被丢弃时连附件解析都不做
    assert adapter.segments == []
    assert adapter.sent == []
    assert adapter.uploads == []


async def test_store_bot_chunks_annotates_first_chunk(tmp_path) -> None:
    adapter = _RichAdapter()
    service = _rich_service(tmp_path, adapter)
    att = Attachment(kind="image", path=str(tmp_path / "ws" / "replies" / "a.png"))

    service._store_bot_chunks(
        _plain_msg(), [("在的", None), ("还有", None)], attachments=(att,)
    )

    stored = service.store.recent_bot_messages_in(ChatType.GROUP, GROUP, 2)
    contents = sorted(message.content for message in stored)
    assert contents == ["在的\n[图片] replies/a.png", "还有"]


async def test_store_bot_chunks_without_attachments_unchanged(tmp_path) -> None:
    adapter = _RichAdapter()
    service = _rich_service(tmp_path, adapter)

    service._store_bot_chunks(_plain_msg(), [("哈哈", None)])

    stored = service.store.recent_bot_messages_in(ChatType.GROUP, GROUP, 1)
    assert stored[0].content == "哈哈"


async def test_single_reply_path_wires_attachments_and_mentions(
    tmp_path, monkeypatch
) -> None:
    ws = tmp_path / "ws"
    (ws / "replies").mkdir(parents=True)
    (ws / "replies" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    adapter = _RichAdapter()
    service = _rich_service(tmp_path, adapter)
    reply = BrainReply(
        True,
        "在的",
        attachments=(Attachment(kind="image", path="replies/a.png"),),
        at=("阿鱼",),
    )

    async def fake_ask(_msg):
        return reply

    monkeypatch.setattr(service, "_ask_brain", fake_ask)
    await service._reply(_addressed_msg())

    assert len(adapter.segments) == 1
    segments = adapter.segments[0][2]
    assert [seg["type"] for seg in segments] == ["reply", "at", "text", "image"]
    stored = service.store.recent_bot_messages_in(ChatType.GROUP, GROUP, 1)
    assert stored[0].content.endswith("[图片] replies/a.png")
