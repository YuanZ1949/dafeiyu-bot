"""微信本地库只读镜像：SQLCipher 页解密、WAL 解析、增量同步。

夹具全部离线：用 ``sqlite3`` 建真实小库，再用测试内的 CNG 加密函数把它变成
微信 4.1 的页格式。这样解密逻辑有一个独立的加密实现做 round-trip 证明，
而不是拿生产代码自己加密自己。
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import shutil
import sqlite3
import struct
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

if sys.platform != "win32":
    pytest.skip("SQLCipher 页解密依赖 Windows CNG", allow_module_level=True)

from dafeiyu.gateway.wechat_bridge.localdb.mirror import (
    Mirror,
    MirrorError,
    MirrorSet,
    SyncResult,
)
from dafeiyu.gateway.wechat_bridge.localdb.sqlcipher import (
    HMAC_OFFSET,
    IV_OFFSET,
    PAGE_SIZE,
    RESERVE_SIZE,
    SALT_SIZE,
    SQLITE_HEADER,
    CipherError,
    decrypt_page,
    verify_page,
)
from dafeiyu.gateway.wechat_bridge.localdb.wal import (
    WAL_HEADER_SIZE,
    WalError,
    read_wal,
)

KEY = bytes(range(32))
SALT = bytes(range(0x40, 0x50))

# --------------------------------------------------------------- CNG 加密夹具
_bcrypt = ctypes.WinDLL("bcrypt")
_bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.ULONG,
]
_bcrypt.BCryptSetProperty.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCWSTR,
    ctypes.c_char_p,
    wintypes.ULONG,
    wintypes.ULONG,
]
_bcrypt.BCryptGenerateSymmetricKey.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.HANDLE),
    ctypes.c_char_p,
    wintypes.ULONG,
    ctypes.c_char_p,
    wintypes.ULONG,
    wintypes.ULONG,
]
_bcrypt.BCryptEncrypt.argtypes = [
    wintypes.HANDLE,
    ctypes.c_char_p,
    wintypes.ULONG,
    ctypes.c_void_p,
    ctypes.c_char_p,
    wintypes.ULONG,
    ctypes.c_char_p,
    wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG),
    wintypes.ULONG,
]
_bcrypt.BCryptDestroyKey.argtypes = [wintypes.HANDLE]
_bcrypt.BCryptCloseAlgorithmProvider.argtypes = [wintypes.HANDLE, wintypes.ULONG]


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """测试用 AES-256-CBC 加密（无填充），与生产解密实现相互独立。"""
    h_alg = wintypes.HANDLE()
    if _bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0):
        raise RuntimeError("测试夹具：打开 CNG AES 失败")
    try:
        mode = "ChainingModeCBC\x00".encode("utf-16-le")
        if _bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0):
            raise RuntimeError("测试夹具：设置 CBC 失败")
        h_key = wintypes.HANDLE()
        if _bcrypt.BCryptGenerateSymmetricKey(
            h_alg, ctypes.byref(h_key), None, 0, key, len(key), 0
        ):
            raise RuntimeError("测试夹具：生成密钥失败")
        try:
            iv_buf = ctypes.create_string_buffer(iv, len(iv))
            out = ctypes.create_string_buffer(len(data))
            written = ctypes.c_ulong(0)
            status = _bcrypt.BCryptEncrypt(
                h_key,
                data,
                len(data),
                None,
                iv_buf,
                len(iv),
                out,
                len(out),
                ctypes.byref(written),
                0,
            )
            if status:
                raise RuntimeError(f"测试夹具：CNG 加密失败 {status:#x}")
            return out.raw[: written.value]
        finally:
            _bcrypt.BCryptDestroyKey(h_key)
    finally:
        _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


def _test_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    mac_salt = bytes(value ^ 0x3A for value in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)


def _deterministic_iv(pgno: int) -> bytes:
    return hashlib.sha256(struct.pack(">I", pgno)).digest()[:16]


def encrypt_page(
    plain: bytes,
    pgno: int,
    *,
    key: bytes = KEY,
    salt: bytes = SALT,
    iv: bytes | None = None,
) -> bytes:
    """把一个明文 SQLite 页加密成微信 4.1 的页格式。"""
    if len(plain) != PAGE_SIZE:
        raise ValueError(f"明文页长度必须是 {PAGE_SIZE}")
    if len(key) != 32 or len(salt) != SALT_SIZE:
        raise ValueError("密钥或 salt 长度不对")
    actual_iv = iv or _deterministic_iv(pgno)
    if len(actual_iv) != 16:
        raise ValueError("IV 必须是 16 字节")

    if pgno == 1:
        if plain[:SALT_SIZE] != SQLITE_HEADER:
            raise ValueError("page 1 的明文必须以 SQLite 文件头开头")
        ciphertext = aes_cbc_encrypt(key, actual_iv, plain[SALT_SIZE:IV_OFFSET])
        body = salt + ciphertext
    else:
        ciphertext = aes_cbc_encrypt(key, actual_iv, plain[:IV_OFFSET])
        body = ciphertext

    digest = hmac.new(_test_mac_key(key, salt), ciphertext + actual_iv, hashlib.sha512)
    digest.update(struct.pack("<I", pgno))
    return body + actual_iv + digest.digest()


def encrypt_db(
    source: Path,
    dest: Path,
    *,
    key: bytes = KEY,
    salt: bytes = SALT,
) -> int:
    """加密整个明文 SQLite 文件，返回页数。"""
    data = source.read_bytes()
    if len(data) % PAGE_SIZE:
        raise ValueError("夹具数据库不是 4096 的整数倍")
    pages = len(data) // PAGE_SIZE
    out = bytearray()
    for index in range(pages):
        pgno = index + 1
        plain = data[index * PAGE_SIZE:pgno * PAGE_SIZE]
        out += encrypt_page(plain, pgno, key=key, salt=salt)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bytes(out))
    return pages


def encrypted_pages(source: Path, *, key: bytes = KEY, salt: bytes = SALT) -> list[bytes]:
    """把明文库逐页加密成字节列表（构造 WAL 帧用）。"""
    data = source.read_bytes()
    if len(data) % PAGE_SIZE:
        raise ValueError("夹具数据库不是 4096 的整数倍")
    return [
        encrypt_page(
            data[index * PAGE_SIZE:(index + 1) * PAGE_SIZE],
            index + 1,
            key=key,
            salt=salt,
        )
        for index in range(len(data) // PAGE_SIZE)
    ]


# ------------------------------------------------------------------ 数据库夹具
def _sqlite3_library() -> ctypes.CDLL:
    """加载 Python 自带的 ``sqlite3.dll``（只为设置页保留区）。"""
    dll_path = Path(sys.base_prefix) / "DLLs" / "sqlite3.dll"
    if not dll_path.is_file():
        pytest.skip(f"找不到 sqlite3.dll：{dll_path}")
    library = ctypes.CDLL(str(dll_path))
    library.sqlite3_open_v2.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_int,
        ctypes.c_char_p,
    ]
    library.sqlite3_open_v2.restype = ctypes.c_int
    library.sqlite3_exec.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    library.sqlite3_exec.restype = ctypes.c_int
    library.sqlite3_file_control.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    library.sqlite3_file_control.restype = ctypes.c_int
    library.sqlite3_close.argtypes = [ctypes.c_void_p]
    library.sqlite3_close.restype = ctypes.c_int
    return library


def _exec_sql(library: ctypes.CDLL, db: ctypes.c_void_p, sql: str) -> None:
    error = ctypes.c_char_p()
    status = library.sqlite3_exec(
        db, sql.encode("utf-8"), None, None, ctypes.byref(error)
    )
    if status != 0:
        message = error.value.decode("utf-8", "replace") if error.value else ""
        raise RuntimeError(f"夹具建库失败（{status}）：{message}")


def _create_db_with_reserve(path: Path, schema: str, reserve: int = RESERVE_SIZE) -> None:
    """用 SQLite 建库并把页保留区设为 80（微信 SQLCipher 的真实布局）。

    Python 的 ``sqlite3`` 没有设置保留区的接口，必须走
    ``sqlite3_file_control(SQLITE_FCNTL_RESERVE_BYTES)``。保留区在第一次写页
    之前生效，之后 Python ``sqlite3`` 会按文件头里的值正常读写。
    """
    library = _sqlite3_library()
    db = ctypes.c_void_p()
    status = library.sqlite3_open_v2(
        str(path).encode("utf-8"), ctypes.byref(db), 0x2 | 0x4, None
    )
    if status != 0:
        raise RuntimeError(f"夹具打开 SQLite 失败：{status}")
    try:
        _exec_sql(library, db, "PRAGMA page_size=4096")
        requested = ctypes.c_int(reserve)
        status = library.sqlite3_file_control(
            db, b"main", 38, ctypes.byref(requested)  # SQLITE_FCNTL_RESERVE_BYTES
        )
        if status != 0:
            raise RuntimeError(f"夹具设置页保留区失败：{status}")
        _exec_sql(library, db, schema)
    finally:
        library.sqlite3_close(db)


def make_plain_db(path: Path, rows: int = 240) -> list[tuple[int, str]]:
    """建一个真实的小 SQLite 库（多页、80 字节页保留区），返回插入的行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    _create_db_with_reserve(
        path, "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
    )
    header = path.read_bytes()[:32]
    if header[16:18] != b"\x10\x00" or header[20] != RESERVE_SIZE:
        raise RuntimeError("夹具数据库的页大小/保留区不符合 SQLCipher 布局")

    records = [(index, f"name-{index:04d}-" + "x" * 120) for index in range(1, rows + 1)]
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executemany(
            "INSERT INTO items (id, name) VALUES (?, ?)", records
        )
        connection.commit()
    finally:
        connection.close()
    return records


def read_rows(path: Path) -> list[tuple[int, str]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return list(connection.execute("SELECT id, name FROM items ORDER BY id"))
    finally:
        connection.close()


def update_value(path: Path, row_id: int, value: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE items SET name = ? WHERE id = ?", (value, row_id))
        connection.commit()
    finally:
        connection.close()


@dataclass(frozen=True)
class Fixture:
    plain: Path
    source: Path
    mirror_path: Path
    state_path: Path
    total_pages: int


def make_fixture(tmp_path: Path) -> Fixture:
    plain = tmp_path / "plain" / "contact.db"
    plain.parent.mkdir(parents=True, exist_ok=True)
    make_plain_db(plain)
    source = tmp_path / "source" / "contact.db"
    total_pages = encrypt_db(plain, source)
    mirror_path = tmp_path / "mirror" / "contact.db"
    state_path = tmp_path / "mirror" / "contact.db.state.json"
    return Fixture(
        plain=plain,
        source=source,
        mirror_path=mirror_path,
        state_path=state_path,
        total_pages=total_pages,
    )


@pytest.fixture()
def fixture(tmp_path: Path) -> Fixture:
    return make_fixture(tmp_path)


# --------------------------------------------------------------- 密码学 round-trip
def test_decrypt_page_roundtrip_page1_and_pagen(fixture: Fixture) -> None:
    data = fixture.plain.read_bytes()
    for pgno in (1, 2, 3):
        plain = data[(pgno - 1) * PAGE_SIZE:pgno * PAGE_SIZE]
        encrypted = encrypt_page(plain, pgno, salt=fixture.source.read_bytes()[:SALT_SIZE])

        assert verify_page(encrypted, KEY, pgno, SALT) is True
        decrypted = decrypt_page(encrypted, KEY, pgno, SALT)

        assert decrypted[:IV_OFFSET] == plain[:IV_OFFSET]
        assert decrypted[IV_OFFSET:] == b"\x00" * RESERVE_SIZE
    assert decrypt_page(
        encrypt_page(data[:PAGE_SIZE], 1), KEY, 1
    ).startswith(SQLITE_HEADER)


def test_decrypt_page_rejects_tampered_hmac(fixture: Fixture) -> None:
    plain = fixture.plain.read_bytes()[:PAGE_SIZE]
    encrypted = bytearray(encrypt_page(plain, 1))

    damaged = bytearray(encrypted)
    damaged[100] ^= 0xFF
    assert verify_page(bytes(damaged), KEY, 1) is False
    with pytest.raises(CipherError, match="HMAC"):
        decrypt_page(bytes(damaged), KEY, 1)

    damaged_hmac = bytearray(encrypted)
    damaged_hmac[HMAC_OFFSET + 3] ^= 0x01
    assert verify_page(bytes(damaged_hmac), KEY, 1) is False
    with pytest.raises(CipherError):
        decrypt_page(bytes(damaged_hmac), KEY, 1)

    assert verify_page(bytes(encrypted), bytes(32), 1) is False
    assert verify_page(bytes(encrypted), KEY, 1, bytes(16)) is False


def test_decrypt_page_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="32 字节"):
        decrypt_page(b"\x00" * PAGE_SIZE, b"short", 1)
    with pytest.raises(ValueError, match="页长度"):
        decrypt_page(b"\x00" * 100, KEY, 1)
    with pytest.raises(ValueError, match="salt"):
        decrypt_page(b"\x00" * PAGE_SIZE, KEY, 2)


# ------------------------------------------------------------------ Mirror 基础
def test_mirror_full_then_noop(fixture: Fixture) -> None:
    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)

    first = mirror.sync()

    assert first == SyncResult(
        changed=True,
        pages_written=fixture.total_pages,
        wal_frames=0,
        full=True,
    )
    assert read_rows(fixture.mirror_path) == read_rows(fixture.plain)
    assert fixture.mirror_path.stat().st_size == fixture.total_pages * PAGE_SIZE

    second = mirror.sync()

    assert second.full is False
    assert second.changed is False
    assert second.pages_written == 0
    assert second.wal_frames == 0


def test_mirror_incremental_rewrites_only_changed_pages(fixture: Fixture) -> None:
    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    mirror.sync()

    update_value(fixture.plain, 1, "updated-by-test")
    encrypt_db(fixture.plain, fixture.source)

    result = mirror.sync()

    assert result.full is False
    assert 0 < result.pages_written < fixture.total_pages
    assert dict(read_rows(fixture.mirror_path))[1] == "updated-by-test"


def test_mirror_bad_page_leaves_mirror_untouched(fixture: Fixture) -> None:
    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    mirror.sync()
    before = fixture.mirror_path.read_bytes()

    damaged = bytearray(fixture.source.read_bytes())
    damaged[2 * PAGE_SIZE + 500] ^= 0xFF
    fixture.source.write_bytes(bytes(damaged))

    with pytest.raises(MirrorError, match="解密"):
        mirror.sync()

    assert fixture.mirror_path.read_bytes() == before


# ---------------------------------------------------------------------- WAL 夹具
def wal_checksum(data: bytes, s1: int = 0, s2: int = 0) -> tuple[int, int]:
    usable = len(data) - (len(data) % 8)
    words = struct.unpack(f"<{usable // 4}I", data[:usable])
    for index in range(0, len(words), 2):
        s1 = (s1 + words[index] + s2) & 0xFFFFFFFF
        s2 = (s2 + words[index + 1] + s1) & 0xFFFFFFFF
    return s1, s2


def build_wal(
    frames: list[dict[str, Any]],
    *,
    salt1: int = 0x11111111,
    salt2: int = 0x22222222,
) -> bytes:
    """按真实格式构造 WAL；``frames`` 每项支持 pgno/page/dbsize/salt/checksum。"""
    core = struct.pack(">6I", 0x377F0682, 3007000, PAGE_SIZE, 0, salt1, salt2)
    header_s1, header_s2 = wal_checksum(core)
    out = bytearray(core + struct.pack(">2I", header_s1, header_s2))
    s1, s2 = header_s1, header_s2
    for spec in frames:
        pgno = int(spec["pgno"])
        page = bytes(spec["page"])
        if len(page) != PAGE_SIZE:
            raise ValueError("WAL 帧页长度必须是 4096")
        dbsize = int(spec.get("dbsize", 0))
        frame_salt1 = int(spec.get("salt1", salt1))
        frame_salt2 = int(spec.get("salt2", salt2))
        s1, s2 = wal_checksum(struct.pack(">2I", pgno, dbsize), s1, s2)
        s1, s2 = wal_checksum(page, s1, s2)
        stored1, stored2 = spec.get("checksum", (s1, s2))
        out += struct.pack(
            ">6I", pgno, dbsize, frame_salt1, frame_salt2, stored1, stored2
        )
        out += page
    return bytes(out)


def frames_from_pages(pages: list[bytes], *, commit: bool = True) -> list[dict[str, Any]]:
    total = len(pages)
    return [
        {
            "pgno": index + 1,
            "page": page,
            "dbsize": total if commit and index == total - 1 else 0,
        }
        for index, page in enumerate(pages)
    ]


def write_wal(source: Path, payload: bytes) -> Path:
    wal_path = Path(f"{source}-wal")
    wal_path.write_bytes(payload)
    return wal_path


def make_updated_copy(plain: Path, dest: Path, row_id: int = 1) -> Path:
    """复制明文库并改一行，得到与主库页布局一致的“新版本”。"""
    shutil.copyfile(plain, dest)
    update_value(dest, row_id, "from-wal")
    return dest


# ---------------------------------------------------------------------- WAL 用例
def test_wal_parses_frames_and_replays_new_value(fixture: Fixture, tmp_path: Path) -> None:
    updated = make_updated_copy(fixture.plain, tmp_path / "updated.db")
    pages = encrypted_pages(updated)
    payload = build_wal(frames_from_pages(pages))
    write_wal(fixture.source, payload)

    image = read_wal(payload, KEY, db_salt=SALT)

    assert image.has_commit is True
    assert image.dbsize == fixture.total_pages
    assert len(image.frames) == fixture.total_pages
    assert image.frames[0].page.startswith(SQLITE_HEADER)
    assert image.header_salt == (0x11111111, 0x22222222)

    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    result = mirror.sync()

    assert result.wal_frames == fixture.total_pages
    assert dict(read_rows(fixture.mirror_path)) == dict(read_rows(updated))


def test_wal_replay_is_idempotent(fixture: Fixture, tmp_path: Path) -> None:
    updated = make_updated_copy(fixture.plain, tmp_path / "updated.db")
    write_wal(fixture.source, build_wal(frames_from_pages(encrypted_pages(updated))))

    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    first = mirror.sync()
    second = mirror.sync()

    assert first.wal_frames == second.wal_frames == fixture.total_pages
    assert second.pages_written == 0
    assert dict(read_rows(fixture.mirror_path)) == dict(read_rows(updated))


def test_wal_ignores_frames_with_wrong_salt(fixture: Fixture, tmp_path: Path) -> None:
    updated = make_updated_copy(fixture.plain, tmp_path / "updated.db")
    frames = frames_from_pages(encrypted_pages(updated))
    for frame in frames:
        frame["salt1"] = 0xAAAA1111
        frame["salt2"] = 0xBBBB2222
    write_wal(fixture.source, build_wal(frames))

    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    result = mirror.sync()

    assert read_wal(
        Path(f"{fixture.source}-wal").read_bytes(), KEY, db_salt=SALT
    ).frames == ()
    assert result.wal_frames == 0
    assert read_rows(fixture.mirror_path) == read_rows(fixture.plain)


def test_wal_ignores_frame_with_bad_checksum(fixture: Fixture, tmp_path: Path) -> None:
    updated = make_updated_copy(fixture.plain, tmp_path / "updated.db")
    pages = encrypted_pages(updated)
    assert len(pages) >= 3

    # 数据页校验和故意写坏；随后页 1 作为 commit 帧仍然有效。
    # 坏帧必须被跳过，所以数据页保持主库旧值。
    frames = [
        {"pgno": 2, "page": pages[1], "dbsize": 0, "checksum": (0, 0)},
        {
            "pgno": 1,
            "page": pages[0],
            "dbsize": len(pages),
        },
    ]
    write_wal(fixture.source, build_wal(frames))

    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    result = mirror.sync()

    assert result.wal_frames == 1
    assert dict(read_rows(fixture.mirror_path))[1].startswith("name-0001")


def test_wal_without_commit_frame_applies_nothing(fixture: Fixture, tmp_path: Path) -> None:
    updated = make_updated_copy(fixture.plain, tmp_path / "updated.db")
    payload = build_wal(frames_from_pages(encrypted_pages(updated), commit=False))
    write_wal(fixture.source, payload)

    image = read_wal(payload, KEY, db_salt=SALT)
    assert image.has_commit is False
    assert image.frames == ()

    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    result = mirror.sync()

    assert result.wal_frames == 0
    assert read_rows(fixture.mirror_path) == read_rows(fixture.plain)


def test_wal_reset_with_stale_frames_is_not_replayed(
    fixture: Fixture, tmp_path: Path
) -> None:
    """WAL 重置后只剩上一代 salt 的帧：一帧都不能应用。"""
    updated = make_updated_copy(fixture.plain, tmp_path / "updated.db")
    stale = frames_from_pages(encrypted_pages(updated))
    for frame in stale:
        frame["salt1"] = 0x01020304
        frame["salt2"] = 0x05060708
    write_wal(
        fixture.source,
        build_wal(stale, salt1=0x99999999, salt2=0xAAAAAAAA),
    )

    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    result = mirror.sync()

    assert result.wal_frames == 0
    assert read_rows(fixture.mirror_path) == read_rows(fixture.plain)
    assert dict(read_rows(fixture.mirror_path))[1].startswith("name-0001")


def test_wal_rejects_non_wal_header() -> None:
    with pytest.raises(WalError, match="magic"):
        read_wal(b"\x00" * WAL_HEADER_SIZE, KEY, db_salt=SALT)
    short = read_wal(b"", KEY, db_salt=SALT)
    assert short.frames == ()
    assert short.frame_count == 0


# ------------------------------------------------------------------ MirrorSet
def _write_encrypted(plain: Path, dest: Path) -> None:
    encrypt_db(plain, dest)


def _message_names() -> list[str]:
    return [
        "message_0.db",
        "message_1.db",
        "message_0_fts.db",
        "message_1_fts.db",
        "biz_message_9.db",
        "kvdb.db",
        "weclaw_0.db",
    ]


def test_mirror_set_syncs_contact_and_messages_only(
    fixture: Fixture, tmp_path: Path
) -> None:
    db_root = tmp_path / "db_storage"
    _write_encrypted(fixture.plain, db_root / "contact" / "contact.db")
    for name in _message_names():
        _write_encrypted(fixture.plain, db_root / "message" / name)

    keys = {"contact\\contact.db": KEY}
    keys.update({f"message/{name}": KEY for name in _message_names()})
    out_dir = tmp_path / "out"

    mirror_set = MirrorSet(db_root, keys, out_dir, refresh_seconds=0)
    mirror_set.sync()

    assert (out_dir / "contact" / "contact.db").is_file()
    assert (out_dir / "message" / "message_0.db").is_file()
    assert (out_dir / "message" / "message_1.db").is_file()
    for name in ("message_0_fts.db", "message_1_fts.db", "biz_message_9.db", "kvdb.db", "weclaw_0.db"):
        assert not (out_dir / "message" / name).exists(), name
    assert mirror_set.last_errors == {}


def test_mirror_set_refresh_throttle(fixture: Fixture, tmp_path: Path) -> None:
    db_root = tmp_path / "db_storage"
    source = db_root / "contact" / "contact.db"
    _write_encrypted(fixture.plain, source)
    out_dir = tmp_path / "out"
    now = [0.0]

    mirror_set = MirrorSet(
        db_root,
        {"contact/contact.db": KEY},
        out_dir,
        refresh_seconds=5.0,
        clock=lambda: now[0],
    )
    mirror_path = mirror_set.path_for("contact\\contact.db")
    assert dict(read_rows(mirror_path))[1].startswith("name-0001")

    update_value(fixture.plain, 1, "after-refresh")
    _write_encrypted(fixture.plain, source)

    now[0] = 4.9
    mirror_set.path_for("contact/contact.db")
    assert dict(read_rows(mirror_path))[1].startswith("name-0001")

    now[0] = 5.0
    mirror_set.path_for("contact/contact.db")
    assert dict(read_rows(mirror_path))[1] == "after-refresh"


def test_mirror_set_zero_refresh_always_syncs(fixture: Fixture, tmp_path: Path) -> None:
    db_root = tmp_path / "db_storage"
    source = db_root / "contact" / "contact.db"
    _write_encrypted(fixture.plain, source)
    out_dir = tmp_path / "out"
    mirror_set = MirrorSet(
        db_root,
        {"contact/contact.db": KEY},
        out_dir,
        refresh_seconds=0,
        clock=lambda: 0.0,
    )
    mirror_path = mirror_set.path_for("contact/contact.db")

    update_value(fixture.plain, 1, "always")
    _write_encrypted(fixture.plain, source)

    mirror_set.path_for("contact/contact.db")
    assert dict(read_rows(mirror_path))[1] == "always"


def test_mirror_set_isolates_single_library_failure(
    fixture: Fixture, tmp_path: Path
) -> None:
    db_root = tmp_path / "db_storage"
    _write_encrypted(fixture.plain, db_root / "contact" / "contact.db")
    _write_encrypted(fixture.plain, db_root / "message" / "message_0.db")
    _write_encrypted(fixture.plain, db_root / "message" / "message_1.db")

    damaged = bytearray((db_root / "message" / "message_0.db").read_bytes())
    damaged[PAGE_SIZE + 200] ^= 0xFF
    (db_root / "message" / "message_0.db").write_bytes(bytes(damaged))

    out_dir = tmp_path / "out"
    mirror_set = MirrorSet(
        db_root,
        {
            "contact/contact.db": KEY,
            "message/message_0.db": KEY,
            "message/message_1.db": KEY,
        },
        out_dir,
        refresh_seconds=0,
    )
    mirror_set.sync()

    assert (out_dir / "contact" / "contact.db").is_file()
    assert (out_dir / "message" / "message_1.db").is_file()
    assert not (out_dir / "message" / "message_0.db").exists()
    assert "message/message_0.db" in mirror_set.last_errors
    assert (out_dir / "contact" / "contact.db").read_bytes() != b""


def test_mirror_set_path_for_rejects_bad_paths(tmp_path: Path) -> None:
    mirror_set = MirrorSet(tmp_path, {}, tmp_path / "out")
    with pytest.raises(ValueError):
        mirror_set.path_for("../escape.db")
    with pytest.raises(ValueError):
        mirror_set.path_for("C:\\absolute\\x.db")
    with pytest.raises(MirrorError, match="密钥"):
        mirror_set.path_for("contact/contact.db")


# ------------------------------------------------------------------ 其它边界
def test_mirror_ignores_partial_tail(fixture: Fixture) -> None:
    """源文件尺寸不是 4096 的整数倍时只处理完整页，不报错。"""
    with fixture.source.open("ab") as out:
        out.write(b"\x01" * 123)

    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    result = mirror.sync()

    assert result.pages_written == fixture.total_pages
    assert fixture.mirror_path.stat().st_size == fixture.total_pages * PAGE_SIZE
    assert read_rows(fixture.mirror_path) == read_rows(fixture.plain)


def test_mirror_salt_change_forces_full_rebuild(fixture: Fixture) -> None:
    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    mirror.sync()

    new_salt = bytes(range(0x50, 0x60))
    encrypt_db(fixture.plain, fixture.source, salt=new_salt)
    result = mirror.sync()

    assert result.full is True
    assert result.changed is True
    assert result.pages_written == fixture.total_pages
    assert read_rows(fixture.mirror_path) == read_rows(fixture.plain)


def test_mirror_corrupt_state_forces_full_rebuild(fixture: Fixture) -> None:
    mirror = Mirror(fixture.source, KEY, fixture.mirror_path, fixture.state_path)
    mirror.sync()
    fixture.state_path.write_text("{not json", encoding="utf-8")

    result = mirror.sync()

    assert result.full is True
    assert result.pages_written == fixture.total_pages


def test_mirror_missing_source_raises(tmp_path: Path) -> None:
    mirror = Mirror(
        tmp_path / "missing.db",
        KEY,
        tmp_path / "mirror.db",
        tmp_path / "mirror.db.state.json",
    )
    with pytest.raises(MirrorError, match="无法读取源库"):
        mirror.sync()
