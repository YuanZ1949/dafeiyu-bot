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
from dafeiyu.service import Service
from dafeiyu.timeutil import today_shanghai

from test_opencode_http import FakeServer, make_brain

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


def make_msg(
    content: str = "hi",
    *,
    message_id: str = "m0",
    chat_type: ChatType = ChatType.GROUP,
    chat_id: int = 1001,
    user_id: int = 2002,
    at_bot: bool = False,
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
        timestamp=0,
    )


def build(tmp_path, *, server: FakeServer | None = None, clock=NOON):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
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
        adapter,
        router,
        "你是溟月",
        workspace=workspace,
        injection=InjectionConfig(),
    )
    # 钉一条「上线前」的消息当游标下限，后面的消息才会进批次。
    store.add_message(make_msg("上线前的旧消息", message_id="seed"))
    store.mark_delivered(ChatType.GROUP, 1001, "seed")
    return service, store, brain, adapter, server


def pending_ids(service) -> list[str]:
    latest = service.store.recent_messages(ChatType.GROUP, 1001, 1)
    if not latest:
        return []
    return [
        m.message_id
        for m in service.store.pending_batch(ChatType.GROUP, 1001, latest[0].message_id)
    ]


async def test_e2e_batch_reaches_model_and_sends(tmp_path):
    """三条消息（其中一条 @）→ tick → 完整批次 → 模型 → 发出去。"""
    service, store, brain, adapter, server = build(tmp_path)
    store.upsert_summary(ChatType.GROUP, 1001, yesterday(), "昨天聊了鱼缸")
    store.upsert_impression(ChatType.GROUP, 1001, 2002, "trait", "阿鱼：爱养鱼", 0.9)

    await service.handle(make_msg("今天水草长得不错", message_id="m1"))
    await service.handle(make_msg("鱼缸该换水了", message_id="m2"))
    await service.handle(make_msg("@溟月 你看这缸水草", message_id="m3", at_bot=True))

    # 入库是同步的，但模型一次都还没调。
    assert server.prompts == []
    due = service.scheduler.tick()
    assert due == [(ChatType.GROUP, 1001)]
    for key in due:
        await service.scheduler._flush(*key)

    # 一次批次只调一次模型，prompt 里三段注入在最前、时间线在最后。
    assert len(server.prompts) == 1
    prompt = server.prompts[0]
    assert prompt.index("【长期记忆】") < prompt.index(
        "—— 以下是自你上次开口以来的新消息 ——"
    )
    assert "【昨日摘要】" in prompt and "昨天聊了鱼缸" in prompt
    assert "【群友印象】" in prompt and "阿鱼：爱养鱼" in prompt
    assert "（@了你）" in prompt
    for text in ("今天水草长得不错", "鱼缸该换水了", "@溟月 你看这缸水草"):
        assert text in prompt

    # speak=true → 真的发出去，且是模型返回的那句。
    assert adapter.sent == [(ChatType.GROUP, 1001, "在群聊卖萌")]
    assert server.created_with.get("agent") == "deepseek-qq"
    assert store.get_opencode_session(ChatType.GROUP, 1001) == "ses_new"

    # 游标推进、按点名记账、session 统计累加。
    assert pending_ids(service) == []
    calls, tokens = store.usage_today_by_purpose(
        ChatType.GROUP, 1001, store.today(), ("reply",)
    )
    assert calls == 1 and tokens == 135
    stats = store.session_stats(ChatType.GROUP, 1001)
    assert stats.delivered_count == 3
    assert stats.token_estimate == 135
    await brain.aclose()


async def test_e2e_rotation_switches_session_and_injects_continuation(tmp_path):
    """到达条数阈值 → 交接摘要 → 换 session → 首轮含【接续】且摘要只出现一次。"""
    server = MultiSessionServer(answer="大家在聊鱼缸和水草")
    service, store, brain, _, _ = build(tmp_path, server=server)
    store.set_opencode_session(ChatType.GROUP, 1001, "ses_old")
    store.bump_session_stats(ChatType.GROUP, 1001, messages=60, tokens=0)

    await service.handle(make_msg("接着聊", message_id="m1"))
    due = service.scheduler.tick()
    assert due == [(ChatType.GROUP, 1001)]
    for key in due:
        await service.scheduler._flush(*key)

    prompt = server.prompts[-1]
    assert "【接续】" in prompt
    assert prompt.count("大家在聊鱼缸和水草") == 1
    # 临时摘要 session 用完即删；旧 session 按设计保留，只断开映射。
    assert ("DELETE", "/api/session/ses_1") in server.calls
    assert ("DELETE", "/api/session/ses_old") not in server.calls
    assert store.get_opencode_session(ChatType.GROUP, 1001) == "ses_2"
    assert store.session_stats(ChatType.GROUP, 1001).delivered_count == 1
    await brain.aclose()


async def test_e2e_forget_clears_memory_but_keeps_notes(tmp_path):
    """§14.3-5：/forget 清库 + 删远端 session，但笔记文件不动。"""
    service, store, brain, adapter, server = build(tmp_path)
    store.set_opencode_session(ChatType.GROUP, 1001, "ses_old")
    store.add_message(make_msg("记住我", message_id="m1"))

    workspace = service.workspace
    chat_file = workspace.chat_path(ChatType.GROUP, 1001)
    chat_file.parent.mkdir(parents=True, exist_ok=True)
    chat_file.write_text("- 阿鱼喜欢水草\n", encoding="utf-8")

    await service.handle(make_msg("/forget", message_id="f1", user_id=2002))

    assert store.count_messages(ChatType.GROUP, 1001) == 0
    assert store.get_opencode_session(ChatType.GROUP, 1001) is None
    assert ("DELETE", "/api/session/ses_old") in server.calls
    # 笔记是模型自己写的资产，/forget 只清对话记忆，不能连文件一起删。
    assert workspace.global_path().exists()
    assert "喜欢水草和沉木" in workspace.global_path().read_text(encoding="utf-8")
    assert chat_file.exists()
    assert "阿鱼喜欢水草" in chat_file.read_text(encoding="utf-8")
    assert adapter.sent[-1] == (ChatType.GROUP, 1001, "记忆清空了，本鱼失忆中……🫧")
    await brain.aclose()


async def test_e2e_quiet_hours_never_calls_model(tmp_path):
    """静音时段：不排批、不发 HTTP、消息带 skipped_reason。"""
    service, store, brain, adapter, server = build(tmp_path, clock=NIGHT)

    await service.handle(make_msg("半夜冒个泡", message_id="m1"))

    assert store.skip_reason(ChatType.GROUP, 1001, "m1") == "quiet"
    assert service.scheduler.pending_chats() == []
    assert service.scheduler.tick() == []
    assert server.calls == []
    assert adapter.sent == []
    await brain.aclose()
