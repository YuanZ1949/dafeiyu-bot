"""发送回执校验：本地库回读「自己刚发的那条」，把 typed 升级成 observed。

协议 §7.6：未确认**不能**变成失败——客户端会换新 id 重发，而消息可能已经
发出去了。本文件同时覆盖 repository 的 ``find_self_message`` 与
``LocalDbSource`` 的解析/回读包装。

全部用临时目录自造的明文 SQLite，不碰真实微信库、不真的 sleep。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dafeiyu.gateway.wechat_bridge.localdb.repository import (
    ChatRef,
    LocalMessageStore,
    message_table_name,
)
from dafeiyu.gateway.wechat_bridge.localdb.source import LocalDbSource
from dafeiyu.gateway.wechat_bridge.receipt import UNVERIFIED, ReceiptVerifier

SELF = "wxid_self"
SELF_NAME = "小菜鱼"
OTHER = "wxid_other"
GROUP = "38689267237@chatroom"
GROUP_NAME = "锦绣壹号9#1101"

CONTACT_DDL = """
CREATE TABLE contact(
    id INTEGER PRIMARY KEY, username TEXT, local_type INTEGER, alias TEXT,
    encrypt_username TEXT, flag INTEGER, delete_flag INTEGER, verify_flag INTEGER,
    remark TEXT, remark_quan_pin TEXT, remark_pin_yin_initial TEXT, nick_name TEXT,
    pin_yin_initial TEXT, quan_pin TEXT, big_head_url TEXT, small_head_url TEXT,
    head_img_md5 TEXT, chat_room_notify INTEGER, is_in_chat_room INTEGER,
    description TEXT, extra_buffer BLOB, chat_room_type INTEGER
)
"""
STRANGER_DDL = CONTACT_DDL.replace("CREATE TABLE contact(", "CREATE TABLE stranger(", 1)

MESSAGE_DDL = """
CREATE TABLE "{table}"(
    local_id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER,
    local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER,
    create_time INTEGER, status INTEGER, upload_status INTEGER,
    download_status INTEGER, server_seq INTEGER, origin_source INTEGER,
    source TEXT, message_content TEXT, compress_content TEXT,
    packed_info_data BLOB, WCDB_CT_message_content INTEGER, WCDB_CT_source INTEGER
)
"""


class FakeMirrors:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, rel: str) -> Path:
        return self.root / rel


def _insert_contact(path: Path, username: str, nick_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(CONTACT_DDL)
        con.execute(STRANGER_DDL)
        con.execute(
            "INSERT INTO contact(username, nick_name) VALUES (?, ?)",
            (username, nick_name),
        )


def _insert_messages(
    path: Path, rows: list[tuple[int, int, str]]
) -> None:
    """``rows``: ``(sender_id, create_time, text)``；1=群, 2=自己, 3=别人。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = message_table_name(GROUP)
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)"
        )
        for rowid, username in [(1, GROUP), (2, SELF), (3, OTHER)]:
            con.execute(
                "INSERT INTO Name2Id(rowid, user_name, is_session) VALUES (?, ?, 0)",
                (rowid, username),
            )
        con.execute(MESSAGE_DDL.format(table=table))
        for sender, created, text in rows:
            con.execute(
                f'INSERT INTO "{table}" '
                "(local_type, real_sender_id, create_time, message_content, "
                "WCDB_CT_message_content) VALUES (1, ?, ?, ?, 0)",
                (sender, created, text),
            )


def _store(tmp_path: Path, *, self_name: str = SELF_NAME) -> LocalMessageStore:
    root = tmp_path / "mirror"
    _insert_contact(root / "contact/contact.db", GROUP, GROUP_NAME)
    return LocalMessageStore(
        FakeMirrors(root),
        self_username=SELF,
        self_display_name=self_name,
    )


def _ref(store: LocalMessageStore) -> ChatRef:
    ref = store.resolve(GROUP_NAME)
    assert ref is not None
    return ref


# --------------------------------------------------------- repository 回读
def test_find_self_message_hits_own_recent_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_messages(
        tmp_path / "mirror/message/message_0.db",
        [
            (3, 1001, "别人也说了同样的话"),
            (2, 1002, "机器人回复"),
        ],
    )

    found = store.find_self_message(_ref(store), "机器人回复", since=1001)

    assert found is not None
    assert found["nickname"] == SELF_NAME
    assert found["text"] == "机器人回复"
    assert found["ts"] == 1002


def test_find_self_message_ignores_other_sender(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_messages(
        tmp_path / "mirror/message/message_0.db",
        [(3, 1002, "别人说的")],
    )

    assert store.find_self_message(_ref(store), "别人说的", since=1000) is None


def test_find_self_message_ignores_old_message(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_messages(
        tmp_path / "mirror/message/message_0.db",
        [(2, 900, "很久以前发过")],
    )

    assert store.find_self_message(_ref(store), "很久以前发过", since=1000) is None
    assert store.find_self_message(_ref(store), "很久以前发过", since=900) is not None


def test_find_self_message_requires_display_name(tmp_path: Path) -> None:
    store = _store(tmp_path, self_name="")
    _insert_messages(
        tmp_path / "mirror/message/message_0.db",
        [(2, 1002, "机器人回复")],
    )

    # 没有显示名就无法区分 self 与他人：fail-closed，宁可停在 typed。
    assert store.find_self_message(_ref(store), "机器人回复", since=1000) is None


def test_find_self_message_rejects_blank_text_and_bad_since(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_messages(tmp_path / "mirror/message/message_0.db", [(2, 1002, "")])

    assert store.find_self_message(_ref(store), "", since=1000) is None
    assert (
        store.find_self_message(_ref(store), "x", since="not-a-number") is None  # type: ignore[arg-type]
    )


def test_find_self_message_respects_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rows = [(2, 1000, "目标消息")] + [
        (3, 1000 + index, f"后续 {index}") for index in range(1, 60)
    ]
    _insert_messages(tmp_path / "mirror/message/message_0.db", rows)
    ref = _ref(store)

    assert store.find_self_message(ref, "目标消息", since=1000, limit=10) is None
    assert store.find_self_message(ref, "目标消息", since=1000, limit=60) is not None


# --------------------------------------------------------- LocalDbSource 包装
def _source_with_store(store: LocalMessageStore, monkeypatch) -> LocalDbSource:
    source = LocalDbSource(self_display_name=SELF_NAME)
    monkeypatch.setattr(source, "_build", lambda: store)
    return source


def test_source_resolves_username_and_reads_self_message(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    _insert_messages(
        tmp_path / "mirror/message/message_0.db",
        [(2, 1002, "机器人回复")],
    )
    source = _source_with_store(store, monkeypatch)

    assert source.available is False
    assert source.resolve_username(GROUP_NAME) == GROUP
    assert source.resolve_username("不存在的群") is None
    assert source.resolve_username("  ") is None
    assert source.available is True
    found = source.find_self_message(GROUP_NAME, "机器人回复", since=1000)
    assert found is not None and found["ts"] == 1002
    assert source.find_self_message("不存在的群", "机器人回复", since=1000) is None


def test_source_degrades_to_none_when_localdb_unavailable(monkeypatch) -> None:
    source = LocalDbSource(self_display_name=SELF_NAME)
    monkeypatch.setattr(
        source, "_build", lambda: (_ for _ in ()).throw(RuntimeError("no keys"))
    )

    assert source.resolve_username(GROUP_NAME) is None
    assert source.find_self_message(GROUP_NAME, "x", since=0) is None
    assert source.available is False


# --------------------------------------------------------- ReceiptVerifier
class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = float(now)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _verifier(lookup: Any, *, available: bool = True, timeout: float = 2.0,
              interval: float = 0.5) -> tuple[ReceiptVerifier, FakeClock]:
    clock = FakeClock()
    source = SimpleNamespace(available=available)
    verifier = ReceiptVerifier(
        source,
        timeout=timeout,
        interval=interval,
        skew_seconds=2.0,
        clock=clock,
        sleep=clock.sleep,
        lookup=lookup,
    )
    return verifier, clock


TYPED = {"ok": True, "error_code": None, "delivered": "typed", "message_id": None}


async def test_apply_upgrades_typed_to_observed() -> None:
    calls: list[tuple[str, str, float]] = []

    def lookup(chat_name: str, text: str, since: float) -> dict[str, Any]:
        calls.append((chat_name, text, since))
        return {"nickname": SELF_NAME, "text": text, "ts": 1001}

    verifier, clock = _verifier(lookup)

    out = await verifier.apply(GROUP_NAME, "机器人回复", 1003.0, TYPED)

    assert out == {
        "ok": True,
        "error_code": None,
        "delivered": "observed",
        "message_id": "localdb:1001",
    }
    # since = started_at - skew(2s)
    assert calls == [(GROUP_NAME, "机器人回复", 1001.0)]
    assert clock.sleeps == []


async def test_apply_keeps_backend_message_id() -> None:
    verifier, _ = _verifier(lambda *args: {"ts": 1001})
    receipt = {**TYPED, "message_id": "backend-1"}

    out = await verifier.apply(GROUP_NAME, "x", 1000.0, receipt)

    assert out["delivered"] == "observed"
    assert out["message_id"] == "backend-1"


async def test_apply_skips_when_source_unavailable() -> None:
    called = False

    def lookup(*args: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"ts": 1}

    verifier, _ = _verifier(lookup, available=False)

    out = await verifier.apply(GROUP_NAME, "x", 1000.0, dict(TYPED))

    assert out == TYPED
    assert called is False


async def test_apply_skips_failed_and_already_observed_receipts() -> None:
    called = False

    def lookup(*args: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"ts": 1}

    verifier, _ = _verifier(lookup)
    failed = {"ok": False, "error_code": "boom", "delivered": "failed"}
    observed = {**TYPED, "delivered": "observed"}

    assert await verifier.apply(GROUP_NAME, "x", 0.0, failed) == failed
    assert await verifier.apply(GROUP_NAME, "x", 0.0, observed) == observed
    assert called is False


async def test_apply_times_out_but_never_fails_the_send() -> None:
    verifier, clock = _verifier(lambda *args: None, timeout=2.0, interval=0.5)

    out = await verifier.apply(GROUP_NAME, "机器人回复", 1000.0, dict(TYPED))

    assert out["ok"] is True
    assert out["delivered"] == "typed"
    assert out["error_code"] == UNVERIFIED
    # 2s 窗口 / 0.5s 间隔：4 次等待后到点。
    assert clock.sleeps == [0.5, 0.5, 0.5, 0.5]


async def test_apply_preserves_existing_error_code_on_timeout() -> None:
    verifier, _ = _verifier(lambda *args: None, timeout=0.0)
    receipt = {**TYPED, "error_code": "not_observed"}

    out = await verifier.apply(GROUP_NAME, "x", 0.0, receipt)

    assert out["delivered"] == "typed"
    assert out["error_code"] == "not_observed"


async def test_apply_swallows_lookup_errors_and_non_dict_results() -> None:
    def broken(*args: Any) -> Any:
        raise RuntimeError("db is gone")

    verifier, _ = _verifier(broken, timeout=0.0)
    out = await verifier.apply(GROUP_NAME, "x", 0.0, dict(TYPED))
    assert out["error_code"] == UNVERIFIED

    verifier, _ = _verifier(lambda *args: ["not", "a", "dict"], timeout=0.0)
    out = await verifier.apply(GROUP_NAME, "x", 0.0, dict(TYPED))
    assert out["error_code"] == UNVERIFIED


async def test_apply_finds_after_a_few_polls() -> None:
    seen = 0

    def lookup(*args: Any) -> dict[str, Any] | None:
        nonlocal seen
        seen += 1
        return {"ts": 1005} if seen >= 3 else None

    verifier, clock = _verifier(lookup, timeout=5.0, interval=0.5)

    out = await verifier.apply(GROUP_NAME, "x", 1000.0, dict(TYPED))

    assert out["delivered"] == "observed"
    assert clock.sleeps == [0.5, 0.5]


async def test_apply_uses_source_find_self_message_when_no_lookup() -> None:
    found: list[tuple[str, str, float]] = []

    class Source:
        available = True

        def find_self_message(self, name: str, text: str, *, since: float) -> dict[str, Any]:
            found.append((name, text, since))
            return {"ts": 1234}

    clock = FakeClock()
    verifier = ReceiptVerifier(
        Source(),
        timeout=1.0,
        interval=0.5,
        clock=clock,
        sleep=clock.sleep,
    )

    out = await verifier.apply(GROUP_NAME, "x", 1000.0, dict(TYPED))

    assert out["delivered"] == "observed"
    assert out["message_id"] == "localdb:1234"
    assert found == [(GROUP_NAME, "x", 998.0)]
