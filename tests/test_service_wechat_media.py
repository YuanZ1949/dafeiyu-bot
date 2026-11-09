"""``Service`` 接微信图片解码的接线测试（不启整套服务）。"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from dafeiyu.gateway.base import GatewayError
from dafeiyu.gateway.wechat_bridge.media.aes import aes_ecb_encrypt
from dafeiyu.gateway.wechat_bridge.media.keygen import MediaKey
from dafeiyu.models import Attachment, ChatType
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
    return _v2(HEAD)


def _v2(head: bytes) -> bytes:
    header = (
        SIGNATURE
        + len(head).to_bytes(4, "little")
        + len(TAIL).to_bytes(4, "little")
        + b"\x00"
    )
    return header + aes_ecb_encrypt(AES_KEY, _pkcs7_pad(head)) + bytes(
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


def test_wxgf_original_is_transcoded_to_image(monkeypatch) -> None:
    """原图容器解不开时不能只留占位：wxgf 里的 HEVC 要转成 PNG。"""
    service = _service(StubResolver(MediaKey(AES_KEY, XOR_KEY)))
    wxgf = b"wxgf" + bytes(28) + b"\x00\x00\x00\x01\x40\x01" + bytes(32)
    seen: list[bytes] = []

    def fake_to_image(payload: bytes, **_kwargs) -> bytes:
        seen.append(payload)
        return PNG

    monkeypatch.setattr(
        "dafeiyu.gateway.wechat_bridge.media.emoticon.wxgf_to_image",
        fake_to_image,
    )

    assert _decode(service, _v2(wxgf)) == PNG
    assert seen == [wxgf + TAIL]


def test_non_wxgf_container_falls_back_to_emoticon_path(monkeypatch) -> None:
    """容器不是 wxgf 时不要把随机明文当图片返回。"""
    resolver = StubResolver(MediaKey(AES_KEY, XOR_KEY))
    resolver.emoticon = b"0123456789abcdef"
    service = _service(resolver)
    calls: list[bytes] = []

    def fake_to_image(payload: bytes, **_kwargs) -> None:
        calls.append(payload)
        return None

    monkeypatch.setattr(
        "dafeiyu.gateway.wechat_bridge.media.emoticon.wxgf_to_image",
        fake_to_image,
    )
    monkeypatch.setattr(
        "dafeiyu.gateway.wechat_bridge.media.emoticon.decode_emoticon",
        lambda *_args, **_kwargs: None,
    )

    assert _decode(service, _v2(b"not-an-image" * 4)) is None
    assert len(calls) == 1


# ---- 出站文件：微信没有消息段，走 upload_file -> send_media(kind=file) -------
class _WeChatFileAdapter:
    """微信风格的假适配器：无 send_segments，有 upload_file。"""

    bot_id = "wxg:999"

    def __init__(self, upload_error: Exception | None = None) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.uploads: list[tuple[ChatType, str, str, str]] = []
        self._upload_error = upload_error

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_type, chat_id, text))
        return "plain"

    async def upload_file(
        self, chat_type: ChatType, chat_id: str, path: str, *, name: str = ""
    ) -> None:
        if self._upload_error is not None:
            raise self._upload_error
        self.uploads.append((chat_type, chat_id, path, name))


def _wechat_service(adapter: _WeChatFileAdapter) -> Service:
    service = object.__new__(Service)
    setattr(service, "adapters", {"wechat": adapter})
    return service


async def test_wechat_file_attachment_calls_upload_file() -> None:
    adapter = _WeChatFileAdapter()
    service = _wechat_service(adapter)

    result = await service._send_chunk(
        ChatType.GROUP,
        "wxg:abc123",
        "文件在这",
        attachments=(
            Attachment(kind="file", path="C:/ws/replies/a.pdf", name="报表.pdf"),
        ),
    )

    assert result == "plain"
    assert adapter.uploads == [
        (ChatType.GROUP, "wxg:abc123", "C:/ws/replies/a.pdf", "报表.pdf")
    ]
    assert adapter.sent == [(ChatType.GROUP, "wxg:abc123", "文件在这")]


async def test_wechat_upload_failure_still_sends_text() -> None:
    adapter = _WeChatFileAdapter(upload_error=GatewayError("微信文件发送失败"))
    service = _wechat_service(adapter)

    result = await service._send_chunk(
        ChatType.GROUP,
        "wxg:abc123",
        "文件在这",
        attachments=(Attachment(kind="file", path="C:/ws/replies/a.pdf"),),
    )

    assert result == "plain"
    assert adapter.uploads == []
    assert adapter.sent == [(ChatType.GROUP, "wxg:abc123", "文件在这")]
