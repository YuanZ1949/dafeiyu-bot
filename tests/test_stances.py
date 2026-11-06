"""跨群立场（P3）：store 读写、Reflector 解析/写入、注入。"""

from __future__ import annotations

import json
from typing import Any

from dafeiyu.config import InjectionConfig
from dafeiyu.memory.context import build_injection
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType
from dafeiyu.workers.reflector import parse_reflection


def make_store(tmp_path) -> Store:
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    return store


def test_upsert_then_list_by_confidence(tmp_path) -> None:
    store = make_store(tmp_path)
    store.upsert_stance("熬夜", "熬夜不好，但偶尔可以", confidence=0.4)
    store.upsert_stance("香菜", "香菜是灵魂", confidence=0.9)

    stances = store.list_stances(10)

    assert [item.topic for item in stances] == ["香菜", "熬夜"]
    assert stances[0].stance == "香菜是灵魂"
    assert stances[0].confidence == 0.9


def test_same_topic_overwrites_instead_of_piling_up(tmp_path) -> None:
    store = make_store(tmp_path)
    store.upsert_stance("香菜", "还行", confidence=0.3)
    store.upsert_stance("香菜", "其实很好吃", confidence=0.8)

    stances = store.list_stances(10)

    assert len(stances) == 1
    assert stances[0].stance == "其实很好吃"
    assert stances[0].confidence == 0.8


def test_empty_and_out_of_range_values_are_normalised(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.upsert_stance("  ", "有观点") is None
    assert store.upsert_stance("话题", "   ") is None

    store.upsert_stance("话题", "观点", confidence=3.5)
    bad: Any = "bad"
    store.upsert_stance("另一个话题", "另一个观点", confidence=bad)

    stances = {item.topic: item for item in store.list_stances(10)}
    assert stances["话题"].confidence == 1.0
    assert stances["另一个话题"].confidence == 0.5


def test_parse_reflection_keeps_at_most_two_valid_stances() -> None:
    raw = json.dumps(
        {
            "focus": "",
            "mood_note": "",
            "stances": [
                {"topic": "香菜", "stance": "好吃", "confidence": 0.9},
                {"topic": "", "stance": "缺话题"},
                {"topic": "熬夜", "stance": "不好", "confidence": "0.2"},
                {"topic": "第三个", "stance": "不该收"},
            ],
        }
    )

    parsed = parse_reflection(raw)

    assert parsed is not None
    assert [item["topic"] for item in parsed["stances"]] == ["香菜", "熬夜"]
    assert parsed["stances"][1]["confidence"] == 0.2


def test_parse_reflection_without_stances_is_still_valid() -> None:
    parsed = parse_reflection('{"focus":"在意","mood_note":"平静"}')

    assert parsed is not None
    assert parsed["stances"] == []


def test_injection_contains_stance_block(tmp_path) -> None:
    store = make_store(tmp_path)
    store.upsert_stance("香菜", "香菜是灵魂", confidence=0.9)
    store.upsert_stance("熬夜", "尽量不熬", confidence=0.6)

    injection = build_injection(
        store,
        None,
        ChatType.GROUP,
        "qq:1",
        InjectionConfig(
            inject_stances=True,
            inject_impressions=False,
            stances_topk=2,
            stances_chars=200,
        ),
        today="2026-11-07",
    )

    assert "【我的立场】" in injection
    assert "香菜：香菜是灵魂" in injection
    assert "熬夜：尽量不熬" in injection


def test_injection_skips_stances_when_disabled(tmp_path) -> None:
    store = make_store(tmp_path)
    store.upsert_stance("香菜", "香菜是灵魂", confidence=0.9)

    injection = build_injection(
        store,
        None,
        ChatType.GROUP,
        "qq:1",
        InjectionConfig(inject_stances=False, inject_impressions=False),
        today="2026-11-07",
    )

    assert injection == ""


def test_stance_block_respects_char_budget(tmp_path) -> None:
    store = make_store(tmp_path)
    store.upsert_stance("很长的话题", "观点" * 40, confidence=0.9)

    injection = build_injection(
        store,
        None,
        ChatType.GROUP,
        "qq:1",
        InjectionConfig(
            inject_stances=True,
            inject_impressions=False,
            stances_chars=30,
        ),
        today="2026-11-07",
    )

    assert "【我的立场】" in injection
    body = injection.split("【我的立场】", 1)[1].strip().splitlines()[-1]
    assert len(body) <= 30


def test_worker_writes_stances(tmp_path) -> None:
    from dafeiyu.config import MindConfig
    from dafeiyu.workers.reflector import ReflectorWorker

    store = make_store(tmp_path)
    worker = ReflectorWorker(store, brain=object(), mind=MindConfig())

    worker._write_stances(
        [{"topic": "香菜", "stance": "好吃", "confidence": 0.7}]
    )

    stances = store.list_stances(5)
    assert len(stances) == 1
    assert stances[0].topic == "香菜"
    assert stances[0].confidence == 0.7


def test_forget_all_clears_stances(tmp_path) -> None:
    store = make_store(tmp_path)
    store.upsert_stance("香菜", "好吃", confidence=0.7)

    store.forget_all()

    assert store.list_stances(5) == []
