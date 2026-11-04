"""机会检测（规则版）：纯热聊/纯表情批次先攒着不投，等点名或问句再一起送。

规则来自 spec §4.3：`analyze()` 的机会分 ≤ 0.2 且没被点名时不调用模型，
也不 `mark_skipped`——消息留在待投队列，游标不动；等下一次被点名时一起进 prompt。
"""

from __future__ import annotations

import time

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
from dafeiyu.moment import analyze
from dafeiyu.router import Router
from dafeiyu.service import Service

GROUP = "qq:1001"


def make_settings(*, opportunity_guard: bool = True) -> Settings:
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
            "溟月", ("大肥鱼",), 120, 300, 0.05,
            opportunity_guard=opportunity_guard,
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


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return "bot-1"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class FakeBrain:
    def __init__(self, text: str = "在呢") -> None:
        self.text = text
        self.prompts: list[str] = []

    @property
    def reply_calls(self) -> int:
        return len(self.prompts)

    @property
    def last_prompt(self) -> str:
        return self.prompts[-1]

    async def reply(self, chat_type, chat_id, text: str, *, on_tool=None) -> BrainReply:
        self.prompts.append(text)
        return BrainReply(True, self.text, "test", 100, 50)

    def reset_session(self, chat_type, chat_id) -> None:
        pass


def build(tmp_path, *, opportunity_guard: bool = True):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(opportunity_guard=opportunity_guard)
    adapter = FakeAdapter()
    brain = FakeBrain()
    router = Router(settings, store)
    service = Service(
        settings,
        store,
        brain,
        {"qq": adapter},
        router,
        "你是溟月",
        injection=InjectionConfig(),
    )
    return service, store, brain, adapter


def make_msg(
    content: str,
    *,
    message_id: str,
    at_bot: bool = False,
    timestamp: int | None = None,
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=GROUP,
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=at_bot,
        timestamp=int(time.time()) if timestamp is None else timestamp,
    )


def seed_cursor(store: Store) -> None:
    """先投递一条旧消息，建立游标；待投队列的语义从游标之后开始算。"""
    store.add_message(
        make_msg("以前聊过的", message_id="seed", timestamp=int(time.time()) - 3600)
    )
    store.mark_delivered(ChatType.GROUP, GROUP, "seed")


def add_hot_chat(store: Store, *, count: int = 6, prefix: str = "h") -> list[str]:
    """灌一批 60 秒内的纯聊天：没有问句、没有点名，机会分低于阈值。"""
    ids = []
    for i in range(count):
        mid = f"{prefix}{i}"
        store.add_message(make_msg(f"今天这鱼缸真好看{i}", message_id=mid))
        ids.append(mid)
    return ids


async def test_hot_chat_batch_is_held_without_marking_skipped(tmp_path):
    service, store, brain, _ = build(tmp_path)
    seed_cursor(store)
    ids = add_hot_chat(store)

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 0
    # 攒着不投不是终局：既没写 skipped_reason，游标也停在 seed。
    assert all(store.skip_reason(ChatType.GROUP, GROUP, mid) is None for mid in ids)
    assert [
        m.message_id for m in store.pending_batch(ChatType.GROUP, GROUP, ids[-1])
    ] == ids


async def test_held_batch_goes_out_with_next_addressed_message(tmp_path):
    service, store, brain, _ = build(tmp_path)
    seed_cursor(store)
    ids = add_hot_chat(store, count=5)
    await service._run_batch(ChatType.GROUP, GROUP)
    assert brain.reply_calls == 0

    store.add_message(make_msg("@溟月 你觉得呢", message_id="ask", at_bot=True))
    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 1
    # 被吞掉的热聊也要一起进 prompt，接话链不能断。
    assert "今天这鱼缸真好看0" in brain.last_prompt
    assert "@溟月 你觉得呢" in brain.last_prompt
    assert store.last_delivered_id(ChatType.GROUP, GROUP) is not None
    assert ids


async def test_question_batch_is_not_held(tmp_path):
    service, store, brain, _ = build(tmp_path)
    seed_cursor(store)
    for i in range(5):
        store.add_message(make_msg(f"这个怎么弄{i}？", message_id=f"q{i}"))

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 1


async def test_single_short_message_is_not_held(tmp_path):
    """单条短句不走机会检测：短请求也可能是正事，照旧交给模型判断。"""
    service, store, brain, _ = build(tmp_path)
    seed_cursor(store)
    store.add_message(make_msg("哦", message_id="s0"))

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 1


async def test_backlog_cap_forces_hot_chat_through(tmp_path):
    """待投接近 20 条上限时不再攒，否则热聊会把队列越撑越大。"""
    service, store, brain, _ = build(tmp_path)
    seed_cursor(store)
    add_hot_chat(store, count=25)

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 1


async def test_opportunity_guard_can_be_disabled(tmp_path):
    service, store, brain, _ = build(tmp_path, opportunity_guard=False)
    seed_cursor(store)
    add_hot_chat(store)

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 1


async def test_preview_reports_opportunity_hold(tmp_path):
    """批次预览要和真实 flush 一致：纯热聊批次不该显示「会调用模型」。"""
    service, store, _brain, _ = build(tmp_path)
    seed_cursor(store)
    add_hot_chat(store)

    assert service.preview_batch(ChatType.GROUP, GROUP).would_call_model is False

    other_root = tmp_path / "other"
    other_root.mkdir()
    other, other_store, _b, _ = build(other_root, opportunity_guard=False)
    seed_cursor(other_store)
    add_hot_chat(other_store)
    assert other.preview_batch(ChatType.GROUP, GROUP).would_call_model is True


def _recent_human_msgs(count: int = 6):
    return [make_msg(f"今天这鱼缸真好看{i}", message_id=f"h{i}") for i in range(count)]


def test_pending_loop_hint_beats_hot_interjection():
    signals = analyze(
        _recent_human_msgs(6), now_ts=int(time.time()), pending_loop="显卡驱动那个坑"
    )
    assert signals.hot is True
    hint = signals.hint()
    assert "没下文" in hint
    assert "显卡驱动那个坑" in hint
    assert "别插嘴" not in hint


def test_pending_loop_empty_keeps_original_hint():
    signals = analyze(_recent_human_msgs(6), now_ts=int(time.time()), pending_loop="")
    assert signals.hot is True
    assert signals.hint() == "【时机】群里正聊得热，除非你有明确观点，否则别插嘴。"


def test_pending_loop_addressed_early_return_wins():
    signals = analyze(
        _recent_human_msgs(6),
        now_ts=int(time.time()),
        addressed=True,
        pending_loop="显卡驱动那个坑",
    )
    assert signals.hint() == ""
