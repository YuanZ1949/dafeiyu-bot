"""微信 4.1 本地库 repository：临时目录自造明文 SQLite 夹具，不碰真实微信库。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import zstandard

from dafeiyu.gateway.wechat_bridge.localdb import repository
from dafeiyu.gateway.wechat_bridge.localdb.repository import (
    ChatRef,
    LocalMessageStore,
    StoreError,
    message_table_name,
)

SELF = "wxid_self"
SELF_NAME = "小菜鱼"
GROUP = "38689267237@chatroom"
GROUP_NAME = "我们一起测试吖"
OTHER_GROUP = "12345678@chatroom"

# 与真实库一致的列顺序；夹具只填用得到的列，其余留 NULL。
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

NAME2ID_DDL = "CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)"


class FakeMirrors:
    """只实现 repository 需要的协议：path_for(rel)，可选 paths()。"""

    def __init__(self, root: Path, *, rels: list[str] | None = None) -> None:
        self.root = Path(root)
        self._rels = list(rels or [])

    def path_for(self, rel: str) -> Path:
        return self.root / rel

    def paths(self) -> list[Path]:
        return [self.root / rel for rel in self._rels]


class PathOnlyMirrors:
    """没有 paths() 的镜像集：只能靠显式 message_rels 或默认分库。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, rel: str) -> Path:
        return self.root / rel


# ------------------------------------------------------------------ 夹具构建
def make_contact_db(
    path: Path,
    *,
    contacts: list[tuple[str, str, str]] = (),
    strangers: list[tuple[str, str, str]] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(CONTACT_DDL)
        con.execute(STRANGER_DDL)
        for username, remark, nick_name in contacts:
            con.execute(
                "INSERT INTO contact(username, remark, nick_name) VALUES (?, ?, ?)",
                (username, remark, nick_name),
            )
        for username, remark, nick_name in strangers:
            con.execute(
                "INSERT INTO stranger(username, remark, nick_name) VALUES (?, ?, ?)",
                (username, remark, nick_name),
            )


def make_message_db(
    path: Path,
    *,
    name2id: dict[int, str],
    tables: dict[str, list[tuple]],
) -> None:
    """``tables``: username -> [(local_type, real_sender_id, create_time, content, ct)]"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(NAME2ID_DDL)
        for rowid, user_name in name2id.items():
            con.execute(
                "INSERT INTO Name2Id(rowid, user_name, is_session) VALUES (?, ?, 0)",
                (rowid, user_name),
            )
        for username, rows in tables.items():
            table = message_table_name(username)
            con.execute(MESSAGE_DDL.format(table=table))
            for local_type, sender, created, content, ct in rows:
                con.execute(
                    f'INSERT INTO "{table}" '
                    "(local_type, real_sender_id, create_time, message_content, "
                    "WCDB_CT_message_content) VALUES (?, ?, ?, ?, ?)",
                    (local_type, sender, created, content, ct),
                )


def text_row(
    sender: int, content: str, *, created: int = 1000, local_type: int = 1
) -> tuple:
    return (local_type, sender, created, content, 0)


def make_store(
    root: Path,
    *,
    mirrors: object | None = None,
    message_rels: list[str] | None = None,
) -> LocalMessageStore:
    return LocalMessageStore(
        mirrors if mirrors is not None else PathOnlyMirrors(root),
        self_username=SELF,
        self_display_name=SELF_NAME,
        message_rels=message_rels,
    )


def build_group_store(tmp_path: Path) -> LocalMessageStore:
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[(GROUP, "", GROUP_NAME)],
    )
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 2: SELF},
        tables={GROUP: [text_row(2, "自己的话")]},
    )
    return make_store(root)


# ------------------------------------------------------------------ 1. resolve
def test_resolve_hits_nick_name(tmp_path: Path) -> None:
    store = build_group_store(tmp_path)

    ref = store.resolve(GROUP_NAME)

    assert ref == ChatRef(
        display_name=GROUP_NAME,
        username=GROUP,
        shard="message/message_0.db",
        table=message_table_name(GROUP),
    )


def test_resolve_prefers_remark_over_nick_name(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[
            (GROUP, "备注群", "微信原名"),
            (OTHER_GROUP, "", "另一个群"),
        ],
    )
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 2: OTHER_GROUP},
        tables={GROUP: [], OTHER_GROUP: []},
    )
    store = make_store(root)

    assert store.resolve("备注群") is not None
    assert store.resolve("  备注群  ") is not None
    assert store.resolve("微信原名") is None
    assert store.resolve("另一个群") is not None


def test_resolve_duplicate_display_name_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[
            ("111@chatroom", "", "同名群"),
            ("222@chatroom", "", "同名群"),
        ],
    )
    store = make_store(root)

    assert store.resolve("同名群") is None


def test_resolve_ignores_non_chatroom_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[("wxid_friend", "", "某人"), (GROUP, "", GROUP_NAME)],
    )
    store = make_store(root)

    assert store.resolve("某人") is None
    assert store.resolve("不存在的群") is None
    assert store.resolve("") is None


def test_group_names_only_chatrooms_dedup_and_sorted(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[
            ("333@chatroom", "", "B群"),
            ("111@chatroom", "", "A群"),
            ("444@chatroom", "", "B群"),
            ("wxid_friend", "", "朋友"),
        ],
    )
    store = make_store(root)

    assert store.group_names() == ["A群", "B群"]


# ------------------------------------------------- 2. 顺序、limit、缺表/坏分库
def test_recent_orders_old_to_new_and_applies_limit(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(root / "contact/contact.db", contacts=[(GROUP, "", GROUP_NAME)])
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 3: "wxid_a"},
        tables={
            GROUP: [
                text_row(3, "wxid_a:\n一", created=1001),
                text_row(3, "wxid_a:\n二", created=1002),
                text_row(3, "wxid_a:\n三", created=1003),
                text_row(3, "wxid_a:\n四", created=1004),
            ]
        },
    )
    store = make_store(root)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None

    entries = store.recent(ref, limit=3)

    assert [entry["text"] for entry in entries] == ["二", "三", "四"]
    assert [entry["ts"] for entry in entries] == [1002, 1003, 1004]


def test_recent_missing_table_returns_empty(tmp_path: Path) -> None:
    store = build_group_store(tmp_path)

    ref = ChatRef(GROUP_NAME, GROUP, "message/message_0.db", "Msg_deadbeef")

    assert store.recent(ref) == []


def test_bad_shard_is_skipped_when_table_lives_elsewhere(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(root / "contact/contact.db", contacts=[(GROUP, "", GROUP_NAME)])
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 3: "wxid_a"},
        tables={GROUP: [text_row(3, "wxid_a:\n可用")]},
    )
    (root / "message/message_1.db").write_bytes(b"this is not a sqlite database")
    store = make_store(
        root,
        mirrors=FakeMirrors(
            root, rels=["message/message_0.db", "message/message_1.db"]
        ),
    )

    ref = store.resolve(GROUP_NAME)

    assert ref is not None
    assert ref.shard == "message/message_0.db"
    assert [entry["text"] for entry in store.recent(ref)] == ["可用"]


def test_recent_bad_limit_returns_empty(tmp_path: Path) -> None:
    store = build_group_store(tmp_path)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None

    assert store.recent(ref, limit=0) == []
    assert store.recent(ref, limit=-5) == []
    assert store.recent(ref, limit="不是数字") == []


def test_recent_wrong_schema_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(root / "contact/contact.db", contacts=[(GROUP, "", GROUP_NAME)])
    path = root / "message/message_0.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(NAME2ID_DDL)
        con.execute(f'CREATE TABLE "{message_table_name(GROUP)}"(local_id INTEGER)')
    store = make_store(root)
    ref = ChatRef(GROUP_NAME, GROUP, "message/message_0.db", message_table_name(GROUP))

    assert store.recent(ref) == []


# ----------------------------------------------------------- 3. 发送者显示名
def test_sender_display_names(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[
            (GROUP, "", GROUP_NAME),
            ("wxid_a", "", "阿甲"),
            ("wxid_b", "备注乙", "乙原名"),
        ],
        strangers=[("wxid_s", "", "陌生人丙")],
    )
    make_message_db(
        root / "message/message_0.db",
        name2id={
            1: GROUP,
            2: SELF,
            3: "wxid_a",
            4: "wxid_b",
            5: "wxid_s",
            9: "wxid_unknown",
        },
        tables={
            GROUP: [
                text_row(2, "自己发的"),
                text_row(3, "wxid_a:\n甲的话"),
                text_row(4, "wxid_b:\n乙的话"),
                text_row(5, "wxid_s:\n陌生人的话"),
                text_row(9, "wxid_unknown:\n只有前缀"),
                text_row(9, "没有前缀也没有联系人"),
            ]
        },
    )
    store = make_store(root)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None

    entries = store.recent(ref)

    assert [entry["nickname"] for entry in entries] == [
        SELF_NAME,
        "阿甲",
        "备注乙",
        "陌生人丙",
        "wxid_unknown",
        "",
    ]


# --------------------------------------------------------------- 4. 前缀剥离
def test_prefix_stripped_only_when_sender_matches(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[(GROUP, "", GROUP_NAME), ("wxid_a", "", "阿甲"), ("wxid_b", "", "阿乙")],
    )
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 3: "wxid_a", 4: "wxid_b"},
        tables={
            GROUP: [
                text_row(3, "wxid_a:\n你好"),
                text_row(3, "http://x:8080\n"),
                text_row(3, "wxid_b:\n别人的前缀"),
                text_row(3, "wxid_a:\r\n换行也认"),
            ]
        },
    )
    store = make_store(root)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None

    texts = [entry["text"] for entry in store.recent(ref)]

    assert texts == ["你好", "http://x:8080\n", "wxid_b:\n别人的前缀", "换行也认"]


# ------------------------------------------------------------------- 5. zstd
def test_zstd_compressed_content_is_decompressed(tmp_path: Path) -> None:
    body = "wxid_a:\n压缩正文也要剥前缀"
    compressed = zstandard.ZstdCompressor().compress(body.encode("utf-8"))
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[(GROUP, "", GROUP_NAME), ("wxid_a", "", "阿甲")],
    )
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 3: "wxid_a"},
        tables={GROUP: [(1, 3, 1000, compressed, 4)]},
    )
    store = make_store(root)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None

    entries = store.recent(ref)

    assert len(entries) == 1
    assert entries[0]["text"] == "压缩正文也要剥前缀"
    assert entries[0]["nickname"] == "阿甲"


def test_zstd_unavailable_degrades_to_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compressed = zstandard.ZstdCompressor().compress("正文".encode())
    root = tmp_path / "mirror"
    make_contact_db(root / "contact/contact.db", contacts=[(GROUP, "", GROUP_NAME)])
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 3: "wxid_a"},
        tables={GROUP: [(1, 3, 1000, compressed, 4)]},
    )
    monkeypatch.setattr(repository, "_zstandard", None)
    store = make_store(root)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None

    entries = store.recent(ref)

    assert entries[0]["text"] == ""
    assert entries[0]["kind"] == "text"


# --------------------------------------------------------------------- 6. XML
def test_image_message_becomes_placeholder(tmp_path: Path) -> None:
    xml = '<?xml version="1.0"?><msg><img aeskey="abc" /></msg>'
    store, ref = _store_with_single_message(
        tmp_path, text_row(3, f"wxid_a:\n{xml}", local_type=3)
    )

    entry = store.recent(ref)[0]

    assert entry["kind"] == "image"
    assert entry["text"] == "[图片]"
    assert entry["nickname"] == "阿甲"


def test_appmsg_link_uses_title(tmp_path: Path) -> None:
    xml = (
        "<?xml version='1.0'?>"
        "<msg><appmsg><title>这是一条链接标题</title><type>5</type>"
        "<url>https://example.com</url></appmsg></msg>"
    )
    store, ref = _store_with_single_message(
        tmp_path, text_row(3, f"wxid_a:\n{xml}", local_type=49)
    )

    entry = store.recent(ref)[0]

    assert entry["kind"] == "link"
    assert entry["text"] == "这是一条链接标题"
    assert entry["reply_to"] == ""
    assert entry["reply_author"] == ""


def test_quote_message_exposes_refer_fields(tmp_path: Path) -> None:
    xml = (
        "<msg><appmsg><title>我的回复</title><type>57</type>"
        "<refermsg><displayname>小菜鱼</displayname>"
        "<content>被引用的原文</content><type>1</type></refermsg>"
        "</appmsg></msg>"
    )
    store, ref = _store_with_single_message(
        tmp_path, text_row(3, f"wxid_a:\n{xml}", local_type=49)
    )

    entry = store.recent(ref)[0]

    assert entry["kind"] == "quote"
    assert entry["text"] == "我的回复"
    assert entry["reply_author"] == "小菜鱼"
    assert entry["reply_to"] == "被引用的原文"


def test_broken_xml_does_not_raise(tmp_path: Path) -> None:
    store, ref = _store_with_single_message(
        tmp_path, text_row(3, "wxid_a:\n<msg><appmsg><title>没闭合", local_type=49)
    )

    entry = store.recent(ref)[0]

    assert entry["kind"] == "link"
    assert entry["text"] == ""


# ------------------------------------------------------------ 7. 高位标志位
def test_high_bit_local_type_is_masked_to_appmsg(tmp_path: Path) -> None:
    xml = "<msg><appmsg><title>高位标志</title><type>5</type></appmsg></msg>"
    store, ref = _store_with_single_message(
        tmp_path, text_row(3, f"wxid_a:\n{xml}", local_type=0x500000031)
    )

    entry = store.recent(ref)[0]

    assert entry["kind"] == "link"
    assert entry["text"] == "高位标志"


# --------------------------------------------------------------- 8. 多分库
def test_resolve_finds_table_in_second_shard_via_paths(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(root / "contact/contact.db", contacts=[(GROUP, "", GROUP_NAME)])
    make_message_db(root / "message/message_0.db", name2id={1: GROUP}, tables={})
    make_message_db(
        root / "message/message_1.db",
        name2id={1: GROUP, 3: "wxid_a"},
        tables={GROUP: [text_row(3, "wxid_a:\n第二个分库")]},
    )
    # 这些同类文件必须被 paths() 过滤器忽略。
    (root / "message/message_fts.db").write_bytes(b"")
    (root / "message/message_resource.db").write_bytes(b"")
    (root / "message/biz_message_0.db").write_bytes(b"")
    mirrors = FakeMirrors(
        root,
        rels=[
            "message/message_0.db",
            "message/message_1.db",
            "message/message_fts.db",
            "message/message_resource.db",
            "message/biz_message_0.db",
        ],
    )
    store = make_store(root, mirrors=mirrors)

    ref = store.resolve(GROUP_NAME)

    assert ref is not None
    assert ref.shard == "message/message_1.db"
    assert [entry["text"] for entry in store.recent(ref)] == ["第二个分库"]


def test_explicit_message_rels_are_used_without_paths(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(root / "contact/contact.db", contacts=[(GROUP, "", GROUP_NAME)])
    make_message_db(
        root / "message/message_7.db",
        name2id={1: GROUP},
        tables={GROUP: []},
    )
    store = make_store(
        root,
        mirrors=PathOnlyMirrors(root),
        message_rels=["message/message_7.db"],
    )

    ref = store.resolve(GROUP_NAME)

    assert ref is not None
    assert ref.shard == "message/message_7.db"


# -------------------------------------------------------------- 9. 系统消息
def test_system_messages_are_dropped(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(root / "contact/contact.db", contacts=[(GROUP, "", GROUP_NAME)])
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 3: "wxid_a"},
        tables={
            GROUP: [
                text_row(3, "wxid_a:\n正常一"),
                text_row(3, "<sysmsg>你已加入群聊</sysmsg>", local_type=10000),
                text_row(3, "wxid_a:\n正常二"),
                text_row(3, "撤回了一条消息", local_type=10002),
                text_row(3, "未知类型", local_type=9999),
            ]
        },
    )
    store = make_store(root)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None

    assert [entry["text"] for entry in store.recent(ref)] == ["正常一", "正常二"]


# --------------------------------------------------------------- fail-open
def test_missing_contact_db_returns_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path / "nothing")

    assert store.group_names() == []
    assert store.resolve(GROUP_NAME) is None


def test_corrupt_contact_db_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    (root / "contact").mkdir(parents=True)
    (root / "contact/contact.db").write_bytes(b"not a database")
    store = make_store(root)

    assert store.group_names() == []
    assert store.resolve(GROUP_NAME) is None


def test_chinese_and_space_in_path(tmp_path: Path) -> None:
    root = tmp_path / "镜像 目录 #1"
    make_contact_db(root / "contact/contact.db", contacts=[(GROUP, "", GROUP_NAME)])
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 3: "wxid_a"},
        tables={GROUP: [text_row(3, "wxid_a:\n中文路径")]},
    )
    store = make_store(root)

    ref = store.resolve(GROUP_NAME)

    assert ref is not None
    assert [entry["text"] for entry in store.recent(ref)] == ["中文路径"]


def test_missing_name2id_still_uses_prefix(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    path = root / "message/message_0.db"
    path.parent.mkdir(parents=True)
    table = message_table_name(GROUP)
    with sqlite3.connect(path) as con:
        con.execute(MESSAGE_DDL.format(table=table))
        con.execute(
            f'INSERT INTO "{table}" (local_type, real_sender_id, create_time, '
            "message_content, WCDB_CT_message_content) VALUES (1, 3, 1000, ?, 0)",
            ("wxid_a:\n没有 Name2Id",),
        )
    store = make_store(root, message_rels=["message/message_0.db"])
    ref = ChatRef(GROUP_NAME, GROUP, "message/message_0.db", table)

    entries = store.recent(ref)

    # Name2Id 缺失时无法确认前缀归属：正文保持原样（fail closed），昵称仍可用前缀。
    assert entries[0]["text"] == "wxid_a:\n没有 Name2Id"
    assert entries[0]["nickname"] == "wxid_a"


# --------------------------------------------------------- 构造参数校验
def test_constructor_rejects_invalid_mirrors() -> None:
    with pytest.raises(StoreError):
        LocalMessageStore(object(), self_username=SELF, self_display_name=SELF_NAME)


def test_constructor_rejects_empty_self_username(tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        LocalMessageStore(
            PathOnlyMirrors(tmp_path), self_username="  ", self_display_name=SELF_NAME
        )


def test_constructor_rejects_single_string_message_rels(tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        LocalMessageStore(
            PathOnlyMirrors(tmp_path),
            self_username=SELF,
            self_display_name=SELF_NAME,
            message_rels="message/message_0.db",  # type: ignore[arg-type]
        )


# ------------------------------------------------------------------ 小工具
def _store_with_single_message(
    tmp_path: Path, row: tuple
) -> tuple[LocalMessageStore, ChatRef]:
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[(GROUP, "", GROUP_NAME), ("wxid_a", "", "阿甲")],
    )
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 3: "wxid_a"},
        tables={GROUP: [row]},
    )
    store = make_store(root)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None
    return store, ref


# ---------------------------------------------------------- todo 11: 媒体发现
MEDIA_DDL = """
CREATE TABLE message_resource(
    message_local_id INTEGER, local_path TEXT, file_name TEXT,
    file_size INTEGER, duration INTEGER
)
"""


def make_media_db(
    path: Path,
    *,
    local_id: int,
    local_path: str,
    name: str = "",
    size: int = 0,
    duration: int = 0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(MEDIA_DDL)
        con.execute(
            "INSERT INTO message_resource VALUES (?, ?, ?, ?, ?)",
            (local_id, local_path, name, size, duration),
        )


def build_image_store(
    tmp_path: Path,
    *,
    local_path: str,
    size_limit: int = 1024 * 1024,
    make_resource_db: bool = True,
):
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db",
        contacts=[(GROUP, "", GROUP_NAME)],
    )
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 2: SELF},
        tables={GROUP: [(3, 2, 1000, "", 0)]},
    )
    if make_resource_db:
        make_media_db(
            root / "message/message_resource.db",
            local_id=1,
            local_path=local_path,
            name="a.png",
            size=123,
            duration=0,
        )
    store = LocalMessageStore(
        PathOnlyMirrors(root),
        self_username=SELF,
        self_display_name=SELF_NAME,
        message_rels=["message/message_0.db"],
        media_enabled=True,
        media_max_bytes=size_limit,
        media_storage_root=str(root),
    )
    return store, root


def test_media_disabled_row_has_no_media_key(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    make_contact_db(
        root / "contact/contact.db", contacts=[(GROUP, "", GROUP_NAME)]
    )
    make_message_db(
        root / "message/message_0.db",
        name2id={1: GROUP, 2: SELF},
        tables={GROUP: [(3, 2, 1000, "", 0)]},
    )
    (root / "media").mkdir(parents=True)
    (root / "media/a.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    make_media_db(
        root / "message/message_resource.db",
        local_id=1,
        local_path="media/a.png",
        name="a.png",
        size=16,
    )

    store = make_store(root)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None
    rows = store.recent(ref, 1)

    assert rows[0]["text"] == "[图片]"
    assert "media" not in rows[0]


def test_media_enabled_adds_documented_dict(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    (root / "media").mkdir(parents=True)
    png = root / "media/a.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 16
    png.write_bytes(payload)

    store, _ = build_image_store(tmp_path, local_path="media/a.png")
    ref = store.resolve(GROUP_NAME)
    assert ref is not None
    row = store.recent(ref, 1)[0]

    assert row["text"] == "[图片]"
    assert row["media"] == {
        "kind": "image",
        "path": str(png.resolve()),
        "name": "a.png",
        "size": len(payload),
        "duration": 0,
    }


def test_media_missing_file_keeps_placeholder(tmp_path: Path) -> None:
    store, _ = build_image_store(tmp_path, local_path="media/missing.png")
    ref = store.resolve(GROUP_NAME)
    assert ref is not None
    row = store.recent(ref, 1)[0]

    assert row["text"] == "[图片]"
    assert "media" not in row


def test_media_path_outside_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nxx")

    store, _ = build_image_store(tmp_path, local_path=str(outside))
    ref = store.resolve(GROUP_NAME)
    assert ref is not None
    row = store.recent(ref, 1)[0]

    assert "media" not in row


def test_media_over_max_bytes_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    (root / "media").mkdir(parents=True)
    (root / "media/a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    store, _ = build_image_store(tmp_path, local_path="media/a.png", size_limit=10)
    ref = store.resolve(GROUP_NAME)
    assert ref is not None
    row = store.recent(ref, 1)[0]

    assert "media" not in row


def test_media_absent_index_db_is_fail_open(tmp_path: Path) -> None:
    store, _ = build_image_store(
        tmp_path, local_path="media/a.png", make_resource_db=False
    )
    ref = store.resolve(GROUP_NAME)
    assert ref is not None
    row = store.recent(ref, 1)[0]

    assert row["text"] == "[图片]"
    assert "media" not in row
