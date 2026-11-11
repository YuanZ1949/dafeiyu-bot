"""provider 不返回 usage 时，记账按字符估算并标记 estimated。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from dafeiyu.brain.client import BrainClient
from dafeiyu.brain.tokens import estimate_tokens, resolve_usage
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import SHANGHAI
from dafeiyu.workers.impressions import ImpressionWorker
from dafeiyu.workers.summary import summarize_chat


def test_estimate_tokens_mixes_ascii_and_cjk() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("a中") == 1
    assert estimate_tokens("hello 世界") == 3


def test_resolve_usage_only_fills_missing_side() -> None:
    assert resolve_usage("abcdefgh", "中文", 10, 20) == (10, 20, False)
    prompt, completion, estimated = resolve_usage("abcdefgh", "中文", 0, 0)
    assert (prompt, completion) == (2, 2)
    assert estimated is True

    prompt, completion, estimated = resolve_usage("abcdefgh", "中文", 7, 0)
    assert (prompt, completion, estimated) == (7, 2, True)


def make_store(tmp_path: Path | None = None) -> Store:
    store = Store(tmp_path / "usage.db" if tmp_path else ":memory:")
    store.init_schema()
    return store


def usage_rows(store: Store, purpose: str):
    return store._conn.execute(
        "SELECT prompt_tokens, completion_tokens, estimated FROM usage WHERE purpose = ?",
        (purpose,),
    ).fetchall()


class ZeroUsageBrain:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    async def complete_detailed(self, system, messages, max_tokens=400, temperature=0.2):
        return (self.payload, 0, 0)


@pytest.mark.asyncio
async def test_summary_records_estimated_usage() -> None:
    store = make_store()
    ts = int(datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI).timestamp())
    store.add_message(
        Message(ChatType.GROUP, 1, 1, "鱼片", "周末去钓鱼吗", "m1", timestamp=ts)
    )
    brain = ZeroUsageBrain("大家约了周末去钓鱼")

    text = await summarize_chat(store, brain, ChatType.GROUP, 1, "2026-10-24")

    assert text == "大家约了周末去钓鱼"
    rows = usage_rows(store, "summary")
    assert len(rows) == 1
    prompt, completion, estimated = rows[0]
    assert prompt > 0 and completion > 0
    assert estimated == 1
    store.close()


@pytest.mark.asyncio
async def test_impression_records_estimated_usage() -> None:
    store = make_store()
    # user_id 与真实适配器一致带通道前缀：印象抽取只接受本批发言人。
    store.add_message(Message(ChatType.GROUP, 1, "qq:1", "鱼片", "我喜欢钓鱼", "m1"))
    brain = ZeroUsageBrain(
        '[{"user_id": "qq:1", "kind": "preference", "content": "喜欢钓鱼", "confidence": 0.9}]'
    )
    worker = ImpressionWorker(store, brain, every=1)

    added = await worker.extract(ChatType.GROUP, 1)

    assert added == 1
    prompt, completion, estimated = usage_rows(store, "impression")[0]
    assert prompt > 0 and completion > 0
    assert estimated == 1
    store.close()


@pytest.mark.asyncio
async def test_reply_marks_estimated_when_provider_returns_zero_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"speak": true, "reply": "好的，周末去钓鱼"}'
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
        )

    client = BrainClient(
        api_key="test",
        base_url="http://test/v1",
        model="test",
        transport=httpx.MockTransport(handler),
    )
    try:
        reply = await client.decide("你是群友", [{"role": "user", "content": "周末去钓鱼吗"}])
    finally:
        await client.aclose()

    assert reply.estimated is True
    assert reply.prompt_tokens > 0
    assert reply.completion_tokens > 0


def test_legacy_usage_table_gets_estimated_column(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_type TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            purpose TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO usage (chat_type, chat_id, date, purpose) VALUES ('group', 1, '2026-10-24', 'reply')"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    store.init_schema()

    columns = {
        row["name"] for row in store._conn.execute("PRAGMA table_info(usage)").fetchall()
    }
    assert "estimated" in columns
    assert store._conn.execute("SELECT estimated FROM usage").fetchone()[0] == 0
    store.close()
