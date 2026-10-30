from __future__ import annotations

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType


def make_store(tmp_path) -> Store:
    store = Store(str(tmp_path / "chat-state.db"))
    store.init_schema()
    return store


def test_chat_state_roundtrip(tmp_path) -> None:
    store = make_store(tmp_path)
    store.save_chat_state(
        ChatType.GROUP,
        1001,
        {
            "last_bot_spoke": 1000.5,
            "last_user_message": 1001.5,
            "muted_until": 2000.0,
            "ambient_streak": 2,
            "no_reply_streak": 3,
            "downshift_until": 2500.0,
            "last_prompt": "今天吃什么",
            "last_prompt_at": 1003.5,
            "thread_closed_until": 3000.0,
            "last_closed_at": 1002.5,
        },
    )

    row = store.load_chat_state(ChatType.GROUP, 1001)
    assert row is not None
    assert row["last_bot_spoke"] == 1000.5
    assert row["last_user_message"] == 1001.5
    assert row["muted_until"] == 2000.0
    assert row["ambient_streak"] == 2
    assert row["no_reply_streak"] == 3
    assert row["downshift_until"] == 2500.0
    assert row["last_prompt"] == "今天吃什么"
    assert row["last_prompt_at"] == 1003.5
    assert row["thread_closed_until"] == 3000.0
    assert row["last_closed_at"] == 1002.5


def test_chat_state_missing_returns_none(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.load_chat_state(ChatType.GROUP, 1001) is None


def test_chat_state_overwrites(tmp_path) -> None:
    store = make_store(tmp_path)
    store.save_chat_state(ChatType.GROUP, 1001, {"ambient_streak": 1})
    store.save_chat_state(
        ChatType.GROUP, 1001, {"ambient_streak": 0, "thread_closed_until": None}
    )

    row = store.load_chat_state(ChatType.GROUP, 1001)
    assert row is not None
    assert row["ambient_streak"] == 0
    assert row["thread_closed_until"] is None
