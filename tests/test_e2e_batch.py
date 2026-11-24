"""Task 14 Step 1：不连 QQ 的完整批次链路（真实 OpenCodeHttpBrain + MockTransport）。

链路：adapter → router → Service.handle → Store 入库 → BatchScheduler.tick
→ _run_batch（预算 / 轮换 / 三段注入 / 工具提示）→ brain.reply（HTTP mock）
→ adapter.send → 记账 → mark_delivered → bump_session_stats → enforce_limit。

覆盖 spec §14.3 中可离线判定的条目：
- 主链路（三段注入 + 时间线 + @ 标记 + speak=true 后真的发出去）
- 轮换（新 session + 【接续】且摘要不重复）
- 静音时段（不发 HTTP、不发消息、消息带 skipped_reason）
"""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
from test_opencode_http import FakeServer, make_brain

from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    InjectionConfig,
    LoggingConfig,
    MemoryConfig,
    MindConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.memory.store import Store
from dafeiyu.memory.workspace import Workspace
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service
from dafeiyu.timeutil import today_shanghai

SHANGHAI = timezone(timedelta(hours=8))


class MultiSessionServer(FakeServer):
    """每次 POST /api/session 返回不同 id。

    轮换会先建一个临时 session 写交接摘要（用完即删），再建正式 session；
    FakeServer 固定返回 ses_new 会把两者混成一个，看不出「换了 session」。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session_seq = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/session":
            self.session_seq += 1
            sid = f"ses_{self.session_seq}"
            self.calls.append((request.method, request.url.path))
            self.auth.append(request.headers.get("authorization", ""))
            self.created_with = json.loads(request.content.decode("utf-8") or "{}")
            self.known.add(sid)
            self.messages[sid] = []
            return httpx.Response(200, json={"data": {"id": sid}})
        return super().handler(request)

NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)
NIGHT = datetime(2026, 10, 24, 3, 0, tzinfo=SHANGHAI)


def yesterday() -> str:
    # 注入的昨日摘要按真实「今天（上海）」算，不能用夹具里的 NOON 日期。
    return (date.fromisoformat(today_shanghai()) - timedelta(days=1)).isoformat()


def make_settings(
    *, hold_max_seconds: int = 300, mind: MindConfig | None = None
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
        persona=PersonaConfig(
            "溟月", ("大肥鱼",), 120, 300, 0.05, hold_max_seconds=hold_max_seconds
        ),
        triggers=TriggerConfig(
            60,
            0.0,
            ("01:00", "07:00"),
            ("怎么",),
            mode="model",
            batch_seconds=0.0,
            addressed_debounce_seconds=0.0,
            addressed_max_wait_seconds=30.0,
            ambient_min_interval_seconds=0.0,
        ),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({2002})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        mind=MindConfig() if mind is None else mind,
    )


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, int, str]] = []

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
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
    ts: int = 0,
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
        reply_to_bot=False,
        timestamp=ts,
    )


def build(
    tmp_path,
    *,
    server: FakeServer | None = None,
    clock=NOON,
    hold_max_seconds: int = 300,
    mind: MindConfig | None = None,
):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(hold_max_seconds=hold_max_seconds, mind=mind)
    adapter = FakeAdapter()
    server = server or FakeServer()
    brain = make_brain(store, server)
    router = Router(settings, store, clock=lambda: clock)

    workspace = Workspace(tmp_path / "ws")
    workspace.ensure()
    workspace.global_path().write_text(
        "# 大肥鱼的长期记忆\n喜欢水草和沉木\n", encoding="utf-8"
    )

    service = Service(
        settings,
        store,
        brain,
        {"qq": adapter},
        router,
        "你是溟月",
        workspace=workspace,
        injection=InjectionConfig(),
    )
    # 钉一条「上线前」的消息当游标下限，后面的消息才会进批次。
    store.add_message(make_msg("上线前的旧消息", message_id="seed"))
    store.mark_delivered(ChatType.GROUP, CHAT, "seed")
    return service, store, brain, adapter, server


def pending_ids(service) -> list[str]:
    latest = service.store.recent_messages(ChatType.GROUP, CHAT, 1)
    if not latest:
        return []
    return [
        m.message_id
        for m in service.store.pending_batch(ChatType.GROUP, CHAT, latest[0].message_id)
    ]


async def test_e2e_batch_reaches_model_and_sends(tmp_path):
    """三条消息（其中一条 @）→ tick → 完整批次 → 模型 → 发出去。"""
    service, store, brain, adapter, server = build(tmp_path)
    store.upsert_summary(ChatType.GROUP, CHAT, yesterday(), "昨天聊了鱼缸")
    store.upsert_impression(ChatType.GROUP, CHAT, "qq:2002", "trait", "阿鱼：爱养鱼", 0.9)

    await service.handle(make_msg("今天水草长得不错", message_id="m1"))
    await service.handle(make_msg("鱼缸该换水了", message_id="m2"))
    await service.handle(make_msg("@溟月 你看这缸水草", message_id="m3", at_bot=True))

    # 入库是同步的，但模型一次都还没调。
    assert server.prompts == []
    due = service.scheduler.tick()
    assert due == [(ChatType.GROUP, CHAT)]
    for key in due:
        await service.scheduler._flush(*key)

    # 一次批次只调一次模型，prompt 里三段注入在最前、时间线在最后。
    assert len(server.prompts) == 1
    prompt = server.prompts[0]
    assert prompt.index("【长期记忆】") < prompt.index(
        "—— 以下是自你上次开口以来的新消息 ——"
    )
    assert "【近期摘要】" in prompt and "昨天聊了鱼缸" in prompt
    assert "【群友印象】" in prompt and "阿鱼：爱养鱼" in prompt
    assert "（@了你）" in prompt
    for text in ("今天水草长得不错", "鱼缸该换水了", "@溟月 你看这缸水草"):
        assert text in prompt

    # speak=true → 真的发出去，且是模型返回的那句。
    assert adapter.sent == [(ChatType.GROUP, CHAT, "在群聊卖萌")]
    assert server.created_with.get("agent") == "deepseek-qq"
    assert store.get_opencode_session(ChatType.GROUP, CHAT) == "ses_new"

    # 游标推进、按点名记账、session 统计累加。
    assert pending_ids(service) == []
    calls, tokens = store.usage_today_by_purpose(
        ChatType.GROUP, CHAT, store.today(), ("reply",)
    )
    assert calls == 1 and tokens == 135
    stats = store.session_stats(ChatType.GROUP, CHAT)
    assert stats.delivered_count == 3
    assert stats.token_estimate == 135
    await brain.aclose()


async def test_e2e_rotation_switches_session_and_injects_continuation(tmp_path):
    """到达条数阈值 → 交接摘要 → 换 session → 首轮含【接续】且摘要只出现一次。"""
    server = MultiSessionServer(answer="大家在聊鱼缸和水草")
    service, store, brain, _, _ = build(tmp_path, server=server)
    store.set_opencode_session(ChatType.GROUP, CHAT, "ses_old")
    store.bump_session_stats(ChatType.GROUP, CHAT, messages=60, tokens=0)

    await service.handle(make_msg("接着聊", message_id="m1"))
    due = service.scheduler.tick()
    assert due == [(ChatType.GROUP, CHAT)]
    for key in due:
        await service.scheduler._flush(*key)

    prompt = server.prompts[-1]
    assert "【接续】" in prompt
    assert prompt.count("大家在聊鱼缸和水草") == 1
    # 临时摘要 session 用完即删；旧 session 按设计保留，只断开映射。
    assert ("DELETE", "/api/session/ses_1") in server.calls
    assert ("DELETE", "/api/session/ses_old") not in server.calls
    assert store.get_opencode_session(ChatType.GROUP, CHAT) == "ses_2"
    assert store.session_stats(ChatType.GROUP, CHAT).delivered_count == 1
    await brain.aclose()


async def test_e2e_forget_clears_memory_but_keeps_notes(tmp_path):
    """§14.3-5：/forget 清库 + 删远端 session，但笔记文件不动。"""
    service, store, brain, adapter, server = build(tmp_path)
    store.set_opencode_session(ChatType.GROUP, CHAT, "ses_old")
    store.add_message(make_msg("记住我", message_id="m1"))

    workspace = service.workspace
    chat_file = workspace.chat_path(ChatType.GROUP, CHAT)
    chat_file.parent.mkdir(parents=True, exist_ok=True)
    chat_file.write_text("- 阿鱼喜欢水草\n", encoding="utf-8")

    await service.handle(make_msg("/forget", message_id="f1", user_id=2002))

    assert store.count_messages(ChatType.GROUP, CHAT) == 0
    assert store.get_opencode_session(ChatType.GROUP, CHAT) is None
    assert ("DELETE", "/api/session/ses_old") in server.calls
    # 笔记是模型自己写的资产，/forget 只清对话记忆，不能连文件一起删。
    assert workspace.global_path().exists()
    assert "喜欢水草和沉木" in workspace.global_path().read_text(encoding="utf-8")
    assert chat_file.exists()
    assert "阿鱼喜欢水草" in chat_file.read_text(encoding="utf-8")
    assert adapter.sent[-1] == (ChatType.GROUP, CHAT, "记忆清空了，本鱼失忆中……🫧")
    await brain.aclose()


async def test_e2e_quiet_hours_never_calls_model(tmp_path):
    """静音时段：不排批、不发 HTTP、消息带 skipped_reason。"""
    service, store, brain, adapter, server = build(tmp_path, clock=NIGHT)

    await service.handle(make_msg("半夜冒个泡", message_id="m1"))

    assert store.skip_reason(ChatType.GROUP, CHAT, "m1") == "quiet"
    assert service.scheduler.pending_chats() == []
    assert service.scheduler.tick() == []
    assert server.calls == []
    assert adapter.sent == []
    await brain.aclose()


# ---------------------------------------------------------------------------
# 攒着寿命与面板干预：store → service → control 真串（不 mock 层间调用）
# ---------------------------------------------------------------------------

HOT_LINE = "今天这缸水草长得真好看"


def build_control(service, store, adapter, tmp_path):
    """真 Service + 真 Store 上的控制面：HTTP 请求会真的走 service 方法。"""
    from dafeiyu.control import BrainControl, create_app

    control = BrainControl(
        service.settings,
        store,
        service,
        adapter,
        runtime_path=str(tmp_path / "runtime.json"),
    )
    return control, create_app(control, "", None)


async def send_chatter(service, *, count: int, ts: int, prefix: str) -> list[str]:
    """投一串纯闲聊（热聊信号：60 秒内 ≥5 条、无问句、不点名）。"""
    ids: list[str] = []
    for i in range(count):
        mid = f"{prefix}{i}"
        # 正文带上批次前缀：不同批次不能是同一句，否则会被入站去重当成
        # resync 重放（同人同文 + 时间相近）而丢掉。
        await service.handle(make_msg(f"{HOT_LINE}{prefix}{i}", message_id=mid, ts=ts))
        ids.append(mid)
    return ids


async def flush_due(service) -> None:
    for key in service.scheduler.tick():
        await service.scheduler._flush(*key)


def checkpoints(store) -> list[str]:
    return [row["checkpoint"] for row in store.list_decisions(limit=20, chat_id=CHAT)]


async def test_e2e_hold_expires_and_leaves_the_holds_panel(tmp_path):
    """攒着 → 改最早时间戳 → 超龄放行 → 面板不再列出（三段缺一不可）。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path)
    control, app = build_control(service, store, adapter, tmp_path)
    now = int(now_shanghai().timestamp())

    # 1) 真实 handle → 调度器 → _run_batch：纯热聊被攒着，模型一次没调。
    await send_chatter(service, count=6, ts=now, prefix="h")
    await flush_due(service)
    assert server.prompts == []
    assert "opportunity_hold" in checkpoints(store)
    assert control.holds_snapshot()["total"] == 1

    # 2) 把最早那条改成 400 秒前（不 sleep），再投一条触发重判。
    store._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE chat_type = ? AND chat_id = ? AND message_id = ?",
        (now - 400, ChatType.GROUP.value, CHAT, "h0"),
    )
    store._conn.commit()
    await send_chatter(service, count=1, ts=now, prefix="h6x")
    await flush_due(service)

    assert len(server.prompts) == 1
    assert "opportunity_hold_expired" in checkpoints(store)
    # 放行不是丢弃：这批消息没有 skipped_reason，且已经真的投出去。
    assert store.skip_reason(ChatType.GROUP, CHAT, "h0") is None
    assert adapter.sent

    # 3) 全链路串起来：面板接口不再列出该会话。
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        data = await (await client.get("/api/holds")).json()
    finally:
        await client.close()
    assert data["total"] == 0
    assert data["items"] == []
    await brain.aclose()


async def test_e2e_zero_lifetime_keeps_holding(tmp_path):
    """`hold_max_seconds=0` 必须真的关闭过期：仍然攒着、面板仍然列出。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, hold_max_seconds=0)
    control, _ = build_control(service, store, adapter, tmp_path)
    now = int(now_shanghai().timestamp())

    await send_chatter(service, count=6, ts=now, prefix="h")
    store._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE chat_type = ? AND chat_id = ? AND message_id = ?",
        (now - 5000, ChatType.GROUP.value, CHAT, "h0"),
    )
    store._conn.commit()
    await send_chatter(service, count=1, ts=now, prefix="h6y")
    await flush_due(service)

    assert server.prompts == []
    assert "opportunity_hold_expired" not in checkpoints(store)
    assert control.holds_snapshot()["total"] == 1
    await brain.aclose()


async def test_e2e_release_speaks_and_discard_keeps_history(tmp_path):
    """面板动作走真 HTTP：release 立刻开口，discard 只标记未投递那批。"""
    from aiohttp.test_utils import TestClient, TestServer

    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path)
    _, app = build_control(service, store, adapter, tmp_path)
    now = int(now_shanghai().timestamp())
    await send_chatter(service, count=6, ts=now, prefix="h")
    await flush_due(service)
    assert server.prompts == []
    before = store.count_messages(ChatType.GROUP, CHAT)

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        released = await client.post(f"/api/sessions/group/{CHAT}/release")
        release_body = await released.json()
        assert released.status == 200
        assert release_body["action"] == "release"
    finally:
        await client.close()

    # release 真调了一次模型并投出去。
    assert len(server.prompts) == 1
    assert adapter.sent
    assert store.pending_batch(ChatType.GROUP, CHAT, "h5") == []

    # 再攒一批，用 discard 丢弃：只写 delivered_messages，不删历史行。
    await send_chatter(service, count=6, ts=int(now_shanghai().timestamp()), prefix="d")
    await flush_due(service)
    held = pending_ids(service)
    assert held, "预期又攒着一批"
    before_discard = store.count_messages(ChatType.GROUP, CHAT)

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        discarded = await client.post(f"/api/sessions/group/{CHAT}/discard")
        discard_body = await discarded.json()
        assert discarded.status == 200
        assert discard_body["count"] == len(held)
    finally:
        await client.close()

    assert all(
        store.skip_reason(ChatType.GROUP, CHAT, mid) == "hold_discard" for mid in held
    )
    assert store.count_messages(ChatType.GROUP, CHAT) == before_discard
    assert before_discard >= before
    await brain.aclose()


# ---------------------------------------------------------------------------
# 冷场快反馈：连续 3 轮无人接 → 非点名批次 hold 15 分钟（GAP-11）
# ---------------------------------------------------------------------------

MIND_ON = MindConfig(enabled=True)
MIND_BACKOFF_OFF = MindConfig(enabled=True, silence_backoff_enabled=False)


def seed_silent_turns(
    store: Store, *, turns: int, base_ts: int, engaged_oldest: bool = False
) -> None:
    """预置 N「轮」无人接话：每轮独立 spoken_at，engaged 全 0。"""
    for i in range(turns):
        store.upsert_reply_outcome(
            ChatType.GROUP,
            CHAT,
            f"b{i}",
            base_ts + i * 1000,
            180,
            engaged=engaged_oldest and i == 0,
            quoted=False,
            replied=False,
            silence_after=True,
        )


async def test_e2e_silence_backoff_holds_non_addressed_batch(tmp_path):
    """连续 3 轮 silent：非点名批次攒着不投，模型一次没调。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_ON)
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=3, base_ts=now - 100_000)

    await service.handle(make_msg("随口一提", message_id="s1", ts=now))
    await service.handle(make_msg("没人接也再说一句", message_id="s2", ts=now))
    await flush_due(service)

    assert server.prompts == []
    assert "silence_backoff" in checkpoints(store)
    # 攒着不是丢弃：游标不动，下次被点名或有问句时连这批一起进 prompt。
    assert sorted(pending_ids(service)) == ["s1", "s2"]
    await brain.aclose()


async def test_e2e_silence_backoff_releases_after_fifteen_minutes(tmp_path):
    """hold_max_seconds=0 只关寿命；15 分钟冷场窗口独立生效，到点放行送模型。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(
        tmp_path, mind=MIND_ON, hold_max_seconds=0
    )
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=3, base_ts=now - 100_000)
    await service.handle(make_msg("随口一提", message_id="r1", ts=now - 1000))
    await service.handle(make_msg("没人接也再说一句", message_id="r2", ts=now - 900))
    await flush_due(service)

    assert len(server.prompts) == 1
    assert "silence_backoff_expired" in checkpoints(store)
    await brain.aclose()


async def test_e2e_force_release_bypasses_silence_backoff(tmp_path):
    """面板「现在说这一次」跳过整个攒着判定，冷场中也直接送模型。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_ON)
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=3, base_ts=now - 100_000)
    await service.handle(make_msg("随口一提", message_id="s1", ts=now))
    await service.handle(make_msg("没人接也再说一句", message_id="s2", ts=now))

    await service._run_batch(ChatType.GROUP, CHAT, force_release=True)

    assert len(server.prompts) == 1
    assert adapter.sent
    await brain.aclose()


async def test_e2e_addressed_batch_bypasses_silence_backoff(tmp_path):
    """被 @ 的批次永远穿透冷场 hold。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_ON)
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=3, base_ts=now - 100_000)

    await service.handle(
        make_msg("@溟月 看这缸水草", message_id="a1", at_bot=True, ts=now)
    )
    await flush_due(service)

    assert len(server.prompts) == 1
    assert "silence_backoff" not in checkpoints(store)
    await brain.aclose()


async def test_e2e_correction_right_after_bot_bypasses_silence_backoff(tmp_path):
    """紧跟它发言的短纠正也算在跟它说话：别被 15 分钟冷场窗口吞掉。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_ON)
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=3, base_ts=now - 100_000)
    store.save_chat_state(ChatType.GROUP, CHAT, {"last_bot_spoke": now - 20})

    await service.handle(make_msg("好像不是这个 是个德语名字", message_id="c1", ts=now))
    await flush_due(service)

    assert len(server.prompts) == 1
    assert "silence_backoff" not in checkpoints(store)
    await brain.aclose()


async def test_e2e_engaged_turn_resets_the_streak(tmp_path):
    """最早一轮有人接：streak 只剩 2，够不到阈值，批次照常送模型。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_ON)
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=3, base_ts=now - 100_000, engaged_oldest=True)

    await service.handle(make_msg("随口一提", message_id="e1", ts=now))
    await service.handle(make_msg("没人接也再说一句", message_id="e2", ts=now))
    await flush_due(service)

    assert store.consecutive_silent_turns(ChatType.GROUP, CHAT, limit=3) == 2
    assert len(server.prompts) == 1
    assert "silence_backoff" not in checkpoints(store)
    await brain.aclose()


async def test_e2e_two_silent_turns_do_not_backoff(tmp_path):
    """只有 2 轮冷场：够不到阈值，不攒（失败探针）。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_ON)
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=2, base_ts=now - 100_000)

    await service.handle(make_msg("随口一提", message_id="t1", ts=now))
    await service.handle(make_msg("没人接也再说一句", message_id="t2", ts=now))
    await flush_due(service)

    assert len(server.prompts) == 1
    assert "silence_backoff" not in checkpoints(store)
    await brain.aclose()


async def test_e2e_same_spoken_at_counts_as_one_silent_turn(tmp_path):
    """同一次回复的多片 chunk 共享 spoken_at，只算 1 轮。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_ON)
    now = int(now_shanghai().timestamp())
    spoken_at = now - 100_000
    for i in range(3):
        store.upsert_reply_outcome(
            ChatType.GROUP,
            CHAT,
            f"b{i}",
            spoken_at,
            180,
            engaged=False,
            quoted=False,
            replied=False,
            silence_after=True,
        )
    assert store.consecutive_silent_turns(ChatType.GROUP, CHAT) == 1

    await service.handle(make_msg("随口一提", message_id="c1", ts=now))
    await service.handle(make_msg("没人接也再说一句", message_id="c2", ts=now))
    await flush_due(service)

    assert len(server.prompts) == 1
    assert "silence_backoff" not in checkpoints(store)
    await brain.aclose()


async def test_e2e_weak_batch_during_cold_streak_still_skips_the_model(tmp_path):
    """冷场期间的单条弱附和：known_silent 优先，mark_delivered，0 模型调用。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_ON)
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=3, base_ts=now - 100_000)
    # 机器人刚说过话（90 秒冷却窗内），弱附和大概率不是在等它。
    store.save_chat_state(ChatType.GROUP, CHAT, {"last_bot_spoke": now - 10})

    await service.handle(make_msg("哈哈", message_id="w1", ts=now))
    await service._run_batch(ChatType.GROUP, CHAT)

    assert server.prompts == []
    assert "silence_backoff" not in checkpoints(store)
    assert "skip_known_silent" in checkpoints(store)
    assert pending_ids(service) == []
    await brain.aclose()


async def test_e2e_silence_backoff_disabled_matches_old_behavior(tmp_path):
    """silence_backoff_enabled=false：与改动前一致，冷场 streak 不产生攒着。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_BACKOFF_OFF)
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=3, base_ts=now - 100_000)

    await service.handle(make_msg("随口一提", message_id="d1", ts=now))
    await service.handle(make_msg("没人接也再说一句", message_id="d2", ts=now))
    await flush_due(service)

    assert len(server.prompts) == 1
    assert "silence_backoff" not in checkpoints(store)
    await brain.aclose()


async def test_e2e_mind_disabled_never_backoffs(tmp_path):
    """mind.enabled=false（老配置）：行为逐字节不变，不触发 backoff。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path)
    now = int(now_shanghai().timestamp())
    seed_silent_turns(store, turns=3, base_ts=now - 100_000)

    await service.handle(make_msg("随口一提", message_id="o1", ts=now))
    await service.handle(make_msg("没人接也再说一句", message_id="o2", ts=now))
    await flush_due(service)

    assert len(server.prompts) == 1
    assert "silence_backoff" not in checkpoints(store)
    await brain.aclose()


async def test_e2e_silence_backoff_and_downshift_are_independent(tmp_path):
    """冷场 hold（GAP-11）与 note_no_reply 的 30 分钟半速是两套信号，互不覆盖。"""
    from dafeiyu.timeutil import now_shanghai

    service, store, brain, adapter, server = build(tmp_path, mind=MIND_ON)
    now = int(now_shanghai().timestamp())

    # 信号一：reply_outcomes 口径的冷场 streak 命中 → 批次攒着。
    seed_silent_turns(store, turns=3, base_ts=now - 100_000)
    await service.handle(make_msg("随口一提", message_id="n1", ts=now))
    await service.handle(make_msg("没人接也再说一句", message_id="n2", ts=now))
    await flush_due(service)
    assert server.prompts == []
    assert "silence_backoff" in checkpoints(store)
    # 但它不碰 router 的半速状态：no_reply_streak / downshift_until 原样。
    st = service.router._state(ChatType.GROUP, CHAT)
    assert st.no_reply_streak == 0
    assert st.downshift_until is None

    # 信号二：router 侧 3 次无人接 → downshift_until（30 分钟半速）。
    held_before = sum(
        1
        for row in store.list_decisions(limit=20, chat_id=CHAT)
        if row["checkpoint"] == "silence_backoff"
    )
    for _ in range(3):
        service.router.note_no_reply(ChatType.GROUP, CHAT)
    st = service.router._state(ChatType.GROUP, CHAT)
    assert st.no_reply_streak == 3
    assert st.downshift_until is not None
    held_after = sum(
        1
        for row in store.list_decisions(limit=20, chat_id=CHAT)
        if row["checkpoint"] == "silence_backoff"
    )
    assert held_after == held_before
    assert store.consecutive_silent_turns(ChatType.GROUP, CHAT, limit=3) == 3
    await brain.aclose()
