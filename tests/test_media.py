"""todo 12：微信入站附件物化（本地路径 -> workspace/images + content 注记）。

所有测试都在临时目录里合成文件，不碰真实微信目录/网络。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from dafeiyu.config import Settings
from dafeiyu.gateway.wechat_bridge.media.dat import decode_dat
from dafeiyu.media import materialize_attachments
from dafeiyu.models import Attachment, ChatType, Message

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def make_settings(
    tmp_path: Path,
    *,
    workspace: bool = True,
    media: bool = True,
    max_bytes: int = 1024 * 1024,
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
            ),
        ),
    )


def make_message(
    path: str,
    *,
    kind: str = "image",
    content: str = "[图片]",
    name: str = "",
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="wxg:g1",
        user_id="wxg:u1",
        nickname="阿猫",
        content=content,
        message_id="1:1",
        kind=kind,
        attachments=[Attachment(kind=kind, path=path, name=name)],
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
