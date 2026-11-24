"""远端图片直发：URL 形态校验、SSRF 拒绝、下载落盘与降级。

边界：
- ``remote_media.enabled`` 关闭时，URL 附件与历史行为一致（拒绝）；
- 开启后 ``resolve_attachments`` 只产出 ``source=<URL>`` 占位，不联网；
- 下载侧拒绝私网/环回/链路本地、凭据、非图片内容、超限、重定向到内网；
- 任何一个远端附件失败都不影响本地附件与正文。
"""

from __future__ import annotations

import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dafeiyu import reply_media
from dafeiyu.config import RemoteMediaConfig
from dafeiyu.models import Attachment
from dafeiyu.reply_media import (
    download_remote_attachments,
    is_remote_url,
    resolve_attachments,
)
from tests.test_reply_media import PNG_BYTES, make_reply, make_settings

PUBLIC_URL = "http://93.184.216.34/a.png"


# ---------------------------------------------------------------- 工具


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "image/png",
        content_length: int | None = None,
    ) -> None:
        self._body = body
        self.status = status
        headers: dict[str, str] = {"Content-Type": content_type}
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:size], self._body[size:]
        return data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _opener_factory(payload: Any, **kwargs: Any) -> Any:
    """构造 ``opener_factory(allowed_hosts) -> opener``；payload 是异常时抛出。"""

    def factory(_hosts: Any) -> Any:
        def _open(_request: Any, timeout: float | None = None) -> _FakeResponse:
            if isinstance(payload, BaseException):
                raise payload
            return _FakeResponse(payload, **kwargs)

        return types.SimpleNamespace(open=_open)

    return factory


def _remote_settings(tmp_path: Path, **kwargs: Any) -> Any:
    settings = make_settings(tmp_path)
    return replace(settings, remote_media=RemoteMediaConfig(enabled=True, **kwargs))


def _placeholder(url: str = PUBLIC_URL) -> list[Attachment]:
    return [Attachment(kind="image", source=url)]


# ---------------------------------------------------------------- 形态校验


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://example.com/a.png", True),
        ("https://example.com/a.png", True),
        ("HTTP://EXAMPLE.COM/a.png", True),
        ("ftp://example.com/a.png", False),
        ("file:///c:/a.png", False),
        ("replies/a.png", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_is_remote_url(raw: str, expected: bool) -> None:
    assert is_remote_url(raw) is expected


def test_remote_disabled_rejects_url(tmp_path: Path) -> None:
    """默认配置（remote_media 关闭）下 URL 仍被拒绝，与历史行为一致。"""
    result = resolve_attachments(
        make_reply(Attachment(kind="image", path=PUBLIC_URL)), make_settings(tmp_path)
    )
    assert result == []


def test_remote_enabled_returns_placeholder(tmp_path: Path) -> None:
    result = resolve_attachments(
        make_reply(Attachment(kind="image", path=PUBLIC_URL)),
        _remote_settings(tmp_path),
    )
    assert len(result) == 1
    assert result[0].kind == "image"
    assert result[0].source == PUBLIC_URL
    assert result[0].path == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://localhost/a.png",
        "http://LOCALHOST./a.png",
        "http://10.1.2.3/a.png",
        "http://192.168.1.9/a.png",
        "http://172.16.5.5/a.png",
        "http://169.254.1.1/a.png",
        "http://100.64.0.1/a.png",
        "http://[::1]/a.png",
        "http://user:pw@93.184.216.34/a.png",
        "http://printer.local/a.png",
        "http://metadata.google.internal/a.png",
    ],
)
def test_remote_rejects_dangerous_targets(tmp_path: Path, url: str) -> None:
    result = resolve_attachments(
        make_reply(Attachment(kind="image", path=url)), _remote_settings(tmp_path)
    )
    assert result == []


def test_remote_max_items(tmp_path: Path) -> None:
    reply = make_reply(
        Attachment(kind="image", path="http://93.184.216.34/1.png"),
        Attachment(kind="image", path="http://93.184.216.34/2.png"),
        Attachment(kind="image", path="http://93.184.216.34/3.png"),
    )
    result = resolve_attachments(reply, _remote_settings(tmp_path, max_items=2))
    assert [item.source for item in result] == [
        "http://93.184.216.34/1.png",
        "http://93.184.216.34/2.png",
    ]


def test_remote_host_allowlist(tmp_path: Path) -> None:
    settings = _remote_settings(tmp_path, allowed_hosts=("*.example.com",))
    allowed = resolve_attachments(
        make_reply(Attachment(kind="image", path="https://img.example.com/a.png")),
        settings,
    )
    blocked = resolve_attachments(
        make_reply(Attachment(kind="image", path="https://evil.test/a.png")),
        settings,
    )
    assert len(allowed) == 1
    assert blocked == []


# ---------------------------------------------------------------- 下载


@pytest.mark.asyncio
async def test_download_success_writes_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reply_media, "_resolve_reject", lambda host, port: None)
    settings = _remote_settings(tmp_path)
    placeholder = _placeholder()

    result = await download_remote_attachments(
        placeholder, settings, opener_factory=_opener_factory(PNG_BYTES)
    )

    assert len(result) == 1
    item = result[0]
    assert item.kind == "image"
    assert item.source == ""
    assert item.size == len(PNG_BYTES)
    written = Path(item.path)
    assert written.is_file()
    assert written.parent == (tmp_path / "replies").resolve()
    assert written.name.startswith("remote-")
    assert written.suffix == ".png"
    assert written.read_bytes() == PNG_BYTES


@pytest.mark.asyncio
async def test_download_rejects_non_image_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reply_media, "_resolve_reject", lambda host, port: None)
    result = await download_remote_attachments(
        _placeholder(),
        _remote_settings(tmp_path),
        opener_factory=_opener_factory(PNG_BYTES, content_type="text/html"),
    )
    assert result == []


@pytest.mark.asyncio
async def test_download_rejects_content_without_image_magic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reply_media, "_resolve_reject", lambda host, port: None)
    result = await download_remote_attachments(
        _placeholder(),
        _remote_settings(tmp_path),
        opener_factory=_opener_factory(b"<html>not an image</html>"),
    )
    assert result == []
    assert not (tmp_path / "replies").exists()


@pytest.mark.asyncio
async def test_download_rejects_declared_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reply_media, "_resolve_reject", lambda host, port: None)
    result = await download_remote_attachments(
        _placeholder(),
        _remote_settings(tmp_path, max_bytes=16),
        opener_factory=_opener_factory(PNG_BYTES, content_length=len(PNG_BYTES)),
    )
    assert result == []


@pytest.mark.asyncio
async def test_download_rejects_streamed_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 Content-Length 时也必须边读边封顶。"""
    monkeypatch.setattr(reply_media, "_resolve_reject", lambda host, port: None)
    result = await download_remote_attachments(
        _placeholder(),
        _remote_settings(tmp_path, max_bytes=16),
        opener_factory=_opener_factory(PNG_BYTES),
    )
    assert result == []


@pytest.mark.asyncio
async def test_download_network_error_keeps_local_attachments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reply_media, "_resolve_reject", lambda host, port: None)
    local = Attachment(kind="image", path="replies/a.png", source="")
    (tmp_path / "replies").mkdir()
    (tmp_path / "replies" / "a.png").write_bytes(PNG_BYTES)

    result = await download_remote_attachments(
        [local, *_placeholder()],
        _remote_settings(tmp_path),
        opener_factory=_opener_factory(TimeoutError("boom")),
    )

    assert result == [local]


@pytest.mark.asyncio
async def test_download_skipped_when_disabled(tmp_path: Path) -> None:
    result = await download_remote_attachments(
        _placeholder(), make_settings(tmp_path), opener_factory=_opener_factory(PNG_BYTES)
    )
    assert result == []


@pytest.mark.asyncio
async def test_download_skips_dns_internal_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """域名本身公网，但解析到内网地址时必须拒绝。"""
    monkeypatch.setattr(reply_media, "_resolve_reject", lambda host, port: "地址属于私网")
    result = await download_remote_attachments(
        [Attachment(kind="image", source="http://cdn.example.com/a.png")],
        _remote_settings(tmp_path),
        opener_factory=_opener_factory(PNG_BYTES),
    )
    assert result == []


def test_redirect_guard_rejects_internal_target() -> None:
    guard = reply_media._RedirectGuard(())
    request = reply_media.urllib.request.Request(PUBLIC_URL)
    with pytest.raises(reply_media._RemoteReject):
        guard.redirect_request(
            request, None, 302, "Found", {}, "http://127.0.0.1/steal.png"
        )
