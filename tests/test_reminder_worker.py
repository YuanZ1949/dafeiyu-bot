"""提醒轮询 worker：门控、outbox 投递与 at-most-once 语义。

口径：
* 静音/预算不放行时保持 pending，不投递；
* 投递只走 ``_send_outbox``，不调用模型；
* 无论成功、失败还是 enqueue 抛异常，提醒行都在 finally 里变 sent，
  下一轮不会再发同一行（失败重试归 outbox）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dafeiyu.budget import BudgetAction, BudgetDecision
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
from dafeiyu.models import ChatType
from dafeiyu.router import Router
from dafeiyu.service import Service
from dafeiyu.workers.reminders import _deliver_one, deliver_due_reminders, reminder_loop

GROUP = ChatType.GROUP
CHAT = "qq:1001"
CREATOR = "qq:2002"
SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 8, 12, 0, tzinfo=SHANGHAI)


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
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


class CountingBrain:
    """提醒链路不应该调用模型。"""

    def __init__(self) -> None:
        self.calls = 0

    async def reply(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("提醒不应调用模型")

    async def chat(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("提醒不应调用模型")

    async def aclose(self) -> None:
        return None


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.attempts = 0
        self.fail = fail
        self.message_handler: Any = None
        self.recall_handler: Any = None
        self.notice_handler: Any = None

    def on_message(self, handler: Any) -> None:
        self.message_handler = handler

    def on_recall(self, handler: Any) -> None:
        self.recall_handler = handler

    def on_notice(self, handler: Any) -> None:
        self.notice_handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.attempts += 1
        if self.fail:
            raise RuntimeError("bridge down")
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"


def build_service(
    tmp_path: Path, *, adapter_fail: bool = False
) -> tuple[Service, FakeAdapter, CountingBrain, Store]:
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    router = Router(make_settings(), store, clock=lambda: NOON)
    adapter = FakeAdapter(fail=adapter_fail)
    brain = CountingBrain()
    service = Service(make_settings(), store, brain, {"qq": adapter}, router, "test")
    return service, adapter, brain, store


def add_due(store: Store, content: str = "喝水") -> int:
    return store.add_reminder(GROUP, CHAT, CREATOR, content, due_at=0.0)


def reminder_status(store: Store, reminder_id: int) -> dict[str, Any]:
    return next(
        row for row in store.list_reminders(GROUP, CHAT) if row["id"] == reminder_id
    )


def outbox_rows(store: Store) -> list[dict[str, Any]]:
    return store.due_outbox(now=time.time() + 3600)


async def test_due_reminder_is_sent_and_marked(tmp_path: Path) -> None:
    service, adapter, brain, store = build_service(tmp_path)
    reminder_id = add_due(store)

    sent = await deliver_due_reminders(service)

    assert sent == 1
    assert len(adapter.sent) == 1
    chat_type, chat_id, text = adapter.sent[0]
    assert (chat_type, chat_id) == (GROUP, CHAT)
    assert text == "提醒：喝水"
    assert brain.calls == 0
    row = reminder_status(store, reminder_id)
    assert row["status"] == "sent"
    assert row["sent_at"] is not None
    assert row["last_error"] is None
    assert outbox_rows(store) == []


async def test_quiet_window_keeps_reminder_pending(tmp_path: Path) -> None:
    service, adapter, _brain, store = build_service(tmp_path)
    reminder_id = add_due(store)
    service.router.quiet(GROUP, CHAT, 10)

    sent = await deliver_due_reminders(service)

    assert sent == 0
    assert adapter.sent == []
    assert reminder_status(store, reminder_id)["status"] == "pending"


async def test_non_allow_budget_keeps_reminder_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, _brain, store = build_service(tmp_path)
    reminder_id = add_due(store)

    def degraded(*args: Any, **kwargs: Any) -> BudgetDecision:
        return BudgetDecision(BudgetAction.DEGRADE, "budget_test", "per_chat")

    monkeypatch.setattr(service.budget_guard, "evaluate", degraded)

    assert await deliver_due_reminders(service) == 0
    assert adapter.sent == []
    assert reminder_status(store, reminder_id)["status"] == "pending"


async def test_send_failure_marks_sent_and_outbox_retries_once(
    tmp_path: Path,
) -> None:
    """adapter 挂掉 → _send_outbox 返回 False 并入队；提醒行终态、只入队一次。"""
    service, adapter, _brain, store = build_service(tmp_path, adapter_fail=True)
    reminder_id = add_due(store)

    assert await deliver_due_reminders(service) == 0
    assert adapter.sent == []
    assert adapter.attempts == 1
    row = reminder_status(store, reminder_id)
    assert row["status"] == "sent"
    assert row["last_error"] is not None
    assert row["last_error"].startswith("send_failed")
    assert len(outbox_rows(store)) == 1

    # 第二轮：提醒行已不在 due 列表里，不会产生第 2 条 outbox 记录。
    assert await deliver_due_reminders(service) == 0
    assert adapter.attempts == 1
    assert len(outbox_rows(store)) == 1


async def test_send_outbox_raising_still_marks_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, _brain, store = build_service(tmp_path)
    reminder_id = add_due(store)

    async def boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_send_outbox", boom)

    assert await deliver_due_reminders(service) == 0
    row = reminder_status(store, reminder_id)
    assert row["status"] == "sent"
    assert row["last_error"] == "RuntimeError: boom"
    assert adapter.sent == []


async def test_enqueue_outbox_raising_still_marks_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, _brain, store = build_service(tmp_path, adapter_fail=True)
    reminder_id = add_due(store)

    def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("enqueue boom")

    monkeypatch.setattr(store, "enqueue_outbox", broken)

    assert await deliver_due_reminders(service) == 0
    assert adapter.sent == []
    row = reminder_status(store, reminder_id)
    assert row["status"] == "sent"
    assert row["last_error"] is not None
    assert "enqueue boom" in row["last_error"]


async def test_cancelled_reminder_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停机取消：CancelledError 不落 sent，提醒保持 pending 等下一轮重投。"""
    service, adapter, _brain, store = build_service(tmp_path)
    reminder_id = add_due(store)
    marks: list[int] = []
    real_mark = store.mark_reminder_sent

    def spy_mark(reminder_id: int, last_error: str | None = None) -> bool:
        marks.append(reminder_id)
        return bool(real_mark(reminder_id, last_error=last_error))

    async def cancelled(*args: Any, **kwargs: Any) -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_send_outbox", cancelled)
    monkeypatch.setattr(store, "mark_reminder_sent", spy_mark)
    row = next(r for r in store.due_reminders(time.time()) if r["id"] == reminder_id)

    with pytest.raises(asyncio.CancelledError):
        await _deliver_one(service, row)

    assert marks == []
    assert adapter.sent == []
    assert reminder_status(store, reminder_id)["status"] == "pending"


async def test_loop_exits_within_one_interval(tmp_path: Path) -> None:
    service, _adapter, _brain, _store = build_service(tmp_path)
    stop = asyncio.Event()
    interval = 0.5
    task = asyncio.create_task(reminder_loop(service, interval=interval, stop=stop))
    # 让第一轮跑完，loop 进入 stop.wait()。
    await asyncio.sleep(0.05)
    stop.set()

    await asyncio.wait_for(task, timeout=interval)
    assert task.done()
