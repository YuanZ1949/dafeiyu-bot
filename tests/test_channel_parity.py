"""QQ 与微信只在接入端有区别：判定、模型、人格必须是同一套。

需求来源：微信要「不 @ 也回」，且判定与 QQ 一致。实现方式不是给微信开小灶，
而是证明 `Service.handle` 对 `wxg:` 与 `qq:` 会话走的是同一条
批次 → prompt → brain → 发送路径（`channel_of` 只决定用哪个适配器发）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeChannelAdapter

from dafeiyu.behavior import BehaviorSettings
from dafeiyu.brain.client import BrainReply
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
NOON = datetime(2026, 11, 3, 12, 0, tzinfo=SHANGHAI)
QQ_CHAT = "qq:1001"
WX_CHAT = "wxg:0d5c1f2a3b4c"


class RecordingBrain:
    """model 模式的假大脑：固定开口，记录收到的 prompt。"""

    def __init__(self, text: str = "在呢") -> None:
        self.text = text
        self.calls: list[dict] = []

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        self.calls.append({"chat_type": chat_type, "chat_id": chat_id, "prompt": text})
        return BrainReply(True, self.text, "test", 10, 5)

    async def aclose(self) -> None:
        pass


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
        persona=PersonaConfig("大肥鱼", ("大肥鱼",), 120, 300, 0.05),
        triggers=TriggerConfig(
            60,
            0.0,
            ("00:00", "00:00"),
            ("怎么",),
            mode="model",
            batch_seconds=0.0,
        ),
        # 空白名单 = 不按群过滤，两条通道判定条件完全一致。
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


def build(
    tmp_path,
    *,
    clock: Callable[[], datetime] | None = None,
    chat_intensity: str = "normal",
):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    settings = replace(
        settings,
        # closed_sample_rate 已经废弃（收尾不再抽样放行）；明确置 0 是个
        # 金丝雀：判定完全不依赖这个字段，行为只取决于 chat_intensity 本身。
        triggers=replace(settings.triggers, closed_sample_rate=0.0),
        behavior=BehaviorSettings(chat_intensity=chat_intensity),
    )
    brain = RecordingBrain()
    qq = FakeChannelAdapter(bot_id="qq:999", chat_id=QQ_CHAT)
    wx = FakeChannelAdapter(bot_id="wxg:self", chat_id=WX_CHAT)
    router = Router(settings, store, clock=clock or (lambda: NOON))
    service = Service(
        settings,
        store,
        brain,
        {"qq": qq, "wechat": wx},
        router,
        "你是大肥鱼 $max_chars",
    )
    return service, store, brain, qq, wx


def group_msg(
    chat_id: str,
    *,
    content: str = "今天风好大",
    message_id: str = "m1",
    at_bot: bool = False,
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id=chat_id.split(":")[0] + ":u1",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=at_bot,
        reply_to_bot=False,
        timestamp=0,
    )


async def test_wechat_non_at_message_reaches_model_and_sends(tmp_path):
    """不 @ 的微信群消息在 model 模式必须走到大脑并经由微信适配器发出。"""
    service, store, brain, qq, wx = build(tmp_path)

    await service.handle(group_msg(WX_CHAT))
    await service.drain()

    assert [call["chat_id"] for call in brain.calls] == [WX_CHAT]
    assert [sent[1] for sent in wx.sent] == [WX_CHAT]
    # 不能串到 QQ 适配器
    assert qq.sent == []
    # 消息被正常消化（不是被 mark_skipped 丢在待投队列里）
    assert store.skip_reason(ChatType.GROUP, WX_CHAT, "m1") is None


async def test_qq_and_wechat_share_the_same_ambient_path(tmp_path):
    """同一条非 @ 消息，两条通道产出同构结果（只差会话 id / 适配器）。"""
    service, _store, brain, qq, wx = build(tmp_path)

    await service.handle(group_msg(QQ_CHAT, message_id="q1"))
    await service.handle(group_msg(WX_CHAT, message_id="w1"))
    await service.drain()

    assert [call["chat_id"] for call in brain.calls] == [QQ_CHAT, WX_CHAT]
    # 同一份 prompt 构建：两条通道的注入段/时间线格式一致
    assert brain.calls[0]["prompt"].count("—— 以下是自你上次开口以来的新消息 ——") == 1
    assert brain.calls[1]["prompt"].count("—— 以下是自你上次开口以来的新消息 ——") == 1
    assert [sent[1] for sent in qq.sent] == [QQ_CHAT]
    assert [sent[1] for sent in wx.sent] == [WX_CHAT]


@pytest.mark.parametrize(
    ("content", "at_bot"),
    [
        ("今天风好大", False),
        ("@大肥鱼 在吗", True),
        ("这个怎么弄", False),
    ],
)
def test_judgment_is_identical_across_channels(tmp_path, content, at_bot):
    """判定只看消息内容/状态，不看通道前缀。"""
    service, _store, _brain, _qq, _wx = build(tmp_path)

    qq_verdict = service.explain_trigger(
        group_msg(QQ_CHAT, content=content, at_bot=at_bot), now=NOON
    )
    wx_verdict = service.explain_trigger(
        group_msg(WX_CHAT, content=content, at_bot=at_bot), now=NOON
    )

    assert wx_verdict["verdict"] == qq_verdict["verdict"]
    assert wx_verdict["reason"] == qq_verdict["reason"]
    assert wx_verdict["hard_source"] == qq_verdict["hard_source"]


async def test_active_intensity_opens_closed_and_downshifted_threads_for_both_channels(
    tmp_path,
):
    """非点名消息收尾后照常进模型；降频窗口在 quiet/normal 档仍拦截。

    状态全部用公开 API 制造，不碰私有属性，两条通道流程完全相同：
    - 收尾（只作观测/提示信号，不再拦截消息）：router.note_bot_spoke(
      ambient=True) 连发到 ambient_max_turns 上限，is_thread_closed 会把
      thread_closed_until 钉到 now + thread_closed_seconds；
    - 降频：router.note_no_reply 连续 3 次（连续三批选择不说）后进入
      downshift_until 降频窗口。
    时钟是可变 holder，停在 NOON：两个窗口离过期都远。收尾不再拦截，
    所以 normal 档被拦的原因只剩降频（消息不带问句/接话请求）。
    """
    verdicts: dict[str, dict[str, object]] = {}
    for chat, tag in ((QQ_CHAT, "qq"), (WX_CHAT, "wx")):
        holder = {"now": NOON}
        service, store, brain, qq, wx = build(
            tmp_path / tag,
            chat_intensity="normal",
            clock=lambda h=holder: h["now"],
        )
        adapter = qq if chat == QQ_CHAT else wx
        router = service.router

        # 制造收尾 + 降频：正常第 2 次即收尾，10 次上限只为防配置改动。
        for _ in range(10):
            if router.is_thread_closed(ChatType.GROUP, chat):
                break
            router.note_bot_spoke(ChatType.GROUP, chat, ambient=True)
        for _ in range(3):
            router.note_no_reply(ChatType.GROUP, chat)
        assert router.is_thread_closed(ChatType.GROUP, chat)
        assert router.is_downshifted(ChatType.GROUP, chat)

        # normal：非 @ 消息在降频窗口里被拦下，不进模型、不从适配器发出。
        # （话题收尾已不再拦截。）但不做终局丢弃（不 mark_skipped）：
        # 下次被点名时还要能一起进 prompt。
        normal_msg = group_msg(chat, message_id=f"n-{tag}")
        await service.handle(normal_msg)
        await service.drain()
        assert brain.calls == []
        assert adapter.sent == []
        assert store.skip_reason(ChatType.GROUP, chat, normal_msg.message_id) is None
        verdicts[tag] = service.explain_trigger(
            group_msg(chat, message_id=f"e-{tag}"), now=NOON
        )

        # active（更精确地说：非 quiet/normal 档）：同一份收尾/降频状态下，
        # 新的非 @ 消息不走降频闸门，照常排批、进模型并发送。
        service.set_behavior(BehaviorSettings(chat_intensity="active"))
        active_msg = group_msg(chat, message_id=f"a-{tag}")
        await service.handle(active_msg)
        await service.drain()
        assert [call["chat_id"] for call in brain.calls] == [chat]
        assert [sent[1] for sent in adapter.sent] == [chat]
        assert store.skip_reason(ChatType.GROUP, chat, active_msg.message_id) is None

    # 判定与通道无关：explain 对 qq:/wxg: 给出的判定字段完全一致。
    assert verdicts["wx"]["verdict"] == verdicts["qq"]["verdict"]
    assert verdicts["wx"]["reason"] == verdicts["qq"]["reason"]
    assert verdicts["wx"]["hard_source"] == verdicts["qq"]["hard_source"]


async def test_set_groups_whitelist_applies_to_both_channels_and_empty_clears(
    tmp_path,
):
    """面板保存的白名单即时生效：非空只拦非 @，清空后立刻恢复不限制。

    QQ 与微信同名群靠 id 前缀区分：名单里只放 qq:1001 时，wxg:... 的非 @
    消息被拦成 whitelist，@ 仍然必回；清空后两条通道都回到「不限制回复对象」。
    """
    holder = {"now": NOON}
    service, store, brain, qq, wx = build(
        tmp_path, clock=lambda h=holder: h["now"]
    )
    service.set_groups(GroupsConfig(frozenset({QQ_CHAT}), frozenset(), frozenset()))

    # 非白名单的微信群：非 @ 消息被拦下，进 skip 账本，不调模型。
    await service.handle(group_msg(WX_CHAT, message_id="w1"))
    await service.drain()
    assert brain.calls == []
    assert wx.sent == []
    assert store.skip_reason(ChatType.GROUP, WX_CHAT, "w1") == "whitelist"

    # 白名单内的 QQ 群：同一条非 @ 消息正常进模型并由 QQ 适配器发出。
    await service.handle(group_msg(QQ_CHAT, message_id="q1"))
    await service.drain()
    assert [call["chat_id"] for call in brain.calls] == [QQ_CHAT]
    assert [sent[1] for sent in qq.sent] == [QQ_CHAT]

    # @ 不受白名单影响：没被列入的微信群被点名仍然必回。
    verdict = service.explain_trigger(
        group_msg(WX_CHAT, message_id="w2", at_bot=True), now=holder["now"]
    )
    assert verdict["hard_source"] == "at"
    assert verdict["verdict"] == "batch"
    assert verdict["reason"] == "batch"

    # 留空 = 不限制回复对象：清空后微信非 @ 消息立刻放行。
    holder["now"] = NOON + timedelta(seconds=120)
    service.set_groups(GroupsConfig(frozenset(), frozenset(), frozenset()))
    await service.handle(group_msg(WX_CHAT, message_id="w3"))
    await service.drain()
    assert [call["chat_id"] for call in brain.calls] == [QQ_CHAT, WX_CHAT]
    assert [sent[1] for sent in wx.sent] == [WX_CHAT]
