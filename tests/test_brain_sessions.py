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
