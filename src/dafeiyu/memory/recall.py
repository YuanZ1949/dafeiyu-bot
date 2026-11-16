"""召回打分核心：四因子纯函数（相关性/新鲜度/重要度/关系权重）。

spec §5.4：``score = 0.45*相关性 + 0.25*新鲜度 + 0.20*重要度 + 0.10*关系权重``。
只做纯计算，不查库、不依赖 FTS；FTS5 bm25「越小越相关」的方向约定由
normalize_relevance 统一翻转，调用方不要自行取反。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

# 相关性, 新鲜度, 重要度, 关系权重；和固定为 1.0。
WEIGHTS = (0.45, 0.25, 0.20, 0.10)

_SECONDS_PER_DAY = 86400.0

K = TypeVar("K")


def freshness(ts: float, now_ts: float, half_life_days: float = 7.0) -> float:
    """指数衰减，7 天半衰期（spec §5.4）。未来时间戳（时钟偏差）按 1.0 处理。"""
    age_days = max(0.0, (now_ts - ts) / _SECONDS_PER_DAY)
    return 0.5 ** (age_days / half_life_days)


def normalize_relevance(ranks: Mapping[K, float | None]) -> dict[K, float]:
    """对候选集做 min-max 归一化，输出 [0,1] 的相关性。

    FTS5 bm25 越小越相关，因此 ``relevance = (max_rank - rank) / (max_rank -
    min_rank)``。rank 为 None（LIKE 回退候选）按 0.5；全体相等或全 None 时
    统一 0.5，不给任何候选制造虚假满分。
    """
    present = [value for value in ranks.values() if value is not None]
    if not present:
        return {key: 0.5 for key in ranks}
    lo = min(present)
    hi = max(present)
    span = hi - lo
    if span <= 0.0:
        return {key: 0.5 for key in ranks}
    return {
        key: 0.5 if rank is None else (hi - rank) / span
        for key, rank in ranks.items()
    }


def score(
    relevance: float, freshness: float, importance: float, relationship: float
) -> float:
    """四因子加权求和；权重和固定 1.0，各项 clamp 到 [0,1]。"""
    clamped = (
        min(1.0, max(0.0, float(value)))
        for value in (relevance, freshness, importance, relationship)
    )
    return sum(w * v for w, v in zip(WEIGHTS, clamped))
