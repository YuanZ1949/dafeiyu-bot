"""``Service`` 接微信图片解码的接线测试（不启整套服务）。"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from dafeiyu.gateway.wechat_bridge.media.aes import aes_ecb_encrypt
from dafeiyu.gateway.wechat_bridge.media.keygen import MediaKey
from dafeiyu.service import Service

SIGNATURE = b"\x07\x08V2\x08\x07"
AES_KEY = b"0123456789abcdef"
XOR_KEY = 0x5A
HEAD = b"\xff\xd8\xff\xe0" + bytes(range(28))
TAIL = bytes(range(32, 64))
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def _sample() -> bytes:
    header = (
        SIGNATURE
        + len(HEAD).to_bytes(4, "little")
        + len(TAIL).to_bytes(4, "little")
        + b"\x00"
    )
    return header + aes_ecb_encrypt(AES_KEY, _pkcs7_pad(HEAD)) + bytes(
        byte ^ XOR_KEY for byte in TAIL
    )


class StubResolver:
    def __init__(self, key: MediaKey | None) -> None:
        self.key = key
        self.samples: list[bytes] = []
        self.scanned = threading.Event()
        self.prewarmed = threading.Event()
        self.emoticon: bytes | None = None

    def cached(self, sample: bytes) -> MediaKey | None:
        return self.key

    def resolve(self, sample: bytes) -> MediaKey | None:
        self.samples.append(sample)
        self.scanned.set()
        return self.key

    def emoticon_key(self) -> bytes | None:
        return self.emoticon

    def prewarm(self) -> bool:
        self.prewarmed.set()
        return False


def _service(resolver: StubResolver | None, *, enabled: bool = True) -> Service:
    service = object.__new__(Service)
    setattr(
        service,
        "settings",
        SimpleNamespace(
            wechat=SimpleNamespace(
                media=SimpleNamespace(enabled=enabled, storage_root="")
            )
        ),
    )
    setattr(service, "_media_resolver", resolver)
    setattr(service, "_media_resolver_ready", True)
    setattr(service, "_media_resolver_lock", threading.Lock())
    setattr(service, "_media_scan_lock", threading.Lock())
    return service


def test_media_disabled_falls_back_to_legacy_decode() -> None:
    service = _service(None, enabled=False)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    encoded = bytes(byte ^ 0x37 for byte in png)

    assert _decode(service, encoded) == png
    assert _decode(service, _sample()) is None


def _decode(service: Service, data: bytes) -> bytes | None:
    return asyncio.run(service._decode_wechat_dat(data))


def test_cached_key_decodes_v2() -> None:
    service = _service(StubResolver(MediaKey(AES_KEY, XOR_KEY)))

    assert _decode(service, _sample()) == HEAD + TAIL


def test_miss_kicks_background_scan_and_keeps_placeholder() -> None:
    resolver = StubResolver(None)
    service = _service(resolver)
    sample = _sample()

    assert _decode(service, sample) is None
    assert resolver.scanned.wait(2.0) is True
    assert resolver.samples == [sample]


def test_miss_without_resolver_does_not_raise() -> None:
    service = _service(None)

    assert _decode(service, _sample()) is None


def test_emoticon_key_runs_cbc_decode(monkeypatch) -> None:
    resolver = StubResolver(MediaKey(AES_KEY, XOR_KEY))
    resolver.emoticon = b"0123456789abcdef"
    service = _service(resolver)
    calls: list[tuple[bytes, bytes]] = []

    def fake_decode(data: bytes, key: bytes, **_kwargs) -> bytes:
        calls.append((data, key))
        return PNG

    monkeypatch.setattr(
        "dafeiyu.gateway.wechat_bridge.media.emoticon.decode_emoticon", fake_decode
    )

    assert _decode(service, b"\x00" * 32) == PNG
    assert calls == [(b"\x00" * 32, resolver.emoticon)]


def test_emoticon_miss_kicks_prewarm() -> None:
    resolver = StubResolver(MediaKey(AES_KEY, XOR_KEY))
    service = _service(resolver)

    assert _decode(service, b"\x00" * 16) is None
    assert resolver.prewarmed.wait(2.0) is True
