"""确定性心智 eval：8 个离线场景，全部走真实生产函数。

每个场景用独立的临时 SQLite 库（``Store + init_schema``），不写 ``data/``、
不联网、不读真实配置、不启动 loop。断言全部来自生产代码路径——recall 用
``context._recall_lines``，冷场用 ``service.silence_backoff_decision``，
episode 用 Observer 的 ``_write_episode``，额度用真实的 Observer/Reflector
``run_once``——场景里不复制任何生产逻辑来「自己断言自己」。

任一场景抛异常只把该场景标 failed（detail 带异常类型与消息），其余场景
照常跑完，``run_all`` 本身不崩。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dafeiyu.config import InjectionConfig, MindConfig
from dafeiyu.memory.context import _recall_lines, build_injection
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Episode, Message, OpenLoop, Participant
from dafeiyu.service import silence_backoff_decision
from dafeiyu.timeutil import today_shanghai
from dafeiyu.workers.failure import FailureBackoff
from dafeiyu.workers.observer import ObserverWorker, chat_type_allowed
from dafeiyu.workers.reflector import ReflectorWorker

TODAY = "2026-11-16"

SCENARIO_GROUP = "eval:mind-group"
SCENARIO_GROUP_A = "eval:mind-group-a"
SCENARIO_GROUP_B = "eval:mind-group-b"
SCENARIO_PRIVATE = "eval:mind-private"
SCENARIO_USER_7 = "eval:7"
SCENARIO_USER_8 = "eval:8"

ScenarioFunc = Callable[[Path], tuple[bool, str]]


def _new_store(root: Path, name: str) -> Store:
    store = Store(root / f"{name}.db")
    store.init_schema()
    return store


def _message(
    chat_type: ChatType,
    chat_id: str,
    user_id: str,
    nickname: str,
    content: str,
    message_id: str,
    *,
    timestamp: int = 0,
    is_bot: bool = False,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=message_id,
        is_bot=is_bot,
        timestamp=timestamp,
    )


def _episode(
    chat_id: str,
    start_id: str,
    topic: str,
    summary: str,
    *,
    status: str = "open",
    started_at: int = 0,
    importance: float = 0.5,
    participants: tuple[Participant, ...] = (),
) -> Episode:
    return Episode(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        start_message_id=start_id,
        last_message_id=start_id,
        topic=topic,
        summary=summary,
        participants=participants,
        status=status,
        started_at=started_at,
        importance=importance,
    )


# ---- ① recall_ranking ------------------------------------------------------


def _scenario_recall_ranking(root: Path) -> tuple[bool, str]:
    """60 天前高相关 closed episode 要排在今天低相关消息之前。

    今天给两条内容相同的低相关消息候选（单候选 min-max 归一化恒 0.5 的坑
    规避掉：同分归一化同为 0.5，不制造虚假满分）。
    """
    store = _new_store(root, "recall_ranking.db")
    try:
        now = int(time.time())
        store.save_episode(
            _episode(
                SCENARIO_GROUP,
                "ep-old-driver",
                "显卡驱动",
                "显卡驱动怎么装都不行，后来重装好了",
                status="closed",
                started_at=now - 60 * 86400,
                importance=0.9,
            )
        )
        # open episode 只进「正在聊的事」，绝不进召回段。
        store.save_episode(
            _episode(
                SCENARIO_GROUP,
                "ep-open-dinner",
                "今晚吃什么",
                "大家正在讨论晚饭",
                status="open",
                started_at=now - 120,
            )
        )
        for index, (mid, ts) in enumerate(
            (("m-today-1", now - 120), ("m-today-2", now - 60))
        ):
            store.add_message(
                _message(
                    ChatType.GROUP,
                    SCENARIO_GROUP,
                    "eval:1",
                    "阿猫",
                    "怎么装",
                    mid,
                    timestamp=ts,
                )
            )

        lines = _recall_lines(
            store,
            ChatType.GROUP,
            SCENARIO_GROUP,
            ["显卡驱动怎么装"],
            5,
            set(),
            float(now),
        )
        assert lines, "召回段为空：60 天前的 closed episode 没被召回"
        assert lines[0].startswith(
            "旧话题｜显卡驱动："
        ), f"旧 episode 没排第一：{lines}"
        assert "旧话题｜今晚吃什么" not in "".join(lines), "open episode 混进召回段"
        message_lines = [line for line in lines if not line.startswith("旧话题｜")]
        assert len(message_lines) == 2, f"今天的消息候选应有 2 条：{lines}"
        return True, "旧话题排第一；open episode 未出现；2 条今日消息候选在其后"
    finally:
        store.close()


# ---- ② silence_backoff -----------------------------------------------------


def _scenario_silence_backoff(root: Path) -> tuple[bool, str]:
    """同一 ``spoken_at`` 的多片回复算 1 轮；3 轮 silent 触发 hold。"""
    store = _new_store(root, "silence_backoff.db")
    try:
        # 同一次回复的 3 片 bot chunk 共享同一 spoken_at → 只算 1 轮。
        for index in range(3):
            store.upsert_reply_outcome(
                ChatType.GROUP,
                SCENARIO_GROUP,
                f"bot-same-{index}",
                spoken_at=1000,
                window_seconds=60,
                engaged=False,
                quoted=False,
                replied=False,
                silence_after=True,
            )
        assert (
            store.consecutive_silent_turns(ChatType.GROUP, SCENARIO_GROUP, limit=3)
            == 1
        ), "同一 spoken_at 的 3 片回复应只算 1 轮"
        # 3 个不同 spoken_at 的 silent 轮 → 连续 3 轮。
        for index in range(3):
            store.upsert_reply_outcome(
                ChatType.GROUP,
                SCENARIO_GROUP,
                f"bot-distinct-{index}",
                spoken_at=2000 + index * 1000,
                window_seconds=60,
                engaged=False,
                quoted=False,
                replied=False,
                silence_after=True,
            )
        assert (
            store.consecutive_silent_turns(ChatType.GROUP, SCENARIO_GROUP, limit=3)
            == 3
        ), "3 个不同 spoken_at 的 silent 轮应算 3 轮"

        # T6 抽出的模块级纯函数：不构造 Service。
        hold = silence_backoff_decision(
            enabled=True,
            addressed=False,
            streak=3,
            threshold=3,
            age=None,
            window_seconds=900,
            known_silent=False,
        )
        assert hold == ("hold", "silence_backoff", None), f"3 轮 silent 应 hold：{hold}"
        expired = silence_backoff_decision(
            enabled=True,
            addressed=False,
            streak=3,
            threshold=3,
            age=1000,
            window_seconds=900,
            known_silent=False,
        )
        assert expired == (
            "release",
            "silence_backoff_expired",
            1000,
        ), f"超过 15 分钟窗口应放行：{expired}"
        assert (
            silence_backoff_decision(
                enabled=True,
                addressed=True,
                streak=3,
                threshold=3,
                age=None,
                window_seconds=900,
                known_silent=False,
            )
            is None
        ), "被点名（addressed）不应触发冷场 hold"
        assert (
            silence_backoff_decision(
                enabled=False,
                addressed=False,
                streak=3,
                threshold=3,
                age=None,
                window_seconds=900,
                known_silent=False,
            )
            is None
        ), "开关关闭不应触发冷场 hold"
        return True, "同 spoken_at 3 片=1 轮；3 轮 silent→hold；超窗 release；点名/关闸 None"
    finally:
        store.close()


# ---- ③ multi_episode -------------------------------------------------------


class _NeverCalledBrain:
    """场景只走 ``_write_episode`` 落库路径，模型绝不该被调用。"""

    async def complete_detailed(self, *args: Any, **kwargs: Any) -> tuple[str, int, int]:
        raise AssertionError("multi_episode 场景不应调用模型")


def _observe_topic(
    observer: ObserverWorker,
    topic: str,
    *,
    importance: float,
    first_ts: int,
    start_id: str,
    last_id: str,
) -> None:
    payload = {
        "topic": topic,
        "summary": f"{topic}的观察摘要",
        "mood": "平静",
        "importance": importance,
        "participants": [{"user_id": "eval:1", "nickname": "阿猫"}],
    }
    messages = [
        _message(
            ChatType.GROUP,
            SCENARIO_GROUP,
            "eval:1",
            "阿猫",
            f"聊到{topic}",
            start_id,
            timestamp=first_ts,
        ),
        _message(
            ChatType.GROUP,
            SCENARIO_GROUP,
            "eval:1",
            "阿猫",
            f"{topic}又说了一句",
            last_id,
            timestamp=first_ts + 30,
        ),
    ]
    observer._write_episode(ChatType.GROUP, SCENARIO_GROUP, messages, messages, payload)


def _open_topics(store: Store) -> list[str]:
    return [
        episode.topic
        for episode in store.episodes_for_injection(
            ChatType.GROUP, SCENARIO_GROUP, limit=10
        )
    ]


def _scenario_multi_episode(root: Path) -> tuple[bool, str]:
    """同 gap 并行话题各自成行；第 4 个话题挤掉最低重要度；30 分钟 gap 换段。"""
    store = _new_store(root, "multi_episode.db")
    try:
        observer = ObserverWorker(
            store,
            _NeverCalledBrain(),
            MindConfig(enabled=True, observer_enabled=True),
            backoff=FailureBackoff(3600, monotonic=lambda: 0.0),
            now=lambda: 2000.0,
        )
        # 同一 gap 内两个话题 → 2 条 open。
        base = 1_000_000
        _observe_topic(
            observer, "鱼缸换水", importance=0.9, first_ts=base,
            start_id="m-e0", last_id="m-e0b",
        )
        _observe_topic(
            observer, "显卡驱动报错", importance=0.8, first_ts=base + 60,
            start_id="m-e1", last_id="m-e1b",
        )
        assert sorted(_open_topics(store)) == [
            "显卡驱动报错",
            "鱼缸换水",
        ], f"同 gap 两个话题应有 2 条 open：{_open_topics(store)}"

        # 4 个不同话题 → 恰 3 条 open，最低 importance 的被关。
        _observe_topic(
            observer, "晚饭吃什么", importance=0.7, first_ts=base + 120,
            start_id="m-e2", last_id="m-e2b",
        )
        _observe_topic(
            observer, "周末爬山计划", importance=0.6, first_ts=base + 180,
            start_id="m-e3", last_id="m-e3b",
        )
        topics = _open_topics(store)
        assert len(topics) == 3, f"4 个话题后应恰 3 条 open：{topics}"
        assert "周末爬山计划" not in topics, "最低 importance 的话题应被收尾"
        all_rows = store.episodes_between(
            ChatType.GROUP, SCENARIO_GROUP, 0, base + 600, limit=50
        )
        closed_low = [
            row for row in all_rows if row.topic == "周末爬山计划" and row.status != "open"
        ]
        assert closed_low, "被挤掉的话题应是 closed 而不是消失"

        # 间隔 >30 分钟 → 换段：仅新段 open，旧的全部收尾。
        _observe_topic(
            observer, "明日聚餐地点", importance=0.5, first_ts=base + 210 + 1801,
            start_id="m-e4", last_id="m-e4b",
        )
        topics = _open_topics(store)
        assert topics == ["明日聚餐地点"], f"30 分钟 gap 后应只剩新段 open：{topics}"
        return True, "同 gap 2 话题=2 open；4 话题=3 open（最低分收尾）；30 分钟 gap 后仅新段 open"
    finally:
        store.close()


# ---- ④ mood_ttl ------------------------------------------------------------


def _scenario_mood_ttl(root: Path) -> tuple[bool, str]:
    """会话级近况/心情：12h TTL 到期不注入；A/B 群互不串台。"""
    store = _new_store(root, "mood_ttl.db")
    try:
        cfg = InjectionConfig(inject_impressions=True, inject_state=True)
        now = time.time()
        store.set_bot_state_chat(
            ChatType.GROUP,
            SCENARIO_GROUP_A,
            focus="鱼缸换水",
            mood_note="有点困",
            now_ts=now,
        )
        injection_a = build_injection(
            store,
            None,
            ChatType.GROUP,
            SCENARIO_GROUP_A,
            cfg,
            today=TODAY,
            now_ts=now,
            state_ttl_hours=12,
        )
        injection_b = build_injection(
            store,
            None,
            ChatType.GROUP,
            SCENARIO_GROUP_B,
            cfg,
            today=TODAY,
            now_ts=now,
            state_ttl_hours=12,
        )
        assert "现在在意：鱼缸换水" in injection_a, "A 群自己的近况应注入"
        assert "心情：有点困" in injection_a, "A 群 12h 内的心情应注入"
        assert injection_b == "", f"B 群不该看到 A 群的状态：{injection_b!r}"

        # 心情时间戳退到 13h 前：超过 12h TTL，心情不再注入（focus 仍在）。
        store.set_bot_state_chat(
            ChatType.GROUP,
            SCENARIO_GROUP_A,
            focus="鱼缸换水",
            mood_note="有点困",
            now_ts=now - 13 * 3600,
        )
        injection_a_stale = build_injection(
            store,
            None,
            ChatType.GROUP,
            SCENARIO_GROUP_A,
            cfg,
            today=TODAY,
            now_ts=now,
            state_ttl_hours=12,
        )
        assert "心情：有点困" not in injection_a_stale, "13h 前的心情不应再注入"
        assert "有点困" not in injection_a_stale, "心情整行都应消失"
        assert "现在在意：鱼缸换水" in injection_a_stale, "focus 不受心情 TTL 影响"
        return True, "A 群 12h 内注入 focus+心情；B 群为空不串台；13h 后心情整行消失"
    finally:
        store.close()


# ---- ⑤ privacy_delete ------------------------------------------------------


def _scenario_privacy_delete(root: Path) -> tuple[bool, str]:
    """用户级删除：可归因记忆消失、他人保留、不可归因如实上报。"""
    store = _new_store(root, "privacy_delete.db")
    try:
        now = int(time.time())
        store.add_message(
            _message(
                ChatType.GROUP,
                SCENARIO_GROUP,
                SCENARIO_USER_7,
                "小七",
                "我用的显卡驱动一直装不上",
                "msg-7",
                timestamp=now - 60,
            )
        )
        store.add_message(
            _message(
                ChatType.GROUP,
                SCENARIO_GROUP,
                SCENARIO_USER_8,
                "小八",
                "周末一起去爬山吗",
                "msg-8",
                timestamp=now - 30,
            )
        )
        store.upsert_impression(
            ChatType.GROUP,
            SCENARIO_GROUP,
            SCENARIO_USER_7,
            "trait",
            "喜欢折腾硬件",
            0.8,
            subject_nickname="小七",
        )
        store.upsert_impression(
            ChatType.GROUP,
            SCENARIO_GROUP,
            SCENARIO_USER_8,
            "trait",
            "喜欢户外运动",
            0.8,
            subject_nickname="小八",
        )
        store.add_open_loop(
            OpenLoop(
                chat_type=ChatType.GROUP,
                chat_id=SCENARIO_GROUP,
                kind="question",
                content="小七的驱动问题还没人接",
                subject_user_id=SCENARIO_USER_7,
            )
        )
        store.add_open_loop(
            OpenLoop(
                chat_type=ChatType.GROUP,
                chat_id=SCENARIO_GROUP,
                kind="plan",
                content="小八约的周末爬山等回复",
                subject_user_id=SCENARIO_USER_8,
            )
        )
        # 只有 7 参与的 episode（整行删）与 7/8 共同参与（participants 更新）。
        store.save_episode(
            _episode(
                SCENARIO_GROUP,
                "ep-only-7",
                "驱动装不上",
                "小七装驱动失败",
                started_at=now - 120,
                participants=(Participant(SCENARIO_USER_7, "小七"),),
            )
        )
        store.save_episode(
            _episode(
                SCENARIO_GROUP,
                "ep-both",
                "周末安排",
                "聊周末去哪",
                started_at=now - 90,
                participants=(
                    Participant(SCENARIO_USER_7, "小七"),
                    Participant(SCENARIO_USER_8, "小八"),
                ),
            )
        )
        store.record_decision(
            ChatType.GROUP,
            SCENARIO_GROUP,
            outcome="silent",
            checkpoint="batch",
            message_ids=("msg-7",),
        )
        store.record_decision(
            ChatType.GROUP,
            SCENARIO_GROUP,
            outcome="replied",
            checkpoint="batch",
            message_ids=("msg-8",),
        )
        store.add_shared_fact(
            "群主的猫叫什么名字？",
            "叫小黑，是一只橘猫。",
            ChatType.GROUP,
            SCENARIO_GROUP,
            nickname="小七",
            user_id=SCENARIO_USER_7,
        )

        result = store.delete_user_data(
            ChatType.GROUP, SCENARIO_GROUP, SCENARIO_USER_7
        )
        assert result["deleted_impressions"] == 1, f"应删 7 的 1 条印象：{result}"
        assert result["deleted_open_loops"] == 1, f"应删 7 的 1 条未了事：{result}"
        assert result["deleted_decisions"] == 1, f"应删按 msg-7 归因的决策：{result}"
        assert result["deleted_shared_facts"] == 1, f"应删 7 来源的共享事实：{result}"
        assert result["deleted_episodes"] == 1, f"只剩 7 参与的 episode 应整行删：{result}"
        assert result["updated_episodes"] == 1, f"共同参与的 episode 应更新 participants：{result}"
        assert "summaries" in result["unattributable"], "summaries 应列为不可归因"
        assert "weekly_summaries" in result["unattributable"], "周汇总应列为不可归因"

        # 7 的可归因记忆消失，8 的保留。
        impressions = store.list_impressions(ChatType.GROUP, SCENARIO_GROUP, 10)
        assert [imp.subject_nickname for imp in impressions] == ["小八"], (
            f"删后只剩 8 的印象：{impressions}"
        )
        loops = store.open_loops(ChatType.GROUP, SCENARIO_GROUP, limit=10)
        assert [loop.subject_user_id for loop in loops] == [SCENARIO_USER_8], (
            f"删后只剩 8 的未了事：{loops}"
        )
        assert store.shared_facts() == [], "7 来源的共享事实应被删光"
        # 8 的数据原样保留（决策按 msg-8 归因，未受影响）。
        stats = store.decision_stats(
            chat_type=ChatType.GROUP, chat_id=SCENARIO_GROUP, days=1
        )
        assert stats["total"] == 1, f"8 的决策应保留、7 的应删除：{stats}"
        remaining = store.episodes_between(
            ChatType.GROUP, SCENARIO_GROUP, 0, now + 1, limit=10
        )
        both = [row for row in remaining if row.topic == "周末安排"]
        assert len(both) == 1, f"共同 episode 应保留：{remaining}"
        assert [p.user_id for p in both[0].participants] == [SCENARIO_USER_8], (
            "共同 episode 的 participants 应只剩 8"
        )
        return True, "7 的印象/未了事/决策/共享事实/独占 episode 消失；8 全部保留；unattributable 如实上报"
    finally:
        store.close()


# ---- ⑥ private_gate --------------------------------------------------------


def _scenario_private_gate(root: Path) -> tuple[bool, str]:
    """observe_private 四层门控矩阵；私聊数据不进入群聊注入。"""
    store = _new_store(root, "private_gate.db")
    try:
        full = MindConfig(
            enabled=True,
            observer_enabled=True,
            episodes_enabled=True,
            observe_private=True,
        )
        assert chat_type_allowed(full, ChatType.GROUP), "全开时群聊应放行"
        assert chat_type_allowed(full, ChatType.PRIVATE), "全开时私聊应放行"

        assert not chat_type_allowed(
            MindConfig(
                enabled=False,
                observer_enabled=True,
                episodes_enabled=True,
                observe_private=True,
            ),
            ChatType.PRIVATE,
        ), "总闸关闭时私聊必须 False"
        assert not chat_type_allowed(
            MindConfig(
                enabled=False,
                observer_enabled=True,
                episodes_enabled=True,
                observe_private=True,
            ),
            ChatType.GROUP,
        ), "总闸对群聊同样生效，没有 GROUP 直通"
        assert not chat_type_allowed(
            MindConfig(
                enabled=True,
                observer_enabled=False,
                episodes_enabled=True,
                observe_private=True,
            ),
            ChatType.PRIVATE,
        ), "observer_enabled 关时私聊 False"
        assert not chat_type_allowed(
            MindConfig(
                enabled=True,
                observer_enabled=True,
                episodes_enabled=False,
                observe_private=True,
            ),
            ChatType.PRIVATE,
        ), "episodes_enabled 关时私聊 False"
        closed_to_private = MindConfig(
            enabled=True,
            observer_enabled=True,
            episodes_enabled=True,
            observe_private=False,
        )
        assert not chat_type_allowed(
            closed_to_private, ChatType.PRIVATE
        ), "observe_private 关时私聊 False"
        assert chat_type_allowed(
            closed_to_private, ChatType.GROUP
        ), "observe_private 关不影响群聊"

        # 私聊印象/话题不进入群聊注入（数据按 (chat_type, chat_id) 取）。
        now = time.time()
        same_id = "eval:same-id"
        store.upsert_impression(
            ChatType.PRIVATE,
            same_id,
            "eval:9",
            "trait",
            "私聊里说过喜欢钓鱼",
            0.9,
            subject_nickname="私友",
        )
        store.save_episode(
            Episode(
                chat_type=ChatType.PRIVATE,
                chat_id=same_id,
                start_message_id="ep-private",
                last_message_id="ep-private",
                topic="私聊聊的鱼竿",
                summary="对方想买新鱼竿",
                status="open",
                started_at=int(now) - 60,
            )
        )
        cfg = InjectionConfig(
            inject_impressions=True,
            inject_episodes=True,
            mind_chars=2000,
        )
        group_injection = build_injection(
            store, None, ChatType.GROUP, same_id, cfg, today=TODAY, now_ts=now
        )
        private_injection = build_injection(
            store, None, ChatType.PRIVATE, same_id, cfg, today=TODAY, now_ts=now
        )
        assert "私聊里说过喜欢钓鱼" not in group_injection, "私聊印象不得进群聊注入"
        assert "私聊聊的鱼竿" not in group_injection, "私聊话题不得进群聊注入"
        assert "私聊里说过喜欢钓鱼" in private_injection, "私聊注入应含自己的印象"
        assert "私聊聊的鱼竿" in private_injection, "私聊注入应含自己的话题"
        return True, "四层门控矩阵全对；私聊印象/话题不串进同 id 群聊注入"
    finally:
        store.close()


# ---- ⑦ chat_isolation ------------------------------------------------------


def _scenario_chat_isolation(root: Path) -> tuple[bool, str]:
    """A 群状态对 B 群不可见；``forget_chat(A)`` 只清 A 不动 B。"""
    store = _new_store(root, "chat_isolation.db")
    try:
        cfg = InjectionConfig(inject_impressions=True, inject_state=True)
        now = time.time()
        store.set_bot_state_chat(
            ChatType.GROUP,
            SCENARIO_GROUP_A,
            focus="A群在聊的事",
            mood_note="热闹",
            now_ts=now,
        )
        store.upsert_impression(
            ChatType.GROUP,
            SCENARIO_GROUP_A,
            "eval:1",
            "trait",
            "A群印象内容",
            0.8,
            subject_nickname="小甲",
        )
        store.save_chat_state(ChatType.GROUP, SCENARIO_GROUP_A, {"muted_until": now + 999})
        store.record_decision(
            ChatType.GROUP, SCENARIO_GROUP_A, outcome="silent", checkpoint="batch"
        )

        # B 群没有任何数据：注入应为空，不串 A 群的状态与印象。
        injection_b_empty = build_injection(
            store,
            None,
            ChatType.GROUP,
            SCENARIO_GROUP_B,
            cfg,
            today=TODAY,
            now_ts=now,
        )
        assert injection_b_empty == "", f"B 群无数据时注入应为空：{injection_b_empty!r}"

        # B 群写入自己的数据后，/forget A 只清 A。
        store.set_bot_state_chat(
            ChatType.GROUP,
            SCENARIO_GROUP_B,
            focus="B群在聊的事",
            mood_note="平静",
            now_ts=now,
        )
        store.upsert_impression(
            ChatType.GROUP,
            SCENARIO_GROUP_B,
            "eval:2",
            "trait",
            "B群印象内容",
            0.8,
            subject_nickname="小乙",
        )
        store.record_decision(
            ChatType.GROUP, SCENARIO_GROUP_B, outcome="replied", checkpoint="batch"
        )

        store.forget_chat(ChatType.GROUP, SCENARIO_GROUP_A)
        assert store.get_bot_state_chat(ChatType.GROUP, SCENARIO_GROUP_A) is None, (
            "forget_chat 后 A 群 bot_state_chats 应清零"
        )
        assert store.load_chat_state(ChatType.GROUP, SCENARIO_GROUP_A) is None, (
            "forget_chat 后 A 群 chat_state（含 muted_until）应清零"
        )
        stats_a = store.decision_stats(
            chat_type=ChatType.GROUP, chat_id=SCENARIO_GROUP_A, days=1
        )
        assert stats_a["total"] == 0, f"forget_chat 后 A 群决策流水应清零：{stats_a}"
        assert store.list_impressions(ChatType.GROUP, SCENARIO_GROUP_A, 10) == [], (
            "forget_chat 后 A 群印象应清零"
        )

        # B 群原样保留。
        state_b = store.get_bot_state_chat(ChatType.GROUP, SCENARIO_GROUP_B)
        assert state_b is not None and state_b["focus"] == "B群在聊的事", (
            f"forget A 不应动 B 的状态：{state_b}"
        )
        stats_b = store.decision_stats(
            chat_type=ChatType.GROUP, chat_id=SCENARIO_GROUP_B, days=1
        )
        assert stats_b["total"] == 1, f"forget A 不应动 B 的决策流水：{stats_b}"
        injection_b = build_injection(
            store, None, ChatType.GROUP, SCENARIO_GROUP_B, cfg, today=TODAY, now_ts=now
        )
        assert "B群印象内容" in injection_b and "B群在聊的事" in injection_b
        assert "A群印象内容" not in injection_b and "A群在聊的事" not in injection_b
        return True, "B 群注入为空不串台；forget_chat(A) 清 chat_state/bot_state_chats/decisions，B 全保留"
    finally:
        store.close()


# ---- ⑧ quota_reserve -------------------------------------------------------


class _RefusingBrain:
    """Observer 让位后绝不该碰到模型：被调用即判失败。"""

    async def complete_detailed(self, *args: Any, **kwargs: Any) -> tuple[str, int, int]:
        raise AssertionError("Observer 在额度让位后不应调用模型")


class _ReflectionBrain:
    """返回一份可解析的最小自省 JSON；记录调用次数供断言。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_detailed(
        self, system: str, messages: list[dict[str, str]], max_tokens: int = 600, temperature: float = 0.7
    ) -> tuple[str, int, int]:
        self.calls += 1
        return (
            json.dumps(
                {"focus": "在意群里的话题", "mood_note": "平静"},
                ensure_ascii=False,
            ),
            10,
            5,
        )


def _scenario_quota_reserve(root: Path) -> tuple[bool, str]:
    """``observer_daily_calls=2``：Observer 用满 ``daily-1=1`` 次让位，Reflector 仍跑 1 次。

    Observer/Reflector 都用真实 ``run_once``（fake brain 离线应答），不复制
    生产里的门限表达式。
    """
    store = _new_store(root, "quota_reserve.db")
    try:
        now_ts = int(time.time())
        today = today_shanghai()
        mind = MindConfig(
            enabled=True,
            observer_enabled=True,
            observer_daily_calls=2,
            observer_min_messages=1,
            reflection_hour=0,
        )
        store.add_message(
            _message(
                ChatType.GROUP,
                SCENARIO_GROUP,
                "eval:1",
                "阿猫",
                "今天聊点什么",
                "m-quota-1",
                timestamp=now_ts - 60,
            )
        )
        # Observer 先把它的份额（daily-1=1）用满。
        assert store.bump_observer_calls(ChatType.GROUP, SCENARIO_GROUP, today) == 1

        observer = ObserverWorker(
            store,
            _RefusingBrain(),
            mind,
            backoff=FailureBackoff(3600, monotonic=lambda: 0.0),
            now=lambda: float(now_ts),
        )
        assert asyncio.run(observer.run_once()) == 0, (
            "Observer 用满 daily-1 后应让位（run_once 返回 0）"
        )
        assert store.observer_calls_today(ChatType.GROUP, SCENARIO_GROUP, today) == 1, (
            "Observer 让位时不得再烧额度"
        )

        reflector = ReflectorWorker(
            store,
            _ReflectionBrain(),
            mind,
            None,
            backoff=FailureBackoff(3600, monotonic=lambda: 0.0),
            now=lambda: float(now_ts),
        )
        assert asyncio.run(reflector.run_once()) == 1, (
            "Observer 让位后 Reflector 仍应能跑 1 次"
        )
        assert store.observer_calls_today(ChatType.GROUP, SCENARIO_GROUP, today) == 2, (
            "Reflector 应从共享池再占 1 次"
        )
        assert asyncio.run(reflector.run_once()) == 0, "当日额度耗尽/已自省后不再跑"
        return True, "真实 run_once：Observer 用满 daily-1 让位 0 次；Reflector 仍跑 1 次；当日第二次不再跑"
    finally:
        store.close()


_SCENARIOS: dict[str, ScenarioFunc] = {
    "recall_ranking": _scenario_recall_ranking,
    "silence_backoff": _scenario_silence_backoff,
    "multi_episode": _scenario_multi_episode,
    "mood_ttl": _scenario_mood_ttl,
    "privacy_delete": _scenario_privacy_delete,
    "private_gate": _scenario_private_gate,
    "chat_isolation": _scenario_chat_isolation,
    "quota_reserve": _scenario_quota_reserve,
}


def run_all(tmp_dir: Path | None = None) -> dict[str, Any]:
    """跑全部 8 个心智场景，返回 ``{"ok": bool, "scenarios": [...]}``。

    ``tmp_dir`` 不传时内部用 ``tempfile.TemporaryDirectory``（跑完即清）；
    传入时把各场景的 SQLite 库放在该目录下（目录不存在会自动创建）。单场景
    异常只把该场景标 failed，不影响其余场景，也不让本函数抛出。
    """
    if tmp_dir is None:
        holder = tempfile.TemporaryDirectory(prefix="dafeiyu-eval-mind-")
        root = Path(holder.name)
        cleanup = holder.cleanup
    else:
        root = Path(tmp_dir)
        root.mkdir(parents=True, exist_ok=True)
        cleanup = None
    try:
        scenarios: list[dict[str, Any]] = []
        for name, scenario in _SCENARIOS.items():
            try:
                passed, detail = scenario(root)
            except Exception as exc:  # noqa: BLE001 - 单场景失败不拖垮整体
                passed, detail = False, f"{type(exc).__name__}: {exc}"
            scenarios.append(
                {"name": name, "passed": bool(passed), "detail": str(detail)}
            )
    finally:
        if cleanup is not None:
            cleanup()
    return {"ok": all(item["passed"] for item in scenarios), "scenarios": scenarios}
