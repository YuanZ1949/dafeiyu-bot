"""B4：印象抽取只对群聊生效——私聊既不调模型，也不写 impressions/usage。"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service
from dafeiyu.workers.impressions import ImpressionWorker

GROUP_ID = "qq:1001"
PRIVATE_ID = "qq:2002"
EVERY = 3
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone(timedelta(hours=8)))
EXTRACTED = json.dumps(
    [{"user_id": "qq:123", "kind": "nickname", "content": "外号叫鱼片", "confidence": 0.8}]
)


class FakeAdapter:
    bot_id = "qq:999"

    def on_message(self, handler) -> None:
        self.handler = handler

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        return "bot-1"


class FakeBrain:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system: str, messages: list[dict], max_tokens: int = 400) -> str:
        self.calls += 1
        return EXTRACTED


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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, EVERY),
        logging=LoggingConfig("INFO", "logs"),
    )


@pytest.fixture
def store(tmp_path: Path):
    s = Store(str(tmp_path / "scope.db"))
    s.init_schema()
    yield s
    s.close()


def make_service(store: Store, brain: FakeBrain) -> Service:
    settings = make_settings()
    router = Router(settings, store, clock=lambda: NOW)
    worker = ImpressionWorker(store, brain, every=EVERY)
    return Service(
        settings,
        store,
        brain,
        {"qq": FakeAdapter()},
        router,
        "你是溟月",
        impressions=worker,
    )


def make_msg(i: int, chat_type: ChatType, chat_id: str) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id="qq:123",
        nickname="鱼片",
        content=f"消息 {i}",
        message_id=f"m{i}",
        segments=[],
        is_bot=False,
        timestamp=int(NOW.timestamp()),
    )


async def settle() -> None:
    """等 _schedule_impressions 创建的 asyncio 任务跑完。"""
    for _ in range(5):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


async def trigger(service: Service, store: Store, chat_type: ChatType, chat_id: str) -> None:
    last = None
    for i in range(EVERY):
        last = make_msg(i, chat_type, chat_id)
        store.add_message(last)
    assert last is not None
    service._schedule_impressions(last)
    await settle()


def impression_usage(store: Store, chat_type: ChatType, chat_id: str) -> int:
    calls, _ = store.usage_today_by_purpose(
        chat_type, chat_id, store.today(), ("impression",)
    )
    return calls


async def test_private_chat_does_not_extract_impressions(store: Store) -> None:
    brain = FakeBrain()
    service = make_service(store, brain)

    await trigger(service, store, ChatType.PRIVATE, PRIVATE_ID)

    assert brain.calls == 0, "私聊不应调用模型抽取印象"
    assert store.list_impressions(ChatType.PRIVATE, PRIVATE_ID, topk=10) == []
    assert impression_usage(store, ChatType.PRIVATE, PRIVATE_ID) == 0


async def test_group_chat_still_extracts_impressions(store: Store) -> None:
    brain = FakeBrain()
    service = make_service(store, brain)

    await trigger(service, store, ChatType.GROUP, GROUP_ID)

    assert brain.calls == 1, "群聊达到阈值仍应抽取一次"
    assert impression_usage(store, ChatType.GROUP, GROUP_ID) == 1
    assert [im.content for im in store.list_impressions(ChatType.GROUP, GROUP_ID, topk=10)] == [
        "外号叫鱼片"
    ]
