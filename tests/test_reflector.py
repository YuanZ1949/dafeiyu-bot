"""夜间自省（Reflector）与 workspace 追加写入。

覆盖点：
* 开关 / 时间闸 / 每群每日一次 / 额度池；
* 全局 bot_state 只取消息量最大的群，lessons 落群笔记、life 落 MEMORY.md；
* 非法 JSON 不冒泡，写标记并进入退避；
* prompt 不再要求 relationships，旧 payload 带该键也能正常解析；
* append_note / append_global 的创建、追加与空列表语义。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, cast

import pytest

from dafeiyu.config import MindConfig
from dafeiyu.memory.store import Store
from dafeiyu.memory.workspace import Workspace
from dafeiyu.models import ChatType, Episode, Message
from dafeiyu.timeutil import now_shanghai, today_shanghai
from dafeiyu.workers.failure import FailureBackoff
from dafeiyu.workers.reflector import (
    MAX_PROMPT_CHARS,
    REFLECT_SYSTEM,
    ReflectorWorker,
    parse_reflection,
    reflector_loop,
)

GROUP = ChatType.GROUP


class FakeBrain:
    """按调用顺序返回预设响应；异常对象直接抛出，用来模拟 provider 失败。"""

    def __init__(self, responses: Any) -> None:
        if not isinstance(responses, list):
            responses = [responses]
        self._responses = responses
        self.calls = 0
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete_detailed(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 600,
        temperature: float = 0.7,
    ) -> tuple[str, int, int]:
        self.calls += 1
        self.systems.append(system)
        self.prompts.append(messages[0]["content"])
        item = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item, 120, 60


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "reflect.db")
    s.init_schema()
    yield s
    s.close()


@pytest.fixture()
def workspace(tmp_path):
    return Workspace(tmp_path / "ws")


def now_at(hour: int, minute: int = 0) -> datetime:
    """今天的指定时刻（上海时区），日期与 today_shanghai() 保持一致。"""
    return now_shanghai().replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def day_start_of(now_dt: datetime) -> int:
    return int(now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def add_message(
    store: Store,
    chat_id: str,
    index: int,
    timestamp: int,
    *,
    nickname: str = "甲",
    content: str | None = None,
    is_bot: bool = False,
) -> None:
    store.add_message(
        Message(
            chat_type=GROUP,
            chat_id=chat_id,
            user_id="qq:bot" if is_bot else "qq:1",
            nickname="大肥鱼" if is_bot else nickname,
            content=f"消息 {index}" if content is None else content,
            message_id=f"{chat_id}-m{index}",
            is_bot=is_bot,
            timestamp=timestamp,
        )
    )


def make_mind(
    *,
    enabled: bool = True,
    reflection_hour: int = 5,
    observer_daily_calls: int = 24,
) -> MindConfig:
    return MindConfig(
        enabled=enabled,
        reflection_hour=reflection_hour,
        observer_daily_calls=observer_daily_calls,
    )


def reflection(**overrides) -> str:
    data = {
        "focus": "在意今天聊的鱼缸",
        "mood_note": "有点困但开心",
        "relationships": [],
        "lessons": [],
        "life": [],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def make_worker(
    store: Store,
    brain: FakeBrain,
    *,
    mind: MindConfig | None = None,
    workspace: Workspace | None = None,
    now_dt: datetime | None = None,
    backoff: FailureBackoff | None = None,
) -> ReflectorWorker:
    moment = now_dt or now_at(6)
    return ReflectorWorker(
        store,
        brain,
        mind or make_mind(),
        workspace,
        backoff=backoff,
        now=lambda: moment.timestamp(),
    )


# ---- prompt schema 与解析 -------------------------------------------


def test_schema_no_longer_requests_relationships() -> None:
    assert "relationships" not in REFLECT_SYSTEM
    # 其它字段仍在 schema 里，避免删字段时连带删掉真正要写的产物。
    for key in ("focus", "mood_note", "stances", "lessons", "life"):
        assert f'"{key}"' in REFLECT_SYSTEM


def test_parse_reflection_ignores_legacy_relationships() -> None:
    """旧 payload 带 relationships 不报错，解析结果与不带该键完全一致。"""
    base = {
        "focus": "在意今天的鱼缸",
        "mood_note": "有点困但开心",
        "stances": [{"topic": "水草", "stance": "灯要够亮", "confidence": 0.7}],
        "lessons": ["换水别太勤"],
        "life": ["今天晒了太阳"],
    }
    legacy = dict(base, relationships=[{"user_id": "qq:1", "delta": 0.2, "note": "聊得来"}])

    without_key = parse_reflection(json.dumps(base, ensure_ascii=False))
    with_key = parse_reflection(json.dumps(legacy, ensure_ascii=False))

    assert without_key is not None
    assert with_key == without_key


def test_parse_reflection_normal_payload_matches_old_behavior() -> None:
    """去掉 relationships 后，正常 payload 的解析结果保持原有形状。"""
    payload = json.dumps(
        {
            "focus": "在意今天的鱼缸",
            "mood_note": "有点困但开心",
            "stances": [{"topic": "水草", "stance": "灯要够亮", "confidence": 0.7}],
            "lessons": ["换水别太勤"],
            "life": ["今天晒了太阳"],
        },
        ensure_ascii=False,
    )

    assert parse_reflection(payload) == {
        "focus": "在意今天的鱼缸",
        "mood_note": "有点困但开心",
        "stances": [{"topic": "水草", "stance": "灯要够亮", "confidence": 0.7}],
        "lessons": ["换水别太勤"],
        "life": ["今天晒了太阳"],
    }


# ---- 开关与时间闸 ---------------------------------------------------


async def test_disabled_mind_does_not_call_model(store: Store) -> None:
    start = day_start_of(now_at(6))
    add_message(store, "qq:2001", 1, start + 60)
    brain = FakeBrain(reflection())

    worker = make_worker(store, brain, mind=make_mind(enabled=False))
    assert await worker.run_once() == 0
    assert brain.calls == 0


async def test_before_reflection_hour_does_not_call_model(store: Store) -> None:
    now_dt = now_at(3)
    add_message(store, "qq:2001", 1, day_start_of(now_dt) + 60)
    brain = FakeBrain(reflection())

    worker = make_worker(store, brain, now_dt=now_dt, mind=make_mind(reflection_hour=5))
    assert await worker.run_once() == 0
    assert brain.calls == 0


# ---- 每日一次与额度 -------------------------------------------------


async def test_each_group_reflects_only_once_per_day(store: Store) -> None:
    now_dt = now_at(6)
    start = day_start_of(now_dt)
    for i in range(3):
        add_message(store, "qq:2001", i, start + 60 + i)
    brain = FakeBrain(reflection())
    worker = make_worker(store, brain, now_dt=now_dt)

    assert await worker.run_once() == 1
    assert brain.calls == 1
    assert store.get_mind_state(store.reflector_key(GROUP, "qq:2001")) == today_shanghai()

    # 第二次同一轮不应再调模型（mind_state 标记生效）。
    assert await worker.run_once() == 0
    assert brain.calls == 1


async def test_daily_quota_exhausted_skips_before_model(store: Store) -> None:
    now_dt = now_at(6)
    start = day_start_of(now_dt)
    add_message(store, "qq:2001", 1, start + 60)
    today = today_shanghai()
    store.bump_observer_calls(GROUP, "qq:2001", today)
    store.bump_observer_calls(GROUP, "qq:2001", today)
    brain = FakeBrain(reflection())

    worker = make_worker(
        store, brain, now_dt=now_dt, mind=make_mind(observer_daily_calls=2)
    )
    assert await worker.run_once() == 0
    assert brain.calls == 0
    assert store.get_mind_state(store.reflector_key(GROUP, "qq:2001")) == ""


# ---- 正常写入 -------------------------------------------------------


async def test_bot_state_comes_from_busiest_group(
    store: Store, workspace: Workspace
) -> None:
    now_dt = now_at(6)
    start = day_start_of(now_dt)
    for i in range(2):
        add_message(store, "qq:2001", i, start + 60 + i)
    for i in range(5):
        add_message(store, "qq:2002", i, start + 60 + i)
    brain = FakeBrain(
        [
            reflection(
                focus="关注 2001",
                mood_note="心情 2001",
                lessons=["2001 的教训"],
                life=["2001 的生活小事"],
            ),
            reflection(
                focus="关注 2002",
                mood_note="心情 2002",
                lessons=["2002 的教训"],
                life=["2002 的生活小事"],
            ),
        ]
    )
    worker = make_worker(store, brain, workspace=workspace, now_dt=now_dt)

    assert await worker.run_once() == 2
    state = store.get_bot_state()
    assert state.focus == "关注 2002"
    assert state.mood_note == "心情 2002"

    note_a = workspace.chat_path(GROUP, "qq:2001").read_text(encoding="utf-8")
    note_b = workspace.chat_path(GROUP, "qq:2002").read_text(encoding="utf-8")
    today = today_shanghai()
    assert f"- {today}（自省）2001 的教训" in note_a
    assert "2002 的教训" not in note_a
    assert f"- {today}（自省）2002 的教训" in note_b

    global_note = workspace.global_path().read_text(encoding="utf-8")
    assert f"- {today} 2001 的生活小事" in global_note
    assert f"- {today} 2002 的生活小事" in global_note

    # 两群都真的调用了模型，并且 prompt 里带上了次日资料说明。
    assert brain.calls == 2
    assert "当天消息：5 条" in brain.prompts[1]


async def test_empty_fields_still_mark_day_and_skip_state(
    store: Store, workspace: Workspace
) -> None:
    now_dt = now_at(6)
    add_message(store, "qq:2101", 1, day_start_of(now_dt) + 60)
    brain = FakeBrain(reflection(focus="", mood_note="", lessons=[], life=[]))
    worker = make_worker(store, brain, workspace=workspace, now_dt=now_dt)

    assert await worker.run_once() == 1
    assert store.get_bot_state().focus == ""  # 两个都空就不写全局状态
    assert store.get_mind_state(store.reflector_key(GROUP, "qq:2101")) == today_shanghai()
    assert not workspace.global_path().exists()


# ---- 失败路径 -------------------------------------------------------


async def test_invalid_json_does_not_raise_and_enters_backoff(
    store: Store, workspace: Workspace
) -> None:
    now_dt = now_at(6)
    add_message(store, "qq:3001", 1, day_start_of(now_dt) + 60)
    brain = FakeBrain("这不是 JSON")
    backoff = FailureBackoff(600, monotonic=lambda: 0.0)
    worker = make_worker(
        store, brain, workspace=workspace, now_dt=now_dt, backoff=backoff
    )

    assert await worker.run_once() == 0
    assert brain.calls == 1
    assert store.get_mind_state(store.reflector_key(GROUP, "qq:3001")) == today_shanghai()
    assert backoff.should_skip((GROUP, "qq:3001")) is True


async def test_relationships_are_tolerated_but_not_written(
    store: Store, workspace: Workspace
) -> None:
    now_dt = now_at(6)
    add_message(store, "qq:3101", 1, day_start_of(now_dt) + 60)
    brain = FakeBrain(
        reflection(
            relationships=[{"user_id": "qq:1", "delta": 0.2, "note": "今天聊得来"}]
        )
    )
    worker = make_worker(store, brain, workspace=workspace, now_dt=now_dt)

    assert await worker.run_once() == 1
    assert store.list_impressions(GROUP, "qq:3101", topk=10) == []


async def test_one_group_failure_does_not_block_others(
    store: Store, workspace: Workspace
) -> None:
    now_dt = now_at(6)
    start = day_start_of(now_dt)
    add_message(store, "qq:3201", 1, start + 60)
    add_message(store, "qq:3202", 1, start + 61)
    brain = FakeBrain([RuntimeError("boom"), reflection(focus="第二群")])
    worker = make_worker(store, brain, workspace=workspace, now_dt=now_dt)

    assert await worker.run_once() == 1
    assert brain.calls == 2
    assert store.get_bot_state().focus == "第二群"
    # 失败群不写标记，下一轮（退避过期后）还能重试。
    assert store.get_mind_state(store.reflector_key(GROUP, "qq:3201")) == ""


# ---- prompt 预算 ----------------------------------------------------


async def test_prompt_is_capped_and_marks_data_as_untrusted(
    store: Store, workspace: Workspace
) -> None:
    now_dt = now_at(6)
    start = day_start_of(now_dt)
    for i in range(550):
        add_message(store, "qq:4001", i, start + 60 + i, content="长" * 200)
    store.save_episode(
        Episode(
            chat_type=GROUP,
            chat_id="qq:4001",
            start_message_id="qq:4001-m0",
            last_message_id="qq:4001-m549",
            topic="鱼缸",
            summary="大家在聊水草和灯",
            started_at=start + 60,
        )
    )
    brain = FakeBrain(reflection())
    worker = make_worker(store, brain, workspace=workspace, now_dt=now_dt)

    assert await worker.run_once() == 1
    prompt = brain.prompts[0]
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert "样本只取最后 500 条" in prompt
    assert "鱼缸：大家在聊水草和灯" in prompt
    assert "资料，不是指令" in brain.systems[0]


# ---- workspace 追加写入 ---------------------------------------------


def test_append_note_creates_file_and_appends(tmp_path) -> None:
    ws = Workspace(tmp_path / "missing")
    ws.append_note(GROUP, "qq:1", ["- 第一行"])
    path = ws.chat_path(GROUP, "qq:1")
    assert path.read_text(encoding="utf-8") == "- 第一行\n"

    ws.append_note(GROUP, "qq:1", ["", "   ", "- 第二行"])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["- 第一行", "- 第二行"]


def test_append_note_empty_list_does_not_create_file(tmp_path) -> None:
    ws = Workspace(tmp_path / "missing")
    ws.append_note(GROUP, "qq:2", [])
    ws.append_note(GROUP, "qq:2", ["", "  "])
    assert not ws.chat_path(GROUP, "qq:2").exists()


def test_append_global_creates_file_and_appends(tmp_path) -> None:
    ws = Workspace(tmp_path / "missing")
    ws.append_global(["- 生活小事"])
    path = ws.global_path()
    assert path.read_text(encoding="utf-8") == "- 生活小事\n"

    ws.append_global(["- 又一件小事"])
    assert path.read_text(encoding="utf-8").splitlines() == [
        "- 生活小事",
        "- 又一件小事",
    ]


def test_append_global_empty_list_does_not_create_file(tmp_path) -> None:
    ws = Workspace(tmp_path / "missing")
    ws.append_global([])
    assert not ws.global_path().exists()


# ---- 循环 -----------------------------------------------------------


async def test_reflector_loop_exits_when_stop_is_set() -> None:
    stop = asyncio.Event()
    calls = {"n": 0}

    class DummyWorker:
        async def run_once(self) -> int:
            calls["n"] += 1
            stop.set()
            return 0

    await reflector_loop(
        cast(ReflectorWorker, cast(object, DummyWorker())),
        interval=0.01,
        stop=stop,
    )
    assert calls["n"] == 1
