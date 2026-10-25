"""model 模式：handle 只入库+排批，不 await 模型；rules 模式保持同步旧路径。"""

import asyncio
from datetime import datetime, timedelta, timezone

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
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)
NIGHT = datetime(2026, 10, 24, 3, 0, tzinfo=SHANGHAI)


def make_settings(*, mode: str = "model", batch_seconds: float = 45.0) -> Settings:
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
            60, 0.0, ("01:00", "07:00"), ("怎么",), mode=mode, batch_seconds=batch_seconds
        ),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({2002})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, int, str]] = []
        self.handler = None

    def on_message(self, handler) -> None:
        self.handler = handler

    async def send(self, chat_type: ChatType, chat_id: int, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class FakeOpenCodeBrain:
    def __init__(self, *, delay: float = 0.0, speak: bool = True, text: str = "在呢") -> None:
        self.delay = delay
        self.speak = speak
        self.text = text
        self.calls: list[tuple[ChatType, int, str]] = []

    @property
    def reply_calls(self) -> int:
        return len(self.calls)

    async def reply(
        self, chat_type: ChatType, chat_id: int, text: str, *, on_tool=None
    ) -> BrainReply:
        self.calls.append((chat_type, chat_id, text))
        if self.delay:
            await asyncio.sleep(self.delay)
        return BrainReply(self.speak, self.text, "test", 0, 0)

    async def aclose(self) -> None:
        pass


class FakeTokenBrain:
    """只有 decide，没有 reply —— 用来验证回落到 rules 路径。"""

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, system, messages, max_tokens=400) -> BrainReply:
        self.calls += 1
        return BrainReply(True, "在呢", "test", 10, 5)

    async def aclose(self) -> None:
        pass


def make_msg(
    content: str = "hi",
    *,
    message_id: str = "m1",
    chat_type: ChatType = ChatType.GROUP,
    chat_id: int = 1001,
    user_id: int = 2002,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=content.startswith("@溟月"),
        reply_to_bot=False,
        timestamp=0,
    )


def build(tmp_path, *, mode="model", brain=None, clock=NOON, batch_seconds=45.0):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(mode=mode, batch_seconds=batch_seconds)
    adapter = FakeAdapter()
    brain = brain or FakeOpenCodeBrain()
    router = Router(settings, store, clock=lambda: clock)
    service = Service(settings, store, brain, adapter, router, "你是溟月")
    return service, store, brain, adapter


async def test_handle_returns_before_model(tmp_path):
    service, _, brain, _ = build(tmp_path, brain=FakeOpenCodeBrain(delay=1.0))

    # 点名的消息在 rules 模式会同步 await 模型；model 模式必须立刻返回。
    await asyncio.wait_for(service.handle(make_msg("@溟月 在吗")), timeout=0.2)

    assert brain.reply_calls == 0


async def test_private_still_replies_immediately(tmp_path):
    service, _, brain, adapter = build(tmp_path)

    await service.handle(
        make_msg("hi", chat_type=ChatType.PRIVATE, chat_id=2002, message_id="p1")
    )

    assert brain.reply_calls == 1
    assert adapter.sent


async def test_quiet_hours_skips_with_reason(tmp_path):
    service, store, _, _ = build(tmp_path, clock=NIGHT)

    await service.handle(make_msg("hi"))

    assert store.skip_reason(ChatType.GROUP, 1001, "m1") == "quiet"
    assert service.scheduler.pending_chats() == []


async def test_rules_mode_unchanged(tmp_path):
    service, _, brain, _ = build(tmp_path, mode="rules")

    await service.handle(make_msg("@溟月 在吗"))

    assert brain.reply_calls == 1


async def test_no_reply_method_falls_back_to_rules(tmp_path):
    brain = FakeTokenBrain()
    service, _, _, _ = build(tmp_path, brain=brain)

    await service.handle(make_msg("@溟月 在吗"))

    assert service.scheduler is None
    assert brain.calls == 1


async def test_drain_flushes_due_batch(tmp_path):
    service, store, brain, adapter = build(tmp_path, batch_seconds=0.0)

    await service.handle(make_msg("hi"))
    assert brain.reply_calls == 0

    await service.drain()

    assert brain.reply_calls == 1
    assert store.last_delivered_id(ChatType.GROUP, 1001) is not None
    assert adapter.sent
