"""跨群查证缓存接线：查证成功落库、下一个群命中注入、私聊/隐私不落库。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dafeiyu.brain.client import BrainReply
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    InjectionConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import SHARED_FACT_HEADER, Service

SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)
GROUP_A = "wxg:aaaa"
GROUP_B = "wxg:bbbb"
PRIVATE = "wx:2002"


class FakeAdapter:
    bot_id = "wx:self"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def texts(self) -> list[str]:
        return [text for _, _, text in self.sent]


class FakeBrain:
    def __init__(self, *, text: str = "是《原神》的宣传曲", fire_tool: bool = True) -> None:
        self.text = text
        self.fire_tool = fire_tool
        self.prompts: list[str] = []

    async def reply(self, chat_type, chat_id, text: str, *, on_tool=None) -> BrainReply:
        self.prompts.append(text)
        if on_tool is not None and self.fire_tool:
            await on_tool("websearch")
        return BrainReply(True, self.text, "test", 10, 5)

    def reset_session(self, chat_type, chat_id) -> None:
        pass

    async def aclose(self) -> None:
        pass


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
        triggers=TriggerConfig(
            60, 0.0, ("01:00", "07:00"), ("怎么",), mode="model", batch_seconds=0.0
        ),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


def make_msg(
    content: str,
    *,
    message_id: str,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = GROUP_A,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id="wx:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=True,
        timestamp=0,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(str(tmp_path / "facts.db"))
    s.init_schema()
    yield s
    s.close()


def build(store: Store, brain: FakeBrain) -> tuple[Service, FakeAdapter]:
    adapter = FakeAdapter()
    service = Service(
        make_settings(),
        store,
        brain,
        {"wx": adapter},
        Router(make_settings(), store, clock=lambda: NOON),
        "你是溟月",
        injection=InjectionConfig(),
    )
    return service, adapter


def seed(store: Store, message: Message) -> None:
    """投一条消息，并把之前的消息都标成已投递，保证新消息会进下一批。"""
    prior = store.recent_messages(message.chat_type, message.chat_id, 50)
    store.add_message(message)
    if prior:
        store.mark_delivered(
            message.chat_type,
            message.chat_id,
            *[item.message_id for item in prior],
        )


async def ask(
    service: Service,
    store: Store,
    *,
    question: str,
    message_id: str,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = GROUP_A,
) -> None:
    seed(store, make_msg(question, message_id=message_id, chat_type=chat_type, chat_id=chat_id))
    await service._run_batch(chat_type, chat_id)


async def test_tool_answer_is_reused_in_another_group(store: Store) -> None:
    brain = FakeBrain()
    service, _ = build(store, brain)

    await ask(service, store, question="云朵哒哒哒是什么梗？", message_id="a1")
    facts = store.shared_facts()
    assert len(facts) == 1
    assert facts[0]["question_text"] == "云朵哒哒哒是什么梗？"
    assert facts[0]["answer_text"] == "是《原神》的宣传曲"

    await ask(
        service,
        store,
        question="云朵 哒哒哒 是什么梗",
        message_id="b1",
        chat_id=GROUP_B,
    )
    assert SHARED_FACT_HEADER in brain.prompts[-1]
    assert "是《原神》的宣传曲" in brain.prompts[-1]
    # 命中会累加 hits，方便以后判断哪些结论真的有用。
    assert store.shared_facts()[0]["hits"] == 1


async def test_no_tool_call_does_not_cache(store: Store) -> None:
    brain = FakeBrain(fire_tool=False)
    service, _ = build(store, brain)

    await ask(service, store, question="云朵哒哒哒是什么梗？", message_id="a1")

    assert store.shared_facts() == []


async def test_private_chat_is_never_cached(store: Store) -> None:
    brain = FakeBrain()
    service, _ = build(store, brain)

    await ask(
        service,
        store,
        question="云朵哒哒哒是什么梗？",
        message_id="p1",
        chat_type=ChatType.PRIVATE,
        chat_id=PRIVATE,
    )

    assert store.shared_facts() == []


async def test_private_question_is_not_cached(store: Store) -> None:
    brain = FakeBrain()
    service, _ = build(store, brain)

    await ask(
        service,
        store,
        question="我的手机号 13800138000 能查到是谁吗？",
        message_id="a1",
    )

    assert store.shared_facts() == []


async def test_statement_without_question_is_not_cached(store: Store) -> None:
    brain = FakeBrain()
    service, _ = build(store, brain)

    await ask(service, store, question="今天天气不错", message_id="a1")

    assert store.shared_facts() == []
