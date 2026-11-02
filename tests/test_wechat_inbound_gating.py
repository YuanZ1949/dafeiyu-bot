"""IS-3：微信来源的普通消息必须走三层闸门，未 @ 时绝不进模型/发送。

覆盖真实 `Service.handle` 路径（不是 QQ router 单测，也不是 `/help` 命令路径）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fakes import FakeChannelAdapter
from test_service import FakeBrain, make_settings

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service

WX_CHAT = "wxg:0d5c1f2a3b4c"
NOON = datetime(2026, 11, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _service(tmp_path: Path):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    wx = FakeChannelAdapter()
    qq = FakeChannelAdapter(bot_id="qq:999", chat_id="qq:1001")
    brain = FakeBrain()
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(
        settings,
        store,
        brain,
        {"qq": qq, "wechat": wx},
        router,
        "你是溟月 $max_chars",
    )
    return service, router, wx, brain


def _wx_msg(*, at_bot: bool, content: str) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=WX_CHAT,
        user_id="wxg:u1",
        nickname="阿鱼",
        content=content,
        message_id="m1",
        segments=[],
        is_bot=False,
        at_bot=at_bot,
        reply_to_bot=False,
        timestamp=0,
    )


async def test_wechat_non_at_message_is_gated_before_brain_and_send(tmp_path):
    service, router, wx, brain = _service(tmp_path)
    msg = _wx_msg(at_bot=False, content="今天风好大")

    decision = router.should_handle(msg)
    assert decision.call_model is False

    await service.handle(msg)

    assert brain.calls == 0
    assert wx.sent == []


async def test_wechat_at_bot_message_produces_exactly_one_reply(tmp_path):
    service, _router, wx, brain = _service(tmp_path)

    await service.handle(_wx_msg(at_bot=True, content="在吗"))

    assert brain.calls == 1
    assert len(wx.sent) == 1
    _chat_type, chat_id, text = wx.sent[0]
    assert chat_id == WX_CHAT
    assert text == "在呢"
