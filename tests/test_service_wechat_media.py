"""``Service`` 接微信图片解码的接线测试（不启整套服务）。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

from dafeiyu.gateway.base import GatewayError
from dafeiyu.gateway.wechat_bridge.media.aes import aes_ecb_encrypt
from dafeiyu.gateway.wechat_bridge.media.keygen import MediaKey
from dafeiyu.models import Attachment, ChatType, Message
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


def test_wechat_dat_decode_runs_off_event_loop(monkeypatch) -> None:
    """``decode_dat`` 会整文件逐字节 XOR：必须在事件循环外的线程里跑。"""
    service = _service(None)
    decode_threads: list[int] = []

    def fake_decode_dat(data: bytes, **_kwargs) -> bytes | None:
        decode_threads.append(threading.get_ident())
        return None

    # _decode_wechat_dat 内部是局部 import，patch 模块属性即可生效。
    monkeypatch.setattr(
        "dafeiyu.gateway.wechat_bridge.media.dat.decode_dat", fake_decode_dat
    )

    loop_threads: list[int] = []

    async def run() -> bytes | None:
        loop_threads.append(threading.get_ident())
        return await service._decode_wechat_dat(b"\x00" * 16)

    assert asyncio.run(run()) is None
    assert len(decode_threads) == 1
    assert decode_threads[0] != loop_threads[0]


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


# ---- channel_status：只有微信形态补 desktop_required，QQ 键集合不变 --------
class _DesktopAdapter(_WeChatFileAdapter):
    """微信形态的假适配器：带 desktop / desktop_required。"""

    def __init__(self, *, required: bool = False) -> None:
        super().__init__()
        self.desktop = True
        self.desktop_required = required


class _PlainQQAdapter:
    """QQ 形态的假适配器：既没有 desktop 也没有 desktop_required。"""


def _adapters_service(adapters: dict[str, object]) -> Service:
    """沿用 ``_wechat_service`` 的组装方式，只注入 adapters。"""
    service = object.__new__(Service)
    setattr(service, "adapters", adapters)
    return service


def test_status_exposes_desktop_required_for_wechat_only() -> None:
    service = _adapters_service({"wechat": _DesktopAdapter(), "qq": _PlainQQAdapter()})

    status = service.channel_status()

    assert status["wechat"]["desktop"] is True
    assert status["wechat"]["desktop_required"] is False
    assert set(status["qq"]) == {
        "connected",
        "degraded",
        "last_event_at",
        "last_disconnect_at",
        "last_send_at",
    }
    # uia 后端的适配器要如实透出 True，不能写死 False。
    uia = _adapters_service({"wechat": _DesktopAdapter(required=True)})
    assert uia.channel_status()["wechat"]["desktop_required"] is True


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


# ---- 出站视频/贴纸：无 send_segments 的通道逐条 send_media，视频降级为文件 --
class _WeChatMediaAdapter(_WeChatFileAdapter):
    """加 send_media 的微信假适配器：仍无 send_segments（逐条媒体推送路径）。"""

    def __init__(
        self,
        outcomes: dict[str, object] | None = None,
        upload_error: Exception | None = None,
    ) -> None:
        super().__init__(upload_error)
        self._outcomes = outcomes or {}
        self.media_calls: list[tuple[str, str, str]] = []

    async def send_media(
        self, chat_type: ChatType, chat_id: str, kind: str, path: str, name: str = ""
    ) -> bool:
        self.media_calls.append((kind, path, name))
        outcome = self._outcomes.get(kind, True)
        if isinstance(outcome, Exception):
            raise outcome
        return bool(outcome)


async def test_wechat_video_attachment_pushes_as_file_not_video() -> None:
    """微信 hook 没有视频原语：视频必须按 kind=file 推送，绝不发 kind=video。"""
    adapter = _WeChatMediaAdapter()
    service = _wechat_service(adapter)

    result = await service._send_chunk(
        ChatType.GROUP,
        "wxg:abc123",
        "视频在这",
        attachments=(
            Attachment(kind="video", path="C:/ws/replies/a.mp4", name="剪辑.mp4"),
        ),
    )

    assert result == "plain"
    assert adapter.media_calls == [("file", "C:/ws/replies/a.mp4", "剪辑.mp4")]
    assert all(kind != "video" for kind, _path, _name in adapter.media_calls)
    assert adapter.sent == [(ChatType.GROUP, "wxg:abc123", "视频在这")]


async def test_wechat_sticker_tries_sticker_then_image_fallback() -> None:
    """贴纸仍先推真实 GIF（sticker → send_emoticon）；被拒降级 image 再试。"""
    adapter = _WeChatMediaAdapter({"sticker": False})
    service = _wechat_service(adapter)

    await service._send_chunk(
        ChatType.GROUP,
        "wxg:abc123",
        "看这个",
        attachments=(
            Attachment(kind="sticker", path="C:/ws/replies/x.gif", name="摸鱼"),
        ),
    )

    assert adapter.media_calls == [
        ("sticker", "C:/ws/replies/x.gif", "摸鱼"),
        ("image", "C:/ws/replies/x.gif", "摸鱼"),
    ]
    assert adapter.sent == [(ChatType.GROUP, "wxg:abc123", "看这个")]


async def test_wechat_file_fallback_failure_still_sends_text() -> None:
    """视频按文件推送被拒（False）或桥断连（异常）都不抛异常，正文照发。"""
    rejected = _WeChatMediaAdapter({"file": False})
    result = await _wechat_service(rejected)._send_chunk(
        ChatType.GROUP,
        "wxg:abc123",
        "视频在这",
        attachments=(Attachment(kind="video", path="C:/ws/replies/a.mp4"),),
    )

    assert result == "plain"
    assert rejected.media_calls == [("file", "C:/ws/replies/a.mp4", "")]
    assert rejected.sent == [(ChatType.GROUP, "wxg:abc123", "视频在这")]

    disconnected = _WeChatMediaAdapter({"file": GatewayError("微信桥未连接")})
    result = await _wechat_service(disconnected)._send_chunk(
        ChatType.GROUP,
        "wxg:abc123",
        "视频在这",
        attachments=(Attachment(kind="video", path="C:/ws/replies/a.mp4"),),
    )

    assert result == "plain"
    assert disconnected.media_calls == [("file", "C:/ws/replies/a.mp4", "")]
    assert disconnected.sent == [(ChatType.GROUP, "wxg:abc123", "视频在这")]


# ---- 入站 QQ 文件下载：file_id/busid -> OneBot 索回 URL -> files/ 落盘 -------
class _QQFileAdapter:
    """QQ 形态的假适配器：connected 恒真，call_action 回执可注入。"""

    connected = True

    def __init__(self, response: dict[str, object] | Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._response = response

    async def call_action(
        self, action: str, params: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append((action, dict(params)))
        if isinstance(self._response, Exception):
            raise self._response
        return dict(self._response or {})


def _file_message(chat_id: str = "qq:123456") -> Message:
    """todo-9 解析产物：content 只有一行 [文件] 占位，附件带定位信息。"""
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id="654321",
        nickname="tester",
        content="[文件] report.pdf",
        message_id="m-file-1",
        attachments=[
            Attachment(
                kind="file",
                name="report.pdf",
                size=10,
                file_id="f-123",
                busid="456",
            )
        ],
    )


def _handle_service(
    tmp_path: Path,
    adapter: _QQFileAdapter,
    *,
    file_download: bool = True,
    images_enabled: bool = True,
    file_max_bytes: int = 50 * 1024 * 1024,
) -> Service:
    """只装 handle() 前半段需要的属性：物化后 add_message 立刻短路。"""
    service = object.__new__(Service)
    setattr(service, "mirror", None)
    setattr(service, "brain", SimpleNamespace(supports_tools=False))
    setattr(service, "workspace", None)
    setattr(service, "adapters", {"qq": adapter})
    setattr(
        service,
        "settings",
        SimpleNamespace(
            images=SimpleNamespace(
                enabled=images_enabled,
                max_bytes=10 * 1024 * 1024,
                timeout_seconds=1.0,
                retention_days=0,
                max_edge=0,
                file_download=file_download,
                file_max_bytes=file_max_bytes,
            ),
            wechat=SimpleNamespace(
                media=SimpleNamespace(enabled=False, max_bytes=0, storage_root="")
            ),
            workspace=SimpleNamespace(enabled=True, dir=str(tmp_path)),
        ),
    )
    setattr(service, "store", SimpleNamespace(add_message=lambda msg: False))
    return service


async def test_qq_file_download_replaces_placeholder_once(tmp_path, monkeypatch) -> None:
    """file_id/busid -> OneBot 索回 URL -> 落盘 files/，占位行原位替换且仅一行。"""
    adapter = _QQFileAdapter({"url": "https://cdn.example.com/f.pdf?sig=ticket"})
    service = _handle_service(tmp_path, adapter)
    fetch_calls: list[tuple[str, float, int]] = []

    async def fake_fetch(url: str, timeout: float, max_bytes: int) -> bytes:
        fetch_calls.append((url, timeout, max_bytes))
        return PNG

    monkeypatch.setattr("dafeiyu.media._default_fetch", fake_fetch)
    msg = _file_message()

    await service.handle(msg)

    assert adapter.calls == [
        (
            "get_group_file_url",
            {"group_id": 123456, "file_id": "f-123", "busid": 456},
        )
    ]
    assert len(fetch_calls) == 1
    assert fetch_calls[0][0] == "https://cdn.example.com/f.pdf?sig=ticket"
    assert fetch_calls[0][2] == 50 * 1024 * 1024
    attachment = msg.attachments[0]
    assert attachment.path.startswith("files/")
    assert attachment.file_id == "f-123"
    assert attachment.busid == "456"
    assert attachment.name == "report.pdf"
    assert attachment.size == len(PNG)
    # todo-9 的 [文件] report.pdf 占位行被原位替换，不会出现第二行 [文件]。
    # 注记路径带上 workspace 前缀，模型 cwd 是仓库根时 read 得到。
    assert msg.content == (
        f"[文件] report.pdf ({(tmp_path / attachment.path).as_posix()})"
    )
    assert msg.content.count("[文件] report.pdf") == 1
    # 落盘文件真实存在且字节一致（与 images/ 互不干扰）。
    stored = tmp_path / attachment.path
    assert stored.is_file() and stored.stat().st_size == len(PNG)


async def test_qq_file_fetch_failure_keeps_placeholder(tmp_path, monkeypatch) -> None:
    """下载抛异常：content 保持裸 [文件] 占位，附件不带路径，也绝不抛出。"""

    async def failing_fetch(url: str, timeout: float, max_bytes: int) -> bytes:
        raise RuntimeError("download broke")

    monkeypatch.setattr("dafeiyu.media._default_fetch", failing_fetch)
    adapter = _QQFileAdapter({"url": "https://cdn.example.com/f.pdf"})
    msg = _file_message()

    await _handle_service(tmp_path, adapter).handle(msg)

    assert len(adapter.calls) == 1
    assert msg.content == "[文件] report.pdf"
    assert msg.attachments[0].path == ""
    assert msg.attachments[0].file_id == "f-123"
    assert not (tmp_path / "files").exists()


async def test_qq_file_oversize_keeps_placeholder(tmp_path, monkeypatch) -> None:
    """回包超过 file_max_bytes：保持占位，不落盘，不抛异常。"""

    async def big_fetch(url: str, timeout: float, max_bytes: int) -> bytes:
        return PNG * 4

    monkeypatch.setattr("dafeiyu.media._default_fetch", big_fetch)
    adapter = _QQFileAdapter({"url": "https://cdn.example.com/f.pdf"})
    msg = _file_message()

    await _handle_service(tmp_path, adapter, file_max_bytes=len(PNG) - 1).handle(msg)

    assert msg.content == "[文件] report.pdf"
    assert msg.attachments[0].path == ""
    assert not (tmp_path / "files").exists()


async def test_qq_file_download_disabled_never_resolves_or_fetches(
    tmp_path, monkeypatch
) -> None:
    """file_download=false：解析器不被接线，OneBot 与 fetch 都不发生。"""
    fetch_calls: list[str] = []

    async def fake_fetch(url: str, timeout: float, max_bytes: int) -> bytes:
        fetch_calls.append(url)
        return PNG

    monkeypatch.setattr("dafeiyu.media._default_fetch", fake_fetch)
    adapter = _QQFileAdapter({"url": "https://cdn.example.com/f.pdf"})
    msg = _file_message()

    await _handle_service(tmp_path, adapter, file_download=False).handle(msg)

    assert adapter.calls == []
    assert fetch_calls == []
    assert msg.content == "[文件] report.pdf"
    assert msg.attachments[0].file_id == "f-123"
    assert msg.attachments[0].busid == "456"


async def test_non_qq_file_never_resolves_or_fetches(tmp_path, monkeypatch) -> None:
    """非 QQ 通道不接解析器：即使附件带着 file_id/busid 也不下载。"""
    fetch_calls: list[str] = []

    async def fake_fetch(url: str, timeout: float, max_bytes: int) -> bytes:
        fetch_calls.append(url)
        return PNG

    monkeypatch.setattr("dafeiyu.media._default_fetch", fake_fetch)
    adapter = _QQFileAdapter({"url": "https://cdn.example.com/f.pdf"})
    msg = _file_message(chat_id="wxg:888")

    await _handle_service(tmp_path, adapter).handle(msg)

    assert adapter.calls == []
    assert fetch_calls == []
    assert msg.content == "[文件] report.pdf"
    assert msg.attachments[0].path == ""


async def test_qq_file_resolver_none_keeps_metadata(tmp_path, monkeypatch) -> None:
    """解析器返回 None（回执无 url）：附件元数据原样保留，绝不下载。"""
    fetch_calls: list[str] = []

    async def fake_fetch(url: str, timeout: float, max_bytes: int) -> bytes:
        fetch_calls.append(url)
        return PNG

    monkeypatch.setattr("dafeiyu.media._default_fetch", fake_fetch)
    adapter = _QQFileAdapter({})
    msg = _file_message()

    await _handle_service(tmp_path, adapter).handle(msg)

    assert adapter.calls == [
        (
            "get_group_file_url",
            {"group_id": 123456, "file_id": "f-123", "busid": 456},
        )
    ]
    assert fetch_calls == []
    attachment = msg.attachments[0]
    assert attachment.file_id == "f-123"
    assert attachment.busid == "456"
    assert attachment.name == "report.pdf"
    assert attachment.path == ""
    assert msg.content == "[文件] report.pdf"
