"""model 模式：handle 只入库+排批，不 await 模型；rules 模式保持同步旧路径。"""

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


class FakeAdapterWithoutIds(FakeAdapter):
    """模拟网关拿不到 message_id 的退化情况。"""

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> None:
        self.sent.append((chat_type, chat_id, text))


class FakeOpenCodeBrain:
    def __init__(self, *, delay: float = 0.0, speak: bool = True, text: str = "在呢") -> None:
        self.delay = delay
        self.speak = speak
        self.text = text
        self.calls: list[tuple[ChatType, str, str]] = []

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


class FakeStatelessBrain:
    """有 reply、但要靠 service 显式给历史 —— 对应真实 BrainClient。"""

    accepts_history = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def reply(
        self, chat_type, chat_id, text, *, system, history, on_tool=None
    ) -> BrainReply:
        self.calls.append(
            {
                "chat_type": chat_type,
                "chat_id": chat_id,
                "prompt": text,
                "system": system,
                "history": list(history),
            }
        )
        return BrainReply(True, "在呢", "test", 10, 5)

    async def aclose(self) -> None:
        pass


def make_msg(
    content: str = "hi",
    *,
    message_id: str = "m1",
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = "qq:1001",
    user_id: str = "qq:2002",
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
    service = Service(settings, store, brain, {"qq": adapter}, router, "你是溟月")
    return service, store, brain, adapter


async def test_handle_returns_before_model(tmp_path):
    service, _, brain, _ = build(tmp_path, brain=FakeOpenCodeBrain(delay=1.0))

    # 点名的消息在 rules 模式会同步 await 模型；model 模式必须立刻返回。
    await asyncio.wait_for(service.handle(make_msg("@溟月 在吗")), timeout=0.2)

    assert brain.reply_calls == 0


async def test_private_still_replies_immediately(tmp_path):
    service, _, brain, adapter = build(tmp_path)

    await service.handle(
        make_msg("hi", chat_type=ChatType.PRIVATE, chat_id="qq:2002", message_id="p1")
    )

    assert brain.reply_calls == 1
    assert adapter.sent


async def test_reply_stored_with_gateway_message_id(tmp_path):
    """bot 回复入库要用网关返回的真实 id，而不是合成值。"""
    service, store, _, adapter = build(tmp_path)

    await service.handle(
        make_msg("hi", chat_type=ChatType.PRIVATE, chat_id="qq:2002", message_id="p1")
    )

    bot = [m for m in store.recent_messages(ChatType.PRIVATE, "qq:2002", 5) if m.is_bot]
    assert [m.message_id for m in bot] == ["bot-1"]
    assert adapter.sent


async def test_reply_falls_back_to_synthetic_id_without_gateway_id(tmp_path):
    """网关拿不到 id 时回退合成值，保证历史顺序不断。"""
    adapter = FakeAdapterWithoutIds()
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(settings, store, FakeOpenCodeBrain(), {"qq": adapter}, router, "你是溟月")

    await service.handle(
        make_msg("hi", chat_type=ChatType.PRIVATE, chat_id="qq:2002", message_id="p1")
    )

    bot = [m for m in store.recent_messages(ChatType.PRIVATE, "qq:2002", 5) if m.is_bot]
    assert [m.message_id for m in bot] == ["p1:bot:0"]


async def test_quiet_hours_skips_with_reason(tmp_path):
    service, store, _, _ = build(tmp_path, clock=NIGHT)

    await service.handle(make_msg("hi"))

    assert store.skip_reason(ChatType.GROUP, "qq:1001", "m1") == "quiet"
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


async def test_stateless_brain_runs_through_same_batch_pipeline(tmp_path):
    brain = FakeStatelessBrain()
    service, _, _, _ = build(tmp_path, brain=brain, batch_seconds=0.0)

    await service.handle(make_msg("第一句", message_id="m1"))
    await service.drain()

    assert service.scheduler is not None
    assert len(brain.calls) == 1
    call = brain.calls[0]
    # 和 opencode 同一份 prompt 构建：注入段 + 时间线。
    assert "—— 以下是自你上次开口以来的新消息 ——" in call["prompt"]
    assert "第一句" in call["prompt"]
    assert call["system"]


def test_tools_enabled_requires_tool_brain_and_workspace(tmp_path) -> None:
    """只有「大脑支持工具 + 工作区开着」才把查网页/记笔记写进 system。"""
    brain = FakeStatelessBrain()
    service, _, _, _ = build(tmp_path, brain=brain)

    assert service._tools_enabled() is False

    brain.supports_tools = True  # type: ignore[attr-defined]
    assert service._tools_enabled() is False  # workspace 关着

    service.workspace = object()  # type: ignore[assignment]
    assert service._tools_enabled() is True


async def test_stateless_history_contains_prior_turns(tmp_path):
    brain = FakeStatelessBrain()
    service, store, _, _ = build(tmp_path, brain=brain)
    store.add_message(make_msg("第一句", message_id="m0"))
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id="qq:1001",
            user_id=999,
            nickname="溟月",
            content="旧回复",
            message_id="m0:bot:0",
            is_bot=True,
        )
    )
    current = make_msg("第二句", message_id="m1")

    history = service._history_before(ChatType.GROUP, "qq:1001", [current])

    assert [item["role"] for item in history] == ["user", "assistant"]
    assert "第一句" in history[0]["content"]
    # 带标记渲染（IS-3）：bot 行统一「你：」，不再用裸「溟月: 旧回复」。
    assert history[1]["content"] == "你：旧回复"
    assert all("第二句" not in item["content"] for item in history)


def _batch_msg(
    content: str,
    message_id: str,
    timestamp: int,
    *,
    at_bot: bool = False,
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="qq:1001",
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=at_bot,
        timestamp=timestamp,
    )


def test_build_prompt_counts_mirror_rows_via_bounded_query(tmp_path) -> None:
    """（T3 a2）转发条数来自 recent_messages(include_mirror=True) 的有界查询。

    pending_batch 只取 is_bot=0，镜像行必须另查；查询参数必须是
    upto_message_id=本批最后一条 + 一个明确的小 limit（这里是 50）。
    """
    service, store, _, _ = build(tmp_path, batch_seconds=0.0)
    t0 = int(datetime(2026, 10, 24, 11, 0, tzinfo=SHANGHAI).timestamp())
    # 镜像行：is_bot=1 + mirror=1，时间落在本批窗口之内（先入库 → id 更小）。
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id="qq:1001",
            user_id="qq:other",
            nickname="隔壁老王",
            content="从别的群搬来的话",
            message_id="mir1",
            segments=[],
            is_bot=True,
            mirror=True,
            timestamp=t0 + 30,
        )
    )
    first = _batch_msg("本批第一句", "b1", t0)
    second = _batch_msg("@溟月 在么", "b2", t0 + 60, at_bot=True)
    store.add_message(first)
    store.add_message(second)

    calls: list[tuple[int, str | None, bool]] = []
    original = store.recent_messages

    def spy(chat_type, chat_id, limit, upto_message_id=None, *, include_mirror=True):
        calls.append((limit, upto_message_id, include_mirror))
        return original(
            chat_type,
            chat_id,
            limit,
            upto_message_id=upto_message_id,
            include_mirror=include_mirror,
        )

    store.recent_messages = spy  # type: ignore[method-assign]

    prompt = service._build_prompt(ChatType.GROUP, "qq:1001", [first, second])

    # 查询有界：upto_message_id=本批最后一条、limit=50、include_mirror=True。
    assert (50, "b2", True) in calls
    # 镜像行被计入【本批】块。
    assert "跨群转发：1 条（隔壁老王）" in prompt
    assert prompt.startswith("【本批】")
    assert "说话人：阿鱼" in prompt
    assert "在跟你说话：1 条（阿鱼）" in prompt


async def test_speak_false_is_logged_as_a_decision(tmp_path, caplog):
    brain = FakeOpenCodeBrain(speak=False)
    service, _, _, _ = build(tmp_path, brain=brain, batch_seconds=0.0)

    await service.handle(make_msg("hi"))
    with caplog.at_level("INFO"):
        await service.drain()

    assert "模型选择不说" in caplog.text
    assert "reason=test" in caplog.text


async def test_drain_flushes_due_batch(tmp_path):
    service, store, brain, adapter = build(tmp_path, batch_seconds=0.0)

    await service.handle(make_msg("hi"))
    assert brain.reply_calls == 0

    await service.drain()

    assert brain.reply_calls == 1
    assert store.last_delivered_id(ChatType.GROUP, "qq:1001") is not None
    assert adapter.sent


class FakeQuotaBrain:
    """会话型大脑：第一次撞额度，靠 reply_fresh 兜住。"""

    def __init__(self, error: BrainError | None = None) -> None:
        self.error = error or BrainError(
            "OpenCode 调用失败（provider.free-limit）：Free usage limit reached",
            status=429,
        )
        self.calls: list[tuple] = []

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        self.calls.append(("reply", text))
        raise self.error

    async def reply_fresh(
        self, chat_type, chat_id, text, *, system, history, on_tool=None
    ) -> BrainReply:
        self.calls.append(("fresh", text, system, list(history)))
        return BrainReply(True, "新会话里回的", "test", 20, 5)

    async def aclose(self) -> None:
        pass


async def test_quota_failure_retries_in_a_fresh_session(tmp_path):
    brain = FakeQuotaBrain()
    service, _store, _, _ = build(tmp_path, brain=brain)

    reply = await service._call_brain(ChatType.GROUP, "qq:1001", [make_msg("hi")], "hi")

    assert reply.reply == "新会话里回的"
    assert [c[0] for c in brain.calls] == ["reply", "fresh"]
    fresh = brain.calls[1]
    assert fresh[2]
    assert isinstance(fresh[3], list)


async def test_other_errors_are_not_retried(tmp_path):
    brain = FakeQuotaBrain(error=BrainError("OpenCode 服务不可达：ConnectError"))
    service, _store, _, _ = build(tmp_path, brain=brain)

    with pytest.raises(BrainError):
        await service._call_brain(ChatType.GROUP, "qq:1001", [make_msg("hi")], "hi")

    assert [c[0] for c in brain.calls] == ["reply"]
