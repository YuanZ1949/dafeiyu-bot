"""微信表情缓存（AES-CBC + ``wxgf``/HEVC）的纯单测。

不依赖真实微信目录，也不依赖本机装了 ffmpeg：转码通过替换 ``_run`` 模拟。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from dafeiyu.gateway.wechat_bridge.media import emoticon
from dafeiyu.gateway.wechat_bridge.media.aes import aes_cbc_encrypt
from dafeiyu.gateway.wechat_bridge.media.emoticon import (
    decode_emoticon,
    decrypt_emoticon,
    is_emoticon_name,
    transcode,
    wxgf_hevc,
)
from dafeiyu.gateway.wechat_bridge.media.keygen import (
    derive_aes_key,
    derive_emoticon_key,
)

WXID = "wxid_testuser"
DWORD = 1721210299
KEY = derive_emoticon_key(DWORD, WXID)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
STREAM = b"\x00\x00\x00\x01\x40\x01" + bytes(range(64))


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def _wxgf(stream: bytes = STREAM) -> bytes:
    header = b"wxgf" + b"\x12" + b"\x00" * 17
    assert len(header) == 22
    return header + stream


def _encrypt(payload: bytes, key: bytes = KEY) -> bytes:
    return aes_cbc_encrypt(key, key, _pkcs7_pad(payload))


# ---------------------------------------------------------------- 密钥派生
def test_derive_emoticon_key_uses_raw_digest_and_suffix() -> None:
    expected = hashlib.md5(f"{DWORD}{WXID}EMOTICON".encode()).digest()[:16]

    assert derive_emoticon_key(DWORD, WXID) == expected
    assert len(derive_emoticon_key(DWORD, WXID)) == 16
    # 与图片 .dat 的密钥（ASCII hex）必须不同。
    assert derive_emoticon_key(DWORD, WXID) != derive_aes_key(DWORD, WXID)


# ---------------------------------------------------------------- 文件名
@pytest.mark.parametrize(
    "name, expected",
    [
        ("a" * 32, True),
        ("745C9C20F715400B40F5D679BD12EBDB", True),
        ("a" * 31, False),
        ("a" * 33, False),
        ("a" * 32 + ".dat", False),
        ("g" * 32, False),
        ("", False),
    ],
)
def test_is_emoticon_name(name: str, expected: bool) -> None:
    assert is_emoticon_name(name) is expected


# ---------------------------------------------------------------- 解密
def test_decrypt_emoticon_roundtrip() -> None:
    payload = _wxgf()

    assert decrypt_emoticon(_encrypt(payload), KEY) == payload


def test_decrypt_emoticon_rejects_wrong_key() -> None:
    encrypted = _encrypt(_wxgf(), key=b"0" * 16)

    assert decrypt_emoticon(encrypted, KEY) is None


@pytest.mark.parametrize("raw", [b"", b"\x00" * 15, b"\x00" * 17])
def test_decrypt_emoticon_rejects_bad_length(raw: bytes) -> None:
    assert decrypt_emoticon(raw, KEY) is None


def test_decrypt_emoticon_rejects_bad_key_size() -> None:
    assert decrypt_emoticon(_encrypt(_wxgf()), b"short") is None


def test_decrypt_emoticon_rejects_non_wxgf_plaintext() -> None:
    encrypted = _encrypt(b"plain text payload" * 4)

    assert decrypt_emoticon(encrypted, KEY) is None


# ---------------------------------------------------------------- wxgf
def test_wxgf_hevc_strips_header() -> None:
    assert wxgf_hevc(_wxgf()) == STREAM


def test_wxgf_hevc_requires_magic() -> None:
    assert wxgf_hevc(STREAM) is None


def test_wxgf_hevc_requires_start_code() -> None:
    assert wxgf_hevc(b"wxgf" + b"\x00" * 40) is None


# ---------------------------------------------------------------- 转码
def _patch_ffmpeg(monkeypatch: pytest.MonkeyPatch, result: bytes, *, frames: int):
    captured: dict[str, object] = {}

    def fake_run(args: list[str], timeout: float) -> SimpleNamespace:
        captured["args"] = args
        captured["timeout"] = timeout
        Path(args[-1]).write_bytes(result)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(emoticon, "_locate", lambda name: name)
    monkeypatch.setattr(emoticon, "_run", fake_run)
    monkeypatch.setattr(emoticon, "_frame_count", lambda *_a, **_k: frames)
    return captured


def test_transcode_single_frame_outputs_png(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_ffmpeg(monkeypatch, PNG, frames=1)

    assert transcode(STREAM) == PNG
    args = captured["args"]
    assert isinstance(args, list)
    assert str(args[-1]).endswith("out.png")
    assert "-frames:v" in args


def test_transcode_multi_frame_outputs_gif(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_ffmpeg(monkeypatch, GIF, frames=3)

    assert transcode(STREAM) == GIF
    args = captured["args"]
    assert isinstance(args, list)
    assert str(args[-1]).endswith("out.gif")
    assert "-loop" in args
    assert "-frames:v" not in args


def test_transcode_without_ffmpeg_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emoticon, "_locate", lambda _name: None)

    assert transcode(STREAM) is None


def test_transcode_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emoticon, "_locate", lambda name: name)
    monkeypatch.setattr(emoticon, "_frame_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        emoticon,
        "_run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stderr=b"boom"),
    )

    assert transcode(STREAM) is None


def test_transcode_rejects_non_image_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ffmpeg(monkeypatch, b"not an image", frames=1)

    assert transcode(STREAM) is None


# ---------------------------------------------------------------- 端到端
def test_decode_emoticon_passes_hevc_stream_to_transcode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bytes] = []
    monkeypatch.setattr(
        emoticon, "transcode", lambda stream, **_kwargs: seen.append(stream) or PNG
    )

    assert decode_emoticon(_encrypt(_wxgf()), KEY) == PNG
    assert seen == [STREAM]


def test_decode_emoticon_wrong_key_never_transcodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("解密失败时不应调用 ffmpeg")

    monkeypatch.setattr(emoticon, "transcode", boom)

    assert decode_emoticon(_encrypt(_wxgf()), b"0" * 16) is None
