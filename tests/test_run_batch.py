"""Task 12：`_run_batch` —— 预算分层、purpose、轮换、提示语/补话、容量检查。"""

from datetime import date, datetime, timedelta, timezone

from dafeiyu.brain.client import BrainError, BrainReply
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
from dafeiyu.memory.workspace import Workspace
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import BRAIN_ERROR_REPLY, FOLLOWUP_REPLY, Service
from dafeiyu.timeutil import today_shanghai

SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)


def yesterday() -> str:
    # 记账与摘要都按「今天（上海）」算，不能用夹具里的 NOON 日期。
    return (date.fromisoformat(today_shanghai()) - timedelta(days=1)).isoformat()


def make_settings(
    *, budget: BudgetConfig | None = None, memory: MemoryConfig | None = None
) -> Settings:
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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({2002})),
        budget=budget or BudgetConfig(200, 300000),
        memory=memory or MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, int, str]] = []

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: int, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def texts(self) -> list[str]:
        return [text for _, _, text in self.sent]


class FakeBrain:
    def __init__(
        self,
        *,
        speak: bool = True,
        text: str = "在呢",
        error: bool = False,
        fire_tool: bool = False,
        summary: str = "大家在聊鱼缸和水草",
    ) -> None:
        self.speak = speak
        self.text = text
        self.error = error
        self.fire_tool = fire_tool
        self.summary = summary
        self.prompts: list[str] = []
        self.reset_calls = 0

    @property
    def reply_calls(self) -> int:
        return len(self.prompts)

    @property
    def last_prompt(self) -> str:
        return self.prompts[-1]

    async def reply(self, chat_type, chat_id, text: str, *, on_tool=None) -> BrainReply:
        self.prompts.append(text)
        if on_tool is not None and self.fire_tool:
            await on_tool("webfetch")
        if self.error:
            raise BrainError("boom")
        return BrainReply(self.speak, self.text, "test", 100, 50)

    def reset_session(self, chat_type, chat_id) -> None:
        self.reset_calls += 1

    async def complete_detailed(self, system, messages, max_tokens=400, temperature=0.2):
        return self.summary, 10, 5

    async def aclose(self) -> None:
        pass


def make_msg(
    content: str = "hi",
    *,
    message_id: str = "m0",
    chat_type: ChatType = ChatType.GROUP,
    chat_id: int = 1001,
    user_id: int = 2002,
    at_bot: bool = False,
    reply_to_bot: bool = False,
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
        at_bot=at_bot,
        reply_to_bot=reply_to_bot,
        timestamp=0,
    )


def build(
    tmp_path,
    *,
    brain: FakeBrain | None = None,
    budget: BudgetConfig | None = None,
    memory: MemoryConfig | None = None,
    with_workspace: bool = False,
    injection: InjectionConfig | None = None,
    sleep=None,
):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(budget=budget, memory=memory)
    adapter = FakeAdapter()
    brain = brain or FakeBrain()
    router = Router(settings, store, clock=lambda: NOON)

    workspace = None
    if with_workspace:
        workspace = Workspace(tmp_path / "ws")
        workspace.ensure()
        workspace.global_path().write_bytes(
            "# 大肥鱼的长期记忆\n喜欢水草和沉木\n".encode()
        )

    service = Service(
        settings,
        store,
        brain,
        adapter,
        router,
        "你是溟月",
        workspace=workspace,
        injection=injection or InjectionConfig(),
        sleep=sleep,
    )

    # 先钉一条「上线前」的消息当游标下限，这样 m0/m1 会一起进批次。
    store.add_message(make_msg("上线前的旧消息", message_id="seed"))
    store.mark_delivered(ChatType.GROUP, 1001, "seed")
    store.add_message(make_msg("第一批第一条", message_id="m0"))
    store.add_message(make_msg("第一批第二条", message_id="m1"))
    return service, store, brain, adapter


def batch_ids(service) -> list[str]:
    latest = service.store.recent_messages(ChatType.GROUP, 1001, 1)
    if not latest:
        return []
    return [
        m.message_id
        for m in service.store.pending_batch(ChatType.GROUP, 1001, latest[0].message_id)
    ]


async def test_batch_marks_delivered_and_records_ambient(tmp_path):
    service, store, _brain, adapter = build(tmp_path)

    await service._run_batch(ChatType.GROUP, 1001)

    assert batch_ids(service) == []
    calls, _ = store.usage_today_by_purpose(
        ChatType.GROUP, 1001, store.today(), ("ambient",)
    )
    assert calls == 1
    assert adapter.sent


async def test_batch_injects_recalled_old_messages_when_enabled(tmp_path):
    service, store, brain, _ = build(
        tmp_path,
        memory=MemoryConfig(1, 5, 20),
        injection=InjectionConfig(recall_enabled=True),
    )
    store.add_message(make_msg("显卡驱动坏了", message_id="old"))
    store.mark_delivered(ChatType.GROUP, 1001, "old")
    store.add_message(make_msg("显卡驱动怎么装", message_id="new"))

    await service._run_batch(ChatType.GROUP, 1001)

    assert "【相关旧记忆】" in brain.last_prompt
    assert "显卡驱动坏了" in brain.last_prompt


async def test_batch_does_not_inject_recalled_old_messages_by_default(tmp_path):
    service, store, brain, _ = build(tmp_path, memory=MemoryConfig(1, 5, 20))
    store.add_message(make_msg("显卡驱动坏了", message_id="old"))
    store.mark_delivered(ChatType.GROUP, 1001, "old")
    store.add_message(make_msg("显卡驱动怎么装", message_id="new"))

    await service._run_batch(ChatType.GROUP, 1001)

    assert "【相关旧记忆】" not in brain.last_prompt
    assert "显卡驱动坏了" not in brain.last_prompt


async def test_budget_exhausted_skips_without_calling_brain(tmp_path):
    service, store, brain, _adapter = build(
        tmp_path, budget=BudgetConfig(daily_calls=1, daily_tokens=300000)
    )
    store.record_usage(ChatType.GROUP, 1001, "ambient", 1, 1)

    await service._run_batch(ChatType.GROUP, 1001)

    assert brain.reply_calls == 0
    assert store.skip_reason(ChatType.GROUP, 1001, "m0") == "budget"
    assert batch_ids(service) == []


async def test_addressed_batch_uses_reply_purpose(tmp_path):
    service, store, brain, _ = build(tmp_path)

    # 走 handle 才能真正验证「点名状态从入口传到批次」，而不是测试里手工塞标志。
    await service.handle(
        make_msg("你刚才说啥", message_id="m2", reply_to_bot=True)
    )
    await service._run_batch(ChatType.GROUP, 1001)

    calls, _ = store.usage_today_by_purpose(
        ChatType.GROUP, 1001, store.today(), ("reply",)
    )
    assert calls == 1
    # 引用回复与 @ 是两种不同的点名，prompt 里要能区分。
    assert "（引用回复你）" in brain.last_prompt
    assert "（@了你）" not in brain.last_prompt


async def test_at_bot_batch_marks_at(tmp_path):
    service, _store, brain, _ = build(tmp_path)

    await service.handle(make_msg("在吗", message_id="m2", at_bot=True))
    await service._run_batch(ChatType.GROUP, 1001)

    assert "（@了你）" in brain.last_prompt
    assert "（引用回复你）" not in brain.last_prompt


async def test_addressed_state_survives_service_restart(tmp_path):
    service, store, brain, adapter = build(tmp_path)

    await service.handle(make_msg("在吗", message_id="m2", at_bot=True))

    # 模拟进程重启：新 Service 内存里没有任何点名记录，只能从库里读。
    restarted = Service(
        service.settings,
        store,
        brain,
        adapter,
        service.router,
        "你是溟月",
        injection=InjectionConfig(),
    )
    await restarted._run_batch(ChatType.GROUP, 1001)

    calls, _ = store.usage_today_by_purpose(
        ChatType.GROUP, 1001, store.today(), ("reply",)
    )
    assert calls == 1
    assert "（@了你）" in brain.last_prompt


async def test_brain_error_does_not_requeue_and_sends_fallback(tmp_path):
    service, store, _, adapter = build(tmp_path, brain=FakeBrain(error=True))

    await service._run_batch(ChatType.GROUP, 1001)

    assert BRAIN_ERROR_REPLY in adapter.texts()
    assert batch_ids(service) == []
    assert store.skip_reason(ChatType.GROUP, 1001, "m0") == "error"


async def test_hint_then_speak_false_sends_followup(tmp_path):
    service, _, _, adapter = build(
        tmp_path, brain=FakeBrain(speak=False, text="", fire_tool=True)
    )

    await service._run_batch(ChatType.GROUP, 1001)

    texts = adapter.texts()
    assert texts[0].startswith("本鱼去网上查一下")
    assert texts[-1] == FOLLOWUP_REPLY
    assert FOLLOWUP_REPLY.startswith("没查到")


async def test_rotation_runs_before_model_call(tmp_path):
    service, store, brain, _ = build(tmp_path)
    store.bump_session_stats(ChatType.GROUP, 1001, messages=60, tokens=0)

    await service._run_batch(ChatType.GROUP, 1001)

    assert brain.reset_calls == 1
    assert "上次聊到" in brain.last_prompt
    assert "【接续】" in brain.last_prompt


async def test_continuation_from_db_survives_restart(tmp_path):
    """重启后内存字典空：接续语要从 session_summaries 捞回来，且只注入一次。"""
    service, store, brain, _ = build(tmp_path)
    store.add_session_summary(ChatType.GROUP, 1001, "上次聊到鱼缸和水草")

    await service._run_batch(ChatType.GROUP, 1001)

    assert "【接续】" in brain.last_prompt
    assert "上次聊到鱼缸和水草" in brain.last_prompt

    # 第二轮同一进程：已消费，不再重复注入（哪怕库里那条还在）。
    store.add_message(make_msg("再说一句", message_id="m2"))
    await service._run_batch(ChatType.GROUP, 1001)

    assert "【接续】" not in brain.last_prompt
    assert "上次聊到鱼缸和水草" not in brain.last_prompt


async def test_db_continuation_is_not_consumed_on_brain_error(tmp_path):
    brain = FakeBrain(error=True)
    service, store, brain, _ = build(tmp_path, brain=brain)
    store.add_session_summary(ChatType.GROUP, 1001, "上次聊到鱼缸和水草")

    await service._run_batch(ChatType.GROUP, 1001)

    # 失败不消费：下一批仍要带上接续语。
    brain.error = False
    store.add_message(make_msg("再来一次", message_id="m2"))
    await service._run_batch(ChatType.GROUP, 1001)

    assert "上次聊到鱼缸和水草" in brain.last_prompt


async def test_new_rotation_continuation_reinjects_after_consumed(tmp_path):
    service, store, brain, _ = build(tmp_path)
    store.add_session_summary(ChatType.GROUP, 1001, "第一次接续")

    await service._run_batch(ChatType.GROUP, 1001)
    assert "第一次接续" in brain.last_prompt

    # 下一轮触发轮换：新接续语要重新注入，不能被「已消费」挡住。
    store.bump_session_stats(ChatType.GROUP, 1001, messages=60, tokens=0)
    store.add_message(make_msg("继续聊", message_id="m2"))
    await service._run_batch(ChatType.GROUP, 1001)

    assert brain.reset_calls == 1
    assert "【接续】" in brain.last_prompt
    assert "第一次接续" not in brain.last_prompt
    assert "大家在聊鱼缸和水草" in brain.last_prompt


async def test_prompt_contains_all_segments_and_timeline(tmp_path):
    service, store, brain, _ = build(tmp_path, with_workspace=True)
    store.upsert_summary(ChatType.GROUP, 1001, yesterday(), "昨天聊了鱼缸")
    store.upsert_impression(ChatType.GROUP, 1001, 2002, "trait", "阿鱼：爱养鱼", 0.9)

    await service._run_batch(ChatType.GROUP, 1001)

    prompt = brain.last_prompt
    assert prompt.index("【长期记忆】") < prompt.index(
        "—— 以下是自你上次开口以来的新消息 ——"
    )
    assert "【昨日摘要】" in prompt and "昨天聊了鱼缸" in prompt
    assert "【群友印象】" in prompt and "阿鱼：爱养鱼" in prompt
    assert "第一批第二条" in prompt


async def test_recalled_anchor_skips_batch(tmp_path):
    service, store, brain, adapter = build(tmp_path)
    # 触发本轮的最后一条被撤回了：整批不回复，免得对着空气说话。
    store.mark_recalled(ChatType.GROUP, 1001, "m1")

    await service._run_batch(ChatType.GROUP, 1001)

    assert brain.reply_calls == 0
    assert adapter.texts() == []


async def test_downshift_skips_batch(tmp_path):
    service, _store, brain, adapter = build(tmp_path)
    for _ in range(3):
        service.router.note_no_reply(ChatType.GROUP, 1001)

    await service._handle_batched(make_msg("随便聊聊", message_id="m2"))

    assert brain.reply_calls == 0
    assert adapter.texts() == []


async def test_multi_sentence_reply_is_sent_in_segments(tmp_path):
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    brain = FakeBrain(
        text=(
            "结论是这个方案能跑，配置和依赖都没问题，今天就可以直接上线。"
            "原因有两个，一个是开关没打开，一个是网络不稳，这两点都确认过了。"
        )
    )
    service, _store, _brain, adapter = build(tmp_path, brain=brain, sleep=fake_sleep)

    await service._run_batch(ChatType.GROUP, 1001)

    assert len(adapter.texts()) == 2
    assert delays == [0.8]


async def test_short_reply_stays_single_message(tmp_path):
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    service, _store, _brain, adapter = build(tmp_path, sleep=fake_sleep)

    await service._run_batch(ChatType.GROUP, 1001)

    assert len(adapter.texts()) == 1
    assert delays == []


async def test_private_reply_is_segmented(tmp_path):
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    brain = FakeBrain(
        text=(
            "结论是这个方案能跑，配置和依赖都没问题，今天就可以直接上线。"
            "原因有两个，一个是开关没打开，一个是网络不稳，这两点都确认过了。"
        )
    )
    service, _store, _brain, adapter = build(tmp_path, brain=brain, sleep=fake_sleep)

    await service._handle_batched(
        make_msg("在吗", message_id="p1", chat_type=ChatType.PRIVATE, chat_id=2002)
    )

    assert len(adapter.texts()) == 2
    assert delays == [0.8]
