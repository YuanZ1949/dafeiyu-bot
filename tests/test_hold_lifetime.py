"""攒着寿命：到点放行、0 = 关闭、未知时间不算年龄、预览与真跑同口径。

规则位置：`Service._hold_outcome`（攒着 / 超龄放行 / 本就不攒），
`_run_batch_locked` 与 `preview_batch` 共用它，避免两边判定分叉。
"""

from __future__ import annotations

import time

from dafeiyu.behavior import BehaviorSettings
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
from dafeiyu.service import Service
from dafeiyu.timeutil import now_shanghai

GROUP = "qq:1001"
LONG_LINE = "今天这缸水草长得真好看"


def make_settings(*, hold_max_seconds: int = 300) -> Settings:
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
            "溟月",
            ("大肥鱼",),
            120,
            300,
            0.05,
            hold_max_seconds=hold_max_seconds,
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
    def __init__(self) -> None:
        self.prompts: list[str] = []

    @property
    def reply_calls(self) -> int:
        return len(self.prompts)

    async def reply(self, chat_type, chat_id, text: str, *, on_tool=None) -> BrainReply:
        self.prompts.append(text)
        return BrainReply(True, "在呢", "test", 100, 50)

    def reset_session(self, chat_type, chat_id) -> None:
        pass


def build(tmp_path, *, hold_max_seconds: int = 300):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(hold_max_seconds=hold_max_seconds)
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


def make_msg(content: str, *, message_id: str, ts: int, is_bot: bool = False) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=GROUP,
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=is_bot,
        timestamp=ts,
    )


def seed_cursor(store: Store) -> None:
    """投递一条旧消息钉住游标，之后的才算待投。"""
    store.add_message(
        make_msg("以前聊过的", message_id="seed", ts=int(time.time()) - 3600)
    )
    store.mark_delivered(ChatType.GROUP, GROUP, "seed")


def add_messages(
    store: Store, prefix: str, count: int, *, ts: int
) -> list[str]:
    ids = []
    for i in range(count):
        mid = f"{prefix}{i}"
        store.add_message(make_msg(f"{LONG_LINE}{i}", message_id=mid, ts=ts))
        ids.append(mid)
    return ids


def last_decision(store: Store) -> dict:
    rows = store.list_decisions(limit=1, chat_id=GROUP)
    assert rows, "预期有决策留痕"
    return rows[0]


def find_checkpoint(store: Store, checkpoint: str) -> dict:
    """放行会留两条决策（超龄放行 + 正常的 replied/silent），按 checkpoint 找。"""
    for row in store.list_decisions(limit=10, chat_id=GROUP):
        if row["checkpoint"] == checkpoint:
            return row
    raise AssertionError(f"没有 checkpoint={checkpoint} 的决策：{store.list_decisions(limit=10, chat_id=GROUP)}")


async def test_hot_chat_is_held_before_the_limit(tmp_path):
    service, store, brain, _ = build(tmp_path, hold_max_seconds=300)
    seed_cursor(store)
    add_messages(store, "h", 6, ts=int(time.time()))

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 0
    decision = last_decision(store)
    assert decision["outcome"] == "held"
    assert decision["checkpoint"] == "opportunity_hold"


async def test_hot_chat_is_released_once_past_the_limit(tmp_path):
    service, store, brain, _ = build(tmp_path, hold_max_seconds=300)
    seed_cursor(store)
    ids = add_messages(store, "old", 1, ts=int(time.time()) - 400)
    add_messages(store, "h", 5, ts=int(time.time()))

    await service._run_batch(ChatType.GROUP, GROUP)

    # 到点了就不再攒着：照旧送模型判断，不丢消息。
    assert brain.reply_calls == 1
    decision = find_checkpoint(store, "opportunity_hold_expired")
    assert decision["outcome"] == "held"
    assert decision["detail"]["age_seconds"] >= 300
    # 放行不是丢弃：没有 skipped_reason，消息进了正常投递管线。
    assert store.skip_reason(ChatType.GROUP, GROUP, ids[0]) is None
    assert store.pending_batch(ChatType.GROUP, GROUP, ids[-1]) == []


async def test_zero_disables_expiry(tmp_path):
    service, store, brain, _ = build(tmp_path, hold_max_seconds=0)
    seed_cursor(store)
    add_messages(store, "old", 1, ts=int(time.time()) - 400)
    add_messages(store, "h", 5, ts=int(time.time()))

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 0
    assert last_decision(store)["checkpoint"] == "opportunity_hold"


async def test_quiet_intensity_is_held_until_the_limit(tmp_path):
    service, store, brain, _ = build(tmp_path, hold_max_seconds=300)
    service.set_behavior(BehaviorSettings(chat_intensity="quiet"))
    seed_cursor(store)
    add_messages(store, "q", 3, ts=int(time.time()) - 30)

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 0
    assert last_decision(store)["checkpoint"] == "intensity_quiet"


async def test_quiet_intensity_releases_past_the_limit(tmp_path):
    """安静强度没有条数上限，这才是寿命真正生效的地方。"""
    service, store, brain, _ = build(tmp_path, hold_max_seconds=300)
    service.set_behavior(BehaviorSettings(chat_intensity="quiet"))
    seed_cursor(store)
    add_messages(store, "q", 3, ts=int(time.time()) - 400)

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 1
    decision = find_checkpoint(store, "intensity_quiet_expired")
    assert decision["detail"]["age_seconds"] >= 300


async def test_unknown_timestamp_is_not_an_age(tmp_path):
    service, store, _, _ = build(tmp_path, hold_max_seconds=1)
    seed_cursor(store)
    add_messages(store, "unknown", 1, ts=0)

    expired, age = service._hold_expired(
        ChatType.GROUP, GROUP, int(time.time())
    )

    # timestamp=0 是「时间未知」，不能当成 1970 年立刻到期。
    assert (expired, age) == (False, None)
    assert store.oldest_undelivered_ts(ChatType.GROUP, GROUP) is None


async def test_preview_agrees_with_the_real_run(tmp_path):
    service, store, _, _ = build(tmp_path, hold_max_seconds=300)
    seed_cursor(store)
    add_messages(store, "old", 1, ts=int(time.time()) - 400)
    add_messages(store, "h", 5, ts=int(time.time()))

    preview = service.preview_batch(ChatType.GROUP, GROUP, now=now_shanghai())

    assert preview.would_call_model is True


async def test_preview_still_blocks_before_the_limit(tmp_path):
    service, store, _, _ = build(tmp_path, hold_max_seconds=300)
    seed_cursor(store)
    add_messages(store, "h", 6, ts=int(time.time()))

    preview = service.preview_batch(ChatType.GROUP, GROUP, now=now_shanghai())

    assert preview.would_call_model is False


async def test_prompt_carries_the_age_note_past_the_limit(tmp_path):
    service, store, brain, _ = build(tmp_path, hold_max_seconds=300)
    seed_cursor(store)
    add_messages(store, "old", 1, ts=int(time.time()) - 600)
    add_messages(store, "h", 5, ts=int(time.time()))

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 1
    assert "这批消息已经攒了" in brain.prompts[0]
    assert "话题可能已经结束" in brain.prompts[0]


async def test_prompt_has_no_age_note_before_the_limit(tmp_path):
    service, store, brain, _ = build(tmp_path, hold_max_seconds=300)
    seed_cursor(store)
    add_messages(store, "h", 6, ts=int(time.time()))
    # 让这一批不被攒着，直接走到模型（加一个问句）。
    store.add_message(make_msg("这个怎么弄？", message_id="ask", ts=int(time.time())))

    await service._run_batch(ChatType.GROUP, GROUP)

    assert brain.reply_calls == 1
    assert "这批消息已经攒了" not in brain.prompts[0]


def test_age_note_is_silent_when_lifetime_is_disabled(tmp_path):
    service, store, _, _ = build(tmp_path, hold_max_seconds=0)
    seed_cursor(store)
    add_messages(store, "old", 3, ts=int(time.time()) - 10_000)

    assert service._hold_age_note(store.pending_batch(ChatType.GROUP, GROUP, "old2")) == ""


def test_age_note_uses_the_batch_not_the_backlog(tmp_path):
    """积压里更早的消息不在 prompt 里，年龄不能按它算。"""
    service, store, _, _ = build(tmp_path, hold_max_seconds=300)
    seed_cursor(store)
    add_messages(store, "backlog", 2, ts=int(time.time()) - 10_000)
    add_messages(store, "batch", 20, ts=int(time.time()))

    batch = store.pending_batch(ChatType.GROUP, GROUP, "batch19")
    assert batch[0].message_id == "batch0"  # 老的落在批次窗口之外

    note = service._hold_age_note(batch)

    assert note == ""


async def test_twenty_message_ceiling_wins_over_the_lifetime(tmp_path):
    """热聊路径天然被 PENDING_BATCH_LIMIT 封顶：20 条就不再攒，用不到 300 秒。"""
    service, store, brain, _ = build(tmp_path, hold_max_seconds=300)
    seed_cursor(store)
    add_messages(store, "h", 20, ts=int(time.time()))

    state, checkpoint, _ = service._hold_outcome(
        ChatType.GROUP,
        GROUP,
        store.pending_batch(ChatType.GROUP, GROUP, "h19"),
        addressed=False,
        backlog=0,
        now_ts=int(time.time()),
    )

    assert (state, checkpoint) == ("none", "")

    await service._run_batch(ChatType.GROUP, GROUP)
    assert brain.reply_calls == 1
