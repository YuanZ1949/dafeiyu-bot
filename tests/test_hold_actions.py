"""攒着干预动作：``release``（现在说这一次）/ ``discard``（丢弃这批）。

守两条不变量：
- ``release`` 只跳过「攒着」这一条判定，预算 / 静默 / 收尾照旧；
- ``discard`` 只标记未投递的那批（``skipped_reason="hold_discard"``），不删历史消息行。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

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
from dafeiyu.control import BrainControl, create_app
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service

GROUP = "qq:1001"
CHAT = (ChatType.GROUP, GROUP)
LONG_LINE = "今天这缸水草长得真好看"


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
        persona=PersonaConfig("溟月", ("大肥鱼",), 120, 300, 0.05, hold_max_seconds=300),
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
    connected = True

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


@asynccontextmanager
async def running(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


class Harness:
    def __init__(self, tmp_path) -> None:
        self.settings = make_settings()
        self.store = Store(str(tmp_path / "t.db"))
        self.store.init_schema()
        self.adapter = FakeAdapter()
        self.brain = FakeBrain()
        self.router = Router(self.settings, self.store)
        self.service = Service(
            self.settings,
            self.store,
            self.brain,
            {"qq": self.adapter},
            self.router,
            "你是溟月",
            injection=InjectionConfig(),
        )
        self.control = BrainControl(
            self.settings,
            self.store,
            self.service,
            self.adapter,
            runtime_path=str(tmp_path / "runtime.json"),
        )


def build(tmp_path) -> Harness:
    return Harness(tmp_path)


def make_msg(content: str, *, message_id: str, ts: int) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=GROUP,
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        timestamp=ts,
    )


def seed_cursor(store: Store) -> None:
    """投递一条旧消息钉住游标，之后的才算待投。"""
    store.add_message(
        make_msg("以前聊过的", message_id="seed", ts=int(time.time()) - 3600)
    )
    store.mark_delivered(ChatType.GROUP, GROUP, "seed")


def add_messages(store: Store, prefix: str, count: int, *, ts: int) -> list[str]:
    ids = []
    for i in range(count):
        mid = f"{prefix}{i}"
        store.add_message(make_msg(f"{LONG_LINE}{i}", message_id=mid, ts=ts))
        ids.append(mid)
    return ids


def find_checkpoint(store: Store, checkpoint: str):
    for row in store.list_decisions(limit=20, chat_id=GROUP):
        if row["checkpoint"] == checkpoint:
            return row
    raise AssertionError(
        f"没有 checkpoint={checkpoint} 的决策：{store.list_decisions(limit=20, chat_id=GROUP)}"
    )


def hold_a_batch(harness: Harness) -> list[str]:
    """造一批正在攒着的热聊（还没触发批次处理）。"""
    seed_cursor(harness.store)
    return add_messages(harness.store, "h", 6, ts=int(time.time()))


async def test_release_speaks_now_and_records_the_manual_checkpoint(tmp_path):
    harness = build(tmp_path)
    ids = hold_a_batch(harness)

    result = await harness.control.session_action("release", ChatType.GROUP, GROUP)

    assert result["ok"] is True
    assert result["count"] == len(ids)
    assert harness.brain.reply_calls == 1
    # 面板上要能看出「这条是人手动放行的」，不能和到期放行混成一条。
    find_checkpoint(harness.store, "manual_release")
    # 放行不是丢弃：没有 skipped_reason，游标被推进到这批之后。
    for mid in ids:
        assert harness.store.skip_reason(ChatType.GROUP, GROUP, mid) is None
    assert harness.store.pending_batch(ChatType.GROUP, GROUP, ids[-1]) == []


async def test_release_does_not_unhold_the_next_batch(tmp_path):
    """放行只是一次性的：下一条新消息仍按正常判定走（热聊里还会再次攒着）。"""
    harness = build(tmp_path)
    hold_a_batch(harness)

    await harness.control.session_action("release", ChatType.GROUP, GROUP)
    assert harness.brain.reply_calls == 1

    add_messages(harness.store, "again", 6, ts=int(time.time()))
    await harness.service._run_batch(ChatType.GROUP, GROUP)

    assert harness.brain.reply_calls == 1  # 没有被放行第二次
    assert find_checkpoint(harness.store, "opportunity_hold")


async def test_release_skips_only_the_hold_not_the_budget(tmp_path):
    """``force_release`` 只跳过攒着判定，预算拦截仍然生效。"""
    harness = build(tmp_path)
    hold_a_batch(harness)

    setattr(harness.service, "_chat_budget_decision", lambda *a, **k: SimpleNamespace(scope="day", reason="测试：预算已满"))
    setattr(harness.service, "_budget_allows", lambda *a, **k: False)

    await harness.control.session_action("release", ChatType.GROUP, GROUP)

    assert harness.brain.reply_calls == 0
    assert harness.store.skip_reason(ChatType.GROUP, GROUP, "h0") == "budget"
    find_checkpoint(harness.store, "budget")


async def test_discard_marks_every_undelivered_message_as_skipped(tmp_path):
    harness = build(tmp_path)
    ids = hold_a_batch(harness)

    result = await harness.control.session_action("discard", ChatType.GROUP, GROUP)

    assert result["count"] == len(ids)
    for mid in ids:
        assert harness.store.skip_reason(ChatType.GROUP, GROUP, mid) == "hold_discard"
    # 游标推进：这批不再回到待投队列。
    assert harness.store.pending_batch(ChatType.GROUP, GROUP, ids[-1]) == []
    find_checkpoint(harness.store, "manual_discard")


async def test_discard_never_reaches_the_model_and_never_deletes_history(tmp_path):
    harness = build(tmp_path)
    ids = hold_a_batch(harness)
    before = harness.store.count_messages(ChatType.GROUP, GROUP)

    await harness.control.session_action("discard", ChatType.GROUP, GROUP)

    # 丢弃 = 不投：模型一次都没被叫，prompt 里也不该出现这批内容。
    assert harness.brain.reply_calls == 0
    # 历史行还在（只写 delivered_messages，不删 messages）。
    assert harness.store.count_messages(ChatType.GROUP, GROUP) == before
    assert harness.store.recent_messages(ChatType.GROUP, GROUP, len(ids) + 1)


async def test_discard_clears_more_than_the_twenty_message_window(tmp_path):
    """安静强度积压 50 条时，丢弃必须清完全部未投递，而不是只清 20 条。"""
    harness = build(tmp_path)
    harness.service.set_behavior(BehaviorSettings(chat_intensity="quiet"))
    seed_cursor(harness.store)
    ids = add_messages(harness.store, "q", 50, ts=int(time.time()) - 10)

    result = await harness.control.session_action("discard", ChatType.GROUP, GROUP)

    assert result["count"] == 50
    assert all(
        harness.store.skip_reason(ChatType.GROUP, GROUP, mid) == "hold_discard"
        for mid in ids
    )
    assert harness.store.pending_batch(ChatType.GROUP, GROUP, ids[-1]) == []


async def test_unknown_session_action_is_still_rejected_over_http(tmp_path):
    harness = build(tmp_path)
    app = create_app(harness.control, "", None)
    async with running(app) as client:
        resp = await client.post("/api/sessions/group/100/rotate")
        data = await resp.json()

    assert resp.status == 400
    assert data["error"] == "未知会话操作"


async def test_new_and_forget_keep_their_behaviour(tmp_path):
    harness = build(tmp_path)
    hold_a_batch(harness)
    app = create_app(harness.control, "", None)

    async with running(app) as client:
        new = await client.post("/api/sessions/group/100/new")
        forget = await client.post("/api/sessions/group/100/forget")
        new_body = await new.json()
        forget_body = await forget.json()

    assert new.status == 200
    assert new_body["action"] == "new"
    assert forget.status == 200
    assert forget_body["action"] == "forget"
