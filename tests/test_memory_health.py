"""记忆健康：面板要能看出「摘要落后几天」「为什么停」。

摘要调度只补「昨天」，一旦配额熔断或 worker 没跑，缺口只写进日志，面板上
完全看不见。这个端点是只读的体检结果：最近 N 天里有消息却没有摘要的日子、
最近一条摘要、熔断状态。不看日志也能判断记忆链路是否正常。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from conftest import TEST_CONFIG_YAML  # noqa: F401  (保持与其它控制口测试同环境)
from test_control_api import make_control, running

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import SHANGHAI
from dafeiyu.workers.failure import QUOTA_KEY


def _message(chat_id: int, when: datetime, text: str = "hi") -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id=42,
        nickname="群友",
        content=text,
        message_id=f"{chat_id}-{int(when.timestamp())}",
        timestamp=int(when.timestamp()),
    )


def _today() -> datetime:
    return datetime.now(SHANGHAI).replace(hour=12, minute=0, second=0, microsecond=0)


def test_pending_summary_days_lists_days_without_summary(tmp_path: Path) -> None:
    """今天不算缺口：摘要次日才生成，今天有消息是常态。"""
    store = Store(tmp_path / "test.db")
    store.init_schema()
    today = _today()
    yesterday = today - timedelta(days=1)
    store.add_message(_message(100, today))
    store.add_message(_message(100, yesterday))
    store.upsert_summary(ChatType.GROUP, 100, yesterday.date().isoformat(), "有摘要")

    assert store.pending_summary_days() == []


def test_pending_summary_days_is_per_chat(tmp_path: Path) -> None:
    """同一天两个群，只要有一个没摘要，这天就算有缺口。"""
    store = Store(tmp_path / "test.db")
    store.init_schema()
    three_days_ago = _today() - timedelta(days=3)
    store.add_message(_message(100, three_days_ago))
    store.add_message(_message(200, three_days_ago))
    store.upsert_summary(ChatType.GROUP, 100, three_days_ago.date().isoformat(), "有摘要")

    assert store.pending_summary_days() == [three_days_ago.date().isoformat()]
    slots = store.pending_summary_slots()
    assert [(slot.chat_id, slot.date, slot.messages) for slot in slots] == [
        (200, three_days_ago.date().isoformat(), 1)
    ]


def test_pending_summary_days_ignores_days_outside_window(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.init_schema()
    store.add_message(_message(100, _today() - timedelta(days=30)))

    assert store.pending_summary_days() == []
    assert store.pending_summary_days(days=45) == [
        (_today() - timedelta(days=30)).date().isoformat()
    ]


async def test_memory_health_endpoint_reports_backlog_and_last_summary(
    tmp_path: Path,
) -> None:
    control, _, app = make_control(tmp_path)
    today = _today()
    two_days_ago = today - timedelta(days=2)
    control.store.add_message(_message(100, today))
    control.store.add_message(_message(100, two_days_ago))
    control.store.upsert_summary(
        ChatType.GROUP, 100, (today - timedelta(days=1)).date().isoformat(), "昨天聊了钓鱼"
    )

    async with running(app) as client:
        resp = await client.get("/api/memory/health")
        payload = await resp.json()

    assert resp.status == 200
    assert payload["ok"] is False
    assert payload["pending_days"] == [two_days_ago.date().isoformat()]
    assert payload["pending_count"] == 1
    assert payload["pending_slots"] == [
        {
            "chat_type": "group",
            "chat_id": 100,
            "date": two_days_ago.date().isoformat(),
            "messages": 1,
        }
    ]
    assert payload["summary_total"] == 1
    assert payload["last_summary"]["chat_id"] == 100
    assert payload["quota"]["paused"] is False


async def test_memory_health_is_ok_when_only_today_has_messages(
    tmp_path: Path,
) -> None:
    """今天有消息、昨天已摘要 → 没有落后，面板不该报警。"""
    control, _, app = make_control(tmp_path)
    today = _today()
    control.store.add_message(_message(100, today))
    control.store.upsert_summary(
        ChatType.GROUP,
        100,
        (today - timedelta(days=1)).date().isoformat(),
        "昨天的摘要",
    )

    async with running(app) as client:
        payload = await (await client.get("/api/memory/health")).json()

    assert payload["ok"] is True
    assert payload["pending_days"] == []
    assert payload["pending_slots"] == []


def test_memory_health_does_not_count_yesterday_before_four_am(
    tmp_path: Path,
) -> None:
    """摘要次日 4 点才生成：凌晨 3 点不能把「昨天」算成缺口。"""
    control, _, _ = make_control(tmp_path)
    today = _today()
    yesterday = today - timedelta(days=1)
    control.store.add_message(_message(100, yesterday))

    early = today.replace(hour=3)
    late = today.replace(hour=5)

    assert control.memory_health(now=early)["pending_days"] == []
    assert control.memory_health(now=early)["ok"] is True
    assert control.memory_health(now=late)["pending_days"] == [
        yesterday.date().isoformat()
    ]
    assert control.memory_health(now=late)["ok"] is False


async def test_memory_health_endpoint_reports_quota_pause(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    until = time.time() + 600
    control.store.save_backoff(QUOTA_KEY, until)

    async with running(app) as client:
        payload = await (await client.get("/api/memory/health")).json()

    assert payload["ok"] is False
    assert payload["quota"]["paused"] is True
    assert abs(payload["quota"]["until"] - until) < 1
