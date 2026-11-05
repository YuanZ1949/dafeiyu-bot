"""待发队列：发送失败的分片入队、通道恢复后补发、补发成功写回历史。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dafeiyu.brain.client import BrainReply
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service

SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 3, 12, 0, tzinfo=SHANGHAI)
CHAT = "qq:1001"


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
        persona=PersonaConfig("溟月", ("大肥鱼",), 120, 300, 0.05),
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({CHAT})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


class FlakyAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.fail = False
        self.connected = True

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        if self.fail:
            raise RuntimeError("微信桥已断开")
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def make_msg() -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=CHAT,
        user_id="qq:2002",
        nickname="阿鱼",
        content="@溟月 查一下今天的汇率",
        message_id="m1",
        segments=[],
        is_bot=False,
        at_bot=True,
        reply_to_bot=False,
        timestamp=0,
    )


def build(tmp_path: Path):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    adapter = FlakyAdapter()
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(
        settings,
        store,
        object(),
        {"qq": adapter},
        router,
        "你是溟月",
    )
    return service, store, adapter


async def test_failed_reply_is_queued_instead_of_dropped(tmp_path: Path) -> None:
    service, store, adapter = build(tmp_path)
    adapter.fail = True

    sent = await service._send_batch_reply(
        [make_msg()], BrainReply(True, "答案在这里。", "test", 0, 0),
        ChatType.GROUP, CHAT,
    )

    assert sent == []
    assert store.has_outbox(ChatType.GROUP, CHAT)
    rows = store.due_outbox(now=time.time() + 1)
    assert rows and rows[0]["text"] == "答案在这里。"


async def test_flush_replays_after_channel_recovers(tmp_path: Path) -> None:
    service, store, adapter = build(tmp_path)
    adapter.fail = True
    await service._send_batch_reply(
        [make_msg()], BrainReply(True, "答案在这里。", "test", 0, 0),
        ChatType.GROUP, CHAT,
    )
    queued = [row["text"] for row in store.due_outbox(now=time.time() + 1)]
    assert queued

    adapter.fail = False
    flushed = await service._flush_outbox()

    assert flushed == len(queued)
    assert store.outbox_size() == 0
    assert [text for _, _, text in adapter.sent] == queued
    bots = [
        m
        for m in store.recent_messages(ChatType.GROUP, CHAT, 10)
        if m.is_bot
    ]
    assert [m.content for m in bots] == queued


async def test_flush_keeps_queue_while_channel_is_down(tmp_path: Path) -> None:
    service, store, adapter = build(tmp_path)
    adapter.fail = True
    await service._send_batch_reply(
        [make_msg()], BrainReply(True, "答案在这里。", "test", 0, 0),
        ChatType.GROUP, CHAT,
    )
    before = store.outbox_size()

    adapter.connected = False
    assert await service._flush_outbox() == 0
    assert store.outbox_size() == before
    assert store.due_outbox(now=time.time() + 1), "通道没恢复时不能被清空"


async def test_outbox_store_defers_and_orders(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "o.db"))
    store.init_schema()

    first = store.enqueue_outbox(ChatType.GROUP, CHAT, "一", now=100.0)
    second = store.enqueue_outbox(ChatType.GROUP, CHAT, "二", now=100.0)
    assert (first, second) != (0, 0)

    store.defer_outbox(first, error="boom", next_attempt_at=200.0)
    assert [row["id"] for row in store.due_outbox(now=150.0)] == [second]
    assert [row["id"] for row in store.due_outbox(now=250.0)] == [first, second]

    store.drop_outbox(first)
    assert [row["id"] for row in store.due_outbox(now=250.0)] == [second]
    assert store.outbox_size() == 1


async def test_blank_text_is_not_queued(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "b.db"))
    store.init_schema()
    assert store.enqueue_outbox(ChatType.GROUP, CHAT, "   ") == 0
    assert store.outbox_size() == 0


# ------------------------------------------------- sending deduplication fallback
class DedupeRecordingAdapter:
    """声明 supports_dedupe_key 的记录替身：只为断言 send 收到的 dedupe_key。"""

    bot_id = "qq:999"
    supports_dedupe_key = True

    def __init__(self) -> None:
        self.fail = False
        self.connected = True
        self._handler: Callable[[Message], Awaitable[None]] | None = None
        self.sent: list[tuple[str, str]] = []
        self.sent_keys: list[str] = []

    async def send(
        self, chat_type: ChatType, chat_id: str, text: str, **kwargs: object
    ) -> str:
        key = kwargs.get("dedupe_key")
        self.sent_keys.append(key if isinstance(key, str) else "")
        if self.fail:
            raise RuntimeError("微信桥已断开")
        self.sent.append((chat_id, text))
        return f"bot-{len(self.sent)}"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.connected = False

    def on_message(
        self, handler: Callable[[Message], Awaitable[None]]
    ) -> None:  # pragma: no cover - 本测试不消费入站
        self._handler = handler

def build_with_adapter(
    tmp_path: Path, store: Store, adapter: DedupeRecordingAdapter
) -> Service:
    settings = make_settings()
    router = Router(settings, store, clock=lambda: NOON)
    return Service(
        settings,
        store,
        object(),
        {"qq": adapter},
        router,
        "你是溟月",
    )


async def test_due_outbox_roundtrips_dedupe_key(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "dk.db"))
    store.init_schema()
    store.enqueue_outbox(
        ChatType.GROUP, CHAT, "补发的分片", dedupe_key="reply:qq:1001:m1:2", now=100.0
    )
    rows = store.due_outbox(now=200.0)
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == "reply:qq:1001:m1:2"
    assert rows[0]["text"] == "补发的分片"


async def test_first_send_failure_enqueues_and_flush_reuses_dedupe_key(
    tmp_path: Path,
) -> None:
    """首发失败入队后，补发必须带着与首发同一逻辑分片同一个 dedupe_key。"""
    store = Store(str(tmp_path / "reuse.db"))
    store.init_schema()
    adapter = DedupeRecordingAdapter()
    service = build_with_adapter(tmp_path, store, adapter)

    adapter.fail = True
    sent = await service._send_batch_reply(
        [make_msg()],
        BrainReply(True, "答案在这里。", "test", 0, 0),
        ChatType.GROUP,
        CHAT,
    )
    assert sent == []
    rows = store.due_outbox(now=time.time() + 1)
    expected_key = f"reply:{CHAT}:m1:0"
    assert rows and rows[0]["dedupe_key"] == expected_key

    adapter.fail = False
    assert await service._flush_outbox() == 1
    # 首次失败的那一次调用也被记录：首发与补发是同一个 key。
    assert adapter.sent_keys == [expected_key, expected_key]
    assert adapter.sent == [(CHAT, "答案在这里。")]


async def test_flush_without_dedupe_key_still_sends_plain(tmp_path: Path) -> None:
    """没有 key 的行照旧补发；替身收不到 dedupe_key kwarg。"""
    store = Store(str(tmp_path / "plain.db"))
    store.init_schema()
    adapter = DedupeRecordingAdapter()
    service = build_with_adapter(tmp_path, store, adapter)
    store.enqueue_outbox(ChatType.GROUP, CHAT, "无 key 行", now=time.time())

    assert await service._flush_outbox() == 1
    assert adapter.sent_keys == [""]
