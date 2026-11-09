"""微信失败路径矩阵（plan task 29）：锁屏 / 前台冲突 / 空白名单 / 超频 / 桥重启。

全部离线：不连真微信、不装 pyweixin、不起桥进程。每个用例的名字直接点出
它锁住的闸门；任何断言失败都能定位到具体是哪条失败路径回归了。

闸门编号对应 plan task 29：
① 锁屏拒绝 + wechat_down 告警；② 前台冲突中止且不写 messages 行；
③ 空白名单/空昵称 start() 拒绝；④ send_max_per_hour 超限暂停 + 告警
（静默丢弃为失败）；⑤ 桥重启后回声表从 messages WHERE is_bot=1 重建
（窗口按本地 epoch 秒 timestamp 比较，绝不碰 delivered_messages / created_at）；
⑥ resync 后核心不回填、批调度器不触发；⑦ IS-7 回归：provider=qq 纯 QQ 全链
golden（messages 六元组、brain 提示词与回复、日摘要）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeChannelAdapter
from test_alerts import Clock, FakeChannel, wechat_snapshot
from test_service import make_settings
from test_supervisor_specs import BASE, ENV
from test_wechat_send import WINDOW, FakeClock, FakeDriver, make_config

import dafeiyu.__main__ as bot_main
import dafeiyu.service as service_module
from dafeiyu.brain.client import BrainReply
from dafeiyu.config import (
    AlertRulesConfig,
    AlertsConfig,
    ConfigError,
    WeChatConfig,
    load_settings,
)
from dafeiyu.gateway.base import GatewayError
from dafeiyu.gateway.wechat import WeChatAdapter
from dafeiyu.gateway.wechat_bridge import sender as sender_module
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service
from dafeiyu.supervisor.alerts import AlertManager
from dafeiyu.supervisor.main import build_specs
from dafeiyu.workers.summary import summarize_chat

# --------------------------------------------------------------------------- 假体


class FakeWs:
    """记录 send() 帧的假 websocket：断言「断线期间零帧」用。"""

    def __init__(self) -> None:
        self.frames: list[str] = []

    async def send(self, raw: str) -> None:
        self.frames.append(raw)


def make_wx_config(**overrides: object) -> WeChatConfig:
    base: dict = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 8790,
        "bridge_token": "",
        "account_nickname": "小号",
        # poll_seconds 要小：陈旧健康判定是 3 × poll_seconds。
        "poll_seconds": 0.2,
        "echo_window_seconds": 120.0,
    }
    base.update(overrides)
    return WeChatConfig(**base)


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "t.db")
    store.init_schema()
    return store


def add_msg(
    store: Store,
    *,
    chat_id: str,
    content: str,
    message_id: str,
    is_bot: bool,
    timestamp: int,
    user_id: str = "wxg:user1",
) -> None:
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=chat_id,
            user_id=user_id,
            nickname="溟月" if is_bot else "阿鱼",
            content=content,
            message_id=message_id,
            is_bot=is_bot,
            at_bot=False,
            reply_to_bot=False,
            timestamp=timestamp,
        )
    )


# 闸门 ③：适配器就绪度拒绝在启动后台任务之前发生。
# ---------------------------------------------------------------------- start()


async def test_empty_nickname_start_raises_config_error_with_key_name() -> None:
    # 闸门 ③：account_nickname 为空同样拒绝，且消息里点名这个键。
    adapter = WeChatAdapter(WeChatConfig())

    with pytest.raises(ConfigError, match="account_nickname"):
        await adapter.start()

    assert adapter._task is None


# 闸门 ⑤：断线期间 send() 拒绝且零帧。
# ---------------------------------------------------------------------- send()


async def test_send_while_disconnected_raises_and_writes_no_frame() -> None:
    # 闸门 ⑤：未连接时 send() 不许把帧发给一个不存在的桥。
    adapter = WeChatAdapter(make_wx_config())
    fake_ws = FakeWs()
    adapter._ws = fake_ws
    adapter._socket_open = False

    with pytest.raises(GatewayError, match="未连接"):
        await adapter.send(ChatType.GROUP, "wxg:g1", "不该发出")

    assert fake_ws.frames == []


async def test_connected_goes_false_when_health_is_stale() -> None:
    # 闸门 ⑤：健康帧过期 = 桥进程卡死，connected 必须变 False 并拒发。
    adapter = WeChatAdapter(make_wx_config())
    adapter._socket_open = True
    adapter._health_connected = True
    adapter._last_health_at = time.monotonic() - 3 * 0.2 - 1

    # 新鲜度预算 3 × poll_seconds 已耗尽。
    assert adapter.connected is False

    with pytest.raises(GatewayError, match="未连接"):
        await adapter.send(ChatType.GROUP, "wxg:g1", "过期不发")


# 闸门 ⑤：回声表从 messages WHERE is_bot=1 重建。
# ------------------------------------------------------------------- resync()


async def test_resync_rebuilds_echo_from_messages_not_delivered_messages(
    tmp_path: Path,
) -> None:
    # 闸门 ⑤：bridge 重启 resync 后，回声表 = messages 表窗口内 is_bot=1 行。
    echo_window = 120
    store = make_store(tmp_path)
    now = int(time.time())
    add_msg(
        store,
        chat_id="wxg:aaa",
        content="第一条回复",
        message_id="seed:1",
        is_bot=True,
        timestamp=now - 10,
    )
    add_msg(
        store,
        chat_id="wxg:aaa",
        content="第一条回复",
        message_id="inbound:1",
        is_bot=False,
        timestamp=now - 9,
    )
    add_msg(
        store,
        chat_id="wxg:bbb",
        content="第二条回复",
        message_id="seed:2",
        is_bot=True,
        timestamp=now - 30,
    )
    add_msg(
        store,
        chat_id="wxg:aaa",
        content="过期回复",
        message_id="seed:3",
        is_bot=True,
        timestamp=now - echo_window - 5,
    )

    adapter = WeChatAdapter(make_wx_config(echo_window_seconds=echo_window))
    adapter.set_echo_seed(
        lambda: store.recent_bot_messages(int(time.time()) - echo_window)
    )

    EXPECTED_SQL = (
        "SELECT chat_id, content, timestamp FROM messages"
        " WHERE is_bot=1 ORDER BY id DESC LIMIT 200"
    )
    traced: list[str] = []
    store._conn.set_trace_callback(traced.append)
    try:
        # 只在 resync 这一段开 trace：fixtures 自己的 SQL 不掺和断言。
        traced.clear()
        await adapter.dispatch_envelope({"v": 1, "type": "resync", "reason": "test"})
    finally:
        store._conn.set_trace_callback(None)

    # 重连后回声表必须补回窗口内的两条机器人消息，数量一字不差。
    assert len(adapter._sent_echo) == 2
    # 队首必须是最旧的一条：_is_echo 只从队首按窗口 popleft，顺序错了
    # 过期条目就永远清不掉，旧回复会被误判成回声。
    echo_times = [at for _cid, _text, at in adapter._sent_echo]
    assert echo_times == sorted(echo_times)
    # 匹配键是 (chat_id, text)：过期的第三条与入站的 is_bot=0 行都不许进来。
    assert {(cid, text) for cid, text, _at in adapter._sent_echo} == {
        ("wxg:aaa", "第一条回复"),
        ("wxg:bbb", "第二条回复"),
    }
    # 来源锁定 messages 表（is_bot=1 的那条 SQL 必须逐字出现）。
    assert any(EXPECTED_SQL in stmt for stmt in traced), traced
    # delivered_messages 没有文本列、created_at 是 UTC：谁碰了谁就是错的。
    assert not any("delivered_messages" in stmt for stmt in traced), traced


async def test_resync_does_not_backfill_messages_to_core(tmp_path: Path) -> None:
    # 闸门 ⑥：resync 只重建回声，绝不把历史消息回喂给核心 / 批调度器。
    store = make_store(tmp_path)
    now = int(time.time())
    add_msg(
        store,
        chat_id="wxg:aaa",
        content="第一条回复",
        message_id="seed:1",
        is_bot=True,
        timestamp=now - 10,
    )

    adapter = WeChatAdapter(make_wx_config())
    adapter.set_echo_seed(lambda: [("wxg:aaa", "第一条回复", now - 10)])
    fake_ws = FakeWs()
    adapter._ws = fake_ws
    handler_calls: list[Message] = []

    async def handler(message: Message) -> None:
        handler_calls.append(message)

    adapter.on_message(handler)

    await adapter.dispatch_envelope({"v": 1, "type": "resync", "reason": "test"})

    # 核心的 on_message 处理器一次都不许被触发。
    assert handler_calls == []
    # 也没有任何发送帧（无回溯补发）。
    assert fake_ws.frames == []
    # 但 seed 确实发生了（回声表 +1），证明不是"静默清空"。
    assert len(adapter._sent_echo) == 1


class _SeedRecordingAdapter(FakeChannelAdapter):
    """记录 Service 是否把回声重建源接上（只测接线，不发帧）。"""

    def __init__(self) -> None:
        super().__init__(chat_id="wxg:g1")
        self.seed_provider = None

    def set_echo_seed(self, provider) -> None:
        self.seed_provider = provider


async def test_service_wires_echo_seed_from_messages_window(tmp_path: Path) -> None:
    # 闸门 ⑤（接线）：Service.start() 必须把 store 的窗口查询挂到微信适配器上；
    # 否则 resync 永远走不到重建，单测手挂 seed 会把这条缺口放过去。
    store = make_store(tmp_path)
    now = int(time.time())
    add_msg(
        store,
        chat_id="wxg:g1",
        content="窗口内的回复",
        message_id="seed:fresh",
        is_bot=True,
        timestamp=now - 10,
    )
    add_msg(
        store,
        chat_id="wxg:g1",
        content="窗口外的回复",
        message_id="seed:stale",
        is_bot=True,
        timestamp=now - 500,
    )
    settings = make_settings(wechat=make_wx_config(echo_window_seconds=120))
    adapter = _SeedRecordingAdapter()
    service = Service(
        settings,
        store,
        None,
        {"wechat": adapter},
        Router(settings, store, clock=lambda: None),
        "你是溟月",
    )

    await service.start()
    try:
        assert adapter.seed_provider is not None
        assert adapter.seed_provider() == [("wxg:g1", "窗口内的回复", now - 10)]
    finally:
        await service.stop()


async def test_resync_without_seed_clears_echo_only() -> None:
    # 闸门 ⑤（旧行为锁定）：没挂 seed 的适配器 resync 只清空回声表。
    adapter = WeChatAdapter(make_wx_config())
    adapter._sent_echo.append(("wxg:g1", "旧回复", time.monotonic()))
    fake_ws = FakeWs()
    adapter._ws = fake_ws

    await adapter.dispatch_envelope({"v": 1, "type": "resync", "reason": "test"})

    assert len(adapter._sent_echo) == 0
    assert fake_ws.frames == []


# 闸门 ①②④：桥内 Sender 的发送闸门。
# ------------------------------------------------------------------- sender()


def make_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    driver: FakeDriver,
    *,
    health: Any = None,
    **overrides: object,
):
    monkeypatch.setattr(sender_module, "get_foreground_window", lambda: WINDOW)
    monkeypatch.setattr(sender_module, "last_input_age", lambda: 99.0)
    monkeypatch.setattr(sender_module, "desktop_state", lambda: "interactive")
    instance = sender_module.Sender(
        make_config(tmp_path, **overrides),
        driver=driver,
        clock=clock,
        sleep=clock.sleep,
        health=health,
        uniform=lambda low, high: high,
        session_epoch=7,
    )
    return instance


async def test_sender_desktop_locked_rejects_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 闸门 ①：锁屏时桥侧绝不触碰驱动 —— 发送被拒、驱动零注入。
    clock = FakeClock()
    driver = FakeDriver(clock)
    health = SimpleNamespace(desktop="locked", degraded=False)
    instance = make_sender(tmp_path, monkeypatch, clock, driver, health=health)
    try:
        result = instance.submit("测试群", "锁屏也不许打字")
    finally:
        instance.close(timeout=5.0)

    assert result.ok is False
    assert result.error_code == "desktop_unavailable"
    assert [call for call in driver.calls if call["action"] == "send_text"] == []


async def test_sender_foreground_conflict_aborts_before_driver_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 闸门 ②：驱动窗口 ≠ 真实前台 → 冲突中止，一个字都不许打。
    clock = FakeClock()
    driver = FakeDriver(clock)
    monkeypatch.setattr(sender_module, "get_foreground_window", lambda: WINDOW + 7)
    monkeypatch.setattr(sender_module, "last_input_age", lambda: 99.0)
    instance = sender_module.Sender(
        make_config(tmp_path),
        driver=driver,
        clock=clock,
        sleep=clock.sleep,
        health=SimpleNamespace(desktop="interactive", degraded=False),
        uniform=lambda low, high: high,
        session_epoch=7,
    )
    try:
        result = instance.submit("测试群", "前台不对劲")
    finally:
        instance.close(timeout=5.0)

    assert result.ok is False
    assert result.error_code == "foreground_conflict"
    assert [call for call in driver.calls if call["action"] == "send_text"] == []


async def test_sender_rate_limit_pauses_and_alerts_after_hour_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 闸门 ④：第 21 次（超出 send_max_per_hour）必须明确失败 + 告警，
    # 静默丢弃就是失败路径回归。
    clock = FakeClock()
    driver = FakeDriver(clock)
    instance = make_sender(
        tmp_path,
        monkeypatch,
        clock,
        driver,
        health=SimpleNamespace(desktop="interactive", degraded=False),
        send_max_per_hour=20,
        send_min_interval_seconds=0.0,
    )
    try:
        results = [instance.submit("测试群", f"第{i}条") for i in range(20)]
        capped = instance.submit("测试群", "第21条超限")
    finally:
        instance.close(timeout=5.0)

    assert all(result.ok for result in results)
    # 不是静默丢弃：ok=False 且告警留痕。
    assert capped.ok is False
    assert capped.error_code == "rate_limited"
    assert "rate_limited" in [code for code, _payload in instance.alerts]
    # 驱动注入次数必须停在 20：上限真的把第 21 次挡在桌子外面。
    assert [call for call in driver.calls if call["action"] == "send_text"].__len__() == 20


# 闸门 ①：wechat_down 告警在桌面锁屏后按阈值触发。
# ------------------------------------------------------------------- alerts()


async def test_alert_manager_raises_wechat_down_after_threshold_when_desktop_locked(
    tmp_path: Path,
) -> None:
    # 闸门 ①：desktop=locked → wechat_down 按阈值从 pending 变 firing（critical）。
    clock = Clock()
    channel = FakeChannel()
    mgr = AlertManager(
        AlertsConfig(rules=AlertRulesConfig(wechat_down_seconds=5)),
        tmp_path / "alert-state.json",
        [channel],
        clock=clock,
    )
    snapshot = wechat_snapshot(
        wechat={"connected": False, "logged_in": True, "desktop": "locked"}
    )

    await mgr.evaluate(snapshot)
    assert channel.sent == []

    clock.advance(6)
    await mgr.evaluate(snapshot)

    fired = [
        incident
        for incident, event in channel.sent
        if incident.rule == "wechat_down" and event == "firing"
    ]
    assert len(fired) == 1
    assert fired[0].severity == "critical"
    assert fired[0].detail == "桌面已锁定，微信无法操作"


# 闸门 ②：前台冲突失败在核心侧不写 messages 的 bot 行。
# ------------------------------------------------------------------ service()


class _ConflictWeChatAdapter:
    """send() 第一片就报前台冲突的最小假适配器。"""

    bot_id = "wxg:self"

    def __init__(self) -> None:
        self.handler = None

    def on_message(self, handler) -> None:
        self.handler = handler

    def on_recall(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        raise GatewayError("微信发送失败：foreground_conflict")

    def snapshot(self) -> dict[str, Any]:
        return {"connected": True, "degraded": False}


async def test_service_foreground_conflict_writes_no_message_rows(
    tmp_path: Path,
) -> None:
    # 闸门 ②：发送失败后核心不许沉淀半截回复 —— is_bot=1 的行数为 0。
    store = make_store(tmp_path)
    settings = make_settings()
    adapter = _ConflictWeChatAdapter()
    brain = _FakeBrain(["在呢"])
    router = Router(settings, store, clock=lambda: None)
    service = Service(
        settings, store, brain, {"wechat": adapter}, router, "你是溟月 $max_chars"
    )

    # _reply 吞掉发送异常：断言落在 DB 上，不是落在异常上。
    await service.handle(
        Message(
            chat_type=ChatType.GROUP,
            chat_id="wxg:9f3a11",
            user_id="wxg:u1",
            nickname="阿鱼",
            content="@溟月 在吗",
            message_id="m1",
            is_bot=False,
            at_bot=True,
            reply_to_bot=False,
            timestamp=0,
        )
    )

    row = store._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND is_bot = 1",
        ("wxg:9f3a11",),
    ).fetchone()
    assert row[0] == 0, "前台冲突失败却写出了 bot 行：会把下一轮对话污染成回声"


class _FakeBrain:
    """单回复假脑（同 test_service.FakeBrain 的最小形态）。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    async def decide(self, system, messages, max_tokens=400) -> BrainReply:
        return BrainReply(True, self.replies.pop(0), "test", 10, 5)

    async def aclose(self) -> None:
        pass


# 闸门 ⑦：provider=qq + wechat.enabled=false 的进程与通道闸门。
# ------------------------------------------------------------ build_specs()


def test_build_specs_qq_only_returns_three_processes_and_no_wechat_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_napcat: Path
) -> None:
    # 闸门 ⑦：微信关闭 → 桥不登记、WeChatAdapter 不构造、channel_status 仅 qq。
    config = tmp_path / "config.yaml"
    config.write_text(BASE, encoding="utf-8")
    env_file = tmp_path / "test.env"
    env_file.write_text(ENV, encoding="utf-8")
    settings = load_settings(env_file=env_file, config_file=config)
    assert settings.gateway.provider == "qq"
    assert settings.wechat.enabled is False

    specs = build_specs(settings, "token-abc")
    assert set(specs) == {"opencode", "bot", "napcat"}

    constructed: list[object] = []

    class _SpyWeChatAdapter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            constructed.append(args)
            raise AssertionError("provider=qq 不该构造 WeChatAdapter")

    monkeypatch.setattr(bot_main, "WeChatAdapter", _SpyWeChatAdapter)
    adapters = bot_main.build_adapters(settings)
    assert set(adapters) == {"qq"}
    assert constructed == []

    store = make_store(tmp_path)
    service = Service(
        settings,
        store,
        None,
        adapters=adapters,
        router=Router(settings, store, clock=lambda: None),
        persona_text="test",
    )
    assert set(service.channel_status()) == {"qq"}




# 闸门 ⑦：IS-7 golden —— 纯 QQ 全链路的固定脚本。
# --------------------------------------------------------------- IS-7 golden

SHANGHAI_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=SHANGHAI_TZ)
_T0 = int(datetime(2026, 11, 1, 11, 0, tzinfo=SHANGHAI_TZ).timestamp())
_T1 = int(datetime(2026, 11, 1, 11, 30, tzinfo=SHANGHAI_TZ).timestamp())
_T2 = int(datetime(2026, 11, 1, 11, 40, tzinfo=SHANGHAI_TZ).timestamp())

SCRIPT: tuple[tuple[str, str, int], ...] = (
    ("今天风好大", "m1", _T0),
    ("@溟月 在吗", "m2", _T1),
    ("@溟月 再说一句", "m3", _T2),
)

GOLDEN_MESSAGES = [
    ("qq:1001", "qq:2002", "今天风好大", "m1", 0, 0),
    ("qq:1001", "qq:2002", "@溟月 在吗", "m2", 1, 0),
    ("qq:1001", "qq:999", "在呢", "9f3a7b:1", 0, 0),
    ("qq:1001", "qq:2002", "@溟月 再说一句", "m3", 1, 0),
    ("qq:1001", "qq:999", "再说也是这句", "9f3a7b:2", 0, 0),
]
GOLDEN_SYSTEM = (
    "你是溟月 120 $max_reply_chars_technical"
    "\n\n## 当前场景\n\n"
    "- 群名：本群\n"
    "- 今天：2026-11-01\n"
    "- 长度基调：普通闲聊通常 120 字上下，技术问题通常 300 字上下。\n"
    "  这只是参考，不是硬上限：内容需要就展开，别为凑长度硬撑，也别把答案腰斩。\n"
    "- 想分条发就把 reply 写成字符串数组，每条一个元素；单条尽量不超过 500 字。\n"
)
GOLDEN_PROMPTS = [
    [
        (
            "【此刻】现在是 11-01 周日 12:00（中午）；刚才是 阿鱼 在说。\n\n"
            "—— 以下是自你上次开口以来的新消息 ——\n"
            "[11-01 11:30] 阿鱼：@溟月 在吗（@了你）"
        )
    ],
    [
        (
            "【此刻】现在是 11-01 周日 12:00（中午）；你刚刚才说过话；"
            "刚才是 阿鱼 在说。\n\n"
            "—— 以下是自你上次开口以来的新消息 ——\n"
            "[11-01 11:40] 阿鱼：@溟月 再说一句（@了你）"
        )
    ],
]
GOLDEN_SENT = ["在呢", "再说也是这句"]
GOLDEN_SUMMARY = "小鱼下午挨个回应了群里的问题，说了两句话。"


class _GoldenBrain:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.systems: list[str] = []
        self.batches: list[list[str]] = []

    async def decide(self, system, messages, max_tokens=400) -> BrainReply:
        self.systems.append(system)
        self.batches.append([m["content"] for m in messages])
        return BrainReply(True, self.replies.pop(0), "test", 10, 5)

    async def aclose(self) -> None:
        pass


class _GoldSummaryBrain:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def complete(self, system, messages, max_tokens=400, temperature=0.2) -> str:
        self.calls.append({"system": system, "messages": messages})
        return self.reply


def _golden_msg(content: str, message_id: str, timestamp: int) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="qq:1001",
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        is_bot=False,
        at_bot=content.startswith("@溟月"),
        reply_to_bot=False,
        timestamp=timestamp,
    )


@pytest.fixture
async def scripted_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # provider=qq + wechat.enabled=false（Settings 默认），整条链路只在 QQ 通道上跑。
    monkeypatch.setattr(service_module, "now_shanghai", lambda: FIXED_NOW)
    store = make_store(tmp_path)
    settings = make_settings()
    assert settings.gateway.provider == "qq"
    brain = _GoldenBrain(["在呢", "再说也是这句"])
    adapter = FakeChannelAdapter(bot_id="qq:999", chat_id="qq:1001", epoch="9f3a7b")
    router = Router(settings, store, clock=lambda: FIXED_NOW)
    service = Service(
        settings,
        store,
        brain,
        {"qq": adapter},
        router,
        "你是溟月 $max_chars $max_reply_chars_technical",
    )
    await service.start()
    for content, message_id, ts in SCRIPT:
        await adapter.emit(_golden_msg(content, message_id, ts))
    yield service, store, adapter, brain
    await service.stop()


async def test_is7_qq_golden_message_rows(
    scripted_service,
) -> None:
    _service, store, _adapter, _brain = scripted_service

    rows = store._conn.execute(
        "SELECT chat_id, user_id, content, message_id, at_bot, reply_to_bot"
        " FROM messages ORDER BY id"
    ).fetchall()
    actual = [
        (r["chat_id"], r["user_id"], r["content"], r["message_id"], r["at_bot"], r["reply_to_bot"])
        for r in rows
    ]

    # 六元组序列逐条相等（不是只比条数）：普通消息不回，@ 才回，bot 行的第二
    # 条紧跟自己的触发消息。
    assert actual == GOLDEN_MESSAGES

    # 全程只走 QQ 通道：微信侧零对象掺和就是这条闸门的另一半断言。
    service, _store, _adapter, _brain = scripted_service
    assert set(service.adapters) == {"qq"}


async def test_is7_qq_golden_brain_prompts_and_summary(scripted_service) -> None:
    _service, store, adapter, brain = scripted_service

    # (b) 提示词与批次内容逐字符相等；发送出去的回复逐字相等。
    assert brain.systems == [GOLDEN_SYSTEM, GOLDEN_SYSTEM]
    assert brain.batches == GOLDEN_PROMPTS
    assert [text for _c, _i, text in adapter.sent] == GOLDEN_SENT

    # (c) 固定脚本当天落了消息，日摘要存回 store 的值与 golden 相等。
    summary_brain = _GoldSummaryBrain(GOLDEN_SUMMARY)
    text = await summarize_chat(store, summary_brain, ChatType.GROUP, "qq:1001", "2026-11-01")

    assert text == GOLDEN_SUMMARY
    assert store.get_summary(ChatType.GROUP, "qq:1001", "2026-11-01") == GOLDEN_SUMMARY
