"""Observer 关闭「已了结的未了事」：closed_loop_ids 的归属校验与写关顺序。

要点：
* 模型只能关掉本轮列给它的那几条，陌生 / 编造 / 类型不对的 id 静默丢弃；
* 未了事列表走 user message（不是 SYSTEM 常量），模型才看得到 id；
* 先 ``_write_open_loops`` 后关闭：否则本轮重复输出的同内容 loop 会绕过去重，
  刚了结的事会以一条新 open 行的形式复活。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dafeiyu.config import MindConfig
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message, OpenLoop
from dafeiyu.workers.failure import FailureBackoff
from dafeiyu.workers.observer import ObserverWorker

GROUP = ChatType.GROUP
CHAT = "qq:1"
USER = "qq:2"

LOOP_CONTENT = "阿猫问的灯怎么选，没人接"


class FakeBrain:
    def __init__(self, response: str = "{}") -> None:
        self.response = response
        self.calls: list[list[dict[str, Any]]] = []
        self.systems: list[str] = []

    async def complete_detailed(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 400,
        temperature: float = 0.2,
    ) -> tuple[str, int, int]:
        self.calls.append(messages)
        self.systems.append(system)
        return self.response, 10, 5


def add_messages(store: Store, count: int = 8) -> None:
    for i in range(1, count + 1):
        store.add_message(
            Message(
                chat_type=GROUP,
                chat_id=CHAT,
                user_id=USER,
                nickname="阿猫",
                content=f"消息 {i}",
                message_id=f"m{i}",
                is_bot=False,
                timestamp=1800 + i,
            )
        )


def payload(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "topic": "显卡驱动报错",
        "phase": "discussion",
        "summary": "阿猫装驱动黑屏，鱼片让他先卸干净。",
        "participants": [{"user_id": USER, "nickname": "阿猫"}],
        "open_loops": [],
        "closed_loop_ids": [],
        "mood": "有点急",
        "importance": 0.7,
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "observer_closed.db")
    s.init_schema()
    yield s
    s.close()


def make_worker(store: Store, brain: FakeBrain) -> ObserverWorker:
    return ObserverWorker(
        store,
        brain,
        MindConfig(enabled=True, observer_enabled=True),
        backoff=FailureBackoff(3600, monotonic=lambda: 0.0),
        now=lambda: 2000.0,
    )


def loop_status(store: Store, loop_id: int) -> str:
    row = store._conn.execute(
        "SELECT status FROM open_loops WHERE id = ?", (loop_id,)
    ).fetchone()
    assert row is not None
    return str(row["status"])


def loop_row_count(store: Store, content: str) -> int:
    row = store._conn.execute(
        "SELECT COUNT(*) AS n FROM open_loops WHERE chat_id = ? AND content = ?",
        (CHAT, content),
    ).fetchone()
    assert row is not None
    return int(row["n"])


# ---- 正常关闭 -------------------------------------------------------------


async def test_closes_listed_loop_and_ignores_foreign_id(store: Store) -> None:
    existing = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", LOOP_CONTENT)
    )
    add_messages(store)
    brain = FakeBrain(payload(closed_loop_ids=[existing, 999999]))
    worker = make_worker(store, brain)

    assert await worker.observe_chat(GROUP, CHAT) is True

    assert loop_status(store, existing) == "done"
    assert store.open_loops(GROUP, CHAT) == []
    # 999999 不存在，既不该报错也不该留下任何痕迹。
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM open_loops WHERE id = 999999"
    ).fetchone()[0] == 0

    # 未了事列表走 user message，模型才可能回填 id（不进 SYSTEM 常量）。
    prompt = brain.calls[0][0]["content"]
    assert f"{existing}|question|{LOOP_CONTENT}" in prompt
    assert LOOP_CONTENT not in brain.systems[0]


# ---- 先写后关：重复输出不会复活 -------------------------------------------


async def test_duplicate_output_is_not_reinserted_before_close(store: Store) -> None:
    existing = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", LOOP_CONTENT)
    )
    add_messages(store)
    # 模型既说这条已经解决，又把同一件事原样再记一遍（真实模型常见的抖动）。
    brain = FakeBrain(
        payload(
            closed_loop_ids=[existing],
            open_loops=[
                {"kind": "question", "user_id": USER, "content": LOOP_CONTENT}
            ],
        )
    )
    worker = make_worker(store, brain)

    assert await worker.observe_chat(GROUP, CHAT) is True

    # 先写：命中原 open 行去重，不插入新行；再关：原行变 done。
    # 若顺序反了，关闭会让去重失效，这里会多出一条同内容的 open 行。
    assert store.open_loops(GROUP, CHAT) == []
    assert loop_row_count(store, LOOP_CONTENT) == 1
    assert loop_status(store, existing) == "done"


# ---- 非法 closed_loop_ids 一律忽略 ----------------------------------------


@pytest.mark.parametrize("bad_ids", ["12", 12, {"id": 12}])
async def test_non_list_closed_ids_are_ignored(
    store: Store, bad_ids: Any
) -> None:
    existing = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", LOOP_CONTENT)
    )
    add_messages(store)
    brain = FakeBrain(payload(closed_loop_ids=bad_ids))
    worker = make_worker(store, brain)

    assert await worker.observe_chat(GROUP, CHAT) is True

    assert store.open_loops(GROUP, CHAT)[0].id == existing
    assert loop_status(store, existing) == "open"


async def test_non_int_entries_in_list_are_ignored(store: Store) -> None:
    existing = store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", LOOP_CONTENT)
    )
    add_messages(store)
    brain = FakeBrain(
        payload(closed_loop_ids=["12", 12.0, True, None, existing])
    )
    worker = make_worker(store, brain)

    assert await worker.observe_chat(GROUP, CHAT) is True

    # 垃圾条目跳过，合法的 existing 仍然关掉。
    assert loop_status(store, existing) == "done"
