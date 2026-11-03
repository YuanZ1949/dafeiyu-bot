"""`localdb.keyscan` 的离线单测。

内存扫描用注入的假 reader，HMAC 校验用注入的假 verifier——不接触真实
`Weixin.exe` 进程，也不依赖尚未就绪的 `sqlcipher.py`。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from dafeiyu.gateway.wechat_bridge.localdb import keyscan

PAGE_SIZE = keyscan.PAGE_SIZE
KEY = bytes(range(32))
KEY2 = bytes(range(1, 33))
SALT_A = bytes.fromhex("00112233445566778899aabbccddeeff")
SALT_B = bytes.fromhex("ffeeddccbbaa99887766554433221100")
SALT_C = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")


def _page(salt: bytes) -> bytes:
    """构造一页假 page1：前 16 字节 salt，其余补零（校验由假 verifier 负责）。"""
    assert len(salt) == 16
    return salt + bytes(PAGE_SIZE - 16)


def _literal(enc_key: bytes, salt: bytes | None = None, *, upper: bool = True) -> bytes:
    marker = b"X'" if upper else b"x'"
    payload = enc_key.hex().encode("ascii")
    if salt is not None:
        payload += salt.hex().encode("ascii")
    return marker + payload + b"'"


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


def test_candidates_key_with_embedded_salt() -> None:
    blob = b"noise" + _literal(KEY, SALT_A) + b"tail"
    assert list(keyscan.candidates(blob)) == [(KEY, SALT_A)]


def test_candidates_key_only_lowercase_marker() -> None:
    blob = _literal(KEY, upper=False)
    assert list(keyscan.candidates(blob)) == [(KEY, None)]


def test_candidates_rejects_short_hex() -> None:
    blob = b"X'" + b"ab" * 30 + b"'"
    assert list(keyscan.candidates(blob)) == []


def test_candidates_accepts_mixed_case_hex() -> None:
    mixed = KEY.hex().upper()[:32] + KEY.hex().lower()[32:]
    blob = b"X'" + mixed.encode("ascii") + b"'"
    assert list(keyscan.candidates(blob)) == [(KEY, None)]


def test_candidates_yields_multiple_in_order() -> None:
    blob = _literal(KEY, SALT_A) + b"gap" + _literal(KEY2, upper=False)
    assert list(keyscan.candidates(blob)) == [(KEY, SALT_A), (KEY2, None)]


def test_candidates_skips_invalid_odd_length_salt() -> None:
    # 65 个 hex：key 合法，剩余 1 个字符拼不成 salt。
    blob = b"X'" + (KEY.hex() + "a").encode("ascii") + b"'"
    assert list(keyscan.candidates(blob)) == [(KEY, None)]


# ---------------------------------------------------------------------------
# 分块 + scan_pids
# ---------------------------------------------------------------------------


def test_iter_region_chunks_keeps_overlap() -> None:
    literal = _literal(KEY, SALT_A)
    memory = b"\x00" * 100 + literal + b"\x00" * 100
    reads: list[tuple[int, int]] = []

    def read(address: int, length: int) -> bytes:
        reads.append((address, length))
        return memory[address : address + length]

    chunks = list(
        keyscan._iter_region_chunks(read, 0, len(memory), chunk=120, overlap=0x40)
    )
    assert any(literal in data for _address, data in chunks)
    assert len(reads) > 1
    # 除第一块外，每块都带上一块尾巴，候选不会被切断；地址同步左移。
    assert chunks[1] == (120 - 0x40, memory[120 - 0x40 : 240])
    assert all(
        memory[address : address + len(data)] == data for address, data in chunks
    )


def test_scan_pids_reassembles_candidate_across_chunks() -> None:
    literal = _literal(KEY, SALT_A)
    cut = 40  # 切在 hex 中间

    def reader(pid: int):
        assert pid == 4321
        yield b"\x00" * 16 + literal[:cut]
        yield literal[cut:] + b"\x00" * 16

    def verifier(enc_key: bytes, page1: bytes) -> bool:
        return enc_key == KEY and page1[:16] == SALT_A

    result = keyscan.scan_pids(
        [4321], {SALT_A.hex(): _page(SALT_A)}, reader=reader, verifier=verifier
    )
    assert result == {SALT_A.hex(): KEY}


def test_scan_pids_only_keeps_verified_key() -> None:
    good = _literal(KEY)
    bad = _literal(KEY2)

    def reader(pid: int):
        yield good + b"sep" + bad

    def verifier(enc_key: bytes, page1: bytes) -> bool:
        return enc_key == KEY

    result = keyscan.scan_pids(
        [1], {SALT_A.hex(): _page(SALT_A)}, reader=reader, verifier=verifier
    )
    assert result == {SALT_A.hex(): KEY}


def test_scan_pids_shared_key_matches_all_salts_without_rescanning() -> None:
    calls: list[tuple[bytes, bytes]] = []

    def reader(pid: int):
        # 同一个 key 连续出现两次；没有内嵌 salt，必须对两个 salt 各校验一次。
        yield _literal(KEY) + b"..." + _literal(KEY)

    def verifier(enc_key: bytes, page1: bytes) -> bool:
        calls.append((enc_key, page1[:16]))
        return enc_key == KEY

    pages = {SALT_A.hex(): _page(SALT_A), SALT_B.hex(): _page(SALT_B)}
    result = keyscan.scan_pids([1], pages, reader=reader, verifier=verifier)
    assert result == {SALT_A.hex(): KEY, SALT_B.hex(): KEY}
    # 两个 salt 各一次；重复候选不会触发额外校验。
    assert len(calls) == 2
    assert {salt for _key, salt in calls} == {SALT_A, SALT_B}


def test_scan_pids_embedded_salt_limits_verification() -> None:
    calls: list[bytes] = []

    def reader(pid: int):
        yield _literal(KEY, SALT_B)

    def verifier(enc_key: bytes, page1: bytes) -> bool:
        calls.append(page1[:16])
        return page1[:16] == SALT_B

    pages = {SALT_A.hex(): _page(SALT_A), SALT_B.hex(): _page(SALT_B)}
    result = keyscan.scan_pids([1], pages, reader=reader, verifier=verifier)
    assert result == {SALT_B.hex(): KEY}
    assert calls == [SALT_B]


def test_scan_pids_continues_after_failing_pid() -> None:
    def reader(pid: int):
        if pid == 1:
            raise keyscan.KeyScanError("无法打开进程")
        yield _literal(KEY)

    def verifier(enc_key: bytes, page1: bytes) -> bool:
        return enc_key == KEY

    result = keyscan.scan_pids(
        [1, 2], {SALT_A.hex(): _page(SALT_A)}, reader=reader, verifier=verifier
    )
    assert result == {SALT_A.hex(): KEY}


def test_scan_pids_failure_inside_generator_does_not_abort_next_pid() -> None:
    def reader(pid: int):
        if pid == 1:
            yield b"\x00" * 8
            raise OSError("读取中断")
        yield _literal(KEY)

    def verifier(enc_key: bytes, page1: bytes) -> bool:
        return enc_key == KEY

    result = keyscan.scan_pids(
        [1, 2], {SALT_A.hex(): _page(SALT_A)}, reader=reader, verifier=verifier
    )
    assert result == {SALT_A.hex(): KEY}


def test_scan_pids_all_fail_raises() -> None:
    def reader(pid: int):
        raise keyscan.KeyScanError(f"pid {pid} 不可读")

    with pytest.raises(keyscan.KeyScanError, match="未能从微信进程内存"):
        keyscan.scan_pids(
            [1, 2],
            {SALT_A.hex(): _page(SALT_A)},
            reader=reader,
            verifier=lambda key, page: True,
        )


def test_scan_pids_without_salts_raises() -> None:
    with pytest.raises(keyscan.KeyScanError):
        keyscan.scan_pids([1], {}, reader=lambda pid: iter(()), verifier=lambda k, p: True)


def test_scan_pids_no_match_raises() -> None:
    def reader(pid: int):
        yield _literal(KEY)

    with pytest.raises(keyscan.KeyScanError):
        keyscan.scan_pids(
            [1],
            {SALT_A.hex(): _page(SALT_A)},
            reader=reader,
            verifier=lambda key, page: False,
        )


# ---------------------------------------------------------------------------
# WCDB Config.Cipher 结构化扫描
# ---------------------------------------------------------------------------

_NEEDLE = keyscan._CONFIG_CIPHER_NAME
_BASE = 0x10000
_NEEDLE_ADDR = _BASE + 0x0000
_NODE_ADDR = _BASE + 0x0100
_CONFIG_ADDR = _BASE + 0x1000
_BLOB_ADDR = _BASE + 0x2000


class FakeProcessMemory:
    """按地址读取的假进程内存，行为对齐 :class:`keyscan.ProcessMemory`。"""

    def __init__(self, image: bytes, *, base: int = _BASE) -> None:
        self.base = base
        self.image = bytearray(image)
        self.regions = [(base, len(image))]
        self.closed = False
        self.reads: list[tuple[int, int]] = []

    def read(self, address: int, size: int) -> bytes:
        self.reads.append((address, size))
        start = address - self.base
        if start < 0 or start >= len(self.image) or size <= 0:
            return b""
        return bytes(self.image[start : start + size])

    def iter_chunks(self, *, chunk: int = 1 << 20, overlap: int = 0x80):
        offset = 0
        tail = b""
        while offset < len(self.image):
            want = min(chunk, len(self.image) - offset)
            data = self.read(self.base + offset, want)
            combined = tail + data
            yield self.base + offset - len(tail), combined
            tail = combined[-overlap:] if overlap > 0 else b""
            offset += want

    def close(self) -> None:
        self.closed = True


def _config_image(
    enc_key: bytes,
    salt: bytes | None = None,
    *,
    config_pointer: int | None = None,
    data_pointer: int | None = None,
    blob: bytes | None = None,
    needle: bool = True,
    mask_blob: bool = True,
) -> bytes:
    """构造含 ``Config.Cipher`` 对象链的内存镜像（字段布局与真实一致）。"""
    import struct

    image = bytearray(0x4000)
    if needle:
        image[_NEEDLE_ADDR - _BASE : _NEEDLE_ADDR - _BASE + len(_NEEDLE)] = _NEEDLE
    plain = blob if blob is not None else _literal(enc_key, salt)
    masked = (
        keyscan._xor_repeat(plain, keyscan._CONFIG_CIPHER_XOR_MASK)
        if mask_blob
        else plain
    )
    image[_BLOB_ADDR - _BASE : _BLOB_ADDR - _BASE + len(masked)] = masked

    node = _NODE_ADDR - _BASE
    struct.pack_into("<Q", image, node + 0x10, _NEEDLE_ADDR)
    struct.pack_into("<Q", image, node + 0x18, len(_NEEDLE))
    struct.pack_into(
        "<Q", image, node + 0x28, _CONFIG_ADDR if config_pointer is None else config_pointer
    )

    value = _CONFIG_ADDR - _BASE + keyscan._CONFIG_VALUE_OFFSET
    struct.pack_into(
        "<Q", image, value + 0x8, _BLOB_ADDR if data_pointer is None else data_pointer
    )
    struct.pack_into("<Q", image, value + 0x10, len(masked))
    return bytes(image)


def _structured_scan(
    image: bytes,
    pages: dict[str, bytes],
    *,
    verifier,
    pids: list[int] | None = None,
) -> dict[str, bytes]:
    memories: list[FakeProcessMemory] = []

    def opener(pid: int) -> FakeProcessMemory:
        memory = FakeProcessMemory(image)
        memories.append(memory)
        return memory

    result = keyscan.scan_pids(
        pids if pids is not None else [1],
        pages,
        opener=opener,
        verifier=verifier,
    )
    assert all(memory.closed for memory in memories)
    return result


def test_structured_scan_extracts_key_from_config_cipher() -> None:
    image = _config_image(KEY, SALT_A)
    result = _structured_scan(
        image,
        {SALT_A.hex(): _page(SALT_A)},
        verifier=lambda key, page: key == KEY and page[:16] == SALT_A,
    )
    assert result == {SALT_A.hex(): KEY}


def test_structured_scan_matches_shared_key_for_all_salts() -> None:
    image = _config_image(KEY)
    result = _structured_scan(
        image,
        {SALT_A.hex(): _page(SALT_A), SALT_B.hex(): _page(SALT_B)},
        verifier=lambda key, page: key == KEY,
    )
    assert result == {SALT_A.hex(): KEY, SALT_B.hex(): KEY}


def test_structured_scan_falls_back_to_literal_without_needle() -> None:
    # 没有 Config.Cipher 结构、但内存里存在明文 ``X'<hex>'`` 的旧构建。
    image = _config_image(KEY, salt=None, needle=False, mask_blob=False)
    result = _structured_scan(
        image,
        {SALT_A.hex(): _page(SALT_A)},
        verifier=lambda key, page: page[:16] == SALT_A,
    )
    assert result == {SALT_A.hex(): KEY}


def test_structured_scan_ignores_out_of_range_config_pointer() -> None:
    image = _config_image(KEY, SALT_A, config_pointer=0x7FFF_FFFF_FFFF)
    with pytest.raises(keyscan.KeyScanError, match="未能从微信进程内存"):
        _structured_scan(
            image,
            {SALT_A.hex(): _page(SALT_A)},
            verifier=lambda key, page: True,
        )


def test_structured_scan_ignores_out_of_range_blob_pointer() -> None:
    image = _config_image(KEY, SALT_A, data_pointer=0x1)
    with pytest.raises(keyscan.KeyScanError, match="未能从微信进程内存"):
        _structured_scan(
            image,
            {SALT_A.hex(): _page(SALT_A)},
            verifier=lambda key, page: True,
        )


def test_structured_scan_rejects_unverified_key() -> None:
    image = _config_image(KEY, SALT_A)
    with pytest.raises(keyscan.KeyScanError, match="未能从微信进程内存"):
        _structured_scan(
            image,
            {SALT_A.hex(): _page(SALT_A)},
            verifier=lambda key, page: False,
        )


def test_structured_scan_continues_after_unopenable_pid() -> None:
    image = _config_image(KEY, SALT_A)

    def opener(pid: int) -> FakeProcessMemory:
        if pid == 1:
            raise keyscan.KeyScanError("无法打开微信进程")
        return FakeProcessMemory(image)

    result = keyscan.scan_pids(
        [1, 2],
        {SALT_A.hex(): _page(SALT_A)},
        opener=opener,
        verifier=lambda key, page: key == KEY,
    )
    assert result == {SALT_A.hex(): KEY}


def test_structured_scan_matches_salt_embedded_in_blob_directly() -> None:
    calls: list[bytes] = []
    image = _config_image(KEY, SALT_B)
    result = _structured_scan(
        image,
        {SALT_A.hex(): _page(SALT_A), SALT_B.hex(): _page(SALT_B)},
        verifier=lambda key, page: calls.append(page[:16]) or True,
    )
    assert result == {SALT_B.hex(): KEY}
    assert calls == [SALT_B]


def test_config_blob_keys_rejects_oversized_and_degenerate_blobs() -> None:
    oversized = b"X'" + b"ab" * 600 + b"'"
    assert list(keyscan._config_blob_keys(oversized)) == []
    zeros = _literal(bytes(32))
    assert list(keyscan._config_blob_keys(zeros)) == []
    assert list(keyscan._config_blob_keys(b"")) == []


def test_probable_key_requires_full_length_and_entropy() -> None:
    assert keyscan._probable_key(KEY)
    assert not keyscan._probable_key(bytes(31))
    assert not keyscan._probable_key(bytes(32))
    assert not keyscan._probable_key(b"\xff" * 32)
    assert not keyscan._probable_key(b"\x00" * 31 + b"\x01")


# ---------------------------------------------------------------------------
# extract_keys
# ---------------------------------------------------------------------------


def test_extract_keys_maps_every_matching_db(tmp_path: Path) -> None:
    db_root = tmp_path / "db_storage"
    _write_db(db_root / "contact" / "contact.db", SALT_A)
    _write_db(db_root / "message" / "message.db", SALT_B)

    def reader(pid: int):
        yield _literal(KEY, SALT_A) + b"|" + _literal(KEY)

    def verifier(enc_key: bytes, page1: bytes) -> bool:
        return enc_key == KEY

    result = keyscan.extract_keys(
        db_root, pids=[99], reader=reader, verifier=verifier
    )
    assert result == {
        "contact/contact.db": KEY,
        "message/message.db": KEY,
    }


def test_extract_keys_without_databases_raises(tmp_path: Path) -> None:
    with pytest.raises(keyscan.KeyScanError):
        keyscan.extract_keys(
            tmp_path / "missing", pids=[1], reader=lambda pid: iter(()), verifier=lambda k, p: True
        )


# ---------------------------------------------------------------------------
# load_or_extract / 缓存
# ---------------------------------------------------------------------------


def _write_db(path: Path, salt: bytes, *, size: int = PAGE_SIZE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_page(salt) + b"\x00" * (size - PAGE_SIZE))


def _same_key(enc_key: bytes, page1: bytes) -> bool:
    return enc_key in (KEY, KEY2) and len(page1) == PAGE_SIZE


def test_load_or_extract_writes_cache_when_missing(tmp_path: Path) -> None:
    db_root = tmp_path / "wxid_abc_dead" / "db_storage"
    _write_db(db_root / "contact" / "contact.db", SALT_A)
    cache_path = tmp_path / "cache" / "nested" / "keys.json"
    calls: list[Path] = []

    def fake_extract(db_root_arg: Path, **kwargs: object) -> dict[str, bytes]:
        calls.append(Path(db_root_arg))
        return {"contact/contact.db": KEY}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(keyscan, "extract_keys", fake_extract)
    try:
        result = keyscan.load_or_extract(db_root, cache_path, verifier=_same_key)
    finally:
        monkeypatch.undo()

    assert result == {"contact/contact.db": KEY}
    assert calls == [db_root]
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["_db_dir"] == str(db_root)
    entry = payload["contact/contact.db"]
    assert entry["enc_key"] == KEY.hex()
    assert entry["salt"] == SALT_A.hex()
    assert entry["size_mb"] == round(PAGE_SIZE / 1024 / 1024, 2)


def test_load_or_extract_uses_valid_cache_without_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_root = tmp_path / "wxid_abc_dead" / "db_storage"
    _write_db(db_root / "contact" / "contact.db", SALT_A)
    cache_path = tmp_path / "keys.json"
    cache_path.write_text(
        json.dumps(
            {
                "contact\\contact.db": {
                    "enc_key": KEY.hex(),
                    "salt": SALT_A.hex(),
                    "size_mb": 0.0,
                },
                "_db_dir": str(db_root),
            }
        ),
        encoding="utf-8",
    )

    def exploding_extract(*args: object, **kwargs: object) -> dict[str, bytes]:
        raise AssertionError("缓存有效时不应该重新扫描")

    monkeypatch.setattr(keyscan, "extract_keys", exploding_extract)
    result = keyscan.load_or_extract(db_root, cache_path, verifier=_same_key)
    assert result == {"contact/contact.db": KEY}


def test_load_or_extract_re_extracts_when_salt_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_root = tmp_path / "wxid_abc_dead" / "db_storage"
    _write_db(db_root / "contact" / "contact.db", SALT_B)
    cache_path = tmp_path / "keys.json"
    cache_path.write_text(
        json.dumps(
            {
                "contact/contact.db": {
                    "enc_key": KEY.hex(),
                    "salt": SALT_A.hex(),  # 旧 salt
                }
            }
        ),
        encoding="utf-8",
    )
    calls: list[int] = []

    def fake_extract(db_root_arg: Path, **kwargs: object) -> dict[str, bytes]:
        calls.append(1)
        return {"contact/contact.db": KEY2}

    monkeypatch.setattr(keyscan, "extract_keys", fake_extract)
    result = keyscan.load_or_extract(db_root, cache_path, verifier=_same_key)

    assert calls == [1]
    assert result == {"contact/contact.db": KEY2}
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["contact/contact.db"]["salt"] == SALT_B.hex()
    assert payload["contact/contact.db"]["enc_key"] == KEY2.hex()


def test_load_or_extract_re_extracts_when_cache_hmac_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_root = tmp_path / "wxid_abc_dead" / "db_storage"
    _write_db(db_root / "contact" / "contact.db", SALT_A)
    cache_path = tmp_path / "keys.json"
    cache_path.write_text(
        json.dumps(
            {
                "contact/contact.db": {
                    "enc_key": KEY2.hex(),
                    "salt": SALT_A.hex(),
                }
            }
        ),
        encoding="utf-8",
    )
    calls: list[int] = []

    def fake_extract(db_root_arg: Path, **kwargs: object) -> dict[str, bytes]:
        calls.append(1)
        return {"contact/contact.db": KEY}

    def verifier(enc_key: bytes, page1: bytes) -> bool:
        return enc_key == KEY  # KEY2 通不过

    monkeypatch.setattr(keyscan, "extract_keys", fake_extract)
    result = keyscan.load_or_extract(db_root, cache_path, verifier=verifier)
    assert calls == [1]
    assert result == {"contact/contact.db": KEY}


def test_load_or_extract_refresh_ignores_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_root = tmp_path / "wxid_abc_dead" / "db_storage"
    _write_db(db_root / "contact" / "contact.db", SALT_A)
    cache_path = tmp_path / "keys.json"
    cache_path.write_text(
        json.dumps(
            {
                "contact/contact.db": {
                    "enc_key": KEY.hex(),
                    "salt": SALT_A.hex(),
                }
            }
        ),
        encoding="utf-8",
    )
    calls: list[int] = []

    def fake_extract(db_root_arg: Path, **kwargs: object) -> dict[str, bytes]:
        calls.append(1)
        return {"contact/contact.db": KEY}

    monkeypatch.setattr(keyscan, "extract_keys", fake_extract)
    result = keyscan.load_or_extract(
        db_root, cache_path, refresh=True, verifier=_same_key
    )
    assert calls == [1]
    assert result == {"contact/contact.db": KEY}


def test_load_or_extract_corrupt_cache_triggers_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_root = tmp_path / "wxid_abc_dead" / "db_storage"
    _write_db(db_root / "contact" / "contact.db", SALT_A)
    cache_path = tmp_path / "keys.json"
    cache_path.write_text("{not json", encoding="utf-8")
    calls: list[int] = []

    def fake_extract(db_root_arg: Path, **kwargs: object) -> dict[str, bytes]:
        calls.append(1)
        return {"contact/contact.db": KEY}

    monkeypatch.setattr(keyscan, "extract_keys", fake_extract)
    result = keyscan.load_or_extract(db_root, cache_path, verifier=_same_key)
    assert calls == [1]
    assert result == {"contact/contact.db": KEY}


# ---------------------------------------------------------------------------
# find_db_root / self_username / collect_pages
# ---------------------------------------------------------------------------


def _touch(path: Path, stamp: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (stamp, stamp))


def test_find_db_root_picks_most_recent_account(tmp_path: Path) -> None:
    base = tmp_path / "Documents" / "xwechat_files"
    older = base / "wxid_old_0001" / "db_storage"
    newer = base / "wxid_new_0002" / "db_storage"
    (older / "contact").mkdir(parents=True)
    (newer / "contact").mkdir(parents=True)
    now = time.time()
    _touch(older / "session" / "session.db", now - 3600)
    _touch(newer / "session" / "session.db", now)
    assert keyscan.find_db_root(tmp_path) == newer


def test_find_db_root_uses_wal_activity(tmp_path: Path) -> None:
    base = tmp_path / "Documents" / "xwechat_files"
    older = base / "wxid_old_0001" / "db_storage"
    newer = base / "wxid_new_0002" / "db_storage"
    (older / "contact").mkdir(parents=True)
    (newer / "contact").mkdir(parents=True)
    now = time.time()
    _touch(older / "session" / "session.db", now - 7200)
    _touch(newer / "session" / "session.db", now - 7200)
    _touch(newer / "contact" / "contact.db-wal", now)
    assert keyscan.find_db_root(tmp_path) == newer


def test_find_db_root_returns_none_without_dirs(tmp_path: Path) -> None:
    assert keyscan.find_db_root(tmp_path) is None


def test_self_username_parses_wxid(tmp_path: Path) -> None:
    db_root = tmp_path / "xwechat_files" / "wxid_abc123_dead" / "db_storage"
    db_root.mkdir(parents=True)
    assert keyscan.self_username(db_root) == "wxid_abc123"


def test_self_username_rejects_unexpected_name(tmp_path: Path) -> None:
    db_root = tmp_path / "xwechat_files" / "not-an-account" / "db_storage"
    db_root.mkdir(parents=True)
    assert keyscan.self_username(db_root) is None


def test_collect_pages_filters_and_collects(tmp_path: Path) -> None:
    root = tmp_path / "db_storage"
    _write_db(root / "contact" / "contact.db", SALT_A)
    _write_db(root / "message" / "nested" / "message.db", SALT_B)
    # 应该被跳过：
    (root / "contact" / "short.db").parent.mkdir(parents=True, exist_ok=True)
    (root / "contact" / "short.db").write_bytes(b"x" * 100)
    (root / "contact" / "contact.db-wal").write_bytes(_page(SALT_C))
    (root / "Backup" / "backup.db").parent.mkdir(parents=True, exist_ok=True)
    (root / "Backup" / "backup.db").write_bytes(_page(SALT_C))
    (root / "all_users" / "all.db").parent.mkdir(parents=True, exist_ok=True)
    (root / "all_users" / "all.db").write_bytes(_page(SALT_C))
    (root / "loose.db").write_bytes(_page(SALT_C))

    pages = keyscan.collect_pages(root)

    assert set(pages) == {"contact/contact.db", "message/nested/message.db"}
    assert all(len(page) == PAGE_SIZE for page in pages.values())
    assert keyscan.salts_of(pages) == {
        "contact/contact.db": SALT_A.hex(),
        "message/nested/message.db": SALT_B.hex(),
    }


def test_collect_pages_missing_root_returns_empty(tmp_path: Path) -> None:
    assert keyscan.collect_pages(tmp_path / "nope") == {}


# ---------------------------------------------------------------------------
# 非 Windows / 依赖缺失时的收敛
# ---------------------------------------------------------------------------


def test_read_process_memory_rejects_bad_parameters() -> None:
    if os.name != "nt":
        pytest.skip("Windows 专属")
    with pytest.raises(keyscan.KeyScanError):
        keyscan.read_process_memory(1, chunk=0)


def test_default_verifier_requires_sqlcipher(monkeypatch: pytest.MonkeyPatch) -> None:
    # 不真的调用 sqlcipher：把默认解析器替换成会抛错的桩，确认 scan_pids 用它。
    def missing(verifier: object) -> object:
        raise keyscan.KeyScanError("缺少 page1 HMAC 校验实现")

    monkeypatch.setattr(keyscan, "_resolve_verifier", missing)
    with pytest.raises(keyscan.KeyScanError):
        keyscan.scan_pids([1], {SALT_A.hex(): _page(SALT_A)}, reader=lambda pid: iter(()))
