"""表情回应改成模型可选：默认不贴，只有回复 JSON 显式要才贴一次。

用户反馈「每条回复气泡下面都被贴了一个表情」不合适，所以发送链路不再无条件
调用 ``set_msg_emoji_like``；这里直接跑 ``_send_batch_reply``，用能记录调用的
假适配器证明默认路径一次都不调、模型要求时只贴本轮第一条。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

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

GROUP = "qq:1001"
WX_GROUP = "wxg:0d5c1f2a3b4c"


class ReactingAdapter(FakeChannelAdapter):
    """QQ 形态的假适配器：多一个 ``set_msg_emoji_like``，记录每次调用。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(bot_id="qq:999", chat_id=GROUP, **kwargs)
        self.reactions: list[tuple[str, str]] = []

    async def set_msg_emoji_like(
        self, chat_type: ChatType, chat_id: str, message_id: str, emoji_id: str
    ) -> bool:
        self.reactions.append((message_id, emoji_id))
        return True


class UnusedBrain:
    """用例直接调 ``_send_batch_reply``，模型不该被碰到。"""

    async def reply(self, *args: Any, **kwargs: Any) -> BrainReply:
        raise AssertionError("这组用例不该调用模型")

    async def aclose(self) -> None:
        pass


async def _no_sleep(seconds: float) -> None:
    return None


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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


def build(
    tmp_path, *, emoji_reactions: bool = True
) -> tuple[Service, ReactingAdapter]:
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    settings = replace(
        settings,
        mind=replace(settings.mind, emoji_reactions=emoji_reactions),
        behavior=BehaviorSettings(),
    )
    adapter = ReactingAdapter()
    service = Service(
        settings,
        store,
        UnusedBrain(),
        {"qq": adapter},
        Router(settings, store),
        "你是大肥鱼 $max_chars",
        sleep=_no_sleep,
    )
    return service, adapter


def group_msg(chat_id: str = GROUP, *, message_id: str = "m1") -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id=chat_id.split(":")[0] + ":u1",
        nickname="阿鱼",
        content="今天风好大",
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=True,
        reply_to_bot=False,
        timestamp=0,
    )


async def test_reply_without_reaction_never_touches_emoji(tmp_path) -> None:
    """默认路径：模型 JSON 没有 reaction，一次 set_msg_emoji_like 都不该发。"""
    service, adapter = build(tmp_path)

    sent = await service._send_batch_reply(
        [group_msg()], BrainReply(True, "在呢"), ChatType.GROUP, GROUP
    )

    assert [chunk for chunk, _message_id in sent] == ["在呢"]
    assert adapter.reactions == []


async def test_reaction_name_maps_to_face_id_on_first_message(tmp_path) -> None:
    """显式 reaction="微笑"：只给第一条回复贴，名称解析成 face id 14。"""
    service, adapter = build(tmp_path)

    sent = await service._send_batch_reply(
        [group_msg()],
        BrainReply(True, "在呢", reaction="微笑"),
        ChatType.GROUP,
        GROUP,
    )

    assert [message_id for _chunk, message_id in sent] == ["deadbeef:1"]
    assert adapter.reactions == [("deadbeef:1", "14")]


async def test_multi_part_reply_reacts_only_once(tmp_path) -> None:
    """分片成两条发：表情仍然只贴一次，贴在第一条上。"""
    service, adapter = build(tmp_path)
    reply = BrainReply(True, "一二", parts=("一", "二"), reaction="微笑")

    sent = await service._send_batch_reply(
        [group_msg()], reply, ChatType.GROUP, GROUP
    )

    assert [chunk for chunk, _message_id in sent] == ["一", "二"]
    assert adapter.reactions == [("deadbeef:1", "14")]


async def test_unlisted_reaction_name_is_skipped_quietly(tmp_path) -> None:
    """表情表里没有的名字：不调用、不抛错，正文照样发出。"""
    service, adapter = build(tmp_path)

    sent = await service._send_batch_reply(
        [group_msg()],
        BrainReply(True, "在呢", reaction="流口水"),
        ChatType.GROUP,
        GROUP,
    )

    assert len(sent) == 1
    assert adapter.reactions == []


async def test_master_switch_off_suppresses_model_request(tmp_path) -> None:
    """总闸 mind.emoji_reactions=false：模型写了 reaction 也不发。"""
    service, adapter = build(tmp_path, emoji_reactions=False)

    sent = await service._send_batch_reply(
        [group_msg()],
        BrainReply(True, "在呢", reaction="微笑"),
        ChatType.GROUP,
        GROUP,
    )

    assert len(sent) == 1
    assert adapter.reactions == []


async def test_wechat_adapter_without_capability_stays_silent(tmp_path) -> None:
    """微信适配器没有 set_msg_emoji_like：静默跳过，不影响发送。"""
    service, _adapter = build(tmp_path)
    wx = FakeChannelAdapter(bot_id="wxg:self", chat_id=WX_GROUP)
    service.adapters["wechat"] = wx

    sent = await service._send_batch_reply(
        [group_msg(WX_GROUP)],
        BrainReply(True, "在呢", reaction="微笑"),
        ChatType.GROUP,
        WX_GROUP,
    )

    assert [chunk for chunk, _message_id in sent] == ["在呢"]
    assert [text for _chat_type, _chat_id, text in wx.sent] == ["在呢"]
