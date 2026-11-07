"""`/remind`：非对称权限、参数校验、列表/取消，回复走普通发送路径。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dafeiyu.brain.client import BrainReply
from dafeiyu.commands import CommandRegistry
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
from dafeiyu.service import REMIND_USAGE, Service

SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 8, 12, 0, tzinfo=SHANGHAI)

GROUP_CHAT = "qq:1001"
PRIVATE_CHAT = "qq:99"
ADMIN = "qq:2002"
ADMIN2 = "qq:3003"
NON_ADMIN = "qq:99"


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
        groups=GroupsConfig(frozenset(), frozenset(), admins),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []

    def on_message(self, handler: Any) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"


class SilentBrain:
    """命令分支不应调用模型；万一走到模型路径也只数次数、不发言。"""

    def __init__(self) -> None:
        self.calls = 0

    async def reply(self, *args: Any, **kwargs: Any) -> BrainReply:
        self.calls += 1
        return BrainReply(False, "", "test", 0, 0)

    async def decide(self, *args: Any, **kwargs: Any) -> BrainReply:
        self.calls += 1
        return BrainReply(False, "", "test", 0, 0)

    async def aclose(self) -> None:
        return None


def build(
    tmp_path: Path, *, admins: frozenset[str] = frozenset({ADMIN})
) -> tuple[Service, Store, FakeAdapter, SilentBrain]:
    settings = make_settings(admins)
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    router = Router(settings, store, clock=lambda: NOON)
    adapter = FakeAdapter()
    brain = SilentBrain()
    service = Service(settings, store, brain, {"qq": adapter}, router, "test")
    return service, store, adapter, brain


def make_msg(
    content: str,
    *,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = GROUP_CHAT,
    user_id: str = ADMIN,
    message_id: str = "r1",
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
        at_bot=False,
        reply_to_bot=False,
        timestamp=0,
    )


def pending(store: Store, chat_type: ChatType, chat_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in store.list_reminders(chat_type, chat_id)
        if row["status"] == "pending"
    ]


def test_help_visibility_matches_asymmetric_permission() -> None:
    registry = CommandRegistry()

    assert "/remind" not in registry.help_text(ChatType.GROUP, is_admin=False)
    assert "/remind" in registry.help_text(ChatType.GROUP, is_admin=True)
    assert "/remind" in registry.help_text(ChatType.PRIVATE, is_admin=False)

    matched = registry.match("/提醒", ChatType.PRIVATE)
    assert matched is not None
    spec, _args = matched
    assert spec.name == "remind"
    assert spec.admin_only is True
    assert spec.cooldown_seconds == 0


async def test_private_remind_creates_pending_reminder(tmp_path: Path) -> None:
    service, store, adapter, brain = build(tmp_path)

    before = time.time()
    await service.handle(
        make_msg(
            "/remind 30 喝水",
            chat_type=ChatType.PRIVATE,
            chat_id=PRIVATE_CHAT,
            user_id=NON_ADMIN,
        )
    )
    after = time.time()

    rows = store.list_reminders(ChatType.PRIVATE, PRIVATE_CHAT)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "pending"
    assert row["content"] == "喝水"
    assert row["creator_id"] == NON_ADMIN
    assert before + 30 * 60 <= float(row["due_at"]) <= after + 30 * 60

    assert brain.calls == 0
    assert len(adapter.sent) == 1
    reply = adapter.sent[0][2]
    assert f"#{row['id']}" in reply
    assert "喝水" in reply


async def test_group_non_admin_is_silent_and_writes_nothing(tmp_path: Path) -> None:
    service, store, adapter, brain = build(tmp_path)

    await service.handle(
        make_msg("/remind 30 喝水", user_id=NON_ADMIN, message_id="g1")
    )

    assert store.list_reminders(ChatType.GROUP, GROUP_CHAT) == []
    assert adapter.sent == []
    assert brain.calls == 0


async def test_group_admin_creates_reminder(tmp_path: Path) -> None:
    service, store, adapter, _brain = build(tmp_path)

    await service.handle(make_msg("/提醒 5 收盘提醒", user_id=ADMIN, message_id="g2"))

    rows = pending(store, ChatType.GROUP, GROUP_CHAT)
    assert len(rows) == 1
    assert rows[0]["content"] == "收盘提醒"
    assert rows[0]["creator_id"] == ADMIN
    assert len(adapter.sent) == 1
    assert f"#{rows[0]['id']}" in adapter.sent[0][2]


async def test_list_shows_pending_reminder(tmp_path: Path) -> None:
    service, store, adapter, _brain = build(tmp_path)

    await service.handle(
        make_msg(
            "/remind 30 喝水",
            chat_type=ChatType.PRIVATE,
            chat_id=PRIVATE_CHAT,
            user_id=NON_ADMIN,
            message_id="l1",
        )
    )
    reminder_id = pending(store, ChatType.PRIVATE, PRIVATE_CHAT)[0]["id"]

    await service.handle(
        make_msg(
            "/remind list",
            chat_type=ChatType.PRIVATE,
            chat_id=PRIVATE_CHAT,
            user_id=NON_ADMIN,
            message_id="l2",
        )
    )

    reply = adapter.sent[-1][2]
    assert f"#{reminder_id}" in reply
    assert "喝水" in reply
    assert "待发" in reply


async def test_cancel_only_works_for_creator(tmp_path: Path) -> None:
    service, store, adapter, _brain = build(
        tmp_path, admins=frozenset({ADMIN, ADMIN2})
    )

    await service.handle(make_msg("/remind 30 交作业", user_id=ADMIN, message_id="c1"))
    reminder_id = pending(store, ChatType.GROUP, GROUP_CHAT)[0]["id"]

    await service.handle(
        make_msg(
            f"/remind cancel {reminder_id}", user_id=ADMIN2, message_id="c2"
        )
    )
    assert "没找到" in adapter.sent[-1][2]
    assert len(pending(store, ChatType.GROUP, GROUP_CHAT)) == 1

    await service.handle(
        make_msg(
            f"/remind cancel {reminder_id}", user_id=ADMIN, message_id="c3"
        )
    )
    assert "已取消" in adapter.sent[-1][2]
    assert pending(store, ChatType.GROUP, GROUP_CHAT) == []
    rows = store.list_reminders(ChatType.GROUP, GROUP_CHAT)
    assert [row["status"] for row in rows] == ["canceled"]


@pytest.mark.parametrize(
    "content",
    [
        "/remind 0 喝水",
        "/remind abc 喝水",
        "/remind 10081 喝水",
        "/remind 30",
        f"/remind 30 {'水' * 201}",
    ],
)
async def test_invalid_arguments_are_rejected_without_writing(
    tmp_path: Path, content: str
) -> None:
    service, store, adapter, _brain = build(tmp_path)

    await service.handle(
        make_msg(
            content,
            chat_type=ChatType.PRIVATE,
            chat_id=PRIVATE_CHAT,
            user_id=NON_ADMIN,
        )
    )

    assert store.list_reminders(ChatType.PRIVATE, PRIVATE_CHAT) == []
    assert adapter.sent == [(ChatType.PRIVATE, PRIVATE_CHAT, REMIND_USAGE)]


async def test_no_arguments_replies_usage_without_writing(tmp_path: Path) -> None:
    service, store, adapter, _brain = build(tmp_path)

    await service.handle(
        make_msg(
            "/remind",
            chat_type=ChatType.PRIVATE,
            chat_id=PRIVATE_CHAT,
            user_id=NON_ADMIN,
        )
    )

    assert store.list_reminders(ChatType.PRIVATE, PRIVATE_CHAT) == []
    assert adapter.sent == [(ChatType.PRIVATE, PRIVATE_CHAT, REMIND_USAGE)]
