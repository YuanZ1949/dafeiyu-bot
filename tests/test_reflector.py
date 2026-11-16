"""夜间自省（Reflector）与 workspace 追加写入。

覆盖点：
* 开关 / 时间闸 / 每群每日一次 / 额度池；
* 每群各自写 bot_state_chats 行，互不覆盖；lessons 落群笔记、life 落 MEMORY.md；
* 非法 JSON 不冒泡，写标记并进入退避；
* prompt 头部带 user_id 名单；relationships 只对名单内、已有印象的群友
  刷新 confidence/last_seen_at，绝不新建印象；
* append_note / append_global 的创建、追加与空列表语义。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
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
    _gather_prompt,
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


def _add_held_row(
    store: Store, chat_type: ChatType, chat_id: str, checkpoint: str
) -> int:
    """写一条 held 决策并把它挪到 2 分钟前，绕开 record_decision 的 60 秒去重。"""
    row_id = store.record_decision(
        chat_type, chat_id, outcome="held", checkpoint=checkpoint
    )
    store._conn.execute(
        "UPDATE decisions SET created_at = created_at - 120 WHERE id = ?", (row_id,)
    )
    store._conn.commit()
    return row_id


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


def test_schema_requests_relationships_within_roster() -> None:
    assert '"relationships"' in REFLECT_SYSTEM
    assert '"delta"' in REFLECT_SYSTEM
    assert "user_id" in REFLECT_SYSTEM
    # delta 的取值范围与「只准引用名单内的 user_id」都要写进说明。
    assert "-1" in REFLECT_SYSTEM and "1" in REFLECT_SYSTEM
    assert "名单" in REFLECT_SYSTEM
    # 其它字段仍在 schema 里，避免删字段时连带删掉真正要写的产物。
    for key in ("focus", "mood_note", "stances", "lessons", "life"):
        assert f'"{key}"' in REFLECT_SYSTEM


def test_parse_reflection_filters_relationships_by_roster() -> None:
    """只收名单内 user_id 的关系；非法项丢弃，重复 id 取最后一次。"""
    payload = json.dumps(
        {
            "focus": "在意今天的鱼缸",
            "mood_note": "有点困但开心",
            "stances": [{"topic": "水草", "stance": "灯要够亮", "confidence": 0.7}],
            "relationships": [
                {"user_id": "qq:1", "delta": 0.2, "note": "聊得来"},
                {"user_id": "qq:9", "delta": 0.9},  # 名单外
                {"user_id": "", "delta": 0.1},  # 空 id
                {"user_id": "qq:2", "delta": "0.5"},  # 非数值 delta
                "oops",  # 非 dict
                {"user_id": "qq:1", "delta": -0.4},  # 重复 id，取最后一次
            ],
            "lessons": ["换水别太勤"],
            "life": ["今天晒了太阳"],
        },
        ensure_ascii=False,
    )

    rostered = parse_reflection(payload, valid_user_ids={"qq:1", "qq:2"})
    assert rostered is not None
    assert rostered["relationships"] == {"qq:1": pytest.approx(-0.4)}

    # 默认不传名单 = 不做名单校验：名单外但结构合法的项仍保留。
    open_parsed = parse_reflection(payload)
    assert open_parsed is not None
    assert open_parsed["relationships"] == {"qq:9": pytest.approx(0.9), "qq:1": pytest.approx(-0.4)}


def test_parse_reflection_clamps_delta_into_unit_range() -> None:
    payload = json.dumps(
        {
            "focus": "在意今天的鱼缸",
            "mood_note": "有点困但开心",
            "relationships": [
                {"user_id": "qq:1", "delta": 5},
                {"user_id": "qq:2", "delta": -3},
                {"user_id": "qq:3", "delta": "abc"},
                {"user_id": "qq:4"},  # 缺 delta
            ],
        },
        ensure_ascii=False,
    )

    parsed = parse_reflection(payload)
    assert parsed is not None
    assert parsed["relationships"] == {"qq:1": 1.0, "qq:2": -1.0}


def test_parse_reflection_caps_relationships_at_ten() -> None:
    items = [{"user_id": f"qq:{i}", "delta": 0.1} for i in range(12)]

    parsed = parse_reflection(json.dumps({"relationships": items}))

    assert parsed is not None
    assert len(parsed["relationships"]) == 10


def test_parse_reflection_normal_payload_matches_old_behavior() -> None:
    """正常 payload 的解析结果保持原有形状；relationships 空时收敛成空 dict。"""
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
        "relationships": {},
        "lessons": ["换水别太勤"],
        "life": ["今天晒了太阳"],
    }


def test_prompt_header_reports_both_engaged_and_replied_rates() -> None:
    """header 同时给「互动率（有人接）」与「被回复率」，两个口径不混用。"""
    prompt = _gather_prompt(
        GROUP,
        "qq:1",
        "2026-11-15",
        total_count=3,
        messages=[],
        episodes=[],
        bot_messages=[],
        stats={
            "total": 4,
            "engaged": 1,
            "quoted": 0,
            "replied": 2,
            "silent": 3,
            "engaged_rate": 0.25,
            "replied_rate": 0.5,
        },
    )

    header = prompt.splitlines()[3]
    assert "互动率" in header
    assert "有人接" in header
    assert "被回复率" in header
    assert "0.25" in header
    assert "0.5" in header


def make_prompt_message(user_id: str, nickname: str, index: int) -> Message:
    return Message(
        chat_type=GROUP,
        chat_id="qq:1",
        user_id=user_id,
        nickname=nickname,
        content=f"消息 {index}",
        message_id=f"m{index}",
        timestamp=index,
    )


def test_prompt_leads_with_user_id_roster() -> None:
    """prompt 最前面给 user_id|昵称 名单（最多 10 人），模型才有 id 可引用。"""
    messages = []
    for i in range(12):
        user_id, nickname = f"qq:{i}", f"成员{i}"
        for j in range(12 - i):  # 消息越早的人发言越多，保证排名确定
            messages.append(make_prompt_message(user_id, nickname, i * 10 + j))
    episodes = [
        Episode(
            chat_type=GROUP,
            chat_id="qq:1",
            start_message_id="m0",
            last_message_id="m0",
            topic="水草",
            summary="讨论灯够不够亮",
        )
    ]

    prompt = _gather_prompt(
        GROUP,
        "qq:1",
        "2026-11-15",
        total_count=len(messages),
        messages=messages,
        episodes=episodes,
        bot_messages=[make_prompt_message("qq:bot", "大肥鱼", 999)],
        stats={
            "total": 1,
            "engaged": 0,
            "quoted": 0,
            "replied": 0,
            "silent": 1,
            "engaged_rate": 0.0,
            "replied_rate": 0.0,
        },
    )

    assert "- qq:0|成员0: 12 条" in prompt
    assert "qq:9|成员9: 3 条" in prompt
    assert "qq:10|" not in prompt  # 只留前 10 人
    assert "qq:bot" not in prompt  # 名单不含 bot 自己
    # 名单排在对话小结与自己的发言之前：截断从尾部丢，名单不能被裁掉。
    assert prompt.index("可引用群友") < prompt.index("当天对话小结")
    assert prompt.index("可引用群友") < prompt.index("我当天说过的话")
    assert len(prompt) <= MAX_PROMPT_CHARS


def test_gather_prompt_appends_decision_line() -> None:
    """decision_line 是独立关键字参数，直接插进模板，不走 .format(**stats)。"""
    prompt = _gather_prompt(
        GROUP,
        "qq:1",
        "2026-11-15",
        total_count=3,
        messages=[],
        episodes=[],
        bot_messages=[],
        stats={
            "total": 1,
            "engaged": 0,
            "quoted": 0,
            "replied": 0,
            "silent": 1,
            "engaged_rate": 0.0,
            "replied_rate": 0.0,
        },
        decision_line="我今天的开口决策：回 1，静 2，攒 3，拦 0；最常见原因：opportunity_hold",
    )

    assert "我今天的开口决策：回 1，静 2，攒 3，拦 0；最常见原因：opportunity_hold" in prompt


def test_gather_prompt_without_decision_line_keeps_old_header() -> None:
    """默认 decision_line="" 时旧行为不变：不出现空行或占位。"""
    prompt = _gather_prompt(
        GROUP,
        "qq:1",
        "2026-11-15",
        total_count=3,
        messages=[],
        episodes=[],
        bot_messages=[],
        stats={
            "total": 1,
            "engaged": 0,
            "quoted": 0,
            "replied": 0,
            "silent": 1,
            "engaged_rate": 0.0,
            "replied_rate": 0.0,
        },
    )

    assert "开口决策" not in prompt
    assert prompt.splitlines()[3].startswith("我的搭话结果")


def test_prompt_roster_falls_back_to_user_id_without_nickname() -> None:
    prompt = _gather_prompt(
        GROUP,
        "qq:1",
        "2026-11-15",
        total_count=1,
        messages=[make_prompt_message("qq:7", "", 0)],
        episodes=[],
        bot_messages=[],
        stats={
            "total": 1,
            "engaged": 0,
            "quoted": 0,
            "replied": 0,
            "silent": 1,
            "engaged_rate": 0.0,
            "replied_rate": 0.0,
        },
    )

    assert "- qq:7|qq:7: 1 条" in prompt


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


# ---- 私聊自省（T14） --------------------------------------------------


def add_private_message(
    store: Store,
    chat_id: str,
    index: int,
    timestamp: int,
    content: str | None = None,
) -> None:
    store.add_message(
        Message(
            chat_type=ChatType.PRIVATE,
            chat_id=chat_id,
            user_id="qq:2",
            nickname="阿鱼",
            content=f"消息 {index}" if content is None else content,
            message_id=f"{chat_id}-p{index}",
            timestamp=timestamp,
        )
    )


def private_mind(*, observe_private: bool = True) -> MindConfig:
    return MindConfig(
        enabled=True,
        reflection_hour=5,
        observer_enabled=True,
        episodes_enabled=True,
        observe_private=observe_private,
    )


async def test_private_chat_reflected_when_observed(store, workspace) -> None:
    """observe_private 时私聊也自省：只写会话级 focus/mood，不写跨群立场/全局 MEMORY。"""
    now_dt = now_at(6)
    start = day_start_of(now_dt)
    add_private_message(store, "qq:7001", 1, start + 60, content="我在写 Rust 编译器")
    brain = FakeBrain(
        reflection(
            stances=[{"topic": "Rust", "stance": "应该自己写一个", "confidence": 0.8}],
            life=["私聊里聊了编译器"],
            lessons=["私聊也要按时换水"],
        )
    )
    worker = make_worker(
        store, brain, mind=private_mind(), workspace=workspace, now_dt=now_dt
    )

    assert await worker.run_once() == 1
    assert brain.calls == 1
    # 会话级 focus/mood 写入本私聊的 bot_state_chats 行。
    row = store.get_bot_state_chat(ChatType.PRIVATE, "qq:7001")
    assert row is not None
    assert row["focus"] == "在意今天聊的鱼缸"
    assert row["mood_note"] == "有点困但开心"
    # 隐私边界：私聊内容不得变成跨群立场或全局 MEMORY 笔记。
    assert store.list_stances(10) == []
    global_path = workspace.global_path()
    assert (
        not global_path.exists()
        or "私聊里聊了编译器" not in global_path.read_text(encoding="utf-8")
    )


async def test_private_chat_skipped_when_observe_private_false(store) -> None:
    now_dt = now_at(6)
    start = day_start_of(now_dt)
    add_private_message(store, "qq:7001", 1, start + 60)
    brain = FakeBrain(reflection())
    worker = make_worker(
        store, brain, mind=private_mind(observe_private=False), now_dt=now_dt
    )

    assert await worker.run_once() == 0
    assert brain.calls == 0
    assert store.get_bot_state_chat(ChatType.PRIVATE, "qq:7001") is None


# ---- 决策聚合进 prompt ------------------------------------------------


async def test_reflect_prompt_contains_scoped_decision_counts(
    store: Store, workspace: Workspace
) -> None:
    """prompt 里出现本会话的开口决策聚合计数，别会话的数据不串台。"""
    now_dt = now_at(6)
    start = day_start_of(now_dt)
    add_message(store, "qq:6001", 1, start + 60)
    _add_held_row(store, GROUP, "qq:6001", "opportunity_hold")
    store.record_decision(GROUP, "qq:6001", outcome="held", checkpoint="opportunity_hold")
    store.record_decision(GROUP, "qq:6001", outcome="silent", reason="太吵不接话")
    store.record_decision(GROUP, "qq:6001", outcome="blocked", checkpoint="budget")
    # 别的群的决策不许出现在本群 prompt。
    store.record_decision(GROUP, "qq:9999", outcome="replied", reason="只属于 9999 的原因")
    brain = FakeBrain(reflection())
    worker = make_worker(store, brain, workspace=workspace, now_dt=now_dt)

    assert await worker.run_once() == 1
    prompt = brain.prompts[0]
    assert (
        "我今天的开口决策：回 0，静 1，攒 2，拦 1；最常见原因：opportunity_hold"
        in prompt
    )
    # 只放聚合计数与 checkpoint 名，绝不放 decisions 的 reason 原文。
    assert "太吵不接话" not in prompt
    assert "只属于 9999 的原因" not in prompt


async def test_reflect_prompt_reports_none_without_decisions(
    store: Store, workspace: Workspace
) -> None:
    """当天没有决策行时计数全 0，checkpoint 写「无」。"""
    now_dt = now_at(6)
    add_message(store, "qq:6101", 1, day_start_of(now_dt) + 60)
    brain = FakeBrain(reflection())
    worker = make_worker(store, brain, workspace=workspace, now_dt=now_dt)

    assert await worker.run_once() == 1
    assert (
        "我今天的开口决策：回 0，静 0，攒 0，拦 0；最常见原因：无" in brain.prompts[0]
    )


# ---- 正常写入 -------------------------------------------------------


async def test_each_group_writes_its_own_bot_state(
    store: Store, workspace: Workspace
) -> None:
    """每群各自写自己的 bot_state_chats 行，A/B 互不覆盖，也不再写全局单行。"""
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
    state_a = store.get_bot_state_chat(GROUP, "qq:2001")
    state_b = store.get_bot_state_chat(GROUP, "qq:2002")
    assert state_a["focus"] == "关注 2001"
    assert state_a["mood_note"] == "心情 2001"
    assert state_b["focus"] == "关注 2002"
    assert state_b["mood_note"] == "心情 2002"
    # 全局单行不再被 Reflector 更新（首次升级回退的唯一数据源）。
    assert store.get_bot_state().focus == ""
    assert store.get_bot_state().mood_note == ""

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
    assert store.get_bot_state_chat(GROUP, "qq:2101") is None  # 也不落会话行
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


async def test_relationship_delta_updates_existing_impression(
    store: Store, workspace: Workspace
) -> None:
    now_dt = now_at(6)
    add_message(store, "qq:4001", 1, day_start_of(now_dt) + 60)  # 名单 = {"qq:1"}
    store.upsert_impression(GROUP, "qq:4001", "qq:1", "fact", "喜欢钓鱼", 0.5)
    store._conn.execute(
        "UPDATE impressions SET last_seen_at = datetime('now', '-10 days')"
    )
    store._conn.commit()
    brain = FakeBrain(
        reflection(
            relationships=[{"user_id": "qq:1", "delta": 0.2, "note": "今天聊得投机"}]
        )
    )
    worker = make_worker(store, brain, workspace=workspace, now_dt=now_dt)

    assert await worker.run_once() == 1
    rows = store._conn.execute(
        """
        SELECT confidence, last_seen_at, content, kind
        FROM impressions WHERE chat_id = 'qq:4001'
        """
    ).fetchall()
    assert len(rows) == 1  # 行数不变：绝不 INSERT 新印象
    assert rows[0]["confidence"] == pytest.approx(0.7)
    assert rows[0]["content"] == "喜欢钓鱼"  # note 不写进 content
    assert rows[0]["kind"] == "fact"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert rows[0]["last_seen_at"] >= cutoff  # last_seen_at 被刷新


async def test_relationship_for_user_outside_roster_writes_nothing(
    store: Store, workspace: Workspace
) -> None:
    now_dt = now_at(6)
    add_message(store, "qq:4101", 1, day_start_of(now_dt) + 60)  # 名单只有 qq:1
    brain = FakeBrain(
        reflection(relationships=[{"user_id": "qq:999", "delta": 0.9}])
    )
    worker = make_worker(store, brain, workspace=workspace, now_dt=now_dt)

    assert await worker.run_once() == 1
    assert store.list_impressions(GROUP, "qq:4101", topk=10) == []


# ---- apply_relationship_deltas（store 层）---------------------------


def test_apply_relationship_deltas_updates_only_matched_rows(store: Store) -> None:
    store.upsert_impression(GROUP, "qq:5001", "qq:1", "fact", "喜欢钓鱼", 0.5)
    store.upsert_impression(GROUP, "qq:5001", "qq:2", "fact", "养乌龟", 0.9)
    store._conn.execute(
        "UPDATE impressions SET last_seen_at = datetime('now', '-10 days')"
    )
    store._conn.commit()

    matched = store.apply_relationship_deltas(
        GROUP, "qq:5001", {"qq:1": 0.2, "qq:404": 0.5}
    )

    assert matched == 1  # 未知 user_id 不命中
    rows = store._conn.execute(
        "SELECT subject_user_id, confidence, last_seen_at FROM impressions"
    ).fetchall()
    assert len(rows) == 2  # 无新行
    by_user = {row["subject_user_id"]: row for row in rows}
    assert by_user["qq:1"]["confidence"] == pytest.approx(0.7)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert by_user["qq:1"]["last_seen_at"] >= cutoff
    assert by_user["qq:2"]["confidence"] == pytest.approx(0.9)  # 未点名的人不动
    assert by_user["qq:2"]["last_seen_at"] < cutoff


def test_apply_relationship_deltas_clamps_confidence(store: Store) -> None:
    store.upsert_impression(GROUP, "qq:5101", "qq:1", "fact", "喜欢钓鱼", 0.9)
    store.upsert_impression(GROUP, "qq:5102", "qq:2", "fact", "养乌龟", 0.2)

    assert store.apply_relationship_deltas(GROUP, "qq:5101", {"qq:1": 5.0}) == 1
    assert store.apply_relationship_deltas(GROUP, "qq:5102", {"qq:2": -5.0}) == 1

    high = store._conn.execute(
        "SELECT confidence FROM impressions WHERE chat_id = 'qq:5101'"
    ).fetchone()
    low = store._conn.execute(
        "SELECT confidence FROM impressions WHERE chat_id = 'qq:5102'"
    ).fetchone()
    assert high["confidence"] == pytest.approx(1.0)
    assert low["confidence"] == pytest.approx(0.0)


def test_apply_relationship_deltas_empty_and_explicit_now(store: Store) -> None:
    store.upsert_impression(GROUP, "qq:5201", "qq:1", "fact", "喜欢钓鱼", 0.5)

    assert store.apply_relationship_deltas(GROUP, "qq:5201", {}) == 0
    assert store.apply_relationship_deltas(
        GROUP, "qq:5201", {"qq:1": 0.1}, now="2026-01-01 00:00:00"
    ) == 1
    row = store._conn.execute(
        "SELECT last_seen_at, updated_at FROM impressions WHERE chat_id = 'qq:5201'"
    ).fetchone()
    assert row["last_seen_at"] == "2026-01-01 00:00:00"
    assert row["updated_at"] == "2026-01-01 00:00:00"


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
    assert store.get_bot_state_chat(GROUP, "qq:3202")["focus"] == "第二群"
    assert store.get_bot_state().focus == ""  # 全局单行不再被写
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

# ---- 记忆衰减（T10） -------------------------------------------------


async def test_run_once_decays_stances_once_per_day(store: Store) -> None:
    """run_once 末尾跑一次立场衰减并按天去重；同一天第二轮不再衰减。"""
    now_dt = now_at(6)
    start = day_start_of(now_dt)
    add_message(store, "qq:2001", 1, start + 60)
    store.upsert_stance("香菜", "好吃", confidence=1.0)
    brain = FakeBrain(reflection())
    worker = make_worker(store, brain, now_dt=now_dt)

    assert await worker.run_once() == 1

    key = f"reflector/decay/{today_shanghai()}"
    assert store.get_mind_state(key) == "1"
    stances = store.list_stances(5)
    assert stances[0].topic == "香菜"
    assert stances[0].confidence == pytest.approx(0.98)

    # 同一天再跑一轮（reflector_loop 每 10 分钟一次）：decay 不重复，
    # 否则 0.98^114 ≈ 0.10，当天就被 prune_stances 全清。
    store.upsert_stance("新话题", "新观点", confidence=1.0)
    assert await worker.run_once() == 0
    by_topic = {s.topic: s for s in store.list_stances(10)}
    assert by_topic["香菜"].confidence == pytest.approx(0.98)
    assert by_topic["新话题"].confidence == 1.0
