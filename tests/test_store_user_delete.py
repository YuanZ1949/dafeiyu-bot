from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message


def make_msg(
    message_id: str,
    *,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: int = 100,
    user_id: int = 7,
    is_bot: bool = False,
    content: str = "消息",
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname="大肥鱼" if is_bot else f"用户{user_id}",
        content=content,
        message_id=message_id,
        is_bot=is_bot,
        timestamp=1_700_000_000,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "test.db")
    s.init_schema()
    yield s
    s.close()


def _delivered_ids(store: Store) -> set[str]:
    rows = store._conn.execute("SELECT message_id FROM delivered_messages").fetchall()
    return {str(row["message_id"]) for row in rows}


def _fts_hits(store: Store, query: str) -> int:
    if not store._messages_fts:
        return 0
    row = store._conn.execute(
        "SELECT COUNT(*) AS n FROM messages_fts WHERE messages_fts MATCH ?",
        (f'"{query}"',),
    ).fetchone()
    return int(row["n"])


def _seed(store: Store) -> None:
    store.add_message(make_msg("u7-a", user_id=7, content="用户7的第一条"))
    store.add_message(make_msg("u7-b", user_id=7, content="用户7的第二条"))
    store.add_message(make_msg("u7-c", user_id=7, content="用户7的第三条"))
    store.add_message(make_msg("u8-a", user_id=8, content="用户8的消息"))
    store.add_message(make_msg("bot-a", user_id=999, is_bot=True, content="bot 回复"))
    # bot 产出不会进入投递表；只给人类消息插对应投递行。
    store.mark_delivered(ChatType.GROUP, 100, "u7-a", "u7-b", "u7-c", "u8-a")


def test_delete_user_messages_removes_only_that_users_rows(store: Store) -> None:
    _seed(store)
    store.upsert_summary(ChatType.GROUP, 100, "2026-10-25", "昨天的摘要")
    store.upsert_impression(ChatType.GROUP, 100, 7, "fact", "用户7喜欢钓鱼", 0.9)

    before_messages = store.count_messages(ChatType.GROUP, 100)
    before_summaries = store.count_summaries(ChatType.GROUP, 100)
    before_impressions = store.count_impressions(ChatType.GROUP, 100)
    assert before_messages == 5
    assert _delivered_ids(store) == {"u7-a", "u7-b", "u7-c", "u8-a"}
    hits, _ = store.search_message_hits("用户7的第一条", ChatType.GROUP, 100, 10)
    assert hits == 1
    assert _fts_hits(store, "用户7的第一条") == 1

    deleted = store.delete_user_messages(ChatType.GROUP, 100, 7)

    assert deleted == 3
    assert store.count_messages(ChatType.GROUP, 100) == before_messages - 3
    remaining = store.recent_messages(ChatType.GROUP, 100, 10)
    assert [message.message_id for message in remaining] == ["u8-a", "bot-a"]
    assert {message.user_id for message in remaining} == {"8", "999"}
    assert _delivered_ids(store) == {"u8-a"}
    # 聚合表保留：摘要与印象无法可靠归因，不在本操作范围内。
    assert store.count_summaries(ChatType.GROUP, 100) == before_summaries
    assert store.count_impressions(ChatType.GROUP, 100) == before_impressions
    # FTS 触发器必须同步清掉正文，否则隐私删除后仍能检索到原文。
    hits, rows = store.search_message_hits("用户7的第一条", ChatType.GROUP, 100, 10)
    assert hits == 0
    assert rows == []
    # 直接查索引本体，避免 JOIN messages 掩盖触发器缺失导致的孤儿索引行。
    assert _fts_hits(store, "用户7的第一条") == 0


def test_delete_user_messages_is_idempotent(store: Store) -> None:
    _seed(store)

    assert store.delete_user_messages(ChatType.GROUP, 100, 7) == 3
    assert store.delete_user_messages(ChatType.GROUP, 100, 7) == 0
    assert store.count_messages(ChatType.GROUP, 100) == 2
    assert _delivered_ids(store) == {"u8-a"}


def test_delete_user_messages_is_scoped_by_chat(store: Store) -> None:
    store.add_message(make_msg("g100", chat_type=ChatType.GROUP, chat_id=100, user_id=7))
    store.add_message(make_msg("g200", chat_type=ChatType.GROUP, chat_id=200, user_id=7))
    store.add_message(
        make_msg("p100", chat_type=ChatType.PRIVATE, chat_id=100, user_id=7)
    )

    assert store.delete_user_messages(ChatType.GROUP, 100, 7) == 1
    assert store.count_messages(ChatType.GROUP, 100) == 0
    assert store.count_messages(ChatType.GROUP, 200) == 1
    assert store.count_messages(ChatType.PRIVATE, 100) == 1
