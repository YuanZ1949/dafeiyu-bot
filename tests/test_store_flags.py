"""消息的 at_bot / reply_to_bot 必须落库。

批次执行靠这两个标志判断「这一批被点名了」（purpose=reply）以及在 prompt 里
区分「@了你」与「引用回复你」。以前只存在 Service 内存里，进程重启就丢。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

# 加列之前的老库结构（不含 at_bot / reply_to_bot）。
OLD_MESSAGES_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    content TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    timestamp INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chat_type, chat_id, message_id)
);
"""


def make_msg(
    message_id: str,
    *,
    at_bot: bool = False,
    reply_to_bot: bool = False,
    chat_id: int = 1,
    content: str = "hi",
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id=2,
        nickname="鱼片",
        content=content,
        message_id=message_id,
        at_bot=at_bot,
        reply_to_bot=reply_to_bot,
        timestamp=0,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "flags.db")
    s.init_schema()
    yield s
    s.close()


def test_message_flags_survive_roundtrip(store: Store) -> None:
    store.add_message(make_msg("m1", at_bot=True))
    store.add_message(make_msg("m2", reply_to_bot=True))
    store.add_message(make_msg("m3"))

    got = {m.message_id: m for m in store.recent_messages(ChatType.GROUP, 1, 10)}
    assert (got["m1"].at_bot, got["m1"].reply_to_bot) == (True, False)
    assert (got["m2"].at_bot, got["m2"].reply_to_bot) == (False, True)
    assert (got["m3"].at_bot, got["m3"].reply_to_bot) == (False, False)


def test_pending_batch_keeps_flags(store: Store) -> None:
    store.add_message(make_msg("m0"))
    store.mark_delivered(ChatType.GROUP, 1, "m0")
    store.add_message(make_msg("m1"))
    store.add_message(make_msg("m2", reply_to_bot=True))

    batch = store.pending_batch(ChatType.GROUP, 1, "m2")

    assert [m.message_id for m in batch] == ["m1", "m2"]
    assert batch[0].at_bot is False
    assert batch[1].reply_to_bot is True


def test_reply_target_survives_roundtrip(store: Store) -> None:
    msg = make_msg("m2")
    msg.reply_to_message_id = "m1"
    msg.reply_to_user_id = "qq:77"
    store.add_message(msg)

    got = store.recent_messages(ChatType.GROUP, 1, 10)[0]
    assert got.reply_to_message_id == "m1"
    assert got.reply_to_user_id == "qq:77"


def test_message_nicknames_batch_lookup(store: Store) -> None:
    store.add_message(make_msg("m1"))

    assert store.message_nicknames(ChatType.GROUP, 1, ["m1", "missing"]) == {"m1": "鱼片"}


def test_old_database_gains_reply_target_columns(tmp_path: Path) -> None:
    path = tmp_path / "old-reply.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_MESSAGES_SCHEMA)
    conn.execute(
        "INSERT INTO messages (chat_type, chat_id, message_id, user_id, nickname, content)"
        " VALUES (?, 1, 'old', 2, '鱼片', '老消息')",
        (ChatType.GROUP.value,),
    )
    conn.commit()
    conn.close()

    s = Store(path)
    s.init_schema()
    try:
        columns = {
            row["name"] for row in s._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert {"reply_to_message_id", "reply_to_user_id"} <= columns

        old = s.recent_messages(ChatType.GROUP, 1, 10)[0]
        assert old.reply_to_message_id is None
        assert old.reply_to_user_id is None
    finally:
        s.close()


def test_old_database_gains_flag_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_MESSAGES_SCHEMA)
    conn.execute(
        "INSERT INTO messages (chat_type, chat_id, message_id, user_id, nickname, content)"
        " VALUES (?, 1, 'old', 2, '鱼片', '老消息')",
        (ChatType.GROUP.value,),
    )
    conn.commit()
    conn.close()

    s = Store(path)
    s.init_schema()
    try:
        old = s.recent_messages(ChatType.GROUP, 1, 10)
        assert (old[0].at_bot, old[0].reply_to_bot) == (False, False)

        s.add_message(make_msg("new", at_bot=True))
        fresh = {m.message_id: m for m in s.recent_messages(ChatType.GROUP, 1, 10)}
        assert fresh["new"].at_bot is True
    finally:
        s.close()
