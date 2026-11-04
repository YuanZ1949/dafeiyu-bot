"""调试器批次预览：只读、与真实批次同源、预算/prompt/token 可见。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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
from dafeiyu.delivery.scheduler import BatchScheduler
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import PREVIEW_PROMPT_MAX_CHARS, PREVIEW_TRUNCATED_MARK, Service

SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=SHANGHAI)
CHAT = "qq:1001"


def make_settings(*, budget: BudgetConfig | None = None) -> Settings:
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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({CHAT})),
        budget=budget or BudgetConfig(200, 300000),
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


class CapturingBrain:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        self.prompts.append(text)
        return BrainReply(True, "在呢", "test", 10, 5)

    def reset_session(self, chat_type, chat_id) -> None:
        pass

    async def complete_detailed(self, system, messages, max_tokens=400, temperature=0.2):
        return "", 0, 0


def make_msg(
    content: str = "hi",
    *,
    message_id: str,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = CHAT,
    nickname: str = "阿鱼",
    at_bot: bool = False,
    reply_to_bot: bool = False,
    timestamp: int = 0,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id="qq:2002",
        nickname=nickname,
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=at_bot,
        reply_to_bot=reply_to_bot,
        timestamp=timestamp,
    )


def build(tmp_path, *, budget: BudgetConfig | None = None, brain=None):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(budget=budget)
    adapter = FakeAdapter()
    brain = brain or CapturingBrain()
    router = Router(settings, store, clock=lambda: NOW)
    service = Service(
        settings,
        store,
        brain,
        {"qq": adapter},
        router,
        "你是溟月",
        injection=InjectionConfig(),
    )
    return service, store, adapter, brain


def seed_batch(store: Store) -> None:
    """先钉一条已投递的旧消息当游标，再把 3 条未投递消息排进批次。"""
    store.add_message(make_msg("旧消息", message_id="seed", timestamp=int(NOW.timestamp()) - 300))
    store.mark_delivered(ChatType.GROUP, CHAT, "seed")
    base = int(NOW.timestamp()) - 120
    store.add_message(make_msg("今天风好大", message_id="m1", timestamp=base))
    store.add_message(
        make_msg("@溟月 在吗", message_id="m2", at_bot=True, timestamp=base + 30)
    )
    store.add_message(
        make_msg("那我也来", message_id="m3", nickname="阿明", timestamp=base + 60)
    )


# --------------------------------------------------------------- 空批次
def test_empty_store_returns_empty_preview(tmp_path) -> None:
    service, _store, _adapter, _brain = build(tmp_path)

    preview = service.preview_batch(ChatType.GROUP, CHAT, now=NOW)

    assert preview.messages == ()
    assert preview.addressed is False
    assert preview.current_message_id is None
    assert preview.would_call_model is False
    assert preview.prompt == ""
    assert preview.token_estimate == 0
    assert preview.window == {"queued": False}
    data = preview.as_dict()
    assert data["messages"] == []
    assert data["budget"]["action"] in ("allow", "degrade", "deny")


# --------------------------------------------------------------- 批次组成
def test_preview_lists_pending_messages_in_order(tmp_path) -> None:
    service, store, _adapter, _brain = build(tmp_path)
    seed_batch(store)

    preview = service.preview_batch(ChatType.GROUP, CHAT, now=NOW)

    assert [m.message_id for m in preview.messages] == ["m1", "m2", "m3"]
    assert preview.addressed is True
    assert preview.addressed_message_ids == ("m2",)
    assert preview.current_message_id == "m2"
    assert preview.would_call_model is True
    assert preview.token_estimate > 0
    assert preview.cost_estimate == 0.0  # 单价未配置时费用恒为 0
    assert "—— 以下是自你上次开口以来的新消息 ——" in preview.prompt
    assert "阿鱼：@溟月 在吗（@了你）" in preview.prompt

    data = preview.as_dict()
    rows = data["messages"]
    assert [row["message_id"] for row in rows] == ["m1", "m2", "m3"]
    assert [row["is_current"] for row in rows] == [False, True, False]
    assert rows[1]["at_bot"] is True
    assert rows[0]["time_label"]


def test_prompt_puts_scene_before_timeline(tmp_path) -> None:
    service, store, _adapter, _brain = build(tmp_path)
    seed_batch(store)

    prompt = service.preview_batch(ChatType.GROUP, CHAT, now=NOW).prompt

    assert prompt.startswith("【此刻】")
    assert "11-04 周三 12:00（中午）" in prompt


# --------------------------------------------------------------- 预算/强度
def test_budget_denied_marks_model_call_blocked(tmp_path) -> None:
    service, store, _adapter, _brain = build(
        tmp_path, budget=BudgetConfig(1, 300000)
    )
    store.record_usage(ChatType.GROUP, CHAT, "reply", 10, 5)
    seed_batch(store)

    preview = service.preview_batch(ChatType.GROUP, CHAT, now=NOW)

    assert preview.messages  # 消息照常列出
    assert preview.budget.action.value == "deny"
    assert preview.budget.scope == "per_chat"
    assert preview.would_call_model is False


def test_quiet_intensity_blocks_ambient_batch(tmp_path) -> None:
    from dataclasses import replace

    service, store, _adapter, _brain = build(tmp_path)
    store.add_message(make_msg("旧消息", message_id="seed"))
    store.mark_delivered(ChatType.GROUP, CHAT, "seed")
    store.add_message(make_msg("今天风好大", message_id="m1"))
    service.behavior = replace(service.behavior, chat_intensity="quiet")

    preview = service.preview_batch(ChatType.GROUP, CHAT, now=NOW)

    assert preview.messages  # 消息照常列出
    assert preview.addressed is False
    assert preview.would_call_model is False


# --------------------------------------------------------------- 只读性
def test_preview_does_not_write_anything(tmp_path) -> None:
    service, store, adapter, _brain = build(tmp_path)
    seed_batch(store)

    before_messages = store.count_messages(ChatType.GROUP, CHAT)
    before_cursor = store.last_delivered_id(ChatType.GROUP, CHAT)
    before_usage = store.usage_between("2026-11-04", "2026-11-04")
    before_pending = [
        m.message_id
        for m in store.pending_batch(ChatType.GROUP, CHAT, "m3")
    ]

    preview = service.preview_batch(ChatType.GROUP, CHAT, now=NOW)
    assert preview.messages

    assert store.count_messages(ChatType.GROUP, CHAT) == before_messages
    assert store.last_delivered_id(ChatType.GROUP, CHAT) == before_cursor
    after_usage = store.usage_between("2026-11-04", "2026-11-04")
    assert after_usage.calls == before_usage.calls
    assert after_usage.tokens == before_usage.tokens
    assert [
        m.message_id for m in store.pending_batch(ChatType.GROUP, CHAT, "m3")
    ] == before_pending
    assert adapter.sent == []


# --------------------------------------------------------------- 截断/窗口
def test_long_prompt_is_truncated(tmp_path) -> None:
    service, store, _adapter, _brain = build(tmp_path)
    store.add_message(make_msg("旧消息", message_id="seed"))
    store.mark_delivered(ChatType.GROUP, CHAT, "seed")
    store.add_message(make_msg("长" * (PREVIEW_PROMPT_MAX_CHARS + 500), message_id="m1"))

    preview = service.preview_batch(ChatType.GROUP, CHAT, now=NOW)

    assert preview.prompt_truncated is True
    assert preview.prompt.endswith(PREVIEW_TRUNCATED_MARK)
    assert len(preview.prompt) == PREVIEW_PROMPT_MAX_CHARS + len(PREVIEW_TRUNCATED_MARK)


def test_window_snapshot_reports_next_send(tmp_path) -> None:
    service, store, _adapter, _brain = build(tmp_path)
    seed_batch(store)
    scheduler = BatchScheduler(store, service.settings, on_flush=lambda *a: None)
    service.scheduler = scheduler
    scheduler.note(make_msg("在吗", message_id="m2"), addressed=True)

    preview = service.preview_batch(ChatType.GROUP, CHAT, now=NOW)

    assert preview.window["queued"] is True
    assert preview.window["addressed_first_at"] is not None
    assert preview.next_send_at == preview.window["due_at"]
    assert preview.next_send_at is not None

    # 未排队的会话只回 queued=False，不凭空建窗口。
    assert scheduler.window_snapshot(ChatType.GROUP, "qq:404") == {"queued": False}


# --------------------------------------------------------------- 与真实批次对齐
async def test_preview_matches_real_batch_prompt(tmp_path, monkeypatch) -> None:
    from dafeiyu import service as service_module

    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    brain = CapturingBrain()
    service, store, adapter, _brain = build(tmp_path, brain=brain)
    seed_batch(store)

    preview = service.preview_batch(ChatType.GROUP, CHAT, now=NOW)
    await service._run_batch_locked(ChatType.GROUP, CHAT)

    assert brain.prompts == [preview.prompt]
    assert adapter.sent == [(ChatType.GROUP, CHAT, "在呢")]


async def test_preview_does_not_touch_scheduler(tmp_path) -> None:
    service, store, _adapter, _brain = build(tmp_path)
    seed_batch(store)
    scheduler = BatchScheduler(store, service.settings, on_flush=lambda *a: None)
    service.scheduler = scheduler
    scheduler.note(make_msg("在吗", message_id="m2", at_bot=True), addressed=True)
    before = dict(scheduler._windows[(ChatType.GROUP, CHAT)].__dict__)

    service.preview_batch(ChatType.GROUP, CHAT, now=NOW)
    await asyncio.sleep(0)

    assert dict(scheduler._windows[(ChatType.GROUP, CHAT)].__dict__) == before
    assert scheduler.pending_chats() == [(ChatType.GROUP, CHAT)]
