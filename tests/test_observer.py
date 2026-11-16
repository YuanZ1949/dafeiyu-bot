"""Observer：只观察不回复，写 episode / open_loop，失败不影响主链路。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from dafeiyu.config import MindConfig
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import now_shanghai, today_shanghai
from dafeiyu.workers.failure import FailureBackoff
from dafeiyu.workers.observer import (
    ObserverWorker,
    chat_type_allowed,
    observer_loop,
)
from dafeiyu.workers.reflector import ReflectorWorker

GROUP = ChatType.GROUP
PRIVATE = ChatType.PRIVATE
CHAT = "qq:1"

USER = "qq:2"


class FakeBrain:
    def __init__(self, response: str = "{}", exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
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
        if self.exc is not None:
            raise self.exc
        return self.response, 10, 5


def make_message(
    i: int,
    *,
    chat_type: ChatType = GROUP,
    chat_id: str = CHAT,
    user_id: str = USER,
    content: str | None = None,
    is_bot: bool = False,
    timestamp: int | None = None,
    nickname: str = "阿猫",
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=f"消息 {i}" if content is None else content,
        message_id=f"m{i}",
        is_bot=is_bot,
        timestamp=i if timestamp is None else timestamp,
    )


def add_messages(
    store: Store,
    start: int,
    count: int = 8,
    *,
    timestamp: int | None = 1800,
    chat_type: ChatType = GROUP,
    chat_id: str = CHAT,
) -> None:
    for i in range(start, start + count):
        stamp = 9500 + i if timestamp is None else timestamp + i - start
        store.add_message(
            make_message(i, chat_type=chat_type, chat_id=chat_id, timestamp=stamp)
        )


def payload(**overrides: Any) -> str:
    base = {
        "topic": "显卡驱动报错",
        "phase": "discussion",
        "summary": "阿猫装驱动黑屏，鱼片让他先卸干净。",
        "participants": [{"user_id": USER, "nickname": "阿猫"}],
        "open_loops": [
            {
                "kind": "question",
                "user_id": USER,
                "content": "DDU 之后还是黑屏怎么办",
            }
        ],
        "mood": "有点急",
        "importance": 0.7,
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "observer.db")
    s.init_schema()
    yield s
    s.close()


def make_worker(
    store: Store, brain: FakeBrain | SequenceBrain, **mind: Any
) -> ObserverWorker:
    mind.setdefault("enabled", True)
    mind.setdefault("observer_enabled", True)
    return ObserverWorker(
        store,
        brain,
        MindConfig(**mind),
        backoff=FailureBackoff(3600, monotonic=lambda: 0.0),
        now=lambda: 2000.0,
    )


# ---- 总闸 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        {"enabled": False},
        {"observer_enabled": False},
        {"episodes_enabled": False},
    ],
)
async def test_switched_off_never_calls_model(store: Store, flags: dict[str, Any]) -> None:
    add_messages(store, 1)
    brain = FakeBrain(payload())
    worker = make_worker(store, brain, **flags)

    assert await worker.run_once() == 0
    assert brain.calls == []
    assert await worker.observe_chat(GROUP, CHAT) is False
    assert brain.calls == []


async def test_observe_private_false_never_observes_private(store: Store) -> None:
    add_messages(store, 1, chat_type=PRIVATE, chat_id="qq:9")
    brain = FakeBrain(payload())
    worker = make_worker(store, brain, observe_private=False)

    assert await worker.run_once() == 0
    assert await worker.observe_chat(PRIVATE, "qq:9") is False
    assert brain.calls == []


# ---- 私聊观察（observe_private）-------------------------------------------


def test_chat_type_allowed_predicate() -> None:
    mind = MindConfig(enabled=True, observer_enabled=True)
    assert chat_type_allowed(mind, PRIVATE) is True
    assert chat_type_allowed(mind, GROUP) is True

    no_private = MindConfig(
        enabled=True, observer_enabled=True, observe_private=False
    )
    assert chat_type_allowed(no_private, PRIVATE) is False
    assert chat_type_allowed(no_private, GROUP) is True

    # 总闸对群聊同样生效：没有 GROUP 直通。
    off = MindConfig(enabled=False, observer_enabled=True)
    assert chat_type_allowed(off, GROUP) is False
    assert chat_type_allowed(off, PRIVATE) is False


async def test_observe_private_observes_private_chat(store: Store) -> None:
    add_messages(store, 1, chat_type=PRIVATE, chat_id="qq:9")
    brain = FakeBrain(payload())
    worker = make_worker(store, brain)

    assert await worker.run_once() == 1
    assert len(brain.calls) == 1
    # 私聊提示第一行点明这是私聊，不是群聊。
    assert brain.calls[0][0]["content"].startswith(
        "这是与对方的私聊，不是群聊。\n"
    )
    episode = store.latest_episode(PRIVATE, "qq:9")
    assert episode is not None
    assert episode.chat_type == PRIVATE


async def test_private_daily_cap_is_per_chat(store: Store) -> None:
    add_messages(store, 1, chat_type=PRIVATE, chat_id="qq:9")
    brain = FakeBrain(payload())
    # daily=2：Reflector 保留 1 次，Observer 自己最多 1 次。
    worker = make_worker(store, brain, observer_daily_calls=2)

    assert await worker.run_once() == 1
    assert store.observer_calls_today(PRIVATE, "qq:9", today_shanghai()) == 1

    add_messages(store, 9, chat_type=PRIVATE, chat_id="qq:9", timestamp=1200)
    assert await worker.run_once() == 0  # 1 >= 1：第二轮让位给 Reflector
    assert len(brain.calls) == 1


async def test_private_below_min_messages_is_not_observed(store: Store) -> None:
    add_messages(store, 1, count=7, chat_type=PRIVATE, chat_id="qq:9")
    brain = FakeBrain(payload())
    worker = make_worker(store, brain)

    assert await worker.run_once() == 0
    assert brain.calls == []


async def test_master_switch_off_blocks_private_even_with_observe_private(
    store: Store,
) -> None:
    add_messages(store, 1, chat_type=PRIVATE, chat_id="qq:9")
    brain = FakeBrain(payload())
    worker = make_worker(store, brain, enabled=False)

    assert await worker.run_once() == 0
    assert await worker.observe_chat(PRIVATE, "qq:9") is False
    assert brain.calls == []


# ---- 触发条件 -------------------------------------------------------------


async def test_skips_until_min_messages(store: Store) -> None:
    add_messages(store, 1, count=7)
    brain = FakeBrain(payload())
    worker = make_worker(store, brain, observer_min_messages=8)

    assert await worker.run_once() == 0
    assert brain.calls == []


async def test_daily_cap_blocks_extra_calls(store: Store) -> None:
    add_messages(store, 1)
    brain = FakeBrain(payload())
    # daily=2：Reflector 保留 1 次，Observer 自己最多 1 次（daily-1）。
    worker = make_worker(store, brain, observer_daily_calls=2)

    assert await worker.run_once() == 1  # 首轮 0 >= 1 不成立，放行
    assert await worker.run_once() == 0  # 游标已推进，也没有新消息
    assert store.observer_calls_today(GROUP, CHAT, today_shanghai()) == 1

    add_messages(store, 9, timestamp=1200)
    assert await worker.run_once() == 0  # 1 >= 1：额度让给 Reflector
    assert len(brain.calls) == 1


class ReflectorBrain:
    """返回合法自省 JSON 的最小假脑，用来验证 Reflector 的额度保留。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_detailed(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 600,
        temperature: float = 0.7,
    ) -> tuple[str, int, int]:
        self.calls += 1
        payload = {
            "focus": "在意今天的鱼缸",
            "mood_note": "有点困",
            "relationships": [],
            "lessons": [],
            "life": [],
        }
        return json.dumps(payload, ensure_ascii=False), 10, 5


def make_reflector(store: Store, brain: ReflectorBrain, daily: int) -> ReflectorWorker:
    moment = now_shanghai().replace(hour=6, minute=0, second=0, microsecond=0)
    return ReflectorWorker(
        store,
        brain,
        MindConfig(enabled=True, reflection_hour=5, observer_daily_calls=daily),
        now=lambda: moment.timestamp(),
    )


async def test_daily_one_observer_yields_and_reflector_still_runs(store: Store) -> None:
    start = int(
        now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )

    # daily=1：门限 max(0, 0)=0，Observer 立刻让位，一次都不调模型。
    add_messages(store, 1)
    brain = FakeBrain(payload())
    worker = make_worker(store, brain, observer_daily_calls=1)
    assert await worker.run_once() == 0
    assert brain.calls == []

    # Reflector 仍能用这唯一的 1 次。
    store.add_message(
        make_message(1, chat_id="qq:2", user_id="qq:2", timestamp=start + 60)
    )
    reflect_brain = ReflectorBrain()
    reflector = make_reflector(store, reflect_brain, daily=1)
    assert await reflector.run_once() == 1
    assert reflect_brain.calls == 1


async def test_daily_zero_turns_both_off(store: Store) -> None:
    start = int(
        now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )

    add_messages(store, 1)
    brain = FakeBrain(payload())
    worker = make_worker(store, brain, observer_daily_calls=0)
    assert await worker.run_once() == 0
    assert brain.calls == []

    store.add_message(
        make_message(1, chat_id="qq:2", user_id="qq:2", timestamp=start + 60)
    )
    reflect_brain = ReflectorBrain()
    reflector = make_reflector(store, reflect_brain, daily=0)
    assert await reflector.run_once() == 0
    assert reflect_brain.calls == 0


async def test_observer_reserve_leaves_reflector_one_call(store: Store) -> None:
    """Observer 用满 daily-1 次后，Reflector 仍能跑 1 次，再下一次被额度拒绝。"""
    start = int(
        now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    add_messages(store, 1)
    brain = FakeBrain(payload())
    worker = make_worker(store, brain, observer_daily_calls=2)
    assert await worker.run_once() == 1  # 首轮 0 >= 1 不成立，放行
    assert len(brain.calls) == 1
    add_messages(store, 9, timestamp=1200)
    assert await worker.run_once() == 0  # 1 >= 1：让位

    # 同一会话：Reflector 仍能跑 1 次（1 >= 2 不成立）。
    store.add_message(make_message(50, user_id="qq:2", timestamp=start + 60))
    reflect_brain = ReflectorBrain()
    reflector = make_reflector(store, reflect_brain, daily=2)
    assert await reflector.run_once() == 1
    assert reflect_brain.calls == 1

    # 再下一次被额度拒绝：2 >= 2（清掉当日标记，只看额度闸）。
    store.set_mind_state(store.reflector_key(GROUP, CHAT), "")
    assert await reflector.run_once() == 0
    assert reflect_brain.calls == 1


# ---- 正常一次 -------------------------------------------------------------


async def test_observes_group_and_writes_episode_and_loop(store: Store) -> None:
    add_messages(store, 1)
    brain = FakeBrain(payload())
    worker = make_worker(store, brain)

    assert await worker.run_once() == 1
    assert len(brain.calls) == 1
    prompt = brain.calls[0][0]["content"]
    assert f"{USER}|阿猫: 消息 1" in prompt
    # 只观察：system 里明确禁止回复。
    assert "不回复" in brain.systems[0]

    episode = store.latest_episode(GROUP, CHAT)
    assert episode is not None
    assert episode.start_message_id == "m1"
    assert episode.last_message_id == "m8"
    assert episode.topic == "显卡驱动报错"
    assert episode.summary == "阿猫装驱动黑屏，鱼片让他先卸干净。"
    assert episode.phase == "discussion"
    assert episode.importance == 0.7
    assert [(p.user_id, p.nickname) for p in episode.participants] == [
        (USER, "阿猫")
    ]

    loops = store.open_loops(GROUP, CHAT)
    assert [loop.content for loop in loops] == ["DDU 之后还是黑屏怎么办"]
    assert loops[0].kind == "question"
    assert loops[0].subject_user_id == USER
    assert loops[0].source_message_id == "m8"

    assert store.observer_cursor(GROUP, CHAT) == 8
    assert store.observer_calls_today(GROUP, CHAT, today_shanghai()) == 1
    rows = store._conn.execute(
        "SELECT prompt_tokens, completion_tokens, estimated "
        "FROM usage WHERE purpose = 'observer'"
    ).fetchall()
    assert len(rows) == 1


async def test_bot_messages_are_not_fed_to_the_model(store: Store) -> None:
    add_messages(store, 1, count=7)
    store.add_message(
        make_message(
            8,
            user_id="qq:bot",
            nickname="大肥鱼",
            content="机器人自己的话",
            is_bot=True,
            timestamp=1807,
        )
    )
    brain = FakeBrain(payload())
    worker = make_worker(store, brain)

    assert await worker.run_once() == 1
    prompt = brain.calls[0][0]["content"]
    assert "机器人自己的话" not in prompt
    episode = store.latest_episode(GROUP, CHAT)
    assert episode is not None
    assert episode.last_message_id == "m7"  # 最后一条真人消息


# ---- 模型幻觉 -------------------------------------------------------------


async def test_hallucinated_ids_and_sources_are_dropped(store: Store) -> None:
    add_messages(store, 1)
    brain = FakeBrain(
        payload(
            participants=[
                {"user_id": "qq:999", "nickname": "编出来的人"},
                {"user_id": USER, "nickname": "模型瞎起的名字"},
            ],
            open_loops=[
                {
                    "kind": "question",
                    "user_id": "qq:999",
                    "content": "谁在",
                    "source_message_id": "nope",
                },
                {
                    "kind": "plan",
                    "user_id": USER,
                    "content": "周末一起修电脑",
                    "source_message_id": "m3",
                },
            ],
        )
    )
    worker = make_worker(store, brain)

    assert await worker.run_once() == 1

    episode = store.latest_episode(GROUP, CHAT)
    assert episode is not None
    # 只保留真实发言人；昵称以输入里的真实昵称为准。
    assert [(p.user_id, p.nickname) for p in episode.participants] == [
        (USER, "阿猫")
    ]

    loops = {loop.content: loop for loop in store.open_loops(GROUP, CHAT)}
    assert set(loops) == {"谁在", "周末一起修电脑"}
    assert loops["谁在"].subject_user_id is None
    assert loops["谁在"].source_message_id == ""
    assert loops["周末一起修电脑"].subject_user_id == USER
    assert loops["周末一起修电脑"].source_message_id == "m3"


async def test_model_fields_are_normalized(store: Store) -> None:
    add_messages(store, 1)
    brain = FakeBrain(
        payload(
            topic="很长的" * 30,
            summary="很长" * 100,
            mood="很" * 40,
            phase="大结局",
            importance=9.0,
            open_loops=[
                {"kind": "瞎编的", "user_id": "qq:2", "content": "长" * 81},
                {"kind": "promise", "user_id": "qq:2", "content": "   "},
                {"kind": "promise", "user_id": "qq:2", "content": "答应修电脑"},
            ],
        )
    )
    worker = make_worker(store, brain)

    assert await worker.run_once() == 1

    episode = store.latest_episode(GROUP, CHAT)
    assert episode is not None
    assert len(episode.topic) == 40
    assert len(episode.summary) == 120
    assert len(episode.mood) == 20
    assert episode.phase == "discussion"
    assert episode.importance == 1.0
    loops = store.open_loops(GROUP, CHAT)
    assert [loop.content for loop in loops] == ["答应修电脑"]
    assert loops[0].kind == "promise"


# ---- 失败与退避 -----------------------------------------------------------


async def test_invalid_json_does_not_raise_and_backs_off(store: Store) -> None:
    add_messages(store, 1)
    brain = FakeBrain("这不是 JSON")
    worker = make_worker(store, brain)

    assert await worker.run_once() == 0
    assert store.observer_cursor(GROUP, CHAT) == 8
    assert store.observer_calls_today(GROUP, CHAT, today_shanghai()) == 1

    add_messages(store, 9, timestamp=1200)
    assert await worker.run_once() == 0
    assert len(brain.calls) == 1  # 退避窗口内同群直接跳过


async def test_model_exception_is_swallowed(store: Store) -> None:
    add_messages(store, 1)
    brain = FakeBrain(payload(), exc=RuntimeError("provider down"))
    worker = make_worker(store, brain)

    assert await worker.run_once() == 0
    assert store.observer_cursor(GROUP, CHAT) == 0  # 失败不推进，退避后可重试
    assert store.latest_episode(GROUP, CHAT) is None


# ---- 同一段对话 vs 30 分钟空档 --------------------------------------------


async def test_same_episode_without_gap(store: Store) -> None:
    add_messages(store, 1, timestamp=1000)
    brain = FakeBrain(payload())
    worker = make_worker(store, brain)
    assert await worker.run_once() == 1

    add_messages(store, 9, timestamp=1100)
    worker._now = lambda: 1300.0
    assert await worker.run_once() == 1

    rows = store.episodes_between(GROUP, CHAT, 0, 4000)
    assert len(rows) == 1
    assert rows[0].start_message_id == "m1"
    assert rows[0].last_message_id == "m16"
    assert rows[0].status == "open"


async def test_new_episode_after_30_minute_gap(store: Store) -> None:
    add_messages(store, 1, timestamp=1000)
    brain = FakeBrain(payload())
    worker = make_worker(store, brain)
    assert await worker.run_once() == 1

    add_messages(store, 9, timestamp=4000)
    worker._now = lambda: 4200.0
    assert await worker.run_once() == 1

    episodes = store.episodes_between(GROUP, CHAT, 0, 5000)
    assert len(episodes) == 2
    assert {episode.status for episode in episodes} == {"closed", "open"}
    latest = store.latest_episode(GROUP, CHAT)
    assert latest is not None
    assert latest.start_message_id == "m9"
    assert {episode.start_message_id for episode in episodes} == {"m1", "m9"}


# ---- 同 gap 内的并行话题与相似合并 ----------------------------------------


class SequenceBrain:
    """按调用次序返回不同 payload 的假脑：验证并行话题/合并行为。"""

    def __init__(self, payloads: list[str]) -> None:
        self.payloads = list(payloads)
        self.calls = 0

    async def complete_detailed(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 400,
        temperature: float = 0.2,
    ) -> tuple[str, int, int]:
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return self.payloads[index], 10, 5


def add_batch(store: Store, start: int, timestamp: int, **message_kw: Any) -> None:
    """一批 8 条消息（够 observer_min_messages），来自同一人。"""
    for i in range(start, start + 8):
        store.add_message(
            make_message(i, timestamp=timestamp + i - start, **message_kw)
        )


async def test_parallel_topics_within_gap_open_two_episodes(store: Store) -> None:
    """30 分钟内先聊显卡再聊鱼缸：两个并行话题各成一行 open，不互相覆盖。"""
    add_batch(store, 1, 1000)
    worker = make_worker(store, FakeBrain(payload()))
    assert await worker.run_once() == 1

    add_batch(store, 9, 1100)
    worker._now = lambda: 2100.0
    worker.brain = FakeBrain(
        payload(topic="鱼缸造景", summary="大家在聊鱼缸造景。", open_loops=[])
    )
    assert await worker.run_once() == 1

    episodes = store.episodes_between(GROUP, CHAT, 0, 5000)
    assert len(episodes) == 2
    assert {episode.status for episode in episodes} == {"open"}
    assert {episode.start_message_id for episode in episodes} == {"m1", "m9"}
    latest = store.latest_episode(GROUP, CHAT)
    assert latest is not None
    assert latest.start_message_id == "m9"
    assert latest.topic == "鱼缸造景"


async def test_same_topic_within_gap_updates_same_row(store: Store) -> None:
    """同一话题再次观察：行 id 不变、summary 更新、participants 取并集。"""
    add_batch(store, 1, 1000)
    worker = make_worker(store, FakeBrain(payload()))
    assert await worker.run_once() == 1
    first = store.latest_episode(GROUP, CHAT)
    assert first is not None and first.id is not None

    add_batch(store, 9, 1100, user_id="qq:3", nickname="阿九")
    worker._now = lambda: 2100.0
    worker.brain = FakeBrain(
        payload(
            topic="显卡驱动报错",
            summary="阿九也说他的驱动会闪退。",
            participants=[{"user_id": "qq:3", "nickname": "阿九"}],
            open_loops=[],
        )
    )
    assert await worker.run_once() == 1

    episodes = store.episodes_between(GROUP, CHAT, 0, 5000)
    assert len(episodes) == 1
    row = episodes[0]
    assert row.id == first.id
    assert row.start_message_id == "m1"
    assert row.summary == "阿九也说他的驱动会闪退。"
    assert row.last_message_id == "m16"
    # 并集：既有参与者 + 本批新出现的人。
    assert [(p.user_id, p.nickname) for p in row.participants] == [
        (USER, "阿猫"),
        ("qq:3", "阿九"),
    ]
    assert row.status == "open"


async def test_four_topics_in_one_gap_keep_top_three_open(store: Store) -> None:
    """同一 gap 内连来 4 个不同话题：open 恰为 3 条，最低 importance 的收尾。"""
    topics = (
        ("显卡驱动报错", 0.9),
        ("鱼缸造景", 0.8),
        ("周末爬山", 0.7),
        ("晚饭吃什么", 0.6),
    )
    payloads = [
        payload(topic=name, summary=f"在聊{name}。", importance=score, open_loops=[])
        for name, score in topics
    ]
    add_batch(store, 1, 1000)
    worker = make_worker(store, SequenceBrain(payloads))

    for index, base in enumerate((1000, 1100, 1200, 1300)):
        if index > 0:
            add_batch(store, 1 + 8 * index, base)
        worker._now = lambda base=base: float(base + 1000)
        assert await worker.run_once() == 1

    episodes = store.episodes_between(GROUP, CHAT, 0, 5000)
    open_rows = [episode for episode in episodes if episode.status == "open"]
    closed_rows = [episode for episode in episodes if episode.status == "closed"]
    assert len(open_rows) == 3
    assert {episode.importance for episode in open_rows} == {0.9, 0.8, 0.7}
    assert len(closed_rows) == 1
    assert closed_rows[0].importance == 0.6


async def test_missing_topic_creates_new_row_without_raising(store: Store) -> None:
    """payload 缺 topic：退回新建，不抛异常。"""
    add_batch(store, 1, 1000)
    worker = make_worker(store, FakeBrain(payload()))
    assert await worker.run_once() == 1

    add_batch(store, 9, 1100)
    worker._now = lambda: 2100.0
    worker.brain = FakeBrain(payload(topic=None, summary="没有话题。", open_loops=[]))
    assert await worker.run_once() == 1

    episodes = store.episodes_between(GROUP, CHAT, 0, 5000)
    assert len(episodes) == 2
    assert all(episode.status == "open" for episode in episodes)


# ---- 后台循环 -------------------------------------------------------------


async def test_observer_loop_exits_after_stop(store: Store) -> None:
    brain = FakeBrain(payload())
    worker = make_worker(store, brain, observer_min_messages=1000)
    stop = asyncio.Event()
    task = asyncio.create_task(
        observer_loop(worker, interval_seconds=0.01, stop=stop)
    )
    await asyncio.sleep(0)  # 让循环至少跑一轮
    stop.set()

    await asyncio.wait_for(task, timeout=1)
    assert task.done()
