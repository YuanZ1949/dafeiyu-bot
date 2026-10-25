import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from dafeiyu.brain.client import FALLBACK_REPLY, BrainClient, BrainError, BrainReply
from dafeiyu.brain.persona import build_system_prompt, load_persona
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.gateway.qq import parse_event
from dafeiyu.memory.context import CONTEXT_BUDGET, build_context
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router, now_shanghai
from dafeiyu.service import Service
from dafeiyu.workers.impressions import ImpressionWorker
from dafeiyu.workers.summary import SummaryScheduler, summary_loop

GROUP = ChatType.GROUP
NOW = 1_760_000_000
# 固定白天时钟，避免凌晨运行时命中 Router 的 01:00-07:00 静默时段。
CLOCK = datetime(2026, 10, 24, 12, 0, tzinfo=timezone(timedelta(hours=8)))


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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )
    base.update(overrides)
    return Settings(**base)


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, int, str]] = []

    def on_message(self, handler) -> None:
        self.handler = handler

    async def send(self, chat_type: ChatType, chat_id: int, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"


class FakeBrain:
    def __init__(self, speak: bool = True, text: str = "在呢", error=None) -> None:
        self.speak = speak
        self.text = text
        self.error = error
        self.decide_calls = 0
        self.complete_calls = 0

    async def decide(self, system, messages, max_tokens=400) -> BrainReply:
        self.decide_calls += 1
        if self.error:
            raise self.error
        return BrainReply(self.speak, self.text, "test", 10, 5)

    async def complete_detailed(self, system, messages, max_tokens=400, temperature=0.2):
        self.complete_calls += 1
        return "[]", 7, 3


def msg(
    text: str,
    *,
    chat_id: int = 1,
    user_id: int = 2,
    message_id: str = "m1",
    at_bot: bool = False,
    is_bot: bool = False,
) -> Message:
    return Message(
        chat_type=GROUP,
        chat_id=chat_id,
        user_id=user_id,
        nickname="群友",
        content=text,
        message_id=message_id,
        segments=[],
        is_bot=is_bot,
        at_bot=at_bot,
        reply_to_bot=False,
        timestamp=NOW,
    )


def service(settings=None, brain=None, store=None, adapter=None, impressions=None):
    settings = settings or make_settings()
    store = store or Store(":memory:")
    store.init_schema()
    brain = brain or FakeBrain()
    adapter = adapter or FakeAdapter()
    router = Router(settings, store, clock=lambda: CLOCK)
    svc = Service(
        settings, store, brain, adapter, router, "人设", impressions
    )
    return svc, store, brain, adapter


# ---- C1: 真实 persona 下印象/摘要素材必须进入 system -------------------


def test_context_keeps_impressions_summary_and_scene_with_real_persona():
    store = Store(":memory:")
    store.init_schema()
    store.upsert_impression(GROUP, 1, 2, "nickname", "外号叫电鱼佬", 0.9)
    store.upsert_summary(GROUP, 1, "2026-10-23", "RTX5090装机")
    store.add_message(msg("在吗"))

    system = build_system_prompt(load_persona(), "测试群", "2026-10-24", 120, 300)
    ctx = build_context(store, GROUP, 1, 20, 5, system, "2026-10-24")

    assert "外号叫电鱼佬" in ctx.system
    assert "RTX5090装机" in ctx.system
    assert "说话示例" in ctx.system
    assert "当前场景" in ctx.system
    assert ctx.estimated_tokens <= CONTEXT_BUDGET


# ---- C2: 摘要调度循环必须能持续运行并响应停止 -------------------------


@pytest.mark.asyncio
async def test_summary_loop_runs_periodically_until_stopped():
    class FakeScheduler:
        def __init__(self) -> None:
            self.calls = 0

        async def run_pending(self) -> int:
            self.calls += 1
            return 0

    scheduler = FakeScheduler()
    stop = asyncio.Event()
    task = asyncio.create_task(summary_loop(scheduler, interval=0.01, stop=stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert scheduler.calls >= 2


# ---- C3: 机器人自己的话必须进入历史 -----------------------------------


@pytest.mark.asyncio
async def test_bot_reply_is_stored_and_visible_as_assistant_turn():
    svc, store, _, adapter = service()
    await svc.handle(msg("你好", message_id="u1", at_bot=True))
    assert adapter.sent

    history = store.recent_messages(GROUP, 1, 10)
    assert any(m.is_bot for m in history)
    assert history[-1].content == "在呢"


# ---- I1: 硬触发优先于白名单 -------------------------------------------


def test_at_bot_bypasses_whitelist_but_plain_message_does_not():
    settings = make_settings(groups=GroupsConfig(frozenset({777}), frozenset(), frozenset()))
    store = Store(":memory:")
    store.init_schema()
    router = Router(settings, store)

    assert router.should_handle(msg("在吗", at_bot=True)).call_model is True
    assert router.should_handle(msg("在吗", message_id="m2")).call_model is False


# ---- I2: token 预算与失败/旁路记账 ------------------------------------


def test_budget_blocks_when_daily_tokens_exhausted():
    settings = make_settings(budget=BudgetConfig(200, 100))
    store = Store(":memory:")
    store.init_schema()
    store.record_usage(GROUP, 1, "reply", 60, 50)
    router = Router(settings, store)
    decision = router.should_handle(msg("怎么办", message_id="m3"))
    assert decision.call_model is False
    assert decision.reason == "budget"


@pytest.mark.asyncio
async def test_brain_error_records_usage():
    svc, store, _, _ = service(brain=FakeBrain(error=BrainError("boom")))
    await svc.handle(msg("你好", message_id="u1", at_bot=True))
    calls, _ = store.usage_today(GROUP, 1, now_shanghai().date().isoformat())
    assert calls == 1


@pytest.mark.asyncio
async def test_impression_worker_records_usage():
    store = Store(":memory:")
    store.init_schema()
    store.add_message(msg("我喜欢吃鱼"))
    brain = FakeBrain()
    worker = ImpressionWorker(store, brain, every=1)
    await worker.maybe_extract(GROUP, 1)
    calls, tokens = store.usage_today(GROUP, 1, now_shanghai().date().isoformat())
    assert calls == 1
    assert tokens == 10


# ---- I3: 群指令需要管理员 ---------------------------------------------


@pytest.mark.asyncio
async def test_group_command_requires_admin():
    settings = make_settings(groups=GroupsConfig(frozenset(), frozenset(), frozenset({42})))
    svc, _, _, _ = service(settings=settings)

    await svc.handle(msg("/闭嘴", user_id=7, message_id="c1"))
    assert svc.router.is_quiet(GROUP, 1) is False

    await svc.handle(msg("/闭嘴", user_id=42, message_id="c2"))
    assert svc.router.is_quiet(GROUP, 1) is True


# ---- I4: reply 段自带的 qq 也能判定 reply_to_bot ----------------------


def test_reply_to_bot_uses_reply_segment_qq_fallback():
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 1,
        "user_id": 2,
        "message_id": "m9",
        "time": NOW,
        "sender": {"nickname": "群友"},
        "message": [
            {"type": "reply", "data": {"id": "unknown-id", "qq": 999}},
            {"type": "text", "data": {"text": "继续"}},
        ],
    }
    parsed = parse_event(event, bot_qq=999, bot_message_ids=())
    assert parsed is not None
    assert parsed.reply_to_bot is True


# ---- I6: 印象按消息条数触发 -------------------------------------------


@pytest.mark.asyncio
async def test_impression_counts_messages_not_replies():
    store = Store(":memory:")
    store.init_schema()
    brain = FakeBrain()
    worker = ImpressionWorker(store, brain, every=3)
    for i in range(3):
        store.add_message(msg(f"消息{i}", message_id=f"m{i}"))

    assert await worker.maybe_extract(GROUP, 1) == 0
    assert brain.complete_calls == 1

    await worker.maybe_extract(GROUP, 1)
    assert brain.complete_calls == 1  # 同一批消息不重复抽取


# ---- I7/I8: 模型返回归一化 --------------------------------------------


def test_speak_string_false_is_treated_as_false():
    reply = BrainClient._parse_content('{"speak": "false", "reply": ""}', 0, 0)
    assert reply.speak is False


def test_truncated_json_falls_back_instead_of_leaking_protocol():
    reply = BrainClient._parse_content('{"speak": true, "reply": "你好', 0, 0, "length")
    assert reply.reply == FALLBACK_REPLY


def test_plain_text_is_still_used_as_reply():
    reply = BrainClient._parse_content("你好呀", 0, 0)
    assert reply.speak is True
    assert reply.reply == "你好呀"


# ---- I9: 上下文锚定当前消息 -------------------------------------------


def test_context_anchors_to_current_message():
    store = Store(":memory:")
    store.init_schema()
    store.add_message(msg("问题A", message_id="a"))
    store.add_message(msg("问题B", message_id="b"))
    ctx = build_context(
        store, GROUP, 1, window_size=20, impression_topk=5, system="系统", today="2026-10-24",
        upto_message_id="a",
    )
    assert len(ctx.messages) == 1
    assert "问题A" in ctx.messages[-1]["content"]


# ---- I10: 日期口径统一 ------------------------------------------------


def test_store_today_uses_shanghai():
    from dafeiyu.timeutil import today_shanghai

    assert Store.today() == today_shanghai()


# ---- I11: 配置错误走友好退出 ------------------------------------------


def test_config_error_exits_with_message(capsys, monkeypatch, tmp_path):
    from dafeiyu import __main__ as entry

    def boom() -> None:
        raise entry.ConfigError("缺少环境变量 DEEPSEEK_API_KEY")

    monkeypatch.setattr(entry, "main", boom)
    assert entry.run() == 1
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err
