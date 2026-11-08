"""QQ 与微信只在接入端有区别：判定、模型、人格必须是同一套。

需求来源：微信要「不 @ 也回」，且判定与 QQ 一致。实现方式不是给微信开小灶，
而是证明 `Service.handle` 对 `wxg:` 与 `qq:` 会话走的是同一条
批次 → prompt → brain → 发送路径（`channel_of` 只决定用哪个适配器发）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    SocialConfig,
    SpeechConfig,
    TriggerConfig,
    WorkspaceConfig,
)
from dafeiyu.gateway.base import GatewayError
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message, NoticeEvent
from dafeiyu.router import Router
from dafeiyu.service import Service
from dafeiyu.social import POKE_REPLIES

SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 3, 12, 0, tzinfo=SHANGHAI)
QQ_CHAT = "qq:1001"
WX_CHAT = "wxg:0d5c1f2a3b4c"
QQ_PRIVATE = "qq:2002"
WX_PRIVATE = "wxg:8a7b6c5d4e3f"
REPLY_TEXT = "在呢"


class RecordingBrain:
    """model 模式的假大脑：固定开口，记录收到的 prompt。"""

    def __init__(self, text: str = REPLY_TEXT, *, voice: bool = False) -> None:
        self.text = text
        self.voice = voice
        self.calls: list[dict] = []

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        self.calls.append({"chat_type": chat_type, "chat_id": chat_id, "prompt": text})
        return BrainReply(True, self.text, "test", 10, 5, voice=self.voice)

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
    tts: bool = False,
    voice: bool = False,
    qq_segments: bool = False,
    qq_segments_error: Exception | None = None,
    qq_notice: bool = False,
    wx_media: bool = False,
    wx_media_ok: bool = True,
    wx_media_error: Exception | None = None,
    poke_enabled: bool = False,
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
        social=(
            replace(settings.social, poke_enabled=True)
            if poke_enabled
            else settings.social
        ),
        # 语音需要 TTS 与工作区同时打开；工作区落在 tmp 下，不碰仓库 data/。
        speech=SpeechConfig(tts_enabled=True) if tts else settings.speech,
        workspace=(
            WorkspaceConfig(dir=str(tmp_path / "ws")) if tts else settings.workspace
        ),
    )
    brain = RecordingBrain(voice=voice)
    qq = FakeChannelAdapter(
        bot_id="qq:999",
        chat_id=QQ_CHAT,
        segments=qq_segments,
        segments_error=qq_segments_error,
        notice=qq_notice,
    )
    wx = FakeChannelAdapter(
        bot_id="wxg:self",
        chat_id=WX_CHAT,
        media=wx_media,
        media_ok=wx_media_ok,
        media_error=wx_media_error,
    )
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


def private_msg(
    chat_id: str,
    *,
    content: str = "在吗",
    message_id: str = "p1",
) -> Message:
    return Message(
        chat_type=ChatType.PRIVATE,
        chat_id=chat_id,
        user_id=chat_id.split(":")[0] + ":u1",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=False,
        reply_to_bot=False,
        timestamp=0,
    )


def _ok_tts(calls: list[tuple[str, str]] | None = None):
    async def fake(text: str, path: Path, cfg) -> bool:
        if calls is not None:
            calls.append((text, path.name))
        path.write_bytes(b"ID3" + b"\x00" * 64)
        return True

    return fake


def _fail_tts(calls: list[tuple[str, str]] | None = None):
    async def fake(text: str, path: Path, cfg) -> bool:
        if calls is not None:
            calls.append((text, path.name))
        return False

    return fake


def _voice_deliveries(adapter: FakeChannelAdapter) -> int:
    """voice 投递次数：QQ 数成功发出的 record 段，微信数成功的 voice 媒体。"""
    if callable(getattr(adapter, "send_segments", None)):
        return sum(
            1
            for _chat_type, _chat_id, segments in adapter.segments
            for segment in segments
            if segment.get("type") == "record"
        )
    return sum(
        1
        for _chat_type, _chat_id, kind, _path, _name in adapter.media_sent
        if kind == "voice"
    )


def _assert_voice_xor_text(adapter: FakeChannelAdapter) -> None:
    """speak=True 时投递形态必须二选一：一条 voice，或一条纯文本。"""
    assert (_voice_deliveries(adapter), len(adapter.sent)) in {(1, 0), (0, 1)}


def _bot_contents(store: Store, chat_id: str) -> list[str]:
    return [
        message.content
        for message in store.recent_bot_messages_in(ChatType.GROUP, chat_id, 10)
    ]


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


# ---------------------------------------------------------------- 语音出站矩阵


async def test_voice_success_parity_is_one_voice_zero_text_on_both_channels(
    tmp_path, monkeypatch
):
    """同一份 ``voice=True`` 回复：两条通道各投递且只投递一次语音。

    QQ 用 ``send_segments`` 的 ``record`` 段承载，微信用 ``send_media(kind="voice")``；
    两边都不得再补发正文，历史里仍保存原文（只多一条 ``[语音]`` 注记）。
    """
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("dafeiyu.service.synthesize", _ok_tts(calls))
    service, store, _brain, qq, wx = build(
        tmp_path, tts=True, voice=True, qq_segments=True, wx_media=True
    )

    await service.handle(group_msg(QQ_CHAT, message_id="q-voice"))
    await service.handle(group_msg(WX_CHAT, message_id="w-voice"))
    await service.drain()

    # QQ：恰好一段 record，没有 text 段，也没有退化成纯文本。
    assert len(qq.segments) == 1
    types = [segment["type"] for segment in qq.segments[0][2]]
    assert types.count("record") == 1
    assert "text" not in types
    assert qq.sent == []

    # 微信：恰好一次 voice 媒体，没有纯文本补发。
    assert [entry[2] for entry in wx.media] == ["voice"]
    _chat_type, chat_id, kind, path, name = wx.media[0]
    assert (chat_id, kind) == (WX_CHAT, "voice")
    assert Path(path).name == name
    assert wx.sent == []

    _assert_voice_xor_text(qq)
    _assert_voice_xor_text(wx)

    for chat_id in (QQ_CHAT, WX_CHAT):
        contents = _bot_contents(store, chat_id)
        assert len(contents) == 1
        assert REPLY_TEXT in contents[0]
    # 两条通道各合成一次，输入都是原文。
    assert [text for text, _name in calls] == [REPLY_TEXT, REPLY_TEXT]


async def test_voice_synthesis_failure_parity_is_one_text_zero_voice_on_both_channels(
    tmp_path, monkeypatch
):
    """TTS 返回 False 时两条通道都只发一条原文文本，绝不出现 voice 段/媒体。"""
    monkeypatch.setattr("dafeiyu.service.synthesize", _fail_tts())
    service, store, _brain, qq, wx = build(
        tmp_path, tts=True, voice=True, qq_segments=True, wx_media=True
    )

    await service.handle(group_msg(QQ_CHAT, message_id="q-fail"))
    await service.handle(group_msg(WX_CHAT, message_id="w-fail"))
    await service.drain()

    assert qq.sent == [(ChatType.GROUP, QQ_CHAT, REPLY_TEXT)]
    assert wx.sent == [(ChatType.GROUP, WX_CHAT, REPLY_TEXT)]
    assert qq.segments == []
    assert wx.media == []
    assert wx.media_sent == []

    _assert_voice_xor_text(qq)
    _assert_voice_xor_text(wx)

    for chat_id in (QQ_CHAT, WX_CHAT):
        contents = _bot_contents(store, chat_id)
        assert len(contents) == 1 and REPLY_TEXT in contents[0]


@pytest.mark.parametrize("wechat_failure", ["unsupported_false", "gateway_error"])
async def test_wechat_unsupported_media_falls_back_to_text(
    tmp_path, monkeypatch, wechat_failure
):
    """微信桥明确 ``unsupported_media``（或直接抛网关错）时退回原文纯文本。

    与 QQ 消息段被拒收完全同形：1 条纯文本、0 次成功的 voice 投递；
    媒体失败只是加法项丢失，正文不能跟着丢。
    """
    monkeypatch.setattr("dafeiyu.service.synthesize", _ok_tts())
    # unsupported_false = 桥明确回 False；gateway_error = 桥直接抛网关错。
    unsupported = wechat_failure == "unsupported_false"
    media_error = (
        None if unsupported else GatewayError("微信发送失败：unsupported_media")
    )
    service, store, _brain, qq, wx = build(
        tmp_path,
        tts=True,
        voice=True,
        qq_segments=True,
        qq_segments_error=GatewayError("NapCat 拒收语音段"),
        wx_media=True,
        wx_media_ok=not unsupported,
        wx_media_error=media_error,
    )

    await service.handle(group_msg(QQ_CHAT, message_id="q-seg"))
    await service.handle(group_msg(WX_CHAT, message_id="w-media"))
    await service.drain()

    # 两条通道最终都是「一条原文文本」；没有任何成功投递的语音。
    assert qq.sent == [(ChatType.GROUP, QQ_CHAT, REPLY_TEXT)]
    assert qq.segments == []
    assert wx.sent == [(ChatType.GROUP, WX_CHAT, REPLY_TEXT)]
    assert wx.segments == []
    assert [entry[2] for entry in wx.media] == ["voice"]  # 尝试过
    assert _voice_deliveries(qq) == 0
    assert _voice_deliveries(wx) == 0

    _assert_voice_xor_text(qq)
    _assert_voice_xor_text(wx)

    for chat_id in (QQ_CHAT, WX_CHAT):
        contents = _bot_contents(store, chat_id)
        assert len(contents) == 1 and REPLY_TEXT in contents[0]


# ---------------------------------------------------------------- 社交/提醒


async def test_poke_notice_is_qq_only_but_service_gating_is_shared(tmp_path):
    """poke 的通道边界：微信桥协议没有 notice 事件。

    ``WeChatAdapter`` 不实现 ``on_notice``，所以 ``Service.start()`` 只会给
    QQ 适配器注册 notice 处理器——微信私聊根本收不到 poke 回应（不是
    「收到了却不发」）。真正的门控（静音/强度/预算/冷却/每日上限）在
    ``Service._handle_poke`` 里与 ``chat_id`` 前缀无关，两条通道共用同一份；
    这里用 QQ 私聊 poke 证明它只发一条固定短句且绝不调用模型。
    """
    from dafeiyu.gateway.wechat import WeChatAdapter

    assert not hasattr(WeChatAdapter, "on_notice")

    service, _store, brain, qq, wx = build(
        tmp_path, qq_notice=True, poke_enabled=True
    )

    await service.start()
    try:
        assert qq.notice_handlers == [service.handle_notice]
        assert not callable(getattr(wx, "on_notice", None))
        await qq.emit_notice(
            NoticeEvent(
                kind="friend_poke",
                chat_type=ChatType.PRIVATE,
                chat_id=QQ_PRIVATE,
                user_id="qq:999",  # 被戳的是 bot
                operator_id="qq:2002",  # 戳的人
            )
        )
    finally:
        await service.stop()

    assert len(qq.sent) == 1
    _chat_type, chat_id, text = qq.sent[0]
    assert chat_id == QQ_PRIVATE
    assert text in POKE_REPLIES
    # 微信侧一个字节都没发：notice 根本进不了微信适配器。
    assert wx.sent == []
    assert brain.calls == []


async def test_remind_writes_one_pending_row_per_channel(tmp_path):
    """同一个 ``/remind 30 喝水``：qq: 与 wxg: 私聊各写且只写一条待发记录。"""
    service, store, _brain, qq, wx = build(tmp_path)

    for chat_id in (QQ_PRIVATE, WX_PRIVATE):
        await service.handle(
            private_msg(chat_id, content="/remind 30 喝水", message_id=f"r-{chat_id}")
        )

    for chat_id in (QQ_PRIVATE, WX_PRIVATE):
        rows = store.list_reminders(ChatType.PRIVATE, chat_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "pending"
        assert row["content"] == "喝水"
        assert row["creator_id"] == chat_id.split(":")[0] + ":u1"

    # 回执也发回各自通道，不串线；两条通道各一条。
    assert [entry[1] for entry in qq.sent] == [QQ_PRIVATE]
    assert [entry[1] for entry in wx.sent] == [WX_PRIVATE]
