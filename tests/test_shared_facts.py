"""跨群查证缓存：隐私闸门、相似度匹配与落库行为。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.memory import shared_facts
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "facts.db")
    s.init_schema()
    yield s
    s.close()


def test_normalize_strips_punctuation_and_width() -> None:
    assert shared_facts.normalize("云朵 哒哒哒，是什么梗？") == "云朵哒哒哒是什么梗"
    assert shared_facts.normalize("ＡＢＣ！") == "abc"


def test_similarity_matches_reworded_question() -> None:
    score = shared_facts.similarity(
        "云朵哒哒哒是什么梗", "哎？云朵？哒哒哒哒哒是什么梗"
    )
    assert score >= shared_facts.MATCH_THRESHOLD


def test_similarity_rejects_unrelated() -> None:
    assert shared_facts.similarity("云朵哒哒哒是什么梗", "今天天气怎么样") == 0.0


@pytest.mark.parametrize(
    "text",
    [
        "我手机号 13812345678 你记一下",
        "身份证 110101199003071234",
        "银行卡 6222021234567890123",
        "邮箱 fish@example.com",
        "我家住址是锦绣壹号9栋1101",
        "登录密码是 hunter2",
        "验证码 889900",
    ],
)
def test_private_text_detected(text: str) -> None:
    assert shared_facts.is_private(text) is True


def test_private_text_allows_ordinary_question() -> None:
    assert shared_facts.is_private("原神里云朵哒哒哒是什么梗") is False


def test_extract_drops_failure_answer() -> None:
    assert shared_facts.extract("云朵哒哒哒是什么梗", "本鱼没查到，不清楚") is None


def test_extract_drops_private_pair() -> None:
    assert shared_facts.extract("我手机号多少", "13812345678 对吧") is None


def test_extract_accepts_normal_pair() -> None:
    pair = shared_facts.extract(
        "云朵哒哒哒是什么梗",
        "出自《原神》2023 年底「云·原神」的宣传曲，2026 年被翻出来做成鬼畜。",
    )
    assert pair is not None
    assert pair[0].startswith("云朵")


def test_store_skips_private_chat(store: Store) -> None:
    assert (
        store.add_shared_fact(
            "云朵哒哒哒是什么梗",
            "出自《原神》的宣传曲动画短片。",
            ChatType.PRIVATE,
            "qq:1",
            user_id="wx:2002",
        )
        == 0
    )
    assert store.shared_facts() == []


def test_store_dedupes_by_normalized_question(store: Store) -> None:
    first = store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》的宣传曲。",
        ChatType.GROUP,
        "wxg:aaa",
    )
    second = store.add_shared_fact(
        "云朵 哒哒哒 是什么梗？",
        "出自《原神》2023 年底「云·原神」的宣传曲动画短片，2026 年又被网友翻出来做成鬼畜。",
        ChatType.GROUP,
        "wxg:bbb",
    )
    assert first > 0
    assert second == first
    rows = store.shared_facts()
    assert len(rows) == 1
    # 更长的答案覆盖短答案，来源跟着更新。
    assert "2026" in rows[0]["answer_text"]
    assert rows[0]["source_chat_id"] == "wxg:bbb"


def test_store_keeps_longer_answer(store: Store) -> None:
    store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》2023 年底「云·原神」的宣传曲动画短片。",
        ChatType.GROUP,
        "wxg:aaa",
    )
    store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "原神宣传曲。",
        ChatType.GROUP,
        "wxg:bbb",
    )
    rows = store.shared_facts()
    assert rows[0]["source_chat_id"] == "wxg:aaa"


def test_pick_best_across_groups(store: Store) -> None:
    store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》2023 年底「云·原神」的宣传曲动画短片。",
        ChatType.GROUP,
        "wxg:aaa",
    )
    store.add_shared_fact(
        "Python 里 list 和 tuple 啥区别",
        "list 能改，tuple 不能改，tuple 还能当 dict 的 key。",
        ChatType.GROUP,
        "wxg:bbb",
    )
    match = shared_facts.pick_best(store.shared_facts(), "诶云朵哒哒哒哒哒是什么梗")
    assert match is not None
    assert match.source_chat_id == "wxg:aaa"
    assert "原神" in match.answer_text


def test_pick_best_ignores_private_rows() -> None:
    rows = [
        {
            "id": 1,
            "question_text": "我的手机号 13812345678 是多少",
            "answer_text": "13812345678",
            "source_chat_id": "wxg:aaa",
            "created_at": "2026-11-03",
        }
    ]
    assert shared_facts.pick_best(rows, "我的手机号 13812345678 是多少") is None


def test_forget_chat_purges_sourced_facts(store: Store) -> None:
    store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》的宣传曲动画短片。",
        ChatType.GROUP,
        "wxg:aaa",
    )
    store.add_shared_fact(
        "Python 里 list 和 tuple 啥区别",
        "list 能改，tuple 不能改。",
        ChatType.GROUP,
        "wxg:bbb",
    )
    assert store.purge_shared_facts_for_chat(ChatType.GROUP, "wxg:aaa") == 1
    assert [row["source_chat_id"] for row in store.shared_facts()] == ["wxg:bbb"]


def test_store_writes_source_user_id_when_given(store: Store) -> None:
    fact_id = store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》的宣传曲动画短片。",
        ChatType.GROUP,
        "wxg:aaa",
        "提问者",
        user_id="wx:1001",
    )
    assert fact_id > 0
    row = store.shared_facts()[0]
    assert row["source_user_id"] == "wx:1001"
    assert row["source_nickname"] == "提问者"


def test_store_legacy_call_leaves_source_user_id_empty(store: Store) -> None:
    # 旧的 5 个位置参数调用必须继续工作；没有 user_id 就如实落 ''。
    fact_id = store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》的宣传曲动画短片。",
        ChatType.GROUP,
        "wxg:aaa",
        "提问者",
    )
    assert fact_id > 0
    assert store.shared_facts()[0]["source_user_id"] == ""


def test_store_updates_source_user_id_on_longer_answer(store: Store) -> None:
    store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》的宣传曲动画短片。",
        ChatType.GROUP,
        "wxg:aaa",
    )
    # 旧调用留下 '' 的行：更长答案（带 user_id）刷新时要把来源用户洗出来。
    second = store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》2023 年的宣传曲《哒哒哒》动画短片，B 站播放量极高。",
        ChatType.GROUP,
        "wxg:bbb",
        "另一个人",
        user_id="wx:1002",
    )
    assert second > 0
    rows = store.shared_facts()
    assert len(rows) == 1
    assert rows[0]["source_user_id"] == "wx:1002"
    assert rows[0]["source_nickname"] == "另一个人"


def test_store_updates_keep_old_user_when_new_call_has_none(store: Store) -> None:
    store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》的宣传曲动画短片。",
        ChatType.GROUP,
        "wxg:aaa",
        "提问者",
        user_id="wx:1001",
    )
    # 更长的答案来自旧式（无 user_id）调用：不能把已知来源洗成 ''。
    store.add_shared_fact(
        "云朵哒哒哒是什么梗",
        "出自《原神》2023 年的宣传曲《哒哒哒》动画短片，B 站播放量极高。",
        ChatType.GROUP,
        "wxg:bbb",
        "另一个人",
    )
    rows = store.shared_facts()
    assert len(rows) == 1
    assert rows[0]["source_user_id"] == "wx:1001"
