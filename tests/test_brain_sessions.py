from pathlib import Path

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.init_schema()
    return store


def count(store: Store, table: str) -> int:
    return int(store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_sessions_are_isolated_by_provider(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.set_brain_session("opencode", ChatType.GROUP, 1, "oc-1")
    store.set_brain_session("token", ChatType.GROUP, 1, "tk-1")

    assert store.get_brain_session("opencode", ChatType.GROUP, 1) == "oc-1"
    assert store.get_brain_session("token", ChatType.GROUP, 1) == "tk-1"
    assert store.get_brain_session("local_model", ChatType.GROUP, 1) is None


def test_set_brain_session_overwrites(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.set_brain_session("opencode", ChatType.PRIVATE, 2, "a")
    store.set_brain_session("opencode", ChatType.PRIVATE, 2, "b")
    assert store.get_brain_session("opencode", ChatType.PRIVATE, 2) == "b"
    assert count(store, "brain_sessions") == 1


def test_legacy_api_maps_to_opencode_provider(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.set_opencode_session(ChatType.GROUP, 7, "legacy")
    assert store.get_brain_session("opencode", ChatType.GROUP, 7) == "legacy"
    store.clear_opencode_session(ChatType.GROUP, 7)
    assert store.get_brain_session("opencode", ChatType.GROUP, 7) is None


def test_migration_is_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store._conn.execute(
        "INSERT INTO opencode_sessions (chat_type, chat_id, session_id) VALUES (?, ?, ?)",
        ("group", 11, "old-session"),
    )
    store._conn.commit()

    store.migrate_opencode_sessions()
    store.migrate_opencode_sessions()

    assert count(store, "brain_sessions") == 1
    assert store.get_brain_session("opencode", ChatType.GROUP, 11) == "old-session"


def test_migration_does_not_overwrite_newer_row(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.set_brain_session("opencode", ChatType.GROUP, 12, "new")
    store._conn.execute(
        "INSERT INTO opencode_sessions (chat_type, chat_id, session_id) VALUES (?, ?, ?)",
        ("group", 12, "old"),
    )
    store._conn.commit()

    store.migrate_opencode_sessions()
    assert store.get_brain_session("opencode", ChatType.GROUP, 12) == "new"


def test_forget_chat_clears_both_tables(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.set_brain_session("opencode", ChatType.GROUP, 13, "oc")
    store._conn.execute(
        "INSERT INTO opencode_sessions (chat_type, chat_id, session_id) VALUES (?, ?, ?)",
        ("group", 13, "legacy"),
    )
    store.set_brain_session("opencode", ChatType.GROUP, 14, "keep")
    store._conn.commit()

    store.forget_chat(ChatType.GROUP, 13)

    assert count(store, "brain_sessions") == 1
    assert count(store, "opencode_sessions") == 0


def test_forget_all_clears_both_tables(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.set_brain_session("token", ChatType.PRIVATE, 15, "tk")
    store._conn.execute(
        "INSERT INTO opencode_sessions (chat_type, chat_id, session_id) VALUES (?, ?, ?)",
        ("private", 15, "legacy"),
    )
    store._conn.commit()

    store.forget_all()

    assert count(store, "brain_sessions") == 0
    assert count(store, "opencode_sessions") == 0


def test_forget_chat_clears_chat_state_bot_state_chats_and_decisions(
    tmp_path: Path,
) -> None:
    """T15：三张新表走同一份 CHAT_SCOPED_TABLES 清单，别的会话不受影响。"""
    store = make_store(tmp_path)
    store.save_chat_state(
        ChatType.GROUP, "21", {"muted_until": 9999.0, "no_reply_streak": 2}
    )
    store.set_bot_state_chat(ChatType.GROUP, "21", focus="鱼缸", mood_note="好奇")
    store.record_decision(
        ChatType.GROUP,
        "21",
        outcome="replied",
        read="r",
        think="t",
        reason="因为",
        reply_preview="好",
    )
    store.save_chat_state(ChatType.GROUP, "22", {"muted_until": 1234.0})
    store.set_bot_state_chat(ChatType.GROUP, "22", focus="别的")
    store.record_decision(ChatType.GROUP, "22", outcome="held", reason="别的群")

    store.forget_chat(ChatType.GROUP, "21")

    for table in ("chat_state", "bot_state_chats", "decisions"):
        gone = store._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE chat_type = ? AND chat_id = ?",
            (ChatType.GROUP.value, "21"),
        ).fetchone()[0]
        assert int(gone) == 0, f"{table} 该会话的行必须随 forget_chat 清空"
    # 另一会话的三张表都原样保留。
    other_state = store.load_chat_state(ChatType.GROUP, "22")
    assert other_state is not None and other_state["muted_until"] == 1234.0
    other_bot = store.get_bot_state_chat(ChatType.GROUP, "22")
    assert other_bot is not None and other_bot["focus"] == "别的"
    other_decisions = store.list_decisions(chat_type=ChatType.GROUP, chat_id="22")
    assert len(other_decisions) == 1
