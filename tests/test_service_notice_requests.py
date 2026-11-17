"""QQ 好友/群请求 → 管理员通知：只通知、不自动同意、失败不外泄。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
from dafeiyu.models import ChatType, Message, NoticeEvent
from dafeiyu.router import Router
from dafeiyu.service import Service

QQ_ADMIN = "qq:10001"
WX_ADMIN = "wxg:0d5c1f2a3b4c"
SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)


class RecordingAdapter:
    """最小适配器：记录发送与 call_action，「自动同意」会在断言里现形。"""

    def __init__(self, *, bot_id: str = "qq:999") -> None:
        self.bot_id = bot_id
        self.connected = True
        self.message_handler: Callable[[Message], Awaitable[None]] | None = None
        self.sent: list[tuple[ChatType, str, str]] = []
        self.actions: list[tuple[str, dict[str, Any]]] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.connected = False

    def on_message(self, handler: Callable[[Message], Awaitable[None]]) -> None:
        self.message_handler = handler

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"

    async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
        self.actions.append((action, params))
        return {}


class RaisingAdapter(RecordingAdapter):
    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        raise RuntimeError("网关炸了")


def make_settings(admins: frozenset[str]) -> Settings:
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
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), admins=admins),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


def noon_clock() -> datetime:
    return NOON


def make_service(
    tmp_path: Path, *, admins: frozenset[str], qq: RecordingAdapter | None = None
) -> tuple[Service, RecordingAdapter, RecordingAdapter]:
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(admins)
    qq_adapter = qq if qq is not None else RecordingAdapter()
    wx_adapter = RecordingAdapter(bot_id="wxg:self")
    service = Service(
        settings,
        store,
        None,
        adapters={"qq": qq_adapter, "wechat": wx_adapter},
        router=Router(settings, store, clock=noon_clock),
        persona_text="test",
    )
    return service, qq_adapter, wx_adapter


def friend_request(event_id: str = "friend-flag-1") -> NoticeEvent:
    return NoticeEvent(
        kind="friend_request",
        chat_type=ChatType.PRIVATE,
        chat_id="qq:20002",
        user_id="qq:20002",
        nickname="老王",
        comment="加个好友",
        event_id=event_id,
        sub_type="add",
    )


def group_request(event_id: str = "group-flag-1") -> NoticeEvent:
    return NoticeEvent(
        kind="group_request",
        chat_type=ChatType.GROUP,
        chat_id="qq:30003",
        user_id="qq:20002",
        nickname="群名片",
        comment="求进群",
        event_id=event_id,
        sub_type="invite",
    )


async def test_friend_request_notifies_qq_admin_once(tmp_path: Path) -> None:
    service, qq, wx = make_service(tmp_path, admins=frozenset({QQ_ADMIN}))

    await service.handle_notice(friend_request())

    assert len(qq.sent) == 1
    chat_type, chat_id, text = qq.sent[0]
    assert chat_type is ChatType.PRIVATE
    assert chat_id == QQ_ADMIN
    assert "好友申请" in text and "老王" in text and "qq:20002" in text
    assert wx.sent == []
    assert qq.actions == []


def wechat_friend_request(event_id: str = "fmessage:7") -> NoticeEvent:
    return NoticeEvent(
        kind="friend_request",
        chat_type=ChatType.PRIVATE,
        chat_id="wxg:fmessage",
        user_id="wxg:u1",
        nickname="张三",
        event_id=event_id,
        sub_type="add",
    )


async def test_wechat_friend_request_notice_says_wechat_not_qq(tmp_path: Path) -> None:
    service, qq, _wx = make_service(tmp_path, admins=frozenset({QQ_ADMIN}))

    await service.handle_notice(wechat_friend_request())

    assert len(qq.sent) == 1
    chat_type, chat_id, text = qq.sent[0]
    assert (chat_type, chat_id) == (ChatType.PRIVATE, QQ_ADMIN)
    assert "微信好友申请" in text
    assert "QQ 好友申请" not in text
    assert "张三" in text and "wxg:u1" in text
    assert _wx.sent == []
    assert qq.actions == []


async def test_group_request_notifies_qq_admin_once(tmp_path: Path) -> None:
    service, qq, wx = make_service(tmp_path, admins=frozenset({QQ_ADMIN}))

    await service.handle_notice(group_request())

    assert len(qq.sent) == 1
    chat_type, chat_id, text = qq.sent[0]
    assert chat_type is ChatType.PRIVATE
    assert chat_id == QQ_ADMIN
    assert "群申请" in text and "qq:30003" in text and "群名片" in text
    assert wx.sent == []
    assert qq.actions == []


async def test_duplicate_event_id_notifies_only_once(tmp_path: Path) -> None:
    service, qq, _wx = make_service(tmp_path, admins=frozenset({QQ_ADMIN}))

    await service.handle_notice(friend_request("same-flag"))
    await service.handle_notice(friend_request("same-flag"))

    assert len(qq.sent) == 1


async def test_wxg_admin_routes_to_wechat_adapter(tmp_path: Path) -> None:
    service, qq, wx = make_service(tmp_path, admins=frozenset({WX_ADMIN}))

    await service.handle_notice(friend_request())

    assert qq.sent == []
    assert len(wx.sent) == 1
    assert wx.sent[0][:2] == (ChatType.PRIVATE, WX_ADMIN)


async def test_no_admins_logs_but_sends_nothing(tmp_path: Path, caplog: Any) -> None:
    service, qq, wx = make_service(tmp_path, admins=frozenset())

    with caplog.at_level(logging.INFO, logger="dafeiyu.service"):
        await service.handle_notice(friend_request("no-admin-flag"))

    assert qq.sent == [] and wx.sent == []
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "kind=friend_request" in message and "no-admin-flag" in message for message in messages
    )


async def test_disconnected_adapter_still_logs_and_does_not_raise(
    tmp_path: Path, caplog: Any
) -> None:
    service, qq, _wx = make_service(tmp_path, admins=frozenset({QQ_ADMIN}))
    qq.connected = False

    with caplog.at_level(logging.INFO, logger="dafeiyu.service"):
        await service.handle_notice(group_request("offline-flag"))

    assert qq.sent == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("kind=group_request" in message for message in messages)


async def test_send_failure_is_swallowed(tmp_path: Path, caplog: Any) -> None:
    service, _qq, _wx = make_service(tmp_path, admins=frozenset({QQ_ADMIN}), qq=RaisingAdapter())

    with caplog.at_level(logging.INFO, logger="dafeiyu.service"):
        await service.handle_notice(friend_request("boom-flag"))

    messages = [record.getMessage() for record in caplog.records]
    assert any("发送失败" in message and "boom-flag" in message for message in messages)


async def test_second_request_within_throttle_is_logged_not_sent(
    tmp_path: Path, caplog: Any
) -> None:
    service, qq, _wx = make_service(tmp_path, admins=frozenset({QQ_ADMIN}))

    await service.handle_notice(friend_request("first-flag"))
    with caplog.at_level(logging.INFO, logger="dafeiyu.service"):
        await service.handle_notice(group_request("second-flag"))

    assert len(qq.sent) == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("节流" in message and "second-flag" in message for message in messages)


async def test_notification_text_is_clamped(tmp_path: Path) -> None:
    service, qq, _wx = make_service(tmp_path, admins=frozenset({QQ_ADMIN}))
    event = replace(friend_request("long-flag"), comment="备注" * 1000)

    await service.handle_notice(event)

    assert len(qq.sent) == 1
    assert len(qq.sent[0][2]) == 1000
