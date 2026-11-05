"""微信图片 AES key 发现（``cfgDword``）的纯单测。

全部通过注入的假进程内存完成：不读真实微信进程、不碰真实账号目录。
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from dafeiyu.gateway.wechat_bridge.media.aes import aes_ecb_encrypt
from dafeiyu.gateway.wechat_bridge.media.dat import decode_dat
from dafeiyu.gateway.wechat_bridge.media.keygen import (
    MediaKey,
    MediaKeyResolver,
    derive_aes_key,
    derive_emoticon_key,
    guess_xor_key,
)

SIGNATURE = b"\x07\x08V2\x08\x07"
WXID = "wxid_testuser"
DWORD = 1721210299  # 0x669795BB，真实样本里实测到的值
XOR_KEY = DWORD & 0xFF  # 0xBB
# 32 字节 JPEG 头（末字节 0x1b，不会被当成 PKCS7 填充）。
HEAD = b"\xff\xd8\xff\xe0" + bytes(range(28))
TAIL = b"\x00" * 8 + b"\xff\xd9"


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def _build_v2(
    *,
    aes_key: bytes,
    xor_key: int = XOR_KEY,
    head: bytes = HEAD,
    tail: bytes = TAIL,
) -> bytes:
    header = (
        SIGNATURE
        + len(head).to_bytes(4, "little")
        + len(tail).to_bytes(4, "little")
        + b"\x00"
    )
    cipher = aes_ecb_encrypt(aes_key, _pkcs7_pad(head))
    return header + cipher + bytes(byte ^ xor_key for byte in tail)


SAMPLE = _build_v2(aes_key=derive_aes_key(DWORD, WXID))


class FakeMemory:
    """最小只读进程内存替身：``iter_chunks`` + ``close``。"""

    def __init__(self, *blobs: bytes) -> None:
        self.blobs = list(blobs)
        self.closed = False

    def iter_chunks(self, chunk: int = 1 << 20, overlap: int = 0):
        step = max(1, chunk - overlap)
        for blob in self.blobs:
            position = 0
            while position < len(blob):
                end = min(position + chunk, len(blob))
                yield 0, blob[position:end]
                if end >= len(blob):
                    break
                position += step

    def close(self) -> None:
        self.closed = True


def _memory_with(dword: int, *, wxid: str = WXID) -> FakeMemory:
    # dword 放在偏移 0（对齐），尾巴放 wxid 供进程预筛。
    return FakeMemory(struct.pack("<I", dword) + b"\x00" * 12 + wxid.encode())


def _resolver(
    tmp_path: Path,
    *,
    sample: bytes = SAMPLE,
    memory: FakeMemory | None = None,
    pids=(123,),
    **kwargs,
) -> MediaKeyResolver:
    memory = memory if memory is not None else _memory_with(DWORD)

    def factory(_pid: int) -> FakeMemory:
        return memory

    return MediaKeyResolver(
        wxid=WXID,
        cache_path=tmp_path / "media_key.json",
        pids=lambda: list(pids),
        memory_factory=factory,
        clock=lambda: 1000.0,
        scan_seconds=30.0,
        retry_seconds=300.0,
        **kwargs,
    )


# ---------------------------------------------------------------- 派生/反推
def test_derive_aes_key_matches_wechat_rule() -> None:
    expected = hashlib.md5(f"{DWORD}{WXID}".encode()).hexdigest()[:16].encode()

    assert derive_aes_key(DWORD, WXID) == expected
    assert len(derive_aes_key(DWORD, WXID)) == 16


def test_guess_xor_key_from_jpeg_tail() -> None:
    assert guess_xor_key(SAMPLE) == XOR_KEY


def test_guess_xor_key_from_png_iend_tail() -> None:
    iend = b"IEND\xaeB`\x82"
    sample = _build_v2(aes_key=derive_aes_key(DWORD, WXID), tail=iend)

    assert guess_xor_key(sample) == XOR_KEY


def test_guess_xor_key_without_xor_section_is_zero() -> None:
    sample = _build_v2(aes_key=derive_aes_key(DWORD, WXID), tail=b"")
    # 重建头部：xor_size = 0
    sample = bytearray(sample)
    sample[10:14] = (0).to_bytes(4, "little")

    assert guess_xor_key(bytes(sample)) == 0


def test_guess_xor_key_rejects_unknown_tail() -> None:
    sample = _build_v2(
        aes_key=derive_aes_key(DWORD, WXID), tail=b"\x00" * 16 + b"\x01\x02"
    )

    assert guess_xor_key(sample) is None


def test_guess_xor_key_rejects_too_short() -> None:
    assert guess_xor_key(b"\x07\x08V2") is None


# ---------------------------------------------------------------- 解析
def test_cached_returns_none_without_scanning(tmp_path: Path) -> None:
    def boom() -> list[int]:
        raise AssertionError("cached() 不允许扫描进程")

    resolver = MediaKeyResolver(
        wxid=WXID,
        cache_path=tmp_path / "missing.json",
        pids=boom,
        memory_factory=lambda _pid: FakeMemory(),
    )

    assert resolver.cached(SAMPLE) is None


def test_resolve_scans_memory_and_finds_dword(tmp_path: Path) -> None:
    memory = _memory_with(DWORD)
    resolver = _resolver(tmp_path, memory=memory)

    key = resolver.resolve(SAMPLE)

    assert key == MediaKey(derive_aes_key(DWORD, WXID), XOR_KEY)
    assert memory.closed is True
    payload = json.loads((tmp_path / "media_key.json").read_text(encoding="utf-8"))
    assert payload["dword"] == DWORD
    assert payload["xor_key"] == XOR_KEY
    assert payload["wxid"] == WXID


def test_cached_reuses_saved_key_without_rescan(tmp_path: Path) -> None:
    _resolver(tmp_path).resolve(SAMPLE)

    def boom() -> list[int]:
        raise AssertionError("命中缓存后不应再扫进程")

    resolver = MediaKeyResolver(
        wxid=WXID,
        cache_path=tmp_path / "media_key.json",
        pids=boom,
        memory_factory=lambda _pid: boom(),
        clock=lambda: 2000.0,
    )

    assert resolver.cached(SAMPLE) == MediaKey(derive_aes_key(DWORD, WXID), XOR_KEY)


def test_stale_cache_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "media_key.json"
    cache.write_text(
        json.dumps(
            {"dword": DWORD + 1, "xor_key": XOR_KEY, "wxid": WXID}
        ),
        encoding="utf-8",
    )
    resolver = _resolver(tmp_path)

    # 缓存里的 dword 解不出图片：回退到扫描，并命中真实 dword。
    assert resolver.cached(SAMPLE) is None
    assert resolver.resolve(SAMPLE) == MediaKey(derive_aes_key(DWORD, WXID), XOR_KEY)


def test_wrong_candidate_is_rejected(tmp_path: Path) -> None:
    # 低字节同样是 0xBB，但派生出的 key 解不出强图片头。
    decoy = DWORD ^ 0x00010000
    resolver = _resolver(tmp_path, memory=_memory_with(decoy), pids=(123,))

    assert resolver.resolve(SAMPLE) is None


def test_stale_key_is_dropped_and_rescanned(tmp_path: Path) -> None:
    """微信在机器人运行期间重启换 key：不能一直拿旧 key 解出占位。"""
    current = {"dword": DWORD}

    def factory(_pid: int) -> FakeMemory:
        return _memory_with(current["dword"])

    resolver = MediaKeyResolver(
        wxid=WXID,
        cache_path=tmp_path / "media_key.json",
        pids=lambda: [123],
        memory_factory=factory,
        clock=lambda: 1000.0,
    )
    assert resolver.resolve(SAMPLE) == MediaKey(derive_aes_key(DWORD, WXID), XOR_KEY)

    # 模拟微信重启后换了 cfgDword（低字节仍相同，只有扫描才能发现）。
    new_dword = DWORD + 0x00010000
    new_sample = _build_v2(aes_key=derive_aes_key(new_dword, WXID))
    assert resolver.cached(new_sample) is None

    current["dword"] = new_dword
    assert resolver.resolve(new_sample) == MediaKey(
        derive_aes_key(new_dword, WXID), XOR_KEY
    )


def test_failure_cools_down_and_does_not_rescan(tmp_path: Path) -> None:
    calls = [0]

    def factory(_pid: int) -> FakeMemory:
        calls[0] += 1
        return FakeMemory(b"\x00" * 64)

    now = [1000.0]
    resolver = MediaKeyResolver(
        wxid=WXID,
        cache_path=tmp_path / "media_key.json",
        pids=lambda: [123],
        memory_factory=factory,
        clock=lambda: now[0],
        retry_seconds=300.0,
    )

    assert resolver.resolve(SAMPLE) is None
    assert resolver.resolve(SAMPLE) is None
    assert calls[0] == 1

    now[0] += 301.0
    assert resolver.resolve(SAMPLE) is None
    assert calls[0] == 2


def test_process_without_wxid_is_skipped(tmp_path: Path) -> None:
    memory = FakeMemory(struct.pack("<I", DWORD))
    resolver = _resolver(tmp_path, memory=memory)

    assert resolver.resolve(SAMPLE) is None
    # 预筛没找到 wxid 就提前返回，不进入逐字扫描。
    assert memory.closed is True


def test_non_v2_sample_is_ignored(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)

    assert resolver.resolve(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32) is None
    assert resolver.cached(b"not a dat") is None


def test_without_wxid_returns_none(tmp_path: Path) -> None:
    resolver = MediaKeyResolver(
        wxid="",
        db_root=tmp_path / "no-account" / "db_storage",
        cache_path=tmp_path / "media_key.json",
        pids=lambda: [123],
        memory_factory=lambda _pid: _memory_with(DWORD),
    )

    assert resolver.resolve(SAMPLE) is None


# ---------------------------------------------------------------- 表情密钥
def test_emoticon_key_reads_dword_from_cache_without_sample(tmp_path: Path) -> None:
    (tmp_path / "media_key.json").write_text(
        json.dumps({"dword": DWORD, "xor_key": XOR_KEY, "wxid": WXID}),
        encoding="utf-8",
    )
    resolver = MediaKeyResolver(
        wxid=WXID,
        cache_path=tmp_path / "media_key.json",
        pids=lambda: [],
        memory_factory=lambda _pid: FakeMemory(),
    )

    assert resolver.emoticon_key() == derive_emoticon_key(DWORD, WXID)
    assert resolver.dword == DWORD


def test_emoticon_key_none_without_cache(tmp_path: Path) -> None:
    resolver = MediaKeyResolver(
        wxid=WXID,
        cache_path=tmp_path / "missing.json",
        pids=lambda: [],
        memory_factory=lambda _pid: FakeMemory(),
    )

    assert resolver.emoticon_key() is None
    assert resolver.dword == 0


def test_emoticon_key_ignores_cache_for_other_wxid(tmp_path: Path) -> None:
    (tmp_path / "media_key.json").write_text(
        json.dumps({"dword": DWORD, "xor_key": XOR_KEY, "wxid": "wxid_other"}),
        encoding="utf-8",
    )
    resolver = MediaKeyResolver(
        wxid=WXID,
        cache_path=tmp_path / "media_key.json",
        pids=lambda: [],
        memory_factory=lambda _pid: FakeMemory(),
    )

    assert resolver.emoticon_key() is None


def test_prewarm_picks_attach_sample_and_resolves(tmp_path: Path) -> None:
    account = tmp_path / "wxid_testuser_1234"
    db_root = account / "db_storage"
    img = account / "msg" / "attach" / "abc" / "2026-11" / "Img"
    img.mkdir(parents=True)
    (img / "deadbeef_t.dat").write_bytes(SAMPLE)

    resolver = MediaKeyResolver(
        db_root=db_root,
        cache_path=tmp_path / "media_key.json",
        pids=lambda: [123],
        memory_factory=lambda _pid: _memory_with(DWORD),
        clock=lambda: 1000.0,
    )

    assert resolver.prewarm() is True
    assert resolver.key == MediaKey(derive_aes_key(DWORD, WXID), XOR_KEY)


def test_prewarm_without_attach_returns_false(tmp_path: Path) -> None:
    account = tmp_path / "wxid_testuser_1234"
    resolver = MediaKeyResolver(
        db_root=account / "db_storage",
        cache_path=tmp_path / "media_key.json",
        pids=lambda: [123],
        memory_factory=lambda _pid: _memory_with(DWORD),
    )

    assert resolver.prewarm() is False


# ---------------------------------------------------------------- 解码闭环
def test_resolved_key_decodes_real_layout(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)
    key = resolver.resolve(SAMPLE)

    assert key is not None
    assert decode_dat(SAMPLE, aes_key=key.aes_key, xor_key=key.xor_key) == (
        HEAD + TAIL
    )


@pytest.mark.parametrize("xor_key", [0x37, 0x38, 0x39, 0xBB])
def test_guess_xor_key_round_trips_other_keys(xor_key: int) -> None:
    sample = _build_v2(
        aes_key=derive_aes_key(DWORD, WXID),
        xor_key=xor_key,
        tail=b"\x00" * 16 + b"\xff\xd9",
    )

    assert guess_xor_key(sample) == xor_key
