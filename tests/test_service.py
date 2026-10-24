import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from dafeiyu.brain.client import BrainError, BrainReply
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
from dafeiyu.router import Router, now_shanghai
from dafeiyu.service import BRAIN_ERROR_REPLY, FORGET_REPLY, Service

SHANGHAI = timezone(timedelta(hours=8))


def make_settings(**overrides) -> Settings:
    base = dict(
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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({2002})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )
    base.update(overrides)
    return Settings(**base)


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, int, str]] = []
        self.handler = None

    def on_message(self, handler) -> None:
        self.handler = handler

    async def send(self, chat_type: ChatType, chat_id: int, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"


class FakeBrain:
    def __init__(self, speak: bool = True, text: str = "在呢", delay: float = 0.0, error=None):
        self.speak = speak
        self.text = text
        self.delay = delay
        self.error = error
        self.calls = 0

    async def decide(self, system, messages, max_tokens=400) -> BrainReply:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return BrainReply(self.speak, self.text, "test", 10, 5)

    async def aclose(self) -> None:
        pass


def make_msg(chat_id=1001, user_id=2002, content="@溟月 在吗", message_id="m1",
             chat_type=ChatType.GROUP, timestamp=0) -> Message:
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
        timestamp=timestamp,
    )


def build(tmp_path, brain=None, adapter=None, settings=None):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = settings or make_settings()
    adapter = adapter or FakeAdapter()
    brain = brain or FakeBrain()
    router = Router(settings, store)
    service = Service(settings, store, brain, adapter, router, "你是溟月 $max_chars $max_reply_chars_technical")
    return service, store, brain, adapter, router


async def test_hard_trigger_calls_brain_and_sends(tmp_path):
    service, store, brain, adapter, _ = build(tmp_path)
    await service.handle(make_msg())
    assert brain.calls == 1
    assert len(adapter.sent) == 1
    calls, _ = store.usage_today(ChatType.GROUP, 1001, now_shanghai().date().isoformat())
    assert calls == 1


async def test_speak_false_records_usage_without_sending(tmp_path):
    service, store, brain, adapter, _ = build(tmp_path, brain=FakeBrain(speak=False))
    await service.handle(make_msg())
    assert adapter.sent == []
    calls, _ = store.usage_today(ChatType.GROUP, 1001, now_shanghai().date().isoformat())
    assert calls == 1


async def test_private_bypasses_cooldown(tmp_path):
    service, _, brain, adapter, _ = build(tmp_path)
    await service.handle(make_msg(chat_type=ChatType.PRIVATE, content="你好", message_id="p1"))
    await service.handle(make_msg(chat_type=ChatType.PRIVATE, content="再来", message_id="p2"))
    assert brain.calls == 2
    assert len(adapter.sent) == 2


async def test_shut_up_command_mutes_without_model_call(tmp_path):
    service, _, brain, adapter, _ = build(tmp_path)
    await service.handle(make_msg(content="/闭嘴", message_id="c1"))
    assert brain.calls == 0
    assert adapter.sent == []
    await service.handle(make_msg(content="@溟月 在吗", message_id="c2"))
    assert brain.calls == 0


async def test_wake_command_unmutes(tmp_path):
    service, _, brain, adapter, _ = build(tmp_path)
    await service.handle(make_msg(content="/闭嘴", message_id="c1"))
    await service.handle(make_msg(content="/醒来", message_id="c2"))
    await service.handle(make_msg(content="@溟月 在吗", message_id="c3"))
    assert brain.calls == 1


async def test_duplicate_message_id_is_ignored(tmp_path):
    service, _, brain, _, _ = build(tmp_path)
    msg = make_msg()
    await service.handle(msg)
    await service.handle(msg)
    assert brain.calls == 1


async def test_brain_error_sends_short_reply(tmp_path):
    service, _, _, adapter, _ = build(
        tmp_path, brain=FakeBrain(error=BrainError("boom"))
    )
    await service.handle(make_msg())

    assert adapter.sent == [(ChatType.GROUP, 1001, BRAIN_ERROR_REPLY)]


async def test_concurrent_same_group_sends_only_once(tmp_path):
    service, _, brain, adapter, _ = build(tmp_path, brain=FakeBrain(delay=0.05))
    await asyncio.gather(
        service.handle(make_msg(content="这个怎么弄", message_id="a1")),
        service.handle(make_msg(content="这个怎么弄", message_id="a2")),
    )
    assert brain.calls == 1
    assert len(adapter.sent) == 1


async def test_stats_private_only(tmp_path):
    service, store, brain, adapter, _ = build(tmp_path)
    store.record_usage(ChatType.PRIVATE, 2002, "reply", 100, 50)
    await service.handle(
        make_msg(chat_type=ChatType.PRIVATE, chat_id=2002, content="/stats", message_id="s1")
    )
    assert brain.calls == 0
    assert len(adapter.sent) == 1
    assert "1" in adapter.sent[0][2]


async def test_forget_private_clears_memory(tmp_path):
    service, store, _, adapter, _ = build(tmp_path)
    await service.handle(
        make_msg(chat_type=ChatType.PRIVATE, chat_id=2002, content="记住我", message_id="f1")
    )
    await service.handle(
        make_msg(chat_type=ChatType.PRIVATE, chat_id=2002, content="/forget", message_id="f2")
    )
    assert store.count_messages(ChatType.PRIVATE, 2002) == 0
    assert len(adapter.sent) == 2


class FakeOpenCodeBrain:
    def __init__(self, text: str = "在呢", speak: bool = True) -> None:
        self.text = text
        self.speak = speak
        self.calls: list[tuple[ChatType, int, str]] = []
        self.forgotten: list[tuple[ChatType, int]] = []

    async def reply(self, chat_type: ChatType, chat_id: int, text: str) -> BrainReply:
        self.calls.append((chat_type, chat_id, text))
        return BrainReply(self.speak, self.text, "test", 0, 0)

    async def forget(self, chat_type: ChatType, chat_id: int) -> None:
        self.forgotten.append((chat_type, chat_id))


async def test_opencode_brain_receives_speaker_and_chat(tmp_path):
    brain = FakeOpenCodeBrain()
    service, _, _, adapter, _ = build(tmp_path, brain=brain)
    await service.handle(make_msg())
    assert brain.calls == [(ChatType.GROUP, 1001, "阿鱼：@溟月 在吗（@了你）")]
    assert adapter.sent == [(ChatType.GROUP, 1001, "在呢")]


def at(hour: int, minute: int, second: int = 0) -> int:
    return int(
        datetime(2026, 10, 24, hour, minute, second, tzinfo=SHANGHAI).timestamp()
    )


def timeline_settings(**overrides) -> Settings:
    return make_settings(
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么", "什么", "为什么", "吗")),
        **overrides,
    )


async def test_skipped_messages_are_backfilled_into_next_prompt(tmp_path):
    """冷却吃掉的消息不能凭空消失：下一次投递要带上完整时间线。"""
    brain = FakeOpenCodeBrain()
    service, _, _, _, _ = build(tmp_path, brain=brain, settings=timeline_settings())

    await service.handle(
        make_msg(content="@溟月 在吗", message_id="m1", timestamp=at(21, 4, 13))
    )
    # 下面两条都在冷却窗口内、且不是提问 → 被 Router 跳过
    await service.handle(
        make_msg(content="群里谁说话你都会应么", message_id="m2", timestamp=at(21, 4, 40))
    )
    await service.handle(
        make_msg(content="今天天气不错", message_id="m3", timestamp=at(21, 5))
    )
    # 明确提问不受冷却约束
    await service.handle(
        make_msg(content="我问了你什么问题", message_id="m4", timestamp=at(21, 9, 2))
    )

    assert len(brain.calls) == 2
    prompt = brain.calls[-1][2]
    assert "[10-24 21:04] 阿鱼：群里谁说话你都会应么" in prompt
    assert "[10-24 21:05] 阿鱼：今天天气不错" in prompt
    assert "[10-24 21:09] 阿鱼：我问了你什么问题" in prompt
    assert "自你上次开口以来的新消息" in prompt


async def test_each_message_is_delivered_at_most_once(tmp_path):
    """补投过的消息要记账，不能每次接话都重发一遍。"""
    brain = FakeOpenCodeBrain()
    service, _, _, _, _ = build(tmp_path, brain=brain, settings=timeline_settings())

    await service.handle(make_msg(content="@溟月 在吗", message_id="m1"))
    await service.handle(make_msg(content="随便说说", message_id="m2"))  # 冷却跳过
    await service.handle(make_msg(content="到底怎么弄", message_id="m3"))  # 提问，补投 m2
    assert "随便说说" in brain.calls[-1][2]

    await service.handle(make_msg(content="那另一个怎么弄", message_id="m4"))
    prompt = brain.calls[-1][2]
    assert "随便说说" not in prompt  # 已经补投过，不重复
    assert "那另一个怎么弄" in prompt
    assert "在吗" not in prompt


async def test_speak_false_still_advances_cursor(tmp_path):
    """模型看过但选择沉默，也算已投递；否则下次会被当成新消息再喂一遍。"""
    brain = FakeOpenCodeBrain(speak=False)
    service, store, _, adapter, _ = build(
        tmp_path, brain=brain, settings=timeline_settings()
    )

    await service.handle(make_msg(content="@溟月 在吗", message_id="m1"))
    await service.handle(make_msg(content="到底怎么弄", message_id="m2"))

    assert adapter.sent == []
    assert store.last_delivered_id(ChatType.GROUP, 1001) is not None
    assert "在吗" not in brain.calls[-1][2]


async def test_private_prompt_stays_single_line(tmp_path):
    brain = FakeOpenCodeBrain()
    service, _, _, _, _ = build(tmp_path, brain=brain, settings=timeline_settings())

    await service.handle(
        make_msg(chat_type=ChatType.PRIVATE, chat_id=2002, content="在吗", message_id="p1")
    )
    await service.handle(
        make_msg(chat_type=ChatType.PRIVATE, chat_id=2002, content="帮我看看", message_id="p2")
    )

    prompt = brain.calls[-1][2]
    assert prompt.endswith("：帮我看看")
    assert "在吗" not in prompt


async def test_forget_clears_opencode_session(tmp_path):
    brain = FakeOpenCodeBrain()
    service, store, _, _, _ = build(tmp_path, brain=brain)
    store.set_opencode_session(ChatType.PRIVATE, 1001, "ses_x")
    await service.handle(
        make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=1001,
            content="/forget",
            message_id="f1",
        )
    )
    assert store.get_opencode_session(ChatType.PRIVATE, 1001) is None
    assert brain.forgotten == [(ChatType.PRIVATE, 1001)]


async def test_forget_group_clears_memory_for_admin(tmp_path):
    settings = make_settings(
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({2002}))
    )
    service, store, brain, adapter, _ = build(
        tmp_path, settings=settings, brain=FakeOpenCodeBrain()
    )
    store.set_opencode_session(ChatType.GROUP, 1001, "ses_group")
    store.add_message(make_msg(content="记住我", message_id="gf0"))

    await service.handle(make_msg(content="/forget", message_id="gf1"))

    assert store.get_opencode_session(ChatType.GROUP, 1001) is None
    assert store.count_messages(ChatType.GROUP, 1001) == 0
    assert brain.forgotten == [(ChatType.GROUP, 1001)]
    assert adapter.sent[-1] == (ChatType.GROUP, 1001, FORGET_REPLY)


async def test_forget_group_requires_admin(tmp_path):
    settings = make_settings(
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({2002}))
    )
    service, store, brain, adapter, _ = build(
        tmp_path, settings=settings, brain=FakeOpenCodeBrain()
    )
    store.set_opencode_session(ChatType.GROUP, 1001, "ses_group")

    await service.handle(
        make_msg(user_id=99, content="/forget", message_id="gf2")
    )

    assert store.get_opencode_session(ChatType.GROUP, 1001) == "ses_group"
    assert brain.forgotten == []
    assert adapter.sent == []
