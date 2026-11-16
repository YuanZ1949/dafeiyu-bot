"""记忆衰减（T10）：印象 TTL/低置信清理、立场衰减/过期、立场 topic 40 字。

口径钉死：
* 印象：``last_seen_at`` 超过 TTL 删；低置信且超过宽限期删；边界各留一天；
* ``list_impressions`` 与 ``relationship_cards`` 共用同一套过滤，同一条过期
  印象两边都不可见；无过期行时关系卡输出与改动前一致；
* 立场：decay 只乘 ``confidence``、绝不动 ``updated_at``（那是过期判据）；
  prune 按 ``updated_at`` 超期或置信过低删；
* ``ttl_days=0`` 按「立即过期」解释且不抛异常（config 层负责拒绝 0）；
* Reflector 输出的立场 topic 上限 40 字，store 的 60 字硬截断仍是外层保险。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from dafeiyu.config import MindConfig
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType
from dafeiyu.workers.reflector import MAX_STANCE_TOPIC_CHARS, ReflectorWorker, parse_reflection

GROUP = ChatType.GROUP
CHAT = "qq:1"
# 整数秒时刻（2023-11-14 22:13:20 UTC），边界天数直接推算，无小数歧义。
NOW = 1_700_000_000
DAY = 86400


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "decay.db")
    s.init_schema()
    yield s
    s.close()


def dt_text(ts: float) -> str:
    """datetime('now') 口径（UTC 秒精度）文本，直接回写 last_seen_at 用。"""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def add_impression(
    store: Store,
    content: str,
    *,
    confidence: float = 0.5,
    last_seen_ts: float | None = None,
    subject: str = "qq:2",
) -> None:
    store.upsert_impression(GROUP, CHAT, subject, "fact", content, confidence)
    if last_seen_ts is not None:
        store._conn.execute(
            "UPDATE impressions SET last_seen_at = ? WHERE content = ?",
            (dt_text(last_seen_ts), content),
        )
        store._conn.commit()


def add_stance(
    store: Store,
    topic: str,
    *,
    confidence: float,
    updated_at_ts: float | None = None,
) -> None:
    store.upsert_stance(topic, "观点", confidence=confidence)
    if updated_at_ts is not None:
        store._conn.execute(
            "UPDATE bot_stances SET updated_at = ? WHERE topic = ?",
            (dt_text(updated_at_ts), topic),
        )
        store._conn.commit()


# ---- prune_impressions ------------------------------------------------------


def test_prune_impressions_ttl_boundary(store: Store) -> None:
    add_impression(store, "过期印象", confidence=0.9, last_seen_ts=NOW - 46 * DAY)
    add_impression(store, "新鲜印象", confidence=0.9, last_seen_ts=NOW - 44 * DAY)

    assert store.prune_impressions(now_ts=NOW) == 1

    left = {
        im.content for im in store.list_impressions(GROUP, CHAT, topk=10, now_ts=NOW)
    }
    assert left == {"新鲜印象"}


def test_prune_impressions_low_confidence_grace(store: Store) -> None:
    add_impression(store, "低置信过期", confidence=0.2, last_seen_ts=NOW - 8 * DAY)
    add_impression(store, "低置信宽限", confidence=0.2, last_seen_ts=NOW - 6 * DAY)
    # 高置信的旧行只受 TTL 管，不吃低置信宽限分支。
    add_impression(store, "高置信不删", confidence=0.9, last_seen_ts=NOW - 8 * DAY)

    assert store.prune_impressions(now_ts=NOW) == 1

    left = {
        im.content for im in store.list_impressions(GROUP, CHAT, topk=10, now_ts=NOW)
    }
    assert left == {"低置信宽限", "高置信不删"}


def test_prune_impressions_empty_table_returns_zero(store: Store) -> None:
    assert store.prune_impressions(now_ts=NOW) == 0
    assert store.decay_stances() == 0
    assert store.prune_stances(now_ts=NOW) == 0


def test_prune_impressions_ttl_zero_means_immediately_expired(store: Store) -> None:
    """失败口径二选一锁死：store 层 ttl_days=0 = 立即过期，不抛异常。"""
    add_impression(store, "任意印象", confidence=0.9, last_seen_ts=NOW - 3600)

    assert store.prune_impressions(now_ts=NOW, ttl_days=0) == 1
    assert store.count_impressions(GROUP, CHAT) == 0


# ---- 注入与关系卡共用过滤 ----------------------------------------------------


def test_stale_impression_hidden_in_list_and_cards(store: Store) -> None:
    add_impression(
        store, "过期事实", confidence=0.9, last_seen_ts=NOW - 46 * DAY, subject="qq:3"
    )
    add_impression(
        store, "新鲜事实", confidence=0.5, last_seen_ts=NOW - DAY, subject="qq:3"
    )

    impressions = store.list_impressions(GROUP, CHAT, topk=10, now_ts=NOW)
    assert [im.content for im in impressions] == ["新鲜事实"]

    card = store.relationship_cards(GROUP, CHAT, ["qq:3"], now_ts=NOW)["qq:3"]
    assert card.facts == ("新鲜事实",)


def test_relationship_cards_unchanged_when_nothing_stale(store: Store) -> None:
    add_impression(
        store, "事实一", confidence=0.8, last_seen_ts=NOW - DAY, subject="qq:4"
    )
    add_impression(
        store, "事实二", confidence=0.6, last_seen_ts=NOW - 2 * DAY, subject="qq:4"
    )

    card = store.relationship_cards(GROUP, CHAT, ["qq:4"], now_ts=NOW)["qq:4"]

    assert card.facts == ("事实一", "事实二")


# ---- 立场衰减与过期 ----------------------------------------------------------


def test_decay_stances_multiples_confidence_without_touching_updated_at(
    store: Store,
) -> None:
    seen_at = NOW - 5 * DAY
    add_stance(store, "香菜", confidence=1.0, updated_at_ts=seen_at)

    assert store.decay_stances(factor=0.98) == 1

    row = store._conn.execute(
        "SELECT confidence, updated_at FROM bot_stances WHERE topic = '香菜'"
    ).fetchone()
    assert row["confidence"] == pytest.approx(0.98)
    # 衰减绝不碰 updated_at：它是 prune_stances 的过期判据。
    assert row["updated_at"] == dt_text(seen_at)


def test_decay_stances_floors_at_zero(store: Store) -> None:
    add_stance(store, "快没了", confidence=0.05)

    store.decay_stances(factor=0.98)

    row = store._conn.execute("SELECT confidence FROM bot_stances").fetchone()
    assert row["confidence"] == pytest.approx(0.049)


def test_prune_stances_ttl_and_min_confidence(store: Store) -> None:
    add_stance(store, "过期", confidence=0.9, updated_at_ts=NOW - 31 * DAY)
    # 低置信没有宽限期：立即删。
    add_stance(store, "没把握", confidence=0.1, updated_at_ts=NOW)
    add_stance(store, "还在", confidence=0.5, updated_at_ts=NOW - 20 * DAY)

    assert store.prune_stances(now_ts=NOW) == 2

    assert {s.topic for s in store.list_stances(10)} == {"还在"}


# ---- 立场 topic 上限 ---------------------------------------------------------


def test_reflector_topic_capped_at_forty_chars(store: Store) -> None:
    """Reflector 的 50 字 topic 落库后被裁到 40（store 的 60 字截断在外层兜底）。

    走真实管线：模型输出 → parse_reflection（_stances 按 40 裁）→ _write_stances。
    """
    worker = ReflectorWorker(store, brain=object(), mind=MindConfig())
    raw = json.dumps(
        {
            "focus": "",
            "mood_note": "",
            "stances": [{"topic": "香" * 50, "stance": "好吃", "confidence": 0.7}],
        },
        ensure_ascii=False,
    )

    parsed = parse_reflection(raw)
    assert parsed is not None
    worker._write_stances(parsed["stances"])

    stances = store.list_stances(5)
    assert stances[0].topic == "香" * MAX_STANCE_TOPIC_CHARS
    assert len(stances[0].topic) == 40


def test_upsert_stance_outer_guard_still_sixty(store: Store) -> None:
    """60 字硬截断不缩短：已有 41–60 字的行不该被 Rewrite 截掉。"""
    store.upsert_stance("鱼" * 50, "观点")

    assert store.list_stances(5)[0].topic == "鱼" * 50
