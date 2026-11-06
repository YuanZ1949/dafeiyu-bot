"""Task 12：`_run_batch` —— 预算分层、purpose、轮换、工具调用追踪、容量检查。"""

from datetime import date, datetime, timedelta, timezone

from dafeiyu import service as service_module
from dafeiyu.behavior import BehaviorSettings
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
from dafeiyu.models import Attachment, ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service, ToolUse
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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
        budget=budget or BudgetConfig(200, 300000),
        memory=memory or MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
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
        parts: tuple[str, ...] = (),
        error: bool = False,
        fire_tool: bool = False,
        summary: str = "大家在聊鱼缸和水草",
    ) -> None:
        self.speak = speak
        self.text = text
        self.parts = parts
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
        return BrainReply(self.speak, self.text, "test", 100, 50, parts=self.parts)

    def reset_session(self, chat_type, chat_id) -> None:
        self.reset_calls += 1

    async def complete_detailed(self, system, messages, max_tokens=400, temperature=0.2):
        return self.summary, 10, 5

    async def aclose(self) -> None:
        pass


CHAT = "qq:1001"


def make_msg(
    content: str = "hi",
    *,
    message_id: str = "m0",
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = CHAT,
    user_id: str = "qq:2002",
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
    rng=None,
):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(budget=budget, memory=memory)
    adapter = FakeAdapter()
    brain = brain or FakeBrain()
    router = Router(settings, store, clock=lambda: NOON, rng=rng)

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
        {"qq": adapter},
        router,
        "你是溟月",
        workspace=workspace,
        injection=injection or InjectionConfig(),
        sleep=sleep,
    )

    # 先钉一条「上线前」的消息当游标下限，这样 m0/m1 会一起进批次。
    store.add_message(make_msg("上线前的旧消息", message_id="seed"))
    store.mark_delivered(ChatType.GROUP, CHAT, "seed")
    store.add_message(make_msg("第一批第一条", message_id="m0"))
    store.add_message(make_msg("第一批第二条", message_id="m1"))
    return service, store, brain, adapter


def batch_ids(service) -> list[str]:
    latest = service.store.recent_messages(ChatType.GROUP, CHAT, 1)
    if not latest:
        return []
    return [
        m.message_id
        for m in service.store.pending_batch(ChatType.GROUP, CHAT, latest[0].message_id)
    ]


async def test_batch_marks_delivered_and_records_ambient(tmp_path):
    service, store, _brain, adapter = build(tmp_path)

    await service._run_batch(ChatType.GROUP, CHAT)

    assert batch_ids(service) == []
    calls, _ = store.usage_today_by_purpose(
        ChatType.GROUP, CHAT, store.today(), ("ambient",)
    )
    assert calls == 1
    assert adapter.sent


async def test_quiet_intensity_keeps_ambient_batch_pending(tmp_path):
    service, store, brain, adapter = build(tmp_path)
    service.set_behavior(BehaviorSettings(chat_intensity="quiet"))

    await service._run_batch(ChatType.GROUP, CHAT)

    # 非点名批次不送模型，也不标成已投递：下次被点名时还要能一起看到。
    assert brain.reply_calls == 0
    assert batch_ids(service) == ["m0", "m1"]
    assert adapter.sent == []
    calls, _ = store.usage_today_by_purpose(
        ChatType.GROUP, CHAT, store.today(), ("reply", "ambient")
    )
    assert calls == 0


async def test_active_intensity_injects_framing(tmp_path):
    service, _, brain, _ = build(tmp_path)
    service.set_behavior(BehaviorSettings(chat_intensity="active"))

    await service._run_batch(ChatType.GROUP, CHAT)

    assert "【聊天强度：活跃】" in brain.last_prompt


async def test_strict_restriction_drops_matching_reply(tmp_path):
    service, _, _, adapter = build(
        tmp_path, brain=FakeBrain(text="这是内部黑话，不能说")
    )
    service.set_behavior(
        BehaviorSettings(
            restriction_mode="strict", banned_phrases=("内部黑话",)
        )
    )

    await service._run_batch(ChatType.GROUP, CHAT)

    assert adapter.sent == []


async def test_soft_restriction_still_sends_but_injects_hint(tmp_path):
    service, _, brain, adapter = build(
        tmp_path, brain=FakeBrain(text="这是内部黑话，悄悄说")
    )
    service.set_behavior(
        BehaviorSettings(
            restriction_mode="soft", banned_phrases=("内部黑话",)
        )
    )

    await service._run_batch(ChatType.GROUP, CHAT)

    assert "【言论限制】" in brain.last_prompt
    assert adapter.texts() == ["这是内部黑话，悄悄说"]


async def test_batch_injects_recalled_old_messages_when_enabled(tmp_path):
    service, store, brain, _ = build(
        tmp_path,
        memory=MemoryConfig(1, 5, 20),
        injection=InjectionConfig(recall_enabled=True),
    )
    store.add_message(make_msg("显卡驱动坏了", message_id="old"))
    store.mark_delivered(ChatType.GROUP, CHAT, "old")
    store.add_message(make_msg("显卡驱动怎么装", message_id="new"))

    await service._run_batch(ChatType.GROUP, CHAT)

    assert "【相关旧记忆】" in brain.last_prompt
    assert "显卡驱动坏了" in brain.last_prompt


async def test_batch_does_not_inject_recalled_old_messages_by_default(tmp_path):
    service, store, brain, _ = build(tmp_path, memory=MemoryConfig(1, 5, 20))
    store.add_message(make_msg("显卡驱动坏了", message_id="old"))
    store.mark_delivered(ChatType.GROUP, CHAT, "old")
    store.add_message(make_msg("显卡驱动怎么装", message_id="new"))

    await service._run_batch(ChatType.GROUP, CHAT)

    assert "【相关旧记忆】" not in brain.last_prompt
    assert "显卡驱动坏了" not in brain.last_prompt


async def test_budget_exhausted_skips_without_calling_brain(tmp_path):
    service, store, brain, _adapter = build(
        tmp_path, budget=BudgetConfig(daily_calls=1, daily_tokens=300000)
    )
    store.record_usage(ChatType.GROUP, CHAT, "ambient", 1, 1)

    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 0
    assert store.skip_reason(ChatType.GROUP, CHAT, "m0") == "budget"
    assert batch_ids(service) == []


async def test_addressed_batch_uses_reply_purpose(tmp_path):
    service, store, brain, _ = build(tmp_path)

    # 走 handle 才能真正验证「点名状态从入口传到批次」，而不是测试里手工塞标志。
    await service.handle(
        make_msg("你刚才说啥", message_id="m2", reply_to_bot=True)
    )
    await service._run_batch(ChatType.GROUP, CHAT)

    calls, _ = store.usage_today_by_purpose(
        ChatType.GROUP, CHAT, store.today(), ("reply",)
    )
    assert calls == 1
    # 引用回复与 @ 是两种不同的点名，prompt 里要能区分。
    assert "（引用回复你）" in brain.last_prompt
    assert "（@了你）" not in brain.last_prompt


async def test_at_bot_batch_marks_at(tmp_path):
    service, _store, brain, _ = build(tmp_path)

    await service.handle(make_msg("在吗", message_id="m2", at_bot=True))
    await service._run_batch(ChatType.GROUP, CHAT)

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
        {"qq": adapter},
        service.router,
        "你是溟月",
        injection=InjectionConfig(),
    )
    await restarted._run_batch(ChatType.GROUP, CHAT)

    calls, _ = store.usage_today_by_purpose(
        ChatType.GROUP, CHAT, store.today(), ("reply",)
    )
    assert calls == 1
    assert "（@了你）" in brain.last_prompt


async def test_brain_error_does_not_requeue_and_stays_silent(tmp_path):
    service, store, _, adapter = build(tmp_path, brain=FakeBrain(error=True))

    await service._run_batch(ChatType.GROUP, CHAT)

    # 不再发「本鱼断线了」：失败只记账 + mark_skipped，群里保持安静。
    assert adapter.texts() == []
    assert batch_ids(service) == []
    assert store.skip_reason(ChatType.GROUP, CHAT, "m0") == "error"


async def test_tool_use_sends_no_hint_and_keeps_silence(tmp_path):
    """工具调用只做内部标记：不再先发「去网上查一下，稍等」。"""
    service, _, _, adapter = build(
        tmp_path, brain=FakeBrain(speak=False, text="", fire_tool=True)
    )

    await service._run_batch(ChatType.GROUP, CHAT)

    # 模型选择不说：群里一条消息都不该有（既没有过渡语，也没有「没查到」补话）。
    assert adapter.texts() == []


async def test_tool_use_is_recorded_for_internal_use(tmp_path, monkeypatch):
    """on_tool 被触发时只记录，不发消息；名称留给后续记忆功能。"""
    recorded: list[ToolUse] = []

    class RecordingToolUse(ToolUse):
        def __init__(self) -> None:
            super().__init__()
            recorded.append(self)

    monkeypatch.setattr(service_module, "ToolUse", RecordingToolUse)
    service, _, _, adapter = build(tmp_path, brain=FakeBrain(fire_tool=True))

    await service._run_batch(ChatType.GROUP, CHAT)

    assert recorded, "本轮应当构造 ToolUse"
    assert recorded[-1].used is True
    assert recorded[-1].names == ["webfetch"]
    # 只发正式回复，不多一条过渡语。
    assert adapter.texts() == ["在呢"]


async def test_rotation_runs_before_model_call(tmp_path):
    service, store, brain, _ = build(tmp_path)
    store.bump_session_stats(ChatType.GROUP, CHAT, messages=60, tokens=0)

    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reset_calls == 1
    assert "上次聊到" in brain.last_prompt
    assert "【接续】" in brain.last_prompt


async def test_continuation_from_db_survives_restart(tmp_path):
    """重启后内存字典空：接续语要从 session_summaries 捞回来，且只注入一次。"""
    service, store, brain, _ = build(tmp_path)
    store.add_session_summary(ChatType.GROUP, CHAT, "上次聊到鱼缸和水草")

    await service._run_batch(ChatType.GROUP, CHAT)

    assert "【接续】" in brain.last_prompt
    assert "上次聊到鱼缸和水草" in brain.last_prompt

    # 第二轮同一进程：已消费，不再重复注入（哪怕库里那条还在）。
    store.add_message(make_msg("再说一句", message_id="m2"))
    await service._run_batch(ChatType.GROUP, CHAT)

    assert "【接续】" not in brain.last_prompt
    assert "上次聊到鱼缸和水草" not in brain.last_prompt


async def test_db_continuation_is_not_consumed_on_brain_error(tmp_path):
    brain = FakeBrain(error=True)
    service, store, brain, _ = build(tmp_path, brain=brain)
    store.add_session_summary(ChatType.GROUP, CHAT, "上次聊到鱼缸和水草")

    await service._run_batch(ChatType.GROUP, CHAT)

    # 失败不消费：下一批仍要带上接续语。
    brain.error = False
    store.add_message(make_msg("再来一次", message_id="m2"))
    await service._run_batch(ChatType.GROUP, CHAT)

    assert "上次聊到鱼缸和水草" in brain.last_prompt


async def test_new_rotation_continuation_reinjects_after_consumed(tmp_path):
    service, store, brain, _ = build(tmp_path)
    store.add_session_summary(ChatType.GROUP, CHAT, "第一次接续")

    await service._run_batch(ChatType.GROUP, CHAT)
    assert "第一次接续" in brain.last_prompt

    # 下一轮触发轮换：新接续语要重新注入，不能被「已消费」挡住。
    store.bump_session_stats(ChatType.GROUP, CHAT, messages=60, tokens=0)
    store.add_message(make_msg("继续聊", message_id="m2"))
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reset_calls == 1
    assert "【接续】" in brain.last_prompt
    assert "第一次接续" not in brain.last_prompt
    assert "大家在聊鱼缸和水草" in brain.last_prompt


async def test_prompt_contains_all_segments_and_timeline(tmp_path):
    service, store, brain, _ = build(tmp_path, with_workspace=True)
    store.upsert_summary(ChatType.GROUP, CHAT, yesterday(), "昨天聊了鱼缸")
    store.upsert_impression(ChatType.GROUP, CHAT, "qq:2002", "trait", "阿鱼：爱养鱼", 0.9)

    await service._run_batch(ChatType.GROUP, CHAT)

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
    store.mark_recalled(ChatType.GROUP, CHAT, "m1")

    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 0
    assert adapter.texts() == []


async def test_closed_thread_reopens_on_question(tmp_path):
    """收尾窗口里出现明确问句：应该重新叫醒模型，而不是按 5% 抽签。"""
    service, store, brain, _adapter = build(tmp_path, rng=_FixedRng(0.9))
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    assert service.router.is_thread_closed(ChatType.GROUP, CHAT) is True

    msg = make_msg("这个报错怎么弄？", message_id="m3")
    store.add_message(msg)
    await service._handle_batched(msg)
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1


async def test_alias_mention_counts_as_addressed_in_batch(tmp_path):
    """叫名字不像 @/引用那样带标记，批次也要按点名处理（reply 预算/收尾重开）。"""
    service, store, brain, _adapter = build(tmp_path)
    msg = make_msg("大肥鱼在吗", message_id="m3")
    store.add_message(msg)
    await service._handle_batched(msg)
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1
    calls, _ = store.usage_today_by_purpose(
        ChatType.GROUP, CHAT, store.today(), ("reply",)
    )
    assert calls == 1


async def test_question_batch_prompt_has_timing_hint(tmp_path):
    service, store, brain, _adapter = build(tmp_path)
    store.add_message(make_msg("这个报错怎么弄？", message_id="m3"))

    await service._run_batch(ChatType.GROUP, CHAT)

    assert "【时机】" in brain.last_prompt


async def test_prompt_tells_model_to_read_latest_image(tmp_path):
    service, _store, _brain, _adapter = build(tmp_path)
    image = make_msg(
        "[图片]（图存 data/bot-workspace/images/20261031-210535-x.gif，"
        "动图，可用 read 查看）",
        message_id="m3",
    )

    prompt = service._build_prompt(ChatType.GROUP, CHAT, [image])

    assert "【图片】" in prompt
    assert "最近一张" in prompt
    assert "read" in prompt
    assert "首帧" in prompt


async def test_prompt_flags_missing_image(tmp_path):
    service, _store, _brain, _adapter = build(tmp_path)
    prompt = service._build_prompt(
        ChatType.GROUP, CHAT, [make_msg("[图片]", message_id="m3")]
    )

    assert "没有取到内容" in prompt
    assert "不要编造" in prompt


async def test_prompt_finds_recent_image_when_batch_has_no_image(tmp_path):
    """轮换/批次之后才问「刚才那张图」：要从最近消息里把图片路径补回来。"""
    service, store, _brain, _adapter = build(tmp_path)
    store.add_message(
        make_msg(
            "[图片]（图存 data/bot-workspace/images/earlier.jpg，可用 read 查看）",
            message_id="img1",
        )
    )

    prompt = service._build_prompt(
        ChatType.GROUP, CHAT, [make_msg("刚才那张图是猫吗", message_id="m3")]
    )

    assert "最近发过的图" in prompt
    assert "earlier.jpg" in prompt
    assert "read" in prompt


async def test_downshift_skips_batch(tmp_path):
    service, _store, brain, adapter = build(tmp_path)
    for _ in range(3):
        service.router.note_no_reply(ChatType.GROUP, CHAT)

    await service._handle_batched(make_msg("随便聊聊", message_id="m2"))

    assert brain.reply_calls == 0
    assert adapter.texts() == []


async def test_multi_sentence_string_stays_single_message(tmp_path):
    """字符串回复不再被自动拆句：分条由模型用数组决定。"""
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

    await service._run_batch(ChatType.GROUP, CHAT)

    assert len(adapter.texts()) == 1
    assert delays == []


async def test_short_reply_stays_single_message(tmp_path):
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    service, _store, _brain, adapter = build(tmp_path, sleep=fake_sleep)

    await service._run_batch(ChatType.GROUP, CHAT)

    assert len(adapter.texts()) == 1
    assert delays == []


async def test_private_reply_with_parts_is_segmented(tmp_path):
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    brain = FakeBrain(
        text="结论是这个方案能跑。\n原因有两个，一个是开关没打开，一个是网络不稳。",
        parts=(
            "结论是这个方案能跑，配置和依赖都没问题，今天就可以直接上线。",
            "原因有两个，一个是开关没打开，一个是网络不稳，这两点都确认过了。",
        ),
    )
    service, _store, _brain, adapter = build(tmp_path, brain=brain, sleep=fake_sleep)

    await service._handle_batched(
        make_msg("在吗", message_id="p1", chat_type=ChatType.PRIVATE, chat_id="qq:2002")
    )

    assert len(adapter.texts()) == 2
    assert delays == [0.8]


async def test_closed_thread_skips_ambient_batch(tmp_path):
    service, _store, brain, adapter = build(tmp_path)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)

    await service.handle(make_msg("随便聊聊", message_id="m2"))

    assert brain.reply_calls == 0
    assert adapter.texts() == []


async def test_addressed_message_reopens_closed_batch(tmp_path):
    service, _store, brain, adapter = build(tmp_path)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)

    await service.handle(make_msg("在吗", message_id="m2", at_bot=True))
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1
    assert adapter.texts()


async def test_ambient_replies_count_toward_thread_limit(tmp_path):
    service, _store, _brain, _adapter = build(tmp_path)

    await service.handle(make_msg("随便聊聊", message_id="m2"))
    await service._run_batch(ChatType.GROUP, CHAT)
    assert service.router.ambient_turns(ChatType.GROUP, CHAT) == 1
    assert service.router.is_thread_closed(ChatType.GROUP, CHAT) is False

    await service.handle(make_msg("继续说", message_id="m3"))
    await service._run_batch(ChatType.GROUP, CHAT)
    assert service.router.ambient_turns(ChatType.GROUP, CHAT) == 2
    assert service.router.is_thread_closed(ChatType.GROUP, CHAT) is True


async def test_batch_prompt_reports_backlog_before_window(tmp_path):
    service, store, brain, _ = build(tmp_path)
    for i in range(2, 26):
        store.add_message(make_msg(f"第{i}条", message_id=f"m{i}"))

    await service._run_batch(ChatType.GROUP, CHAT)

    # 26 条待投递只取最新 20 条（m6..m25），m0..m5 共 6 条被省略。
    assert "（更早还有 6 条消息未展开）" in brain.last_prompt


class _FixedRng:
    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return self.value


async def test_closed_thread_samples_ambient_message(tmp_path):
    service, store, brain, _adapter = build(tmp_path, rng=_FixedRng(0.0))
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    assert service.router.is_thread_closed(ChatType.GROUP, CHAT) is True

    msg = make_msg("还在聊吗", message_id="m3")
    store.add_message(msg)
    await service._handle_batched(msg)
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1
    assert "收尾" in service._system_prompt(ChatType.GROUP, [msg], CHAT)


async def test_closed_thread_sample_miss_skips(tmp_path):
    service, store, brain, _adapter = build(tmp_path, rng=_FixedRng(0.9))
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    assert service.router.is_thread_closed(ChatType.GROUP, CHAT) is True

    msg = make_msg("随便聊聊", message_id="m3")
    store.add_message(msg)
    await service._handle_batched(msg)

    assert brain.reply_calls == 0


async def test_closed_thread_keeps_message_for_next_addressed_batch(tmp_path):
    """收尾窗口里的消息不做终局丢弃：下次被点名时要能一起进 prompt。"""
    service, store, brain, _adapter = build(tmp_path, rng=_FixedRng(0.9))
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)

    msg = make_msg("刚才那个问题的背景是这样的", message_id="m2")
    store.add_message(msg)
    await service._handle_batched(msg)

    # 没有 mark_skipped：消息仍在待投队列里。
    assert store.skip_reason(ChatType.GROUP, CHAT, "m2") is None
    assert brain.reply_calls == 0

    await service.handle(make_msg("大肥鱼在吗", message_id="m3", at_bot=True))
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1
    assert "刚才那个问题的背景是这样的" in brain.last_prompt


async def test_downshift_keeps_message_for_next_addressed_batch(tmp_path):
    service, store, brain, _adapter = build(tmp_path)
    for _ in range(3):
        service.router.note_no_reply(ChatType.GROUP, CHAT)

    msg = make_msg("刚才那个问题的背景是这样的", message_id="m2")
    store.add_message(msg)
    await service._handle_batched(msg)

    assert store.skip_reason(ChatType.GROUP, CHAT, "m2") is None
    assert brain.reply_calls == 0

    await service.handle(make_msg("大肥鱼在吗", message_id="m3", at_bot=True))
    await service._run_batch(ChatType.GROUP, CHAT)

    assert "刚才那个问题的背景是这样的" in brain.last_prompt


async def test_continuation_request_reopens_closed_thread(tmp_path):
    """用户说「继续说」不是普通闲聊：收尾窗口也要把模型叫起来。"""
    service, store, brain, _adapter = build(tmp_path, rng=_FixedRng(0.9))
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    assert service.router.is_thread_closed(ChatType.GROUP, CHAT) is True

    msg = make_msg("继续说", message_id="m2")
    store.add_message(msg)
    await service._handle_batched(msg)
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1


async def test_addressed_long_reply_is_sent_in_full(tmp_path):
    """被点名要完整答案时不能砍尾：分条发也要把最后一句发出去。"""
    text = (
        "三个原因，按重要性排：第一，美联储的降息预期突然降温，长久期资产先被杀估值，"
        "估值一收缩，跌幅就被放大了。"
        "第二，几家半导体设备商的订单指引低于市场预期，投资者开始担心这轮资本开支"
        "已经见顶，于是先卖再说。"
        "第三，前期涨得太快，拥挤交易一松动就踩踏，量化止损和杠杆盘一起被动减仓，"
        "把跌幅进一步放大。"
    )
    assert len(text) > 120
    service, store, _brain, adapter = build(tmp_path, brain=FakeBrain(text=text))

    msg = make_msg("@小菜鱼 说说半导体为什么跌", message_id="m2", at_bot=True)
    store.add_message(msg)
    await service._handle_batched(msg)
    await service._run_batch(ChatType.GROUP, CHAT)

    assert "".join(adapter.texts()) == text
    assert len(adapter.texts()) <= 3
    assert all(not chunk.endswith("…") for chunk in adapter.texts())


async def test_closed_thread_addressed_still_replies(tmp_path):
    service, store, brain, _adapter = build(tmp_path, rng=_FixedRng(0.9))
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    assert service.router.is_thread_closed(ChatType.GROUP, CHAT) is True

    msg = make_msg("大肥鱼", message_id="m3", at_bot=True)
    store.add_message(msg)
    await service._handle_batched(msg)
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1


class _HistoryBrain(FakeBrain):
    """模拟 opencode 那种会话型大脑：system prompt 由 service 传入。"""

    accepts_history = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.systems: list[str] = []

    async def reply(
        self, chat_type, chat_id, text: str, *, system=None, history=None, on_tool=None
    ) -> BrainReply:
        self.systems.append(system or "")
        return await super().reply(chat_type, chat_id, text, on_tool=on_tool)


async def test_closed_thread_hint_reaches_model_system_prompt(tmp_path):
    brain = _HistoryBrain()
    service, store, _brain, _adapter = build(tmp_path, brain=brain, rng=_FixedRng(0.0))
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    assert service.router.is_thread_closed(ChatType.GROUP, CHAT) is True

    msg = make_msg("还在聊吗", message_id="m3")
    store.add_message(msg)
    await service._handle_batched(msg)
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1
    assert "收尾" in brain.systems[-1]


async def test_open_thread_has_no_closed_hint(tmp_path):
    brain = _HistoryBrain()
    service, store, _brain, _adapter = build(tmp_path, brain=brain, rng=_FixedRng(0.0))

    msg = make_msg("今天下雨", message_id="m3")
    store.add_message(msg)
    await service._handle_batched(msg)
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1
    assert "收尾" not in brain.systems[-1]

async def test_closed_thread_samples_once_in_model_mode(tmp_path):
    """收尾期一条消息只抽一次：入队抽过，跑批次时不能再抽一次。"""
    rng = _FixedRng(0.0)
    service, store, brain, _adapter = build(tmp_path, rng=rng)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)
    service.router.note_bot_spoke(ChatType.GROUP, CHAT, ambient=True)

    msg = make_msg("还在聊吗", message_id="m3")
    store.add_message(msg)
    await service._handle_batched(msg)
    await service._run_batch(ChatType.GROUP, CHAT)

    assert brain.reply_calls == 1
    assert rng.calls == 1


# ----------------------------------------------------------- 增量 token 统计
async def test_incremental_tokens_counts_only_growth(tmp_path):
    service, _store, _brain, _adapter = build(tmp_path)

    first = service._incremental_tokens(
        ChatType.GROUP,
        CHAT,
        BrainReply(True, "好", prompt_tokens=1000, completion_tokens=40),
    )
    assert first == 1040

    # opencode 报的是整段上下文：第二轮 prompt 涨到 1200，只应计 200 增量。
    second = service._incremental_tokens(
        ChatType.GROUP,
        CHAT,
        BrainReply(True, "好", prompt_tokens=1200, completion_tokens=40),
    )
    assert second == 240

    # 上下文变小（压缩/重建）时不能出现负数，只计本轮输出。
    third = service._incremental_tokens(
        ChatType.GROUP,
        CHAT,
        BrainReply(True, "好", prompt_tokens=300, completion_tokens=20),
    )
    assert third == 20


async def test_session_stats_count_only_incremental_tokens(tmp_path):
    service, store, _brain, _adapter = build(tmp_path)

    await service._run_batch(ChatType.GROUP, CHAT)
    first = store.session_stats(ChatType.GROUP, CHAT)
    assert first.token_estimate == 150  # 100 prompt + 50 completion

    store.add_message(make_msg("第二轮", message_id="m2"))
    await service._run_batch(ChatType.GROUP, CHAT)
    second = store.session_stats(ChatType.GROUP, CHAT)
    # 同一段上下文再报 100 prompt 不应重复累计，只加本轮输出 50。
    assert second.token_estimate == 200


# --------------------------------------------------------- 图片密钥迟到重试
class _FakeResolver:
    def __init__(self, key) -> None:
        self.key = key

    def prewarm(self) -> bool:
        return False


def _image_message(message_id: str = "m-img") -> Message:
    msg = make_msg("[图片]", message_id=message_id)
    msg.attachments = [
        Attachment(kind="image", path="C:/wechat/msg/attach/aa/bb.dat")
    ]
    return msg


def _wire_resolver(service, key) -> None:
    service._media_resolver_ready = True
    service._media_resolver = _FakeResolver(key)


async def test_retry_pending_media_backfills_batch_content(tmp_path, monkeypatch):
    service, _store, _brain, _adapter = build(tmp_path)
    service._queue_media_retry(_image_message())
    _wire_resolver(service, key=object())

    async def fake_materialize(message, settings, *, decode=None):
        message.content = "[图片]（图存 images/retry.png，可用 read 查看）"
        return 1

    monkeypatch.setattr(service_module, "materialize_attachments", fake_materialize)

    # 批次里的对象来自库，和 ingest 时登记的附件对象不是同一个。
    batch_message = _image_message()
    await service._retry_pending_media([batch_message])

    assert batch_message.message_id not in service._media_retry
    assert "images/retry.png" in batch_message.content
    assert batch_message.attachments[0].kind == "image"


class _DelayedResolver:
    """前几次读 key 返回 None，模拟重启后密钥扫描还没完成。"""

    def __init__(self, key, *, misses: int = 1) -> None:
        self._key = key
        self._misses = misses

    @property
    def key(self):
        if self._misses > 0:
            self._misses -= 1
            return None
        return self._key

    def prewarm(self) -> bool:
        return False


async def test_retry_pending_media_waits_for_delayed_key(tmp_path, monkeypatch):
    service, _store, _brain, _adapter = build(tmp_path)
    service._queue_media_retry(_image_message())
    service._media_resolver_ready = True
    service._media_resolver = _DelayedResolver(object())
    monkeypatch.setattr(service, "_kick_media_key_scan", lambda *a, **k: None)
    monkeypatch.setattr(service_module, "MEDIA_KEY_POLL_SECONDS", 0.0)
    monkeypatch.setattr(service_module, "MEDIA_KEY_WAIT_SECONDS", 5.0)

    async def fake_materialize(message, settings, *, decode=None):
        message.content = "[图片]（图存 images/delayed.png，可用 read 查看）"
        return 1

    monkeypatch.setattr(service_module, "materialize_attachments", fake_materialize)

    batch_message = _image_message()
    await service._retry_pending_media([batch_message])

    # 等到 key 后必须重新物化一次，占位被路径注记替换。
    assert "images/delayed.png" in batch_message.content
    assert batch_message.message_id not in service._media_retry


async def test_retry_pending_media_timeout_keeps_placeholder(tmp_path, monkeypatch):
    service, _store, _brain, _adapter = build(tmp_path)
    service._queue_media_retry(_image_message())
    _wire_resolver(service, key=None)
    monkeypatch.setattr(service, "_kick_media_key_scan", lambda *a, **k: None)
    monkeypatch.setattr(service_module, "MEDIA_KEY_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(service_module, "MEDIA_KEY_POLL_SECONDS", 0.0)

    called: list[object] = []

    async def fake_materialize(*args, **kwargs):
        called.append(args)
        return 1

    monkeypatch.setattr(service_module, "materialize_attachments", fake_materialize)

    # 等不到密钥也必须直接返回，不能抛异常卡住整轮。
    batch_message = _image_message()
    await service._retry_pending_media([batch_message])

    assert called == []
    assert batch_message.message_id not in service._media_retry
    assert batch_message.content == "[图片]"


async def test_batch_still_replies_when_media_key_wait_times_out(
    tmp_path, monkeypatch
):
    """密钥等不到也必须照常回复：图片保持占位，消息不能丢。"""
    service, store, _brain, adapter = build(tmp_path)
    image = _image_message("m-img")
    store.add_message(image)
    service._queue_media_retry(image)

    _wire_resolver(service, key=None)
    monkeypatch.setattr(service, "_kick_media_key_scan", lambda *a, **k: None)
    monkeypatch.setattr(service_module, "MEDIA_KEY_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(service_module, "MEDIA_KEY_POLL_SECONDS", 0.0)

    await service._run_batch(ChatType.GROUP, CHAT)

    assert adapter.texts() == ["在呢"]
    assert batch_ids(service) == []
