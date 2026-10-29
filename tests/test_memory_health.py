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
    store = Store(tmp_path / "test.db")
    store.init_schema()
    today = _today()
    yesterday = today - timedelta(days=1)
    store.add_message(_message(100, today))
    store.add_message(_message(100, yesterday))
    store.upsert_summary(ChatType.GROUP, 100, yesterday.date().isoformat(), "有摘要")

    assert store.pending_summary_days() == [today.date().isoformat()]


def test_pending_summary_days_is_per_chat(tmp_path: Path) -> None:
    """同一天两个群，只要有一个没摘要，这天就算有缺口。"""
    store = Store(tmp_path / "test.db")
    store.init_schema()
    today = _today()
    store.add_message(_message(100, today))
    store.add_message(_message(200, today))
    store.upsert_summary(ChatType.GROUP, 100, today.date().isoformat(), "有摘要")

    assert store.pending_summary_days() == [today.date().isoformat()]


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
    control.store.add_message(_message(100, today))
    control.store.upsert_summary(ChatType.GROUP, 100, "2026-10-24", "昨天聊了钓鱼")

    async with running(app) as client:
        resp = await client.get("/api/memory/health")
        payload = await resp.json()

    assert resp.status == 200
    assert payload["ok"] is False
    assert payload["pending_days"] == [today.date().isoformat()]
    assert payload["pending_count"] == 1
    assert payload["summary_total"] == 1
    assert payload["last_summary"]["date"] == "2026-10-24"
    assert payload["last_summary"]["chat_id"] == 100
    assert payload["quota"]["paused"] is False


async def test_memory_health_endpoint_reports_quota_pause(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    until = time.time() + 600
    control.store.save_backoff(QUOTA_KEY, until)

    async with running(app) as client:
        payload = await (await client.get("/api/memory/health")).json()

    assert payload["ok"] is False
    assert payload["quota"]["paused"] is True
    assert abs(payload["quota"]["until"] - until) < 1
