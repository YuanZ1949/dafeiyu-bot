"""todo 12：微信入站附件物化（本地路径 -> workspace/images + content 注记）。

所有测试都在临时目录里合成文件，不碰真实微信目录/网络。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import dafeiyu.media as media_module
from dafeiyu import imaging
from dafeiyu.config import Settings
from dafeiyu.gateway.wechat_bridge.media.dat import decode_dat
from dafeiyu.media import materialize_attachments
from dafeiyu.models import Attachment, ChatType, Message

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"video-bytes" * 8


def make_settings(
    tmp_path: Path,
    *,
    workspace: bool = True,
    media: bool = True,
    max_bytes: int = 1024 * 1024,
    retention_days: int = 0,
    images: bool = True,
    max_edge: int = 0,
    speech: bool = False,
) -> Settings:
    return cast(
        Settings,
        cast(
            object,
            SimpleNamespace(
                workspace=SimpleNamespace(
                    enabled=workspace, dir=str(tmp_path / "ws")
                ),
                wechat=SimpleNamespace(
                    media=SimpleNamespace(enabled=media, max_bytes=max_bytes)
                ),
                # workspace/images 与 QQ 侧共用，保留期沿用 images.retention_days；
                # QQ 只有 URL 的附件（语音/视频）由 images.enabled 门控。
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
    path: str,
    *,
    kind: str = "image",
    content: str = "[图片]",
    name: str = "",
    source: str = "",
    duration: int = 0,
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="wxg:g1",
        user_id="wxg:u1",
        nickname="阿猫",
        content=content,
        message_id="1:1",
        kind=kind,
        attachments=[
            Attachment(
                kind=kind, path=path, name=name, source=source, duration=duration
            )
        ],
    )


def xor_png(key: int = 0x37) -> bytes:
    return bytes(byte ^ key for byte in PNG)


def write_dat(tmp_path: Path, payload: bytes | None = None) -> Path:
    target = tmp_path / "msg.dat"
    target.write_bytes(xor_png() if payload is None else payload)
    return target


async def test_dat_image_is_decoded_and_annotated(tmp_path: Path) -> None:
    source = write_dat(tmp_path)
    settings = make_settings(tmp_path)
    message = make_message(str(source))

    count = await materialize_attachments(message, settings, decode=decode_dat)

    assert count == 1
    attachment = message.attachments[0]
    assert attachment.path.startswith("images/")
    assert attachment.path.endswith(".png")
    assert attachment.size == len(PNG)
    stored = tmp_path / "ws" / attachment.path
    assert stored.is_file()
    assert stored.read_bytes() == PNG
    assert f"[图片]（图存 {attachment.path}，可用 read 查看）" in message.content
    assert message.content.count("（图存") == 1


async def test_plain_image_without_decode_is_copied(tmp_path: Path) -> None:
    source = tmp_path / "a.png"
    source.write_bytes(PNG)
    settings = make_settings(tmp_path)
    message = make_message(str(source))

    count = await materialize_attachments(message, settings, decode=None)

    assert count == 1
    assert message.attachments[0].path.startswith("images/")
    assert (tmp_path / "ws" / message.attachments[0].path).read_bytes() == PNG


async def test_media_disabled_touches_no_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write_dat(tmp_path)
    settings = make_settings(tmp_path, media=False)
    message = make_message(str(source))

    def boom(self: Path) -> bytes:
        raise AssertionError("media 关闭时不允许读文件")

    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr(Path, "is_file", boom)

    count = await materialize_attachments(message, settings, decode=decode_dat)

    assert count == 0
    assert message.content == "[图片]"
    assert not (tmp_path / "ws" / "images").exists()


async def test_workspace_disabled_touches_no_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write_dat(tmp_path)
    settings = make_settings(tmp_path, workspace=False)
    message = make_message(str(source))

    def boom(self: Path) -> bytes:
        raise AssertionError("workspace 关闭时不允许读文件")

    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr(Path, "is_file", boom)

    count = await materialize_attachments(message, settings, decode=decode_dat)

    assert count == 0
    assert message.content == "[图片]"


async def test_missing_source_keeps_placeholder(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = make_message(str(tmp_path / "missing.dat"))

    count = await materialize_attachments(message, settings, decode=decode_dat)

    assert count == 0
    assert message.content == "[图片]"
    assert not (tmp_path / "ws" / "images").exists()


async def test_decode_none_keeps_placeholder(tmp_path: Path) -> None:
    source = write_dat(tmp_path, b"not an image")
    settings = make_settings(tmp_path)
    message = make_message(str(source))

    count = await materialize_attachments(message, settings, decode=lambda _: None)

    assert count == 0
    assert message.content == "[图片]"
    assert not (tmp_path / "ws" / "images").exists()


async def test_over_max_bytes_keeps_placeholder(tmp_path: Path) -> None:
    source = write_dat(tmp_path)
    settings = make_settings(tmp_path, max_bytes=8)
    message = make_message(str(source))

    count = await materialize_attachments(message, settings, decode=decode_dat)

    assert count == 0
    assert message.content == "[图片]"
    assert not (tmp_path / "ws" / "images").exists()


async def test_file_attachment_only_gets_note_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    message = make_message(
        str(tmp_path / "报告.pdf"), kind="file", content="看这个", name="报告.pdf"
    )

    def boom(self: Path) -> bytes:
        raise AssertionError("文件附件不允许读取/复制")

    monkeypatch.setattr(Path, "read_bytes", boom)

    first = await materialize_attachments(message, settings)
    second = await materialize_attachments(message, settings)

    assert first == 0
    assert second == 0
    assert message.content == "看这个\n[文件] 报告.pdf (%s)" % (tmp_path / "报告.pdf")
    assert message.content.count("[文件]") == 1
    assert not (tmp_path / "ws" / "images").exists()


async def test_empty_path_is_skipped(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = Message(
        chat_type=ChatType.GROUP,
        chat_id="wxg:g1",
        user_id="wxg:u1",
        nickname="阿猫",
        content="[图片]",
        message_id="1:2",
        kind="image",
        attachments=[Attachment(kind="image", path="")],
    )

    count = await materialize_attachments(message, settings, decode=decode_dat)

    assert count == 0
    assert message.content == "[图片]"


async def test_two_images_one_broken_counts_one(tmp_path: Path) -> None:
    broken = tmp_path / "broken.dat"
    broken.write_bytes(b"\x00\x01\x02\x03")
    valid = write_dat(tmp_path)
    settings = make_settings(tmp_path)
    message = make_message(str(broken), content="[图片] [图片]")
    message.attachments.append(Attachment(kind="image", path=str(valid)))

    count = await materialize_attachments(message, settings, decode=decode_dat)

    assert count == 1
    assert message.content.count("（图存") == 1
    assert "（图存" in message.content


async def test_second_call_is_idempotent(tmp_path: Path) -> None:
    source = write_dat(tmp_path)
    settings = make_settings(tmp_path)
    message = make_message(str(source))

    first = await materialize_attachments(message, settings, decode=decode_dat)
    content = message.content
    second = await materialize_attachments(message, settings, decode=decode_dat)

    assert first == 1
    assert second == 0
    assert message.content == content
    assert message.content.count("（图存") == 1


async def test_sticker_placeholder_is_annotated_and_idempotent(
    tmp_path: Path,
) -> None:
    source = write_dat(tmp_path)
    settings = make_settings(tmp_path)
    message = make_message(str(source), content="[表情:745c9c20]")

    first = await materialize_attachments(message, settings, decode=decode_dat)
    content = message.content
    second = await materialize_attachments(message, settings, decode=decode_dat)

    assert first == 1
    assert "[表情:745c9c20]（图存 images/" in content
    assert content.count("（图存") == 1
    assert second == 0
    assert message.content == content


async def test_emoticon_cache_file_uses_async_decode(tmp_path: Path) -> None:
    digest = "745c9c20f715400b40f5d679bd12ebdb"
    source = tmp_path / digest
    source.write_bytes(b"cipher")
    settings = make_settings(tmp_path)
    message = make_message(
        str(source), content="[表情:745c9c20]"
    )

    async def decode(_data: bytes) -> bytes:
        return PNG

    count = await materialize_attachments(message, settings, decode=decode)

    assert count == 1
    attachment = message.attachments[0]
    assert attachment.path.startswith("images/")
    assert attachment.path.endswith(".png")
    assert (tmp_path / "ws" / attachment.path).read_bytes() == PNG
    assert "[表情:745c9c20]（图存 images/" in message.content


async def test_hex_named_file_without_decode_is_copied(tmp_path: Path) -> None:
    digest = "745c9c20f715400b40f5d679bd12ebdb"
    source = tmp_path / digest
    source.write_bytes(PNG)
    settings = make_settings(tmp_path)
    message = make_message(str(source), content="[表情:745c9c20]")

    count = await materialize_attachments(message, settings, decode=None)

    assert count == 1
    assert message.attachments[0].path.endswith(".png")


async def test_materialize_cleans_expired_images(tmp_path: Path) -> None:
    images_dir = tmp_path / "ws" / "images"
    images_dir.mkdir(parents=True)
    old = images_dir / "old.png"
    old.write_bytes(PNG)
    fresh = images_dir / "fresh.png"
    fresh.write_bytes(PNG)
    eight_days_ago = time.time() - 8 * 86400
    os.utime(old, (eight_days_ago, eight_days_ago))

    source = tmp_path / "a.png"
    source.write_bytes(PNG)
    settings = make_settings(tmp_path, retention_days=7)
    message = make_message(str(source))

    count = await materialize_attachments(message, settings)

    assert count == 1
    assert not old.exists()
    assert fresh.is_file()


async def test_retention_zero_keeps_expired_images(tmp_path: Path) -> None:
    images_dir = tmp_path / "ws" / "images"
    images_dir.mkdir(parents=True)
    old = images_dir / "old.png"
    old.write_bytes(PNG)
    eight_days_ago = time.time() - 8 * 86400
    os.utime(old, (eight_days_ago, eight_days_ago))

    source = tmp_path / "a.png"
    source.write_bytes(PNG)
    settings = make_settings(tmp_path, retention_days=0)
    message = make_message(str(source))

    count = await materialize_attachments(message, settings)

    assert count == 1
    assert old.is_file()


# ------------------------------------------------------------- 视频物化与抽帧
def patch_video_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    duration: float = 10.0,
    fail: bool = False,
) -> list[dict[str, object]]:
    """替换 media 模块的 ffmpeg 入口，返回抽帧调用记录。"""
    calls: list[dict[str, object]] = []

    def fake_frame(
        ffmpeg: str, source: Path, target: Path, offset: float
    ) -> bool:
        calls.append({"ffmpeg": ffmpeg, "source": source, "offset": offset})
        if fail:
            return False
        Path(target).write_bytes(JPG)
        return True

    monkeypatch.setattr(media_module, "_locate", lambda name: name)
    monkeypatch.setattr(
        media_module, "_probe_duration", lambda *_a, **_k: duration
    )
    monkeypatch.setattr(media_module, "_extract_frame", fake_frame)
    return calls


async def test_video_is_copied_and_frames_are_annotated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(MP4)
    calls = patch_video_ffmpeg(monkeypatch)
    settings = make_settings(tmp_path)
    message = make_message(
        str(source), kind="video", content="[视频]", name="clip.mp4"
    )

    count = await materialize_attachments(message, settings)

    # 视频不是图片：返回值仍是成功写盘的图片数。
    assert count == 0
    attachment = message.attachments[0]
    assert attachment.kind == "video"
    assert attachment.path.startswith("videos/")
    assert attachment.path.endswith(".mp4")
    assert attachment.size == len(MP4)
    stored = tmp_path / "ws" / attachment.path
    assert stored.is_file()
    assert stored.read_bytes() == MP4

    digest = hashlib.md5(str(source).encode("utf-8")).hexdigest()[:6]
    assert [call["offset"] for call in calls] == [1.0, 3.5, 6.0, 8.5]
    frames = sorted((tmp_path / "ws" / "images").glob(f"video-{digest}-*.jpg"))
    assert len(frames) == 4
    assert all(frame.read_bytes() == JPG for frame in frames)

    assert message.content.startswith("[视频]（已抽帧 images/video-")
    assert "、images/" in message.content
    assert "，可用 read 查看；原文件 videos/" in message.content
    assert message.content.count("（已抽帧") == 1


async def test_video_without_ffmpeg_copies_original_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(MP4)
    monkeypatch.setattr(media_module, "_locate", lambda _name: None)
    settings = make_settings(tmp_path)
    message = make_message(str(source), kind="video", content="[视频]")

    count = await materialize_attachments(message, settings)

    assert count == 0
    attachment = message.attachments[0]
    assert attachment.path.startswith("videos/")
    assert (tmp_path / "ws" / attachment.path).read_bytes() == MP4
    assert message.content == f"[视频]（原文件 {attachment.path}，无法抽帧）"
    assert not (tmp_path / "ws" / "images").exists()


async def test_video_frame_failure_keeps_original_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(MP4)
    calls = patch_video_ffmpeg(monkeypatch, fail=True)
    settings = make_settings(tmp_path)
    message = make_message(str(source), kind="video", content="[视频]")

    await materialize_attachments(message, settings)

    assert len(calls) == 4
    assert "（已抽帧" not in message.content
    assert "，无法抽帧）" in message.content


async def test_video_dat_decode_none_keeps_original_dat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "clip.dat"
    source.write_bytes(b"not-a-real-image-container")
    monkeypatch.setattr(media_module, "_locate", lambda _name: None)
    settings = make_settings(tmp_path)
    message = make_message(str(source), kind="video", content="[视频]")

    count = await materialize_attachments(
        message, settings, decode=lambda _data: None
    )

    assert count == 0
    attachment = message.attachments[0]
    assert attachment.path.endswith(".dat")
    assert (tmp_path / "ws" / attachment.path).read_bytes() == source.read_bytes()
    assert "，无法抽帧）" in message.content


async def test_video_annotate_false_only_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(MP4)

    async def boom(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("annotate=False 时不允许抽帧")

    monkeypatch.setattr(media_module, "_extract_video_frames", boom)
    settings = make_settings(tmp_path)
    message = make_message(str(source), kind="video", content="[视频]")

    await materialize_attachments(message, settings, annotate=False)

    assert (tmp_path / "ws" / message.attachments[0].path).is_file()
    assert message.content == "[视频]"


async def test_broken_video_does_not_block_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = tmp_path / "a.png"
    valid.write_bytes(PNG)
    monkeypatch.setattr(media_module, "_locate", lambda _name: None)
    settings = make_settings(tmp_path)
    message = make_message(
        str(tmp_path / "missing.mp4"), kind="video", content="[视频] [图片]"
    )
    message.attachments.append(Attachment(kind="image", path=str(valid)))

    count = await materialize_attachments(message, settings)

    assert count == 1
    assert message.content.startswith("[视频] [图片]（图存 images/")
    assert message.content.count("（图存") == 1


async def test_video_second_call_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(MP4)
    patch_video_ffmpeg(monkeypatch)
    settings = make_settings(tmp_path)
    message = make_message(str(source), kind="video", content="[视频]")

    first = await materialize_attachments(message, settings)
    content = message.content
    second = await materialize_attachments(message, settings)

    assert first == 0
    assert second == 0
    assert message.content == content
    assert content.count("（已抽帧") == 1


# --------------------------------------------------------------- 语音转写
SILK = b"#!SILK_V3" + b"0" * 64


async def test_voice_with_local_path_is_stored_and_transcribed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.silk"
    source.write_bytes(SILK)
    calls: list[Path] = []

    async def fake_transcribe(path: Path, _settings: object) -> str:
        calls.append(path)
        return "今天天气不错"

    monkeypatch.setattr(media_module.speech, "transcribe", fake_transcribe)
    settings = make_settings(tmp_path, speech=True)
    message = make_message(
        str(source), kind="voice", content="[语音]", name="voice.silk", duration=3
    )

    count = await materialize_attachments(message, settings)

    assert count == 0  # 返回值仍是成功写盘的图片数
    attachment = message.attachments[0]
    assert attachment.path.startswith("voices/")
    assert attachment.path.endswith(".silk")
    stored = tmp_path / "ws" / attachment.path
    assert stored.read_bytes() == SILK
    assert calls == [stored]
    assert message.content == (
        f"[语音]（转写：今天天气不错，原文件 {attachment.path}）"
    )


async def test_voice_with_url_is_downloaded_even_when_wechat_media_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.com/voice.silk"
    fetched: list[tuple[str, float, int]] = []

    async def fake_fetch(
        source: str, timeout: float, max_bytes: int
    ) -> bytes:
        fetched.append((source, timeout, max_bytes))
        return SILK

    async def fake_transcribe(_path: Path, _settings: object) -> str:
        return "在吗"

    monkeypatch.setattr(media_module.speech, "transcribe", fake_transcribe)
    settings = make_settings(tmp_path, media=False, images=True, speech=True)
    message = make_message(
        "",
        kind="record",
        content="[语音]",
        source=url,
        duration=2,
    )

    count = await materialize_attachments(message, settings, fetch=fake_fetch)

    assert count == 0
    assert fetched and fetched[0][0] == url
    attachment = message.attachments[0]
    assert attachment.path.startswith("voices/")
    assert (tmp_path / "ws" / attachment.path).read_bytes() == SILK
    assert "转写：在吗" in message.content
    assert f"原文件 {attachment.path}" in message.content


async def test_voice_disabled_keeps_placeholder_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.silk"
    source.write_bytes(SILK)

    async def boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("speech 关闭时不允许转写")

    monkeypatch.setattr(media_module.speech, "transcribe", boom)
    settings = make_settings(tmp_path, speech=False)
    message = make_message(str(source), kind="voice", content="[语音]")

    count = await materialize_attachments(message, settings)

    assert count == 0
    assert message.content == "[语音]"
    assert message.attachments[0].path == str(source)
    assert not (tmp_path / "ws" / "voices").exists()


async def test_voice_annotate_false_keeps_transcript_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.silk"
    source.write_bytes(SILK)

    async def fake_transcribe(_path: Path, _settings: object) -> str:
        return "晚安"

    monkeypatch.setattr(media_module.speech, "transcribe", fake_transcribe)
    settings = make_settings(tmp_path, speech=True)
    message = make_message(str(source), kind="voice", content="[语音]")

    await materialize_attachments(message, settings, annotate=False)

    assert message.content == "[语音]（转写：晚安）"
    assert message.attachments[0].path.startswith("voices/")


async def test_too_long_voice_is_not_transcribed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.silk"
    source.write_bytes(SILK)

    async def boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("超长语音不允许转写")

    monkeypatch.setattr(media_module.speech, "transcribe", boom)
    settings = make_settings(tmp_path, speech=True)
    message = make_message(
        str(source), kind="voice", content="[语音]", duration=120
    )

    await materialize_attachments(message, settings)

    assert "太长，未转写" in message.content
    assert message.content.count("（") == 1


# --------------------------------------------------- QQ 视频：只有 URL 没有 path
async def test_video_url_is_downloaded_and_framed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.com/clip.mp4"
    calls = patch_video_ffmpeg(monkeypatch)

    async def fake_fetch(
        source: str, _timeout: float, max_bytes: int
    ) -> bytes:
        assert source == url
        assert max_bytes >= len(MP4)
        return MP4

    settings = make_settings(tmp_path, media=False, images=True)
    message = make_message("", kind="video", content="[视频]", source=url)

    count = await materialize_attachments(message, settings, fetch=fake_fetch)

    assert count == 0
    attachment = message.attachments[0]
    assert attachment.path.startswith("videos/")
    assert attachment.path.endswith(".mp4")
    assert (tmp_path / "ws" / attachment.path).read_bytes() == MP4
    assert len(calls) == 4
    assert message.content.startswith("[视频]（已抽帧 images/video-")
    assert "原文件 videos/" in message.content
    # 临时下载文件必须清理干净。
    assert list((tmp_path / "ws" / "videos").glob(".incoming-*")) == []


async def test_url_attachment_is_skipped_when_both_gates_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(
        *_args: object, **_kwargs: object
    ) -> bytes:
        raise AssertionError("两个开关都关时不允许下载")

    settings = make_settings(tmp_path, media=False, images=False, speech=True)
    message = make_message(
        "",
        kind="record",
        content="[语音]",
        source="https://example.com/voice.silk",
    )

    count = await materialize_attachments(message, settings, fetch=boom)

    assert count == 0
    assert message.content == "[语音]"
    assert not (tmp_path / "ws" / "voices").exists()


# ------------------------------------------------------------- 图片降采样


async def test_wechat_image_passes_max_edge_to_downscale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.png"
    source.write_bytes(PNG)
    calls: list[tuple[Path, int]] = []

    def fake_downscale(path: Path, max_edge: int) -> bool:
        calls.append((Path(path), max_edge))
        Path(path).write_bytes(PNG[:8])
        return True

    monkeypatch.setattr(imaging, "downscale", fake_downscale)
    settings = make_settings(tmp_path, max_edge=1280)
    message = make_message(str(source))

    count = await materialize_attachments(message, settings, decode=None)

    assert count == 1
    assert len(calls) == 1
    path, max_edge = calls[0]
    assert max_edge == 1280
    assert path.parent == tmp_path / "ws" / "images"
    # 降采样成功后 Attachment.size 取真实落盘大小。
    assert message.attachments[0].size == len(PNG[:8])


async def test_wechat_gif_does_not_call_downscale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.gif"
    source.write_bytes(b"GIF89a" + b"0" * 64)
    calls: list[tuple[Path, int]] = []

    def fake_downscale(path: Path, max_edge: int) -> bool:
        calls.append((Path(path), max_edge))
        return False

    monkeypatch.setattr(imaging, "downscale", fake_downscale)
    settings = make_settings(tmp_path, max_edge=1280)
    message = make_message(str(source))

    count = await materialize_attachments(message, settings, decode=None)

    assert count == 1
    assert calls == []
