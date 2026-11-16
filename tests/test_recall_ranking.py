from __future__ import annotations

import math
import time
from typing import cast

from dafeiyu.memory.context import _recall_lines
from dafeiyu.memory.recall import WEIGHTS, freshness, normalize_relevance, score
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

GROUP = ChatType.GROUP


def make_msg(
    message_id: str,
    content: str,
    *,
    timestamp: int = 0,
    chat_type: ChatType = GROUP,
    chat_id: str = "100",
    nickname: str = "鱼片",
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id="7",
        nickname=nickname,
        content=content,
        message_id=message_id,
        is_bot=False,
        timestamp=timestamp,
    )


class FakeStore:
    """只实现 _recall_lines 用到的 search_message_terms 与 episodes_between。"""

    def __init__(self, hits: list[tuple[Message, str, float | None]]) -> None:
        self.hits: list[tuple[Message, str, float | None]] = hits

    def search_message_terms(
        self,
        terms: list[str],
        chat_type: ChatType,
        chat_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[tuple[Message, str, float | None]]]:
        return len(self.hits), self.hits

    def episodes_between(
        self,
        chat_type: ChatType,
        chat_id: str,
        start_ts: int,
        end_ts: int,
        *,
        limit: int = 20,
    ) -> list[object]:
        """本文件没有旧话题场景，窗口恒为空。"""
        return []


def as_store(fake: FakeStore) -> Store:
    """测试替身塞给 _recall_lines：它只用 search_message_terms 与 episodes_between。"""
    return cast(Store, cast(object, fake))


def test_weights_sum_to_one() -> None:
    assert len(WEIGHTS) == 4
    assert math.fsum(WEIGHTS) == 1.0
    assert all(0.0 <= w <= 1.0 for w in WEIGHTS)


def test_freshness_half_life_is_seven_days() -> None:
    now = 1_800_000_000.0
    assert freshness(now, now) == 1.0
    # spec §5.4：7 天半衰期，不是 30 天。
    assert freshness(now - 7 * 86400, now) == 0.5
    assert freshness(now - 14 * 86400, now) == 0.25


def test_freshness_monotonic_and_clamped() -> None:
    now = 1_800_000_000.0
    ages = [0, 3600, 86400, 7 * 86400, 30 * 86400, 365 * 86400]
    values = [freshness(now - a, now) for a in ages]
    assert all(a > b for a, b in zip(values, values[1:]))
    # 未来时间戳（时钟偏差）不应超过 1.0。
    assert freshness(now + 3600, now) == 1.0


def test_relevance_min_max_with_inverted_bm25() -> None:
    """bm25 越小越相关：rank 小的候选归一化后得分高。"""
    rel = normalize_relevance({"a": 1.0, "b": 3.0, "c": 2.0})
    assert rel["a"] == 1.0
    assert rel["b"] == 0.0
    assert rel["c"] == 0.5


def test_relevance_none_and_degenerate_cases() -> None:
    # LIKE 回退候选没有 rank → 0.5；有 rank 的照常归一化。
    rel = normalize_relevance({"a": 1.0, "b": None, "c": 3.0})
    assert rel["a"] == 1.0
    assert rel["b"] == 0.5
    assert rel["c"] == 0.0
    # 全体相等或全 None：统一 0.5，不制造虚假满分。
    assert normalize_relevance({"a": 2.0, "b": 2.0}) == {"a": 0.5, "b": 0.5}
    assert normalize_relevance({"a": None, "b": None}) == {"a": 0.5, "b": 0.5}
    assert normalize_relevance({}) == {}


def test_score_monotonic_per_factor() -> None:
    base = (0.3, 0.4, 0.5, 0.6)
    base_score = score(*base)
    for i in range(4):
        bumped = list(base)
        bumped[i] = min(1.0, base[i] + 0.3)
        assert score(*bumped) > base_score, f"factor {i} not monotonic"


def test_score_weights_applied_and_clamped() -> None:
    # 各因子单独拉满的贡献正好等于对应权重。
    assert score(1.0, 0.0, 0.0, 0.0) == WEIGHTS[0]
    assert score(0.0, 1.0, 0.0, 0.0) == WEIGHTS[1]
    assert score(0.0, 0.0, 1.0, 0.0) == WEIGHTS[2]
    assert score(0.0, 0.0, 0.0, 1.0) == WEIGHTS[3]
    # 越界输入 clamp 到 [0,1]。
    assert score(1.5, -1.0, 2.0, 5.0) == score(1.0, 0.0, 1.0, 1.0)


def test_score_deterministic_bit_identical() -> None:
    args = (0.42, 0.17, 0.5, 0.0)
    first = score(*args)
    for _ in range(3):
        assert score(*args) == first


def test_old_high_relevance_outranks_new_low_relevance() -> None:
    now = float(time.time())
    rel = normalize_relevance({"old": 0.1, "new": 5.0})
    old_score = score(
        rel["old"], freshness(now - 30 * 86400, now), 0.5, 0.0
    )
    new_score = score(rel["new"], freshness(now, now), 0.5, 0.0)
    assert old_score > new_score


def test_recall_lines_sorts_by_score_not_recency() -> None:
    """旧但高度相关的消息要排在最新但弱相关的消息之前。"""
    now = int(time.time())
    old = make_msg("old", "显卡驱动坏了", timestamp=now - 30 * 86400)
    new = make_msg("new", "显卡驱 随口一提", timestamp=now)
    store = FakeStore([(new, "", 5.0), (old, "", 0.1)])

    lines = _recall_lines(as_store(store), GROUP, "100", ["显卡驱动怎么装"], 5, set())

    assert lines[0] == "鱼片: 显卡驱动坏了"
    assert lines[1] == "鱼片: 显卡驱 随口一提"


def test_recall_lines_like_fallback_no_rank_sorts_by_recency() -> None:
    """rank 全 None（LIKE 回退）不抛异常，按新的在前。"""
    old = make_msg("old", "显卡驱动坏了", timestamp=10)
    new = make_msg("new", "显卡驱动好了", timestamp=20)
    store = FakeStore([(old, "", None), (new, "", None)])

    lines = _recall_lines(as_store(store), GROUP, "100", ["显卡驱动怎么装"], 5, set())

    assert lines == ["鱼片: 显卡驱动好了", "鱼片: 显卡驱动坏了"]


def test_recall_lines_tied_scores_break_by_timestamp() -> None:
    """同分（都极旧、freshness 归零）时新的在前。"""
    a = make_msg("a", "内容甲", timestamp=10)
    b = make_msg("b", "内容乙", timestamp=20)
    store = FakeStore([(a, "", 1.0), (b, "", 1.0)])

    lines = _recall_lines(as_store(store), GROUP, "100", ["随便"], 5, set())

    assert lines == ["鱼片: 内容乙", "鱼片: 内容甲"]
