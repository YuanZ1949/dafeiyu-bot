"""QQ notice → Service 管线：注册、分发与「旧适配器」兜底。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.gateway.qq import QQAdapter
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message, NoticeEvent
from dafeiyu.router import Router
from dafeiyu.service import Service

QQ_CHAT = "qq:1001"
SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)

NoticeHandler = Callable[[NoticeEvent], Awaitable[None]]


def make_settings() -> Settings:
    """与 tests/test_service.py 同一份显式配置，避免依赖真实时钟与环境变量。"""
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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


def noon_clock() -> datetime:
    return NOON


class _AdapterBase:
    """最小适配器：只提供 Service 装配/停机需要的接口，发送全部计数。"""

    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.started = False
        self.stopped = False
        self.message_handler: Callable[[Message], Awaitable[None]] | None = None

    def on_message(self, handler: Callable[[Message], Awaitable[None]]) -> None:
        self.message_handler = handler

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"


class NoticeAdapter(_AdapterBase):
    """显式暴露 ``on_notice`` 的假适配器：记录 handler 并可把事件推进管线。"""

    def __init__(self) -> None:
        super().__init__()
        self.notice_handlers: list[NoticeHandler] = []

    def on_notice(self, handler: NoticeHandler) -> None:
        self.notice_handlers.append(handler)

    async def emit_notice(self, event: NoticeEvent) -> None:
        assert self.notice_handlers, "notice handler 未注册"
        for handler in self.notice_handlers:
            await handler(event)


class LegacyAdapter(_AdapterBase):
    """没有 ``on_notice`` 的旧适配器：装配时不得报错。"""


def build_service(tmp_path: Path, adapter: Any) -> tuple[Service, Store]:
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    router = Router(settings, store, clock=noon_clock)
    service = Service(settings, store, None, {"qq": adapter}, router, "test")
    return service, store


def group_ban() -> NoticeEvent:
    return NoticeEvent(
        kind="group_ban",
        chat_type=ChatType.GROUP,
        chat_id=QQ_CHAT,
        user_id="qq:2002",
        operator_id="qq:3003",
        duration=60,
    )


async def test_start_registers_notice_handler(tmp_path: Path) -> None:
    adapter = NoticeAdapter()
    service, _store = build_service(tmp_path, adapter)

    await service.start()
    try:
        assert len(adapter.notice_handlers) == 1
        assert callable(adapter.notice_handlers[0])
        assert adapter.notice_handlers[0] == service.handle_notice
    finally:
        await service.stop()


async def test_group_ban_through_pipeline_sends_nothing(tmp_path: Path) -> None:
    adapter = NoticeAdapter()
    service, _store = build_service(tmp_path, adapter)

    await service.start()
    try:
        adapter.sent.clear()
        await adapter.emit_notice(group_ban())
        await asyncio.sleep(0)
        assert adapter.sent == []
    finally:
        await service.stop()


async def test_handle_notice_routes_poke_and_join_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = NoticeAdapter()
    service, _store = build_service(tmp_path, adapter)
    pokes: list[NoticeEvent] = []
    joins: list[NoticeEvent] = []

    async def fake_poke(event: NoticeEvent) -> None:
        pokes.append(event)

    async def fake_join(event: NoticeEvent) -> None:
        joins.append(event)

    monkeypatch.setattr(service, "_handle_poke", fake_poke)
    monkeypatch.setattr(service, "_handle_join_welcome", fake_join)

    await service.start()
    try:
        await adapter.emit_notice(
            NoticeEvent(
                kind="group_poke",
                chat_type=ChatType.GROUP,
                chat_id=QQ_CHAT,
                user_id="qq:2002",  # 被戳者
                operator_id="qq:3003",  # 戳的人
            )
        )
        await adapter.emit_notice(
            NoticeEvent(
                kind="group_increase",
                chat_type=ChatType.GROUP,
                chat_id=QQ_CHAT,
                user_id="qq:4004",
            )
        )
    finally:
        await service.stop()

    assert len(pokes) == 1
    assert pokes[0].user_id == "qq:2002"
    assert pokes[0].operator_id == "qq:3003"
    assert len(joins) == 1
    assert joins[0].user_id == "qq:4004"
    assert adapter.sent == []


async def test_adapter_without_on_notice_does_not_break_start(tmp_path: Path) -> None:
    adapter = LegacyAdapter()
    assert not hasattr(adapter, "on_notice")
    service, _store = build_service(tmp_path, adapter)

    await service.start()
    try:
        assert adapter.started is True
    finally:
        await service.stop()


async def test_qq_adapter_schedules_notice_task_and_cleans_up() -> None:
    """真实 QQAdapter：注册 handler 后 notice 走后台任务，并在完成后移出集合。"""
    adapter = QQAdapter("127.0.0.1", 0, "", 999)
    seen: list[NoticeEvent] = []

    async def handler(event: NoticeEvent) -> None:
        seen.append(event)

    adapter.on_notice(handler)
    await adapter._handle_frame(  # pyright: ignore[reportPrivateUsage]
        json.dumps(
            {
                "post_type": "notice",
                "notice_type": "group_ban",
                "group_id": 1001,
                "user_id": 2002,
                "operator_id": 3003,
                "duration": 60,
            }
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)

    assert [event.kind for event in seen] == ["group_ban"]
    assert adapter._tasks == set()  # pyright: ignore[reportPrivateUsage]
