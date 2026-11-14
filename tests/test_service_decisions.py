"""Service 决策流水：沉默、预算拦截、成功回复与写库失败的降级。"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from dafeiyu.brain.client import BrainReply
from dafeiyu.budget import BudgetAction, BudgetDecision
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    MindConfig,
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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.handler = None

    def on_message(self, handler) -> None:
        self.handler = handler

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class SpeakingBrain:
    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        return BrainReply(True, "在呢", "test", 0, 0)

    async def aclose(self) -> None:
        pass


class SilentBrain:
    """speak=false，但带模型原文 read/think/reason。"""

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        return BrainReply(
            speak=False,
            reply="",
            reason="这轮不接",
            read="他们在聊昨天的比赛",
            think="没有需要补充的",
        )

    async def aclose(self) -> None:
        pass


def make_msg(content: str = "hi", *, message_id: str = "m1") -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="qq:1001",
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=content.startswith("@溟月"),
        reply_to_bot=False,
        timestamp=0,
    )


def build(tmp_path, brain, settings=None):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = settings or make_settings()
    adapter = FakeAdapter()
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(settings, store, brain, {"qq": adapter}, router, "你是溟月")
    return service, store, adapter


async def test_silent_decision_stores_model_text(tmp_path):
    service, store, adapter = build(tmp_path, SilentBrain())

    await service.handle(make_msg("hi"))
    await service.drain()

    assert adapter.sent == []
    items = store.list_decisions()
    assert len(items) == 1
    item = items[0]
    assert item["outcome"] == "silent"
    assert item["checkpoint"] == ""
    assert item["trigger"] == "batch"
    assert item["reason"] == "这轮不接"
    assert item["read"] == "他们在聊昨天的比赛"
    assert item["think"] == "没有需要补充的"
    assert item["reply_preview"] == ""
    assert item["message_ids"] == ["m1"]


async def test_budget_block_is_recorded(tmp_path, monkeypatch):
    service, store, adapter = build(tmp_path, SpeakingBrain())
    monkeypatch.setattr(
        service.router.budget_guard,
        "evaluate",
        lambda *args, **kwargs: BudgetDecision(
            BudgetAction.DENY, "budget_per_chat", "per_chat"
        ),
    )

    await service.handle(make_msg("hi"))
    await service.drain()

    assert adapter.sent == []
    item = store.list_decisions()[0]
    assert item["outcome"] == "blocked"
    assert item["checkpoint"] == "budget"
    assert item["detail"] == {"scope": "per_chat", "reason": "budget_per_chat"}


async def test_successful_reply_is_recorded_with_preview(tmp_path):
    service, store, adapter = build(tmp_path, SpeakingBrain())

    await service.handle(make_msg("hi"))
    await service.drain()

    assert adapter.sent
    item = store.list_decisions()[0]
    assert item["outcome"] == "replied"
    assert item["checkpoint"] == ""
    assert item["reply_preview"] == "在呢"
    assert item["latency_ms"] >= 0


async def test_send_failure_is_recorded_even_without_outbox(tmp_path, monkeypatch):
    """发送失败且入队也失败时，不能因为 defer=0 就误记成已回复。"""
    service, store, adapter = build(tmp_path, SpeakingBrain())

    async def boom(*args, **kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(adapter, "send", boom)
    monkeypatch.setattr(store, "enqueue_outbox", lambda *args, **kwargs: False)

    await service.handle(make_msg("hi"))
    await service.drain()

    assert adapter.sent == []
    item = store.list_decisions()[0]
    assert item["outcome"] == "error"
    assert item["checkpoint"] == "send_failed"
    assert item["reply_preview"] == "在呢"


async def test_record_failure_does_not_break_sending(tmp_path, monkeypatch):
    service, store, adapter = build(tmp_path, SpeakingBrain())

    def boom(*args, **kwargs):
        raise RuntimeError("decision table unavailable")

    monkeypatch.setattr(store, "record_decision", boom)

    await service.handle(make_msg("hi"))
    await service.drain()

    assert adapter.sent


async def test_outbox_retry_updates_same_decision_row(tmp_path, monkeypatch):
    """补发成功后，原 error/send_failed 决策原地变 replied，不新增一行。"""
    service, store, adapter = build(tmp_path, SpeakingBrain())
    state = {"fail": True}

    async def flaky_send(chat_type, chat_id, text):
        if state["fail"]:
            raise RuntimeError("hook_not_ready")
        adapter.sent.append((chat_type, chat_id, text))
        return f"bot-{len(adapter.sent)}"

    monkeypatch.setattr(adapter, "send", flaky_send)

    await service.handle(make_msg("hi"))
    await service.drain()

    items = store.list_decisions()
    assert len(items) == 1
    assert items[0]["outcome"] == "error"
    assert items[0]["checkpoint"] == "send_failed"
    assert store.outbox_size() == 1
    outbox_id = store.due_outbox()[0]["id"]

    state["fail"] = False
    assert await service._flush_outbox() == 1

    items = store.list_decisions()
    # 关键：仍然只有一行，且从 error 变成 replied。
    assert len(items) == 1
    item = items[0]
    assert item["outcome"] == "replied"
    assert item["checkpoint"] == ""
    assert item["detail"]["retried"] is True
    assert item["detail"]["outbox_id"] == outbox_id
    assert item["detail"]["retry_after_seconds"] >= 0
    assert store.outbox_size() == 0


async def test_retry_mark_failure_does_not_break_outbox(tmp_path, monkeypatch):
    """决策更新写库失败不能影响补发本身。"""
    service, store, adapter = build(tmp_path, SpeakingBrain())
    state = {"fail": True}

    async def flaky_send(chat_type, chat_id, text):
        if state["fail"]:
            raise RuntimeError("hook_not_ready")
        adapter.sent.append((chat_type, chat_id, text))
        return f"bot-{len(adapter.sent)}"

    monkeypatch.setattr(adapter, "send", flaky_send)
    await service.handle(make_msg("hi"))
    await service.drain()

    def boom(*args, **kwargs):
        raise RuntimeError("decision table unavailable")

    monkeypatch.setattr(store, "mark_decision_retried", boom)
    state["fail"] = False

    assert await service._flush_outbox() == 1
    assert adapter.sent
    assert store.outbox_size() == 0


async def test_retry_mark_is_skipped_on_old_store(tmp_path, monkeypatch):
    """老版本 store 没有 mark_decision_retried 时补发照常，不报错。"""
    service, store, adapter = build(tmp_path, SpeakingBrain())
    state = {"fail": True}

    async def flaky_send(chat_type, chat_id, text):
        if state["fail"]:
            raise RuntimeError("hook_not_ready")
        adapter.sent.append((chat_type, chat_id, text))
        return f"bot-{len(adapter.sent)}"

    monkeypatch.setattr(adapter, "send", flaky_send)
    await service.handle(make_msg("hi"))
    await service.drain()

    monkeypatch.delattr(Store, "mark_decision_retried", raising=False)
    state["fail"] = False

    assert await service._flush_outbox() == 1
    assert adapter.sent


# --------------------------------------------- 注定沉默批次的确定性前置过滤
def make_batch_settings() -> Settings:
    """点名的批次也立刻投递：把 addressed 防抖关掉，闸门行为与运行时刻无关。"""
    return replace(
        make_settings(),
        triggers=TriggerConfig(
            60,
            0.0,
            ("01:00", "07:00"),
            ("怎么",),
            mode="model",
            batch_seconds=0.0,
            addressed_debounce_seconds=0.0,
        ),
    )


class CountingBrain:
    """数模型调用次数的桩：每次 reply 记一笔并正常回话。"""

    def __init__(self) -> None:
        self.calls = 0

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        self.calls += 1
        return BrainReply(True, "在呢", "test", 0, 0)

    async def aclose(self) -> None:
        pass


def bot_spoke(store: Store, *, seconds_ago: float) -> None:
    """把「机器人刚说过话」写进 chat_state；闸门读的就是这一格。"""
    store.save_chat_state(
        ChatType.GROUP, "qq:1001", {"last_bot_spoke": time.time() - seconds_ago}
    )


def make_bot_question(message_id: str = "b1") -> Message:
    """机器人上一句是提问：隐式点名豁免的前置。"""
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="qq:1001",
        user_id="qq:999",
        nickname="溟月",
        content="要不要一起点外卖？",
        message_id=message_id,
        segments=[],
        is_bot=True,
        at_bot=False,
        reply_to_bot=False,
        timestamp=0,
    )


async def test_known_silent_sticker_batch_skips_model(tmp_path):
    """四条命中前提齐：单条表情包 + 无 @/引用/接话 + 机器人 90 秒内刚说话。"""
    brain = CountingBrain()
    service, store, adapter = build(tmp_path, brain, settings=make_batch_settings())
    bot_spoke(store, seconds_ago=10)

    await service.handle(make_msg("[表情:狗头]"))
    await service.drain()

    assert brain.calls == 0
    assert adapter.sent == []
    item = store.list_decisions()[0]
    assert item["outcome"] == "silent"
    assert item["checkpoint"] == "skip_known_silent"
    assert item["reason"] == "纯表情占位"


async def test_at_sticker_still_goes_to_model(tmp_path):
    """被 @ 的表情包豁免：照常调模型，不许少回一条本该回的。"""
    brain = CountingBrain()
    service, store, adapter = build(tmp_path, brain, settings=make_batch_settings())
    bot_spoke(store, seconds_ago=10)

    await service.handle(make_msg("@溟月[表情:狗头]"))
    await service.drain()

    assert brain.calls == 1
    assert adapter.sent
    assert store.list_decisions()[0]["outcome"] == "replied"


async def test_short_answer_to_bot_question_not_filtered(tmp_path):
    """「对」接在机器人提问后：隐式点名豁免最先生效，2 字短答不误杀。"""
    brain = CountingBrain()
    service, store, _ = build(tmp_path, brain, settings=make_batch_settings())
    store.add_message(make_bot_question())
    bot_spoke(store, seconds_ago=10)

    await service.handle(make_msg("对"))
    await service.drain()

    assert brain.calls == 1


async def test_silent_skip_respects_cooldown_window(tmp_path):
    """同一条表情包：冷却窗内 0 次调用；超过 90 秒后 1 次调用。"""
    brain = CountingBrain()
    service, _, _ = build(tmp_path, brain, settings=make_batch_settings())
    bot_spoke(store=service.store, seconds_ago=10)
    await service.handle(make_msg("[表情]"))
    await service.drain()
    assert brain.calls == 0

    later = CountingBrain()
    service2, store2, _ = build(tmp_path, later, settings=make_batch_settings())
    bot_spoke(store2, seconds_ago=120)
    await service2.handle(make_msg("[表情]", message_id="m2"))
    await service2.drain()
    assert later.calls == 1


async def test_new_question_within_cooldown_is_exempt(tmp_path):
    """机器人刚说过话但这条是新的提问：「为啥？」短到会命中弱消息形态，
    豁免④必须盖过命中，否则问句被吞。"""
    brain = CountingBrain()
    service, store, _ = build(tmp_path, brain, settings=make_batch_settings())
    bot_spoke(store, seconds_ago=10)

    await service.handle(make_msg("为啥？"))
    await service.drain()

    assert brain.calls == 1


async def test_skip_known_silent_false_keeps_model_call(tmp_path):
    """开关关掉：同一批表情包回到今天的行为，照常调模型。"""
    settings = replace(
        make_batch_settings(), mind=MindConfig(skip_known_silent=False)
    )
    brain = CountingBrain()
    service, store, _ = build(tmp_path, brain, settings=settings)
    bot_spoke(store, seconds_ago=10)

    await service.handle(make_msg("[表情:狗头]"))
    await service.drain()

    assert brain.calls == 1

# ------------------------------------------------- 微信私聊只读 / 自动回复
def build_wechat_private(tmp_path, brain, *, private_reply: bool):
    from dataclasses import replace

    from dafeiyu.config import WeChatConfig

    store = Store(str(tmp_path / "w.db"))
    store.init_schema()
    settings = replace(
        make_settings(),
        wechat=WeChatConfig(
            enabled=True, private_enabled=True, private_reply=private_reply
        ),
    )
    adapter = FakeAdapter()
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(
        settings, store, brain, {"wechat": adapter, "qq": adapter}, router, "你是溟月"
    )
    return service, store, adapter


def make_wx_msg(*, message_id: str = "wx-m1") -> Message:
    return Message(
        chat_type=ChatType.PRIVATE,
        chat_id="wxg:friend1",
        user_id="wxg:friend1",
        nickname="苑长",
        content="在么",
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=False,
        reply_to_bot=False,
        timestamp=0,
    )


async def test_wechat_private_readonly_records_silent(tmp_path):
    service, store, adapter = build_wechat_private(
        tmp_path, SpeakingBrain(), private_reply=False
    )

    await service.handle(make_wx_msg())
    await service.drain()

    assert adapter.sent == []
    item = store.list_decisions()[0]
    assert item["outcome"] == "silent"
    assert item["checkpoint"] == "private_readonly"
    assert item["trigger"] == "private"
    assert item["detail"] == {"private_reply": False}


async def test_wechat_private_replies_when_enabled(tmp_path):
    service, store, adapter = build_wechat_private(
        tmp_path, SpeakingBrain(), private_reply=True
    )

    await service.handle(make_wx_msg())
    await service.drain()

    assert adapter.sent == [(ChatType.PRIVATE, "wxg:friend1", "在呢")]
    item = store.list_decisions()[0]
    assert item["outcome"] == "replied"
    assert item["checkpoint"] == ""


async def test_qq_private_is_not_gated_by_wechat_private_reply(tmp_path):
    from dataclasses import replace

    from dafeiyu.config import WeChatConfig

    settings = replace(
        make_settings(),
        wechat=WeChatConfig(enabled=True, private_reply=False),
    )
    store = Store(str(tmp_path / "q.db"))
    store.init_schema()
    adapter = FakeAdapter()
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(settings, store, SpeakingBrain(), {"qq": adapter}, router, "你是溟月")
    msg = Message(
        chat_type=ChatType.PRIVATE,
        chat_id="qq:504567327",
        user_id="qq:504567327",
        nickname="阿鱼",
        content="在么",
        message_id="qq-m1",
        segments=[],
        is_bot=False,
        at_bot=False,
        reply_to_bot=False,
        timestamp=0,
    )

    await service.handle(msg)
    await service.drain()

    assert adapter.sent == [(ChatType.PRIVATE, "qq:504567327", "在呢")]
    assert store.list_decisions()[0]["outcome"] == "replied"


# ------------------------------------------------- P1-6: private_reply 热切换
async def test_wechat_private_reply_hot_toggles_from_overlay(tmp_path, monkeypatch):
    """面板写覆盖层 private_reply=false：不重启 service，下一条私聊立即只读。"""
    import dafeiyu.hotconfig as hotconfig
    import dafeiyu.service as service_module
    from dafeiyu.settings_local import write_overrides

    path = tmp_path / "settings.local.yaml"
    monkeypatch.setattr(hotconfig, "DEFAULT_PATH", path)
    service_module._WECHAT_RUNTIME_VIEWS.clear()
    try:
        service, store, adapter = build_wechat_private(
            tmp_path, SpeakingBrain(), private_reply=True
        )

        await service.handle(make_wx_msg())
        await service.drain()

        assert adapter.sent == [(ChatType.PRIVATE, "wxg:friend1", "在呢")]

        write_overrides({"wechat": {"private_reply": False}}, path)

        await service.handle(make_wx_msg(message_id="wx-m2"))
        await service.drain()

        # 覆盖层切换后不再回复。
        assert adapter.sent == [(ChatType.PRIVATE, "wxg:friend1", "在呢")]
        readonly = [
            item
            for item in store.list_decisions()
            if item["checkpoint"] == "private_readonly"
        ]
        assert len(readonly) == 1
        assert readonly[0]["outcome"] == "silent"
        assert readonly[0]["trigger"] == "private"
        assert readonly[0]["detail"] == {"private_reply": False}
    finally:
        service_module._WECHAT_RUNTIME_VIEWS.clear()
