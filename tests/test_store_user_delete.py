from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store, UNATTRIBUTABLE_TABLES
from dafeiyu.models import ChatType, Episode, Message, OpenLoop, Participant


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
    store.upsert_impression(ChatType.GROUP, 100, "7", "fact", "用户7喜欢钓鱼", 0.9)
    store.upsert_impression(
        ChatType.GROUP, "100", "8", "fact", "用户8喜欢爬山", 0.9
    )

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
    # 可归因的派生记忆一并清除：用户7的印象消失，用户8的保留。
    assert store.count_impressions(ChatType.GROUP, 100) == before_impressions - 1
    assert store.count_impressions(ChatType.GROUP, 100) == 1
    # summaries 不可归因，永远不删，只在 unattributable 里报告。
    assert store.count_summaries(ChatType.GROUP, 100) == before_summaries
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


def _seed_derived(store: Store) -> None:
    """用户7/8 的消息 + 各可归因派生表 + 一条不可归因摘要。"""
    store.add_message(make_msg("u7-a", user_id=7, content="用户7的第一条"))
    store.add_message(make_msg("u8-a", user_id=8, content="用户8的消息"))
    store.mark_delivered(ChatType.GROUP, "100", "u7-a", "u8-a")
    store.upsert_impression(ChatType.GROUP, "100", "7", "fact", "用户7喜欢钓鱼", 0.9)
    store.upsert_impression(ChatType.GROUP, "100", "8", "fact", "用户8喜欢爬山", 0.9)
    store.add_open_loop(
        OpenLoop(
            chat_type=ChatType.GROUP,
            chat_id="100",
            kind="question",
            content="用户7问的结果出来了吗",
            subject_user_id="7",
        )
    )
    store.add_open_loop(
        OpenLoop(
            chat_type=ChatType.GROUP,
            chat_id="100",
            kind="plan",
            content="用户8周末去爬山",
            subject_user_id="8",
        )
    )
    store.save_episode(
        Episode(
            chat_type=ChatType.GROUP,
            chat_id="100",
            start_message_id="u7-a",
            last_message_id="u8-a",
            topic="钓鱼",
            summary="两人聊钓鱼",
            participants=(Participant("7", "用户7"), Participant("8", "用户8")),
        )
    )
    store.save_episode(
        Episode(
            chat_type=ChatType.GROUP,
            chat_id="100",
            start_message_id="u7-solo",
            last_message_id="u7-solo",
            topic="独白",
            summary="用户7自言自语",
            participants=(Participant("7", "用户7"),),
        )
    )
    store.record_decision(
        ChatType.GROUP, "100", outcome="replied", message_ids=["u7-a"]
    )
    store.record_decision(
        ChatType.GROUP, "100", outcome="held", message_ids=["u8-a"]
    )
    # shared_facts 没有写接口带 user_id（T17 才补），直接落库绕过隐私闸门：
    # 一条带 source_user_id，一条旧格式 ''（旧行不回填，应保留并报告）。
    store._conn.execute(
        """
        INSERT INTO shared_facts (
            question_key, question_text, answer_text,
            source_chat_type, source_chat_id, source_user_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("key7", "用户7的问题", "用户7的答案", "group", "100", "7"),
    )
    store._conn.execute(
        """
        INSERT INTO shared_facts (
            question_key, question_text, answer_text,
            source_chat_type, source_chat_id, source_user_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("key0", "老问题", "老答案", "group", "100", ""),
    )
    store._conn.commit()
    store.upsert_summary(ChatType.GROUP, "100", "2026-10-25", "昨天的摘要")


def test_delete_user_data_removes_only_that_users_derived_memory(
    store: Store,
) -> None:
    _seed_derived(store)
    before_summaries = store.count_summaries(ChatType.GROUP, "100")

    result = store.delete_user_data(ChatType.GROUP, "100", "7")

    assert result["deleted_messages"] == 1
    assert result["deleted_impressions"] == 1
    assert result["deleted_open_loops"] == 1
    assert result["updated_episodes"] == 1
    assert result["deleted_episodes"] == 1
    assert result["deleted_decisions"] == 1
    assert result["deleted_shared_facts"] == 1
    assert result["unattributable"] == list(UNATTRIBUTABLE_TABLES)

    # 用户8 的可归因记忆原样保留，用户8 的消息也不动。
    assert store.count_messages(ChatType.GROUP, "100") == 1
    subjects = {
        str(row["subject_user_id"])
        for row in store._conn.execute(
            "SELECT subject_user_id FROM impressions"
        ).fetchall()
    }
    assert subjects == {"8"}
    loop_subjects = {
        str(row["subject_user_id"])
        for row in store._conn.execute(
            "SELECT subject_user_id FROM open_loops"
        ).fetchall()
    }
    assert loop_subjects == {"8"}

    # 双人 episode 只剩用户8，且 JSON 合法；单人 episode 整行消失。
    episodes = store._conn.execute(
        "SELECT participants FROM episodes"
    ).fetchall()
    assert len(episodes) == 1
    people = json.loads(str(episodes[0]["participants"]))
    assert people == [{"user_id": "8", "nickname": "用户8"}]

    # decisions 按 message_ids 归因：含 u7-a 的删除，含 u8-a 的保留。
    remaining_mids = [
        json.loads(str(row["message_ids"]))
        for row in store._conn.execute("SELECT message_ids FROM decisions")
    ]
    assert remaining_mids == [["u8-a"]]

    # shared_facts：source_user_id='7' 的删除，'' 的保留。
    keys = {
        str(row["question_key"])
        for row in store._conn.execute("SELECT question_key FROM shared_facts")
    }
    assert keys == {"key0"}

    # summaries 不可归因，永远不删。
    assert store.count_summaries(ChatType.GROUP, "100") == before_summaries

    # 二次调用全 0。
    again = store.delete_user_data(ChatType.GROUP, "100", "7")
    for key in (
        "deleted_messages",
        "deleted_impressions",
        "deleted_open_loops",
        "updated_episodes",
        "deleted_episodes",
        "deleted_decisions",
        "deleted_shared_facts",
    ):
        assert again[key] == 0


def test_delete_user_data_without_derived_memory_is_all_zeros(store: Store) -> None:
    _seed(store)

    result = store.delete_user_data(ChatType.GROUP, "100", "7")

    assert result["deleted_messages"] == 3
    for key in (
        "deleted_impressions",
        "deleted_open_loops",
        "updated_episodes",
        "deleted_episodes",
        "deleted_decisions",
        "deleted_shared_facts",
    ):
        assert result[key] == 0


def test_delete_user_data_skips_corrupt_participants_json(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(store)
    store._conn.execute(
        """
        INSERT INTO episodes (
            chat_type, chat_id, start_message_id, last_message_id,
            topic, summary, participants
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("group", "100", "bad", "bad", "坏行", "损坏", "{not-json"),
    )
    store._conn.commit()

    with caplog.at_level(logging.WARNING):
        result = store.delete_user_data(ChatType.GROUP, "100", "7")

    # 坏 JSON 的 episode 行跳过不动，其余删除照常完成。
    assert result["updated_episodes"] == 0
    assert result["deleted_episodes"] == 0
    assert result["deleted_messages"] == 3
    row = store._conn.execute(
        "SELECT participants FROM episodes WHERE start_message_id = 'bad'"
    ).fetchone()
    assert str(row["participants"]) == "{not-json"
    assert "participants" in caplog.text


def test_delete_user_data_removes_facts_written_by_add_shared_fact(
    store: Store,
) -> None:
    """正常写入路径带上 source_user_id 后，用户级删除照样按人清掉。"""
    store.add_shared_fact(
        "用户7喜欢的城市是什么？",
        "用户7说最喜欢上海，还搬过去住了。",
        ChatType.GROUP,
        "100",
        "用户7",
        user_id="7",
    )
    store.add_shared_fact(
        "用户8的爱好是什么？",
        "用户8喜欢爬山，每年都去。",
        ChatType.GROUP,
        "100",
        "用户8",
        user_id="8",
    )

    result = store.delete_user_data(ChatType.GROUP, "100", "7")

    assert result["deleted_shared_facts"] == 1
    remaining = {str(row["source_user_id"]) for row in store.shared_facts()}
    assert remaining == {"8"}
