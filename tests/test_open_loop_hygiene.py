"""open_loops 卫生与「你问我」闭环（chat-driven-behaviour-fixes T7）。

覆盖四件事：
* 近重复归一化去重：去空白与标点、剥「（，）无人回应」类模板尾巴、
  剥「问/追问/反问」类前缀词之后比较，同群同内容只留一条；
* 24 小时不重开：24 小时内重复被观察到 → 返回老 id（不新建、不 drop）；
  超过 24 小时 → 老行先标 dropped，再新建一行（历史保留，只改 status）；
* 全会话过期调度：``open_loop_expiry_loop``（``__main__`` 挂的真实周期
  任务）驱动所有会话的过期，不依赖 observer 的活跃列表；
* 「你问我」闭环：机器人回复成功后，本批里被问到的那条消息对应的
  question loop 被标 done。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

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
from dafeiyu.models import ChatType, Message, OpenLoop
from dafeiyu.router import Router, now_shanghai
from dafeiyu.service import Service
from dafeiyu.workers.observer import expire_open_loops_once, open_loop_expiry_loop

GROUP = ChatType.GROUP
CHAT = "qq:1"
OTHER_CHAT = "qq:99"
OTHER_GROUP_CHAT = "qq:98"

# 固定白天时钟：Router 的静默时段（01:00-07:00）若跟着真实时钟走，
# 测试在凌晨跑就会整批失败（与 tests/test_service.py 同一口径）。
SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "loops_hygiene.db")
    s.init_schema()
    yield s
    s.close()


def loop_rows(store: Store) -> list[sqlite3.Row]:
    return store._conn.execute(
        "SELECT * FROM open_loops ORDER BY id"
    ).fetchall()


def insert_loop(
    store: Store,
    content: str,
    kind: str = "question",
    *,
    age_days: float = 1,
    chat_id: str = CHAT,
) -> int:
    """直接落一条历史 open 行，绕过 add_open_loop 的 24h 去重。"""
    cur = store._conn.execute(
        """
        INSERT INTO open_loops (
            chat_type, chat_id, kind, content, status, created_at
        ) VALUES (?, ?, ?, ?, 'open', datetime('now', ?))
        """,
        (GROUP.value, chat_id, kind, content, f"-{age_days} days"),
    )
    store._conn.commit()
    return int(cur.lastrowid or 0)


# ---- (a) 近重复归一化去重 ---------------------------------------------------


def test_near_duplicate_tail_dedupes_to_one_row(store: Store) -> None:
    first = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "灯怎么选"))
    second = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", "灯怎么选，无人回应")
    )

    assert second == first
    rows = loop_rows(store)
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    # 落库的是第一次观察到的原文，不被归一化改写。
    assert rows[0]["content"] == "灯怎么选"


def test_near_duplicate_prefix_and_punct_dedupes(store: Store) -> None:
    first = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "灯怎么选"))
    again = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", "追问：灯怎么选？？还没人正面回答。")
    )

    assert again == first
    assert len(loop_rows(store)) == 1


def test_different_content_still_creates_new_row(store: Store) -> None:
    first = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "灯怎么选"))
    second = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", "周末去不去钓鱼，无人接话")
    )

    assert second != first
    assert len(loop_rows(store)) == 2


def test_approximate_same_kind_dedupes(store: Store) -> None:
    """同一件事的两种说法（不是逐字相同）也只留一条。"""
    first = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", "大肥鱼取代打工人你怎么看")
    )
    again = store.add_open_loop(
        OpenLoop(
            GROUP,
            CHAT,
            "question",
            "问 AI（大肥鱼）取代打工人怎么看，无人接话",
        )
    )

    assert again == first
    rows = loop_rows(store)
    assert len(rows) == 1
    assert rows[0]["content"] == "大肥鱼取代打工人你怎么看"


def test_similar_but_different_kind_is_not_merged(store: Store) -> None:
    """plan 和 question 共用几个 bigram 也不能并：等结果 != 问看法。"""
    plan = store.add_open_loop(OpenLoop(GROUP, CHAT, "plan", "说好周末去钓鱼"))
    question = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", "周末去钓鱼吗")
    )

    assert question != plan
    assert len(loop_rows(store)) == 2
    assert {row["kind"] for row in loop_rows(store)} == {"plan", "question"}


# ---- (b) 7 天 TTL 仍然生效 --------------------------------------------------


def test_week_old_open_loop_is_dropped_by_ttl(store: Store) -> None:
    loop_id = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "plan", "说好周末去钓鱼")
    )
    store._conn.execute(
        "UPDATE open_loops SET created_at = datetime('now', '-8 days') WHERE id = ?",
        (loop_id,),
    )
    store._conn.commit()

    assert store.expire_open_loops(GROUP, CHAT, days=7) == 1
    assert store.open_loops(GROUP, CHAT) == []
    row = loop_rows(store)[0]
    assert row["status"] == "dropped"
    assert row["closed_at"] is not None


def test_question_expires_sooner_than_plan(store: Store) -> None:
    """默认 TTL 分 kind：问题 3 天，计划 7 天；四天前的问题该走，计划该留。"""
    question = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", "四天前的问题")
    )
    plan = store.add_open_loop(OpenLoop(GROUP, CHAT, "plan", "四天前的计划"))
    store._conn.execute(
        "UPDATE open_loops SET created_at = datetime('now', '-4 days') "
        "WHERE id IN (?, ?)",
        (question, plan),
    )
    store._conn.commit()

    assert store.expire_open_loops(GROUP, CHAT) == 1
    statuses = {row["id"]: row["status"] for row in loop_rows(store)}
    assert statuses[question] == "dropped"
    assert statuses[plan] == "open"


def test_open_loop_cap_drops_oldest(store: Store) -> None:
    """每群最多 6 条 open；第 7 条起最旧的自动 dropped，历史行保留。"""
    topics = [
        "灯怎么选",
        "周末钓鱼",
        "显卡推荐",
        "房租上涨",
        "猫粮换牌",
        "膝盖疼痛",
        "明天下雨",
        "手机续航",
    ]
    ids = [
        store.add_open_loop(OpenLoop(GROUP, CHAT, "question", text))
        for text in topics
    ]

    assert len(set(ids)) == len(topics)
    assert len(store.open_loops(GROUP, CHAT, limit=10)) == 6
    statuses = {row["id"]: row["status"] for row in loop_rows(store)}
    assert statuses[ids[0]] == "dropped"
    assert statuses[ids[1]] == "dropped"
    assert statuses[ids[-1]] == "open"


def test_converge_merges_historical_near_duplicates(store: Store) -> None:
    """绕过 add 的 24h 窗口直接落两条历史行：收敛时保留最新一条。"""
    old = insert_loop(
        store,
        "下一个回收决断点在北京时间明天凌晨2:45，群里等结果",
        "plan",
        age_days=2,
    )
    new = insert_loop(
        store,
        "下一个回收决断点在北京时间明天凌晨2:45，群里仍在等结果",
        "plan",
        age_days=1,
    )

    assert store.converge_open_loops(GROUP, CHAT) == 1
    rows = loop_rows(store)
    assert len(rows) == 2  # 只改 status，不删历史
    statuses = {row["id"]: row["status"] for row in rows}
    assert statuses[old] == "dropped"
    assert statuses[new] == "open"


# ---- (c)/(d) 24 小时不重开 --------------------------------------------------


def test_repeat_within_24h_returns_old_id_without_dropping(store: Store) -> None:
    first = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "灯怎么选"))
    # 人为回拨到 1 小时前：仍在 24 小时窗口内。
    store._conn.execute(
        "UPDATE open_loops SET created_at = datetime('now', '-1 hours') WHERE id = ?",
        (first,),
    )
    store._conn.commit()

    again = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", "灯怎么选，无人回应")
    )

    assert again == first
    rows = loop_rows(store)
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["closed_at"] is None


def test_repeat_after_25h_drops_old_row_and_creates_new(store: Store) -> None:
    old_id = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "灯怎么选"))
    store._conn.execute(
        "UPDATE open_loops SET created_at = datetime('now', '-25 hours') WHERE id = ?",
        (old_id,),
    )
    store._conn.commit()

    new_id = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "灯怎么选"))

    assert new_id != old_id
    rows = loop_rows(store)
    assert len(rows) == 2  # 历史保留，不删行
    by_id = {row["id"]: row for row in rows}
    assert by_id[old_id]["status"] == "dropped"
    assert by_id[old_id]["closed_at"] is not None
    assert by_id[new_id]["status"] == "open"
    assert by_id[new_id]["created_at"] is not None


def test_repeat_is_chat_scoped(store: Store) -> None:
    first = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "灯怎么选"))
    other = store.add_open_loop(
        OpenLoop(GROUP, OTHER_CHAT, "question", "灯怎么选")
    )

    assert other != first
    assert len(loop_rows(store)) == 2


# ---- (e) 全会话过期被真实周期循环驱动 ----------------------------------------


async def test_periodic_expiry_loop_drops_chat_observer_never_visits(
    store: Store,
) -> None:
    """不在 observer 访问列表里的会话（没有消息、不会进活跃列表）也会被清。"""
    loop_id = store.add_open_loop(
        OpenLoop(GROUP, OTHER_GROUP_CHAT, "question", "很久没人理的问题")
    )
    store._conn.execute(
        "UPDATE open_loops SET created_at = datetime('now', '-8 days') WHERE id = ?",
        (loop_id,),
    )
    store._conn.commit()
    # 另一个没过期的会话：不该被误伤。
    fresh_id = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", "刚问的事")
    )

    assert store.active_chats_between(0, 1 << 40) == []  # 没有任何活跃会话

    stop = asyncio.Event()
    task = asyncio.create_task(
        open_loop_expiry_loop(store, interval_seconds=3600, stop=stop)
    )
    # 循环的第一遍在首次挂起前同步跑完：让出一次事件循环即可。
    for _ in range(5):
        await asyncio.sleep(0)
    try:
        row = store._conn.execute(
            "SELECT status FROM open_loops WHERE id = ?", (loop_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "dropped"
        fresh = store._conn.execute(
            "SELECT status FROM open_loops WHERE id = ?", (fresh_id,)
        ).fetchone()
        assert fresh is not None
        assert fresh["status"] == "open"
    finally:
        stop.set()
        await asyncio.wait_for(task, 1)


def test_expire_open_loops_once_sweeps_all_chats(store: Store) -> None:
    old_a = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "A 群的旧账"))
    old_b = store.add_open_loop(
        OpenLoop(GROUP, OTHER_GROUP_CHAT, "question", "B 群的旧账")
    )
    store._conn.execute(
        "UPDATE open_loops SET created_at = datetime('now', '-8 days') "
        "WHERE id IN (?, ?)",
        (old_a, old_b),
    )
    store._conn.commit()

    assert store.open_chats_with_open_loops() == [(GROUP, CHAT), (GROUP, OTHER_GROUP_CHAT)]
    assert expire_open_loops_once(store) == 2
    statuses = {
        row["id"]: row["status"]
        for row in loop_rows(store)
    }
    assert statuses[old_a] == "dropped"
    assert statuses[old_b] == "dropped"


# ---- (f) 「你问我」闭环 ------------------------------------------------------

SERVICE_SETTINGS: dict[str, Any] = {
    "api_key": "sk-test",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "bot_qq": 999,
    "ws_host": "127.0.0.1",
    "ws_port": 8080,
    "ws_token": "",
    "db_path": ":memory:",
    "persona": PersonaConfig("溟月", ("大肥鱼",), 120, 300, 0.05),
    "triggers": TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
    "groups": GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
    "budget": BudgetConfig(200, 300000),
    "memory": MemoryConfig(20, 5, 20),
    "logging": LoggingConfig("INFO", "logs"),
}


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.handler = None

    def on_message(self, handler) -> None:
        self.handler = handler

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"


class FakeBrain:
    def __init__(self, speak: bool = True) -> None:
        self.speak = speak
        self.calls = 0

    async def decide(self, system, messages, max_tokens=400) -> BrainReply:
        self.calls += 1
        return BrainReply(self.speak, "灯的事我懂", "test", 10, 5)

    async def aclose(self) -> None:
        pass


def make_msg(
    message_id: str = "m1",
    content: str = "@溟月 在吗",
    timestamp: int = 0,
) -> Message:
    return Message(
        chat_type=GROUP,
        chat_id=CHAT,
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=content.startswith("@溟月"),
        reply_to_bot=False,
        timestamp=timestamp,
    )


@pytest.fixture()
def service(tmp_path: Path, store: Store):
    settings = Settings(**SERVICE_SETTINGS)
    adapter = FakeAdapter()
    brain = FakeBrain()
    router = Router(settings, store, clock=lambda: NOON)
    svc = Service(
        settings,
        store,
        brain,
        {"qq": adapter},
        router,
        "你是溟月 $max_chars",
    )
    return svc


async def test_bot_reply_closes_question_loop_from_batch(
    store: Store, service: Service
) -> None:
    loop_id = store.add_open_loop(
        OpenLoop(
            GROUP,
            CHAT,
            "question",
            "灯怎么选，没人接",
            source_message_id="m1",
        )
    )

    await service.handle(make_msg())

    row = store._conn.execute(
        "SELECT status, closed_at FROM open_loops WHERE id = ?", (loop_id,)
    ).fetchone()
    assert row is not None
    assert row["status"] == "done"
    assert row["closed_at"] is not None


async def test_unsent_reply_does_not_close_loop(
    store: Store, tmp_path: Path
) -> None:
    settings = Settings(**SERVICE_SETTINGS)
    adapter = FakeAdapter()
    brain = FakeBrain(speak=False)
    router = Router(settings, store, clock=lambda: NOON)
    svc = Service(settings, store, brain, {"qq": adapter}, router, "你是溟月")
    loop_id = store.add_open_loop(
        OpenLoop(
            GROUP,
            CHAT,
            "question",
            "灯怎么选，没人接",
            source_message_id="m1",
        )
    )

    await svc.handle(make_msg())

    row = store._conn.execute(
        "SELECT status FROM open_loops WHERE id = ?", (loop_id,)
    ).fetchone()
    assert row is not None
    assert row["status"] == "open"


async def test_non_question_loops_are_not_closed_by_reply(
    store: Store, service: Service
) -> None:
    loop_id = store.add_open_loop(
        OpenLoop(
            GROUP,
            CHAT,
            "plan",
            "说好周末去钓鱼",
            source_message_id="m1",
        )
    )

    await service.handle(make_msg())

    row = store._conn.execute(
        "SELECT status FROM open_loops WHERE id = ?", (loop_id,)
    ).fetchone()
    assert row is not None
    assert row["status"] == "open"

