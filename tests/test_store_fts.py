from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

GROUP = ChatType.GROUP


def make_msg(
    message_id: str,
    content: str,
    *,
    timestamp: int = 0,
    chat_type: ChatType = GROUP,
    chat_id: int = 100,
    user_id: int = 7,
    nickname: str = "鱼片",
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=message_id,
        timestamp=timestamp,
    )


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "fts.db")
    s.init_schema()
    yield s
    s.close()


def test_trigram_search_matches_three_plus_chinese_and_returns_snippet(
    store: Store,
) -> None:
    store.add_message(make_msg("m1", "显卡驱动坏了", timestamp=1))
    store.add_message(make_msg("m2", "今天吃什么", timestamp=2))
    long_text = "闲聊" * 20 + "显卡驱动" + "收尾" * 20
    store.add_message(make_msg("m3", long_text, timestamp=3))

    total, hits = store.search_message_hits("显卡驱动", GROUP, 100)

    assert total == 2
    assert {message.message_id for message, _ in hits} == {"m1", "m3"}
    snippets = {message.message_id: snippet for message, snippet in hits}
    assert snippets["m1"] == "显卡驱动坏了"
    assert "显卡驱动" in snippets["m3"]
    assert snippets["m3"].startswith("…")
    assert snippets["m3"].endswith("…")
    assert len(snippets["m3"]) < len(long_text)

    total, messages = store.search_messages("显卡驱动", GROUP, 100)
    assert total == 2
    assert {message.message_id for message in messages} == {"m1", "m3"}


def test_two_char_query_falls_back_to_like_and_pages_by_id(store: Store) -> None:
    store.add_message(make_msg("m1", "显卡驱动坏了"))
    store.add_message(make_msg("m2", "显卡驱动怎么装"))

    total, hits = store.search_message_hits("显卡", GROUP, 100)

    assert total == 2
    assert [message.message_id for message, _ in hits] == ["m2", "m1"]

    total, hits = store.search_message_hits("显卡", GROUP, 100, limit=1, offset=1)
    assert total == 2
    assert [message.message_id for message, _ in hits] == ["m1"]


def test_term_search_matches_any_term_in_one_query(store: Store) -> None:
    store.add_message(make_msg("m1", "显卡驱动坏了"))
    store.add_message(make_msg("m2", "今天吃什么"))
    terms = ["我想问", "问一下", "于显卡", "显卡驱", "驱动怎"]

    total, hits = store.search_message_terms(terms, GROUP, 100)

    assert total == 1
    assert [message.message_id for message, _ in hits] == ["m1"]
    assert "显卡驱" in hits[0][1]


def test_like_fallback_escapes_wildcards(store: Store) -> None:
    store.add_message(make_msg("m1", "100% 装机"))
    store.add_message(make_msg("m2", "1000 装机"))
    store.add_message(make_msg("m3", "100_装机"))

    total, hits = store.search_message_hits("100%", GROUP, 100)
    assert total == 1
    assert [message.message_id for message, _ in hits] == ["m1"]

    total, hits = store.search_message_hits("100_", GROUP, 100)
    assert total == 1
    assert [message.message_id for message, _ in hits] == ["m3"]


def test_fts_index_syncs_on_update_and_prune(store: Store) -> None:
    store.add_message(make_msg("m1", "显卡驱动坏了", timestamp=10))
    store.add_message(make_msg("m2", "显卡驱动怎么装", timestamp=20))

    assert store.search_message_hits("显卡驱动", GROUP, 100)[0] == 2

    store._conn.execute(
        "UPDATE messages SET content = ? WHERE message_id = ?",
        ("声卡没有声音", "m1"),
    )
    store._conn.commit()

    assert store.search_message_hits("声卡", GROUP, 100)[0] == 1
    total, hits = store.search_message_hits("显卡驱动", GROUP, 100)
    assert total == 1
    assert [message.message_id for message, _ in hits] == ["m2"]

    removed = store.prune_messages(15)
    assert removed == 1
    assert store.search_message_hits("声卡", GROUP, 100)[0] == 0
    assert store.search_message_hits("显卡驱动", GROUP, 100)[0] == 1


def test_legacy_database_without_message_index_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    store = Store(path)
    store.init_schema()
    store.add_message(make_msg("m1", "显卡驱动坏了"))
    store.close()

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS messages_ai;
        DROP TRIGGER IF EXISTS messages_ad;
        DROP TRIGGER IF EXISTS messages_au;
        DROP TABLE IF EXISTS messages_fts;
        """
    )
    conn.commit()
    conn.close()

    rebuilt = Store(path)
    rebuilt.init_schema()
    try:
        if not rebuilt._messages_fts:
            pytest.skip("当前 SQLite 不支持 trigram tokenizer")
        total, hits = rebuilt.search_message_hits("显卡驱动", GROUP, 100)
        assert total == 1
        assert [message.message_id for message, _ in hits] == ["m1"]
        indexed = rebuilt._conn.execute(
            "SELECT COUNT(*) AS n FROM messages_fts"
        ).fetchone()
        assert int(indexed["n"]) == 1
    finally:
        rebuilt.close()
