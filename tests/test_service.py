import asyncio
import time
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
from dafeiyu.router import Router, TriggerDecision, now_shanghai
from dafeiyu.service import FORGET_REPLY, Service
from dafeiyu.workers.failure import QUOTA_KEY

SHANGHAI = timezone(timedelta(hours=8))

# 固定白天时钟：Router 的静默时段（01:00-07:00）若跟着真实时钟走，
# 测试在凌晨跑就会整批失败。显式注入时钟可让结果与运行时刻无关。
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)


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
    # Service 的回复入库路径按通道取 bot_id；QQ 侧就是 qq:<uin>。
    bot_id = "qq:999"

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


def build(tmp_path, brain=None, adapter=None, settings=None, audit=None):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = settings or make_settings()
    adapter = adapter or FakeAdapter()
    brain = brain or FakeBrain()
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(
        settings,
        store,
        brain,
        adapter,
        router,
        "你是溟月 $max_chars $max_reply_chars_technical",
        audit=audit,
    )
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


async def test_private_speak_false_is_logged_as_a_decision(tmp_path, caplog):
    service, _, _, adapter, _ = build(tmp_path, brain=FakeOpenCodeBrain(speak=False))

    with caplog.at_level("INFO"):
        await service.handle(
            make_msg(chat_type=ChatType.PRIVATE, content="你好", message_id="p1")
        )

    assert adapter.sent == []
    assert "模型选择不说" in caplog.text


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


async def test_brain_error_stays_silent(tmp_path):
    service, _, _, adapter, _ = build(
        tmp_path, brain=FakeBrain(error=BrainError("boom"))
    )
    await service.handle(make_msg())

    # 用户明确不要「本鱼断线了」这类失败提示：失败只记账、不发言。
    assert adapter.sent == []


async def test_brain_error_records_usage_without_sending(tmp_path):
    service, store, _, adapter, _ = build(
        tmp_path, brain=FakeBrain(error=BrainError("boom"))
    )

    await service._reply(make_msg(chat_type=ChatType.PRIVATE, chat_id=2002))
    await service._reply(
        make_msg(chat_type=ChatType.PRIVATE, chat_id=2002, message_id="m2")
    )

    assert adapter.sent == []
    calls, _ = store.usage_today_by_purpose(
        ChatType.PRIVATE, 2002, store.today(), ("error",)
    )
    assert calls == 2


async def test_short_answer_to_bot_question_is_addressed(tmp_path):
    """bot 刚问完「你用的什么卡？」，下一句「4060」算隐式点名。"""
    service, store, _, _, _ = build(tmp_path)
    bot = Message(
        chat_type=ChatType.GROUP,
        chat_id=1001,
        user_id=999,
        nickname="溟月",
        content="你用的什么卡？",
        message_id="b1",
        is_bot=True,
        timestamp=1000,
    )
    store.add_message(bot)

    assert service._is_addressed(make_msg(content="4060", message_id="m2", timestamp=1005))
    # 中间隔了 10 分钟、或回答太长，都不再算接它的话。
    assert not service._is_addressed(
        make_msg(content="4060", message_id="m3", timestamp=1605)
    )
    assert not service._is_addressed(
        make_msg(
            content=(
                "4060 但是我觉得驱动也得换一下，顺便电源可能也不够，"
                "最好先看看整机功耗再决定要不要升级，不然白折腾"
            ),
            message_id="m4",
            timestamp=1006,
        )
    )


async def test_reply_to_old_bot_message_is_resolved_from_db(tmp_path):
    """重启后网关的 bot_message_ids 清空，引用老消息仍要按点名处理。"""
    service, store, brain, _, _ = build(tmp_path)
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=1001,
            user_id=999,
            nickname="溟月",
            content="在呢",
            message_id="b1",
            is_bot=True,
            timestamp=1000,
        )
    )
    user = Message(
        chat_type=ChatType.GROUP,
        chat_id=1001,
        user_id=2002,
        nickname="阿鱼",
        content="继续",
        message_id="u1",
        reply_to_message_id="b1",
        timestamp=1005,
    )

    await service.handle(user)

    assert user.reply_to_bot is True
    assert brain.calls == 1


async def test_agent_missing_error_rebuilds_session_and_replies(tmp_path):
    """仓库移动后旧 opencode session 会报 Agent not found：应自动换新会话。"""

    class AgentMissingBrain(FakeBrain):
        def __init__(self) -> None:
            super().__init__()
            self.fresh_calls = 0

        async def reply(self, chat_type, chat_id, text, *, on_tool=None):
            raise BrainError(
                'OpenCode 调用失败（unknown）：Agent not found: "deepseek-qq"'
            )

        async def reply_fresh(
            self, chat_type, chat_id, text, *, system="", history=None, on_tool=None
        ):
            self.fresh_calls += 1
            return BrainReply(True, "换好了", "fresh", 1, 1)

    brain = AgentMissingBrain()
    service, _, _, adapter, _ = build(tmp_path, brain=brain)

    await service.handle(make_msg())

    assert brain.fresh_calls == 1
    assert adapter.sent == [(ChatType.GROUP, 1001, "换好了")]


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
    # 单条消息也走批次构建器：带时间线表头，行内仍标出说话人与 @。
    assert brain.calls == [
        (ChatType.GROUP, 1001, "—— 以下是自你上次开口以来的新消息 ——\n阿鱼：@溟月 在吗（@了你）")
    ]
    assert adapter.sent == [(ChatType.GROUP, 1001, "在呢")]
    assert "[人设覆盖]" not in brain.calls[0][2]


async def test_opencode_prompt_gets_os_override_when_configured(tmp_path):
    brain = FakeOpenCodeBrain()
    settings = make_settings(
        persona=PersonaConfig("溟月", ("大肥鱼",), 120, 300, 0.2)
    )
    service, _, _, _, _ = build(tmp_path, brain=brain, settings=settings)

    await service.handle(make_msg())

    assert brain.calls[0][2].endswith(
        "[人设覆盖] 本轮内心 OS 概率约 20%，以本条为准。"
    )


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


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, action: str, **kwargs):
        self.events.append((action, kwargs))
        return {}


class BrokenAudit:
    def write(self, action: str, **kwargs):
        raise RuntimeError("disk full")


async def test_non_admin_group_budget_is_silent_and_not_audited(tmp_path):
    audit = FakeAudit()
    service, _, brain, adapter, _ = build(tmp_path, audit=audit)
    msg = make_msg(user_id=99, content="/budget", message_id="b0")
    matched = service.commands.match(msg.content, msg.chat_type)
    assert matched is not None

    handled = await service._run_command(msg, *matched)

    assert handled is True
    assert adapter.sent == []
    assert brain.calls == 0
    assert audit.events == []


async def test_admin_budget_reports_and_writes_audit(tmp_path):
    audit = FakeAudit()
    service, store, brain, adapter, _ = build(tmp_path, audit=audit)
    store.record_usage(ChatType.GROUP, 1001, "reply", 120, 80)

    await service.handle(make_msg(content="/budget", message_id="b1"))

    assert brain.calls == 0
    assert len(adapter.sent) == 1
    text = adapter.sent[0][2]
    assert "全局" in text
    assert "月度" in text

    assert len(audit.events) == 1
    action, kwargs = audit.events[0]
    assert action == "command.admin"
    assert kwargs["target"] == "/budget"
    assert kwargs["actor"] == "2002"
    detail = kwargs["detail"]
    assert detail == {"chat_type": "group", "chat_id": 1001, "user_id": 2002}
    assert "阿鱼" not in repr(detail)


async def test_blacklisted_group_commands_are_silent(tmp_path):
    settings = make_settings(
        groups=GroupsConfig(frozenset(), frozenset({1001}), frozenset({2002}))
    )
    service, _, brain, adapter, _ = build(tmp_path, settings=settings)

    await service.handle(make_msg(content="/help", message_id="bl1"))
    await service.handle(make_msg(content="/stats", message_id="bl2"))

    assert brain.calls == 0
    assert adapter.sent == []


async def test_non_whitelisted_group_commands_are_silent(tmp_path):
    settings = make_settings(
        groups=GroupsConfig(frozenset({2001}), frozenset(), frozenset({2002}))
    )
    service, _, brain, adapter, _ = build(tmp_path, settings=settings)

    await service.handle(make_msg(content="/help", message_id="wl1"))
    await service.handle(
        make_msg(chat_id=2001, content="/help", message_id="wl2")
    )

    assert brain.calls == 0
    assert len(adapter.sent) == 1
    assert adapter.sent[0][1] == 2001


async def test_stats_group_has_usage_and_no_cost_words(tmp_path):
    audit = FakeAudit()
    service, store, brain, adapter, _ = build(tmp_path, audit=audit)
    store.record_usage(ChatType.GROUP, 1001, "reply", 120, 80)

    await service.handle(make_msg(user_id=99, content="/stats", message_id="st1"))

    assert brain.calls == 0
    text = adapter.sent[0][2]
    assert "calls" in text
    assert "tokens" in text
    assert "剩余" in text
    assert "元" not in text
    assert "¥" not in text
    # /stats 不是管理员命令，不写审计。
    assert audit.events == []


async def test_help_cooldown_blocks_repeat_within_five_seconds(tmp_path):
    service, _, brain, adapter, _ = build(tmp_path)
    now = {"value": 1000.0}
    service._command_clock = lambda: now["value"]

    await service.handle(make_msg(content="/help", message_id="h1"))
    assert len(adapter.sent) == 1

    now["value"] += 1
    await service.handle(make_msg(content="/help", message_id="h2"))
    assert len(adapter.sent) == 1

    now["value"] += 4
    await service.handle(make_msg(content="/help", message_id="h3"))
    assert len(adapter.sent) == 2
    assert brain.calls == 0


async def test_stats_cooldown_is_sixty_seconds_in_group(tmp_path):
    service, _, _, adapter, _ = build(tmp_path)
    now = {"value": 2000.0}
    service._command_clock = lambda: now["value"]

    await service.handle(make_msg(user_id=99, content="/stats", message_id="sg1"))
    assert len(adapter.sent) == 1

    now["value"] += 59
    await service.handle(make_msg(user_id=99, content="/stats", message_id="sg2"))
    assert len(adapter.sent) == 1

    now["value"] += 1
    await service.handle(make_msg(user_id=99, content="/stats", message_id="sg3"))
    assert len(adapter.sent) == 2


async def test_stats_cooldown_is_five_seconds_in_private(tmp_path):
    service, _, _, adapter, _ = build(tmp_path)
    now = {"value": 3000.0}
    service._command_clock = lambda: now["value"]

    def private_msg(message_id: str) -> Message:
        return make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=2002,
            user_id=2002,
            content="/stats",
            message_id=message_id,
        )

    await service.handle(private_msg("sp1"))
    assert len(adapter.sent) == 1

    now["value"] += 4
    await service.handle(private_msg("sp2"))
    assert len(adapter.sent) == 1

    now["value"] += 1
    await service.handle(private_msg("sp3"))
    assert len(adapter.sent) == 2


async def test_help_cooldown_is_isolated_by_group_chat_id(tmp_path):
    service, _, _, adapter, _ = build(tmp_path)
    service._command_clock = lambda: 4000.0

    await service.handle(
        make_msg(user_id=99, chat_id=1001, content="/help", message_id="ci-g1")
    )
    await service.handle(
        make_msg(user_id=99, chat_id=1002, content="/help", message_id="ci-g2")
    )

    assert len(adapter.sent) == 2


async def test_help_cooldown_is_isolated_by_private_user_id(tmp_path):
    service, _, _, adapter, _ = build(tmp_path)
    service._command_clock = lambda: 5000.0

    for user_id, message_id in ((99, "ci-p1"), (98, "ci-p2")):
        await service.handle(
            make_msg(
                chat_type=ChatType.PRIVATE,
                chat_id=user_id,
                user_id=user_id,
                content="/help",
                message_id=message_id,
            )
        )

    assert len(adapter.sent) == 2


async def test_zero_cooldown_command_does_not_record_state(tmp_path):
    service, _, _, _, _ = build(tmp_path)

    await service.handle(make_msg(content="/闭嘴", message_id="zc1"))

    assert service._command_last == {}


async def test_status_private_non_admin_replies_with_fixed_hint(tmp_path):
    service, _, brain, adapter, _ = build(tmp_path)

    await service.handle(
        make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=99,
            user_id=99,
            content="/status",
            message_id="ps1",
        )
    )

    assert brain.calls == 0
    assert adapter.sent == [(ChatType.PRIVATE, 99, "这个命令只有管理员能用。")]


async def test_status_reports_live_state_without_model_name(tmp_path):
    service, store, brain, adapter, _ = build(tmp_path)
    service.adapter.connected = True
    store.save_backoff(QUOTA_KEY, time.time() + 60)

    await service.handle(make_msg(content="/status", message_id="stt1"))

    text = adapter.sent[0][2]
    assert brain.calls == 0
    assert "网关" in text
    assert "已连接" in text
    assert "待投" in text
    assert "配额" in text
    assert "暂停" in text
    assert "预算" in text
    assert "秒" in text
    assert "deepseek-chat" not in text


async def test_status_reads_scheduler_attribute_and_callable_counts(tmp_path):
    class FakeScheduler:
        pending_count = 2

        def inflight_count(self) -> int:
            return 3

    service, _, _, adapter, _ = build(tmp_path)
    service.scheduler = FakeScheduler()

    await service.handle(make_msg(content="/status", message_id="stt2"))

    text = adapter.sent[0][2]
    assert "待投 2" in text
    assert "进行中 3" in text


async def test_help_replies_in_group_and_private_with_scope_filter(tmp_path):
    service, _, brain, adapter, _ = build(tmp_path)

    await service.handle(make_msg(user_id=99, content="/help", message_id="help-g"))
    group_text = adapter.sent[-1][2]
    assert "/stats" in group_text
    assert "/budget" not in group_text
    assert "/status" not in group_text
    assert "/forget" not in group_text

    await service.handle(
        make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=99,
            user_id=99,
            content="/help",
            message_id="help-p",
        )
    )
    private_text = adapter.sent[-1][2]
    assert "/forget" in private_text
    assert "/budget" not in private_text
    assert brain.calls == 0


async def test_help_is_not_audited_even_for_admin(tmp_path):
    audit = FakeAudit()
    service, _, _, adapter, _ = build(tmp_path, audit=audit)

    await service.handle(make_msg(content="/help", message_id="help-a"))

    assert len(adapter.sent) == 1
    assert audit.events == []


async def test_private_forget_allows_non_admin_without_admin_audit(tmp_path):
    audit = FakeAudit()
    service, store, _, adapter, _ = build(tmp_path, audit=audit)
    store.add_message(
        make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=99,
            user_id=99,
            content="记住我",
            message_id="pf0",
        )
    )

    await service.handle(
        make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=99,
            user_id=99,
            content="/forget",
            message_id="pf1",
        )
    )

    assert store.count_messages(ChatType.PRIVATE, 99) == 0
    assert adapter.sent[-1] == (ChatType.PRIVATE, 99, FORGET_REPLY)
    assert audit.events == []


async def test_private_admin_forget_writes_admin_audit(tmp_path):
    audit = FakeAudit()
    service, store, _, adapter, _ = build(tmp_path, audit=audit)
    store.add_message(
        make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=2002,
            user_id=2002,
            content="记住我",
            message_id="pfa0",
        )
    )

    await service.handle(
        make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=2002,
            user_id=2002,
            content="/forget",
            message_id="pfa1",
        )
    )

    assert store.count_messages(ChatType.PRIVATE, 2002) == 0
    assert adapter.sent[-1] == (ChatType.PRIVATE, 2002, FORGET_REPLY)
    assert len(audit.events) == 1
    action, kwargs = audit.events[0]
    assert action == "command.admin"
    assert kwargs["target"] == "/forget"
    assert kwargs["detail"] == {
        "chat_type": "private",
        "chat_id": 2002,
        "user_id": 2002,
    }


async def test_unknown_and_wrong_scope_slash_fall_through_to_model(tmp_path):
    service, _, brain, adapter, _ = build(tmp_path)

    await service.handle(
        make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=2002,
            user_id=2002,
            content="/nope",
            message_id="pp1",
        )
    )
    # shutup 只允许群聊；私聊应当继续走模型，而不是被命令分支吞掉。
    await service.handle(
        make_msg(
            chat_type=ChatType.PRIVATE,
            chat_id=2002,
            user_id=2002,
            content="/shutup",
            message_id="pp2",
        )
    )

    assert brain.calls == 2
    assert len(adapter.sent) == 2


async def test_unknown_group_slash_reaches_router(tmp_path, monkeypatch):
    service, _, _, _, _ = build(tmp_path)
    seen: list[str] = []

    def should_handle(msg):
        seen.append(msg.content)
        return TriggerDecision(False, False, "sample_skip")

    monkeypatch.setattr(service.router, "should_handle", should_handle)

    await service.handle(make_msg(content="/nope", message_id="pp3"))

    assert seen == ["/nope"]


async def test_audit_failure_does_not_block_admin_command(tmp_path):
    service, _, _, adapter, _ = build(tmp_path, audit=BrokenAudit())

    await service.handle(make_msg(content="/budget", message_id="ab1"))

    assert len(adapter.sent) == 1
    assert "全局" in adapter.sent[0][2]


@pytest.mark.parametrize(
    ("content", "chat_type", "chat_id", "user_id"),
    [
        ("/help", ChatType.GROUP, 1001, 2002),
        ("/预算", ChatType.GROUP, 1001, 2002),
        ("/status", ChatType.PRIVATE, 99, 99),
        ("/stats", ChatType.GROUP, 1001, 99),
    ],
)
def test_explain_trigger_recognizes_registry_commands(
    tmp_path, content, chat_type, chat_id, user_id
):
    service, _, _, _, _ = build(tmp_path)

    result = service.explain_trigger(
        make_msg(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=user_id,
            content=content,
            message_id="ex1",
        ),
        now=NOON,
    )

    assert result["verdict"] == "command"
    assert result["reason"] == "command"


def test_explain_trigger_unknown_slash_is_not_a_command(tmp_path):
    service, _, _, _, _ = build(tmp_path)

    result = service.explain_trigger(
        make_msg(content="/nope", message_id="ex2"), now=NOON, seed=1
    )

    assert result["verdict"] != "command"
