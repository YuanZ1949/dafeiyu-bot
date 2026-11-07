"""承诺跟进 worker：开关、静音/预算门控、每日上限与「不打断进行中的对话」。

口径：
* 默认关闭：``followup_loop`` 立即返回，``deliver_due_followups`` 恒为 0；
* 只跟进 ``kind='promise'`` 且超过阈值小时、之后没有新 bot 发言的 open loop；
* 同一会话每日最多 ``mind.followup_daily_cap`` 条，计数存在 ``mind_state``；
* 整条链路只走 ``_send_outbox``，不调用模型。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    MindConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
    load_settings,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message, OpenLoop
from dafeiyu.router import Router
from dafeiyu.service import Service
from dafeiyu.workers.followup import deliver_due_followups, followup_loop

GROUP = ChatType.GROUP
CHAT = "qq:1001"
SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 8, 12, 0, tzinfo=SHANGHAI)

BASE_YAML = """\
persona:
  name: 溟月
  aliases: [大肥鱼]
triggers:
  cooldown_seconds: 60
  sample_rate: 0.05
  quiet_hours: ["01:00", "07:00"]
  keywords: [怎么]
groups:
  whitelist: []
  blacklist: []
budget:
  daily_calls: 200
  daily_tokens: 300000
memory:
  window_size: 20
  impression_topk: 5
  extract_every: 20
logging:
  level: INFO
  dir: logs
"""


def make_settings(mind: MindConfig | None = None) -> Settings:
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
        mind=mind or MindConfig(),
    )


class CountingBrain:
    """跟进链路不应该调用模型。"""

    def __init__(self) -> None:
        self.calls = 0

    async def reply(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("承诺跟进不应调用模型")

    async def chat(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("承诺跟进不应调用模型")

    async def aclose(self) -> None:
        return None


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
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
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"


def build_service(
    tmp_path: Path, *, mind: MindConfig | None = None
) -> tuple[Service, FakeAdapter, CountingBrain, Store]:
    settings = make_settings(mind)
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    router = Router(settings, store, clock=lambda: NOON)
    adapter = FakeAdapter()
    brain = CountingBrain()
    service = Service(settings, store, brain, {"qq": adapter}, router, "test")
    return service, adapter, brain, store


def add_loop(store: Store, *, kind: str = "promise", hours_old: float = 25.0) -> int:
    """写一条 loop，并把 created_at 挪到 ``hours_old`` 小时前。"""
    loop_id = store.add_open_loop(OpenLoop(GROUP, CHAT, kind, "帮你带咖啡"))
    store._conn.execute(
        "UPDATE open_loops SET created_at = datetime('now', ?) WHERE id = ?",
        (f"-{hours_old} hours", loop_id),
    )
    store._conn.commit()
    return loop_id


def add_bot_message(store: Store, *, seconds_ago: float = 0.0) -> None:
    store.add_message(
        Message(
            chat_type=GROUP,
            chat_id=CHAT,
            user_id="qq:999",
            nickname="溟月",
            content="嗯",
            message_id=f"bot-{time.time()}",
            is_bot=True,
            timestamp=int(time.time() - seconds_ago),
        )
    )


def followup_counter(store: Store) -> int:
    key = f"followup/{GROUP.value}/{CHAT}/"
    keys = [
        row["key"]
        for row in store._conn.execute(
            "SELECT key FROM mind_state WHERE key LIKE ?", (key + "%",)
        ).fetchall()
    ]
    if not keys:
        return 0
    return int(store.get_mind_state(keys[0], "0") or "0")


async def test_disabled_by_default_sends_nothing_and_returns_immediately(
    tmp_path: Path,
) -> None:
    service, adapter, brain, store = build_service(tmp_path)
    add_loop(store)

    # deliver 直接短路（开关关时不查库、不发）。
    assert await deliver_due_followups(service) == 0

    # loop 本身也立即返回：不进入 stop.wait()。
    task = asyncio.create_task(followup_loop(service, interval=10.0))
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()

    assert adapter.sent == []
    assert brain.calls == 0
    assert followup_counter(store) == 0


async def test_enabled_sends_one_followup_for_old_promise(tmp_path: Path) -> None:
    service, adapter, brain, store = build_service(
        tmp_path, mind=MindConfig(proactive_followup=True)
    )
    loop_id = add_loop(store)

    assert await deliver_due_followups(service) == 1

    assert len(adapter.sent) == 1
    chat_type, chat_id, text = adapter.sent[0]
    assert (chat_type, chat_id) == (GROUP, CHAT)
    assert text == "上次说的「帮你带咖啡」后来怎样了？"
    assert brain.calls == 0
    assert followup_counter(store) == 1
    # 只计数：不新建 loop、不改状态。
    assert [row.id for row in store.open_loops(GROUP, CHAT)] == [loop_id]


async def test_daily_cap_blocks_second_followup_same_day(tmp_path: Path) -> None:
    service, adapter, _brain, store = build_service(
        tmp_path,
        mind=MindConfig(proactive_followup=True, followup_daily_cap=1),
    )
    add_loop(store)

    assert await deliver_due_followups(service) == 1
    # 第二轮：loop 仍 open、也没有新 bot 发言，但当日配额已用完。
    assert await deliver_due_followups(service) == 0

    assert len(adapter.sent) == 1
    assert followup_counter(store) == 1


async def test_question_loop_is_not_followed_up(tmp_path: Path) -> None:
    service, adapter, _brain, store = build_service(
        tmp_path, mind=MindConfig(proactive_followup=True)
    )
    add_loop(store, kind="question")

    assert await deliver_due_followups(service) == 0
    assert adapter.sent == []


async def test_newer_bot_message_skips_followup(tmp_path: Path) -> None:
    service, adapter, _brain, store = build_service(
        tmp_path, mind=MindConfig(proactive_followup=True)
    )
    add_loop(store)
    add_bot_message(store)

    assert await deliver_due_followups(service) == 0
    assert adapter.sent == []
    assert followup_counter(store) == 0


async def test_quiet_window_skips_followup(tmp_path: Path) -> None:
    service, adapter, _brain, store = build_service(
        tmp_path, mind=MindConfig(proactive_followup=True)
    )
    add_loop(store)
    service.router.quiet(GROUP, CHAT, 10)

    assert await deliver_due_followups(service) == 0
    assert adapter.sent == []


async def test_loop_exits_within_one_interval_when_enabled(tmp_path: Path) -> None:
    service, _adapter, _brain, store = build_service(
        tmp_path, mind=MindConfig(proactive_followup=True)
    )
    add_loop(store)
    stop = asyncio.Event()
    interval = 0.5
    task = asyncio.create_task(followup_loop(service, interval=interval, stop=stop))
    await asyncio.sleep(0.05)
    stop.set()

    await asyncio.wait_for(task, timeout=interval)
    assert task.done()


def test_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(BASE_YAML, encoding="utf-8")
    env_file = tmp_path / "test.env"
    env_file.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")

    settings = load_settings(env_file=env_file, config_file=tmp_path / "config.yaml")

    assert settings.mind.proactive_followup is False
    assert settings.mind.followup_after_hours == 24
    assert settings.mind.followup_daily_cap == 3
