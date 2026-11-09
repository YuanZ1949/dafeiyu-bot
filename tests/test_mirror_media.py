"""T6：跨群镜像的媒体路径解析与入站物化。

覆盖：
- ``resolve_media_path`` 的 workspace 相对路径解析、缺失/空路径、未配置目录、
  ``..`` 逃逸；
- ``images.materialize_relay`` 绕过 annotate 门控但要求 images/workspace，
  且绝不修改 ``msg.content``；
- ``media.materialize_relay_attachments`` 在副本上物化 URL 语音/视频，
  ``speech.enabled`` 只门控 voice/record、不门控 video；
- 解不开的微信 ``.dat`` 不会留下可读本地路径。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import dafeiyu.media as media_module
from dafeiyu.config import Settings
from dafeiyu.images import materialize_relay
from dafeiyu.media import materialize_relay_attachments, resolve_media_path
from dafeiyu.models import Attachment, ChatType, Message, Segment

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
SILK = b"#!SILK_V3" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"video-bytes" * 8


def make_settings(
    tmp_path: Path,
    *,
    workspace: bool = True,
    workspace_dir: str | None = None,
    images: bool = True,
    media: bool = False,
    speech: bool = False,
    max_bytes: int = 1024 * 1024,
    max_edge: int = 0,
    retention_days: int = 0,
) -> Settings:
    return cast(
        Settings,
        cast(
            object,
            SimpleNamespace(
                workspace=SimpleNamespace(
                    enabled=workspace,
                    dir=str(tmp_path / "ws")
                    if workspace_dir is None
                    else workspace_dir,
                ),
                wechat=SimpleNamespace(
                    media=SimpleNamespace(enabled=media, max_bytes=max_bytes)
                ),
                images=SimpleNamespace(
                    enabled=images,
                    max_bytes=max_bytes,
                    timeout_seconds=8.0,
                    retention_days=retention_days,
                    max_edge=max_edge,
                ),
                speech=SimpleNamespace(
                    enabled=speech,
                    model="base",
                    language="zh",
                    device="cpu",
                    compute_type="int8",
                    beam_size=1,
                    max_seconds=60,
                    timeout_seconds=10.0,
                    concurrency=1,
                    max_bytes=20 * 1024 * 1024,
                    hf_endpoint="",
                ),
            ),
        ),
    )


def make_message(
    attachments: list[Attachment],
    *,
    content: str = "",
    segments: list[Segment] | None = None,
    chat_id: str = "qq:1001",
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id="qq:2002" if chat_id.startswith("qq:") else "wxg:u1",
        nickname="阿鱼",
        content=content,
        message_id="m-1",
        segments=segments or [],
        attachments=attachments,
    )


# ------------------------------------------------------- resolve_media_path


def test_resolve_relative_returns_real_absolute_path(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "ws" / "images" / "x.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(PNG)

    resolved = resolve_media_path(
        settings, Attachment(kind="image", path="images/x.png")
    )

    assert resolved is not None
    assert resolved == target.resolve()
    assert resolved.is_absolute()
    assert resolved.is_file()


def test_resolve_missing_file_returns_none(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert (
        resolve_media_path(
            settings, Attachment(kind="image", path="images/nope.png")
        )
        is None
    )
    assert resolve_media_path(settings, Attachment(kind="image", path="")) is None


def test_resolve_disabled_workspace_dir_returns_none(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, workspace_dir="")
    outside = tmp_path / "x.png"
    outside.write_bytes(PNG)

    # 空目录不得拼成 cwd；绝对路径在未配置 workspace 时也不解析。
    assert (
        resolve_media_path(
            settings, Attachment(kind="image", path="images/x.png")
        )
        is None
    )
    assert (
        resolve_media_path(settings, Attachment(kind="image", path=str(outside)))
        is None
    )


def test_resolve_traversal_returns_none(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    escape = tmp_path / "escape.txt"
    escape.write_bytes(b"secret")

    assert (
        resolve_media_path(settings, Attachment(kind="file", path="../escape.txt"))
        is None
    )
    assert (
        resolve_media_path(
            settings, Attachment(kind="file", path="images/../../escape.txt")
        )
        is None
    )


def test_resolve_directory_and_empty_file_return_none(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "ws" / "images").mkdir(parents=True)
    (tmp_path / "ws" / "images" / "empty.png").write_bytes(b"")

    # 相对路径：目录与空文件都不可用。
    assert (
        resolve_media_path(settings, Attachment(kind="image", path="images"))
        is None
    )
    assert (
        resolve_media_path(
            settings, Attachment(kind="image", path="images/empty.png")
        )
        is None
    )
    # 绝对路径：真实非空文件可用，空文件不可用，缺失不可用。
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    assert (
        resolve_media_path(settings, Attachment(kind="image", path=str(outside)))
        == outside.resolve()
    )
    empty_abs = tmp_path / "empty-abs.png"
    empty_abs.write_bytes(b"")
    assert (
        resolve_media_path(
            settings, Attachment(kind="image", path=str(empty_abs))
        )
        is None
    )


# ---------------------------------------------- media.materialize_relay_attachments


async def test_materialize_undecodable_dat_has_no_readable_path(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, media=True)
    source = tmp_path / "msg.dat"
    source.write_bytes(b"not-decodable-image-bytes")
    attachment = Attachment(kind="image", path=str(source))
    msg = make_message([attachment], content="[图片]", chat_id="wxg:g1")

    result = await materialize_relay_attachments(
        msg, settings, decode=lambda _data: None
    )

    assert result is not msg
    assert result.attachments[0].path == ""
    assert resolve_media_path(settings, result.attachments[0]) is None
    # 调用方消息零改动。
    assert msg.attachments[0].path == str(source)
    assert msg.content == "[图片]"


async def test_no_content_mutation_on_relay_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, speech=True)

    async def fake_fetch(_url: str, _timeout: float, _max_bytes: int) -> bytes:
        return SILK

    async def fake_transcribe(_path: Path, _settings: object) -> str:
        return "在吗"

    monkeypatch.setattr(media_module.speech, "transcribe", fake_transcribe)
    msg = make_message(
        [Attachment(kind="record", source="https://x/v.silk", duration=2)],
        content="[语音]",
        segments=[Segment("record", {"url": "https://x/v.silk"})],
    )
    before = msg.content
    original_attachments = list(msg.attachments)

    result = await materialize_relay_attachments(msg, settings, fetch=fake_fetch)

    assert result is not msg
    assert msg.content == before
    assert msg.attachments == original_attachments
    assert result.content == before
    assert result.attachments[0].path.startswith("voices/")


async def test_qq_voice_url_materializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, speech=True)
    fetched: list[str] = []

    async def fake_fetch(url: str, _timeout: float, _max_bytes: int) -> bytes:
        fetched.append(url)
        return SILK

    async def fake_transcribe(_path: Path, _settings: object) -> str:
        return "晚安"

    monkeypatch.setattr(media_module.speech, "transcribe", fake_transcribe)
    msg = make_message(
        [Attachment(kind="record", source="https://x/v.silk")],
        content="[语音]",
    )

    result = await materialize_relay_attachments(msg, settings, fetch=fake_fetch)

    attachment = result.attachments[0]
    assert fetched == ["https://x/v.silk"]
    assert attachment.path.startswith("voices/")
    resolved = resolve_media_path(settings, attachment)
    assert resolved is not None
    assert resolved.is_file()
    assert resolved.read_bytes() == SILK


async def test_qq_video_url_materializes_even_when_speech_disabled(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, speech=False)
    fetched: list[str] = []

    async def fake_fetch(url: str, _timeout: float, _max_bytes: int) -> bytes:
        fetched.append(url)
        return MP4

    msg = make_message(
        [Attachment(kind="video", source="https://x/clip.mp4")],
        content="[视频]",
    )

    result = await materialize_relay_attachments(msg, settings, fetch=fake_fetch)

    attachment = result.attachments[0]
    assert fetched == ["https://x/clip.mp4"]
    assert attachment.path.startswith("videos/")
    assert attachment.path.endswith(".mp4")
    resolved = resolve_media_path(settings, attachment)
    assert resolved is not None
    assert resolved.is_file()
    assert resolved.read_bytes() == MP4
    assert not (tmp_path / "ws" / "voices").exists()


async def test_speech_disabled_blocks_voice_but_not_video(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, speech=False)
    fetched: list[str] = []

    async def fake_fetch(url: str, _timeout: float, _max_bytes: int) -> bytes:
        fetched.append(url)
        return MP4

    msg = make_message(
        [
            Attachment(kind="record", source="https://x/v.silk"),
            Attachment(kind="video", source="https://x/clip.mp4"),
        ],
        content="[语音] [视频]",
    )

    result = await materialize_relay_attachments(msg, settings, fetch=fake_fetch)

    voice, video = result.attachments
    assert voice.path == ""
    assert resolve_media_path(settings, voice) is None
    assert "https://x/v.silk" not in fetched
    assert not (tmp_path / "ws" / "voices").exists()
    assert video.path.startswith("videos/")
    assert resolve_media_path(settings, video) is not None
    assert fetched == ["https://x/clip.mp4"]


# ---------------------------------------------------- images.materialize_relay


async def test_images_materialize_relay_url_writes_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    calls: list[str] = []

    async def fake_fetch(url: str, _timeout: float, _max_bytes: int) -> bytes:
        calls.append(url)
        return PNG

    msg = make_message(
        [],
        content="[图片]",
        segments=[Segment("image", {"url": "https://x/a.png"})],
    )

    produced = await materialize_relay(msg, settings, fetch=fake_fetch)

    assert calls == ["https://x/a.png"]
    assert len(produced) == 1
    attachment = produced[0]
    assert attachment.kind == "image"
    assert attachment.path.startswith("images/")
    assert attachment.size == len(PNG)
    assert msg.content == "[图片]"
    assert msg.attachments == []
    resolved = resolve_media_path(settings, attachment)
    assert resolved is not None
    assert resolved.read_bytes() == PNG


async def test_images_materialize_relay_disabled_returns_empty(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, workspace=False)
    msg = make_message(
        [],
        content="[图片]",
        segments=[Segment("image", {"url": "https://x/a.png"})],
    )

    async def boom(_url: str, _timeout: float, _max_bytes: int) -> bytes:
        raise AssertionError("workspace 关闭时不允许下载")

    assert await materialize_relay(msg, settings, fetch=boom) == []
    assert msg.content == "[图片]"
    assert not (tmp_path / "ws").exists()
