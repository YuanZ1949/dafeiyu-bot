"""引用回复：首片带 reply 段；适配器不支持或拒收时静默退回纯文本。"""

from __future__ import annotations

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
    _build_persona,
)
from dafeiyu.gateway.base import GatewayError
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
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

    async def fake_send_chunk(chat_type, chat_id, chunk, *, quote_id=None):
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

    async def fake_send_chunk(chat_type, chat_id, chunk, *, quote_id=None):
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

    async def fake_send_chunk(chat_type, chat_id, chunk, *, quote_id=None):
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
