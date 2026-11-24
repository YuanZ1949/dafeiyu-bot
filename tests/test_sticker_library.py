"""贴纸库：入站归档 + 索引、容量裁剪、出站引用与越界拒绝。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dafeiyu.media as media_module
from dafeiyu.config import ConfigError, StickerLibraryConfig, _build_sticker_library
from dafeiyu.media import materialize_attachments
from dafeiyu.models import Attachment, ChatType, Message
from dafeiyu.reply_media import resolve_attachments
from tests.test_media import PNG, make_message, make_settings
from tests.test_reply_media import make_reply


def _settings(tmp_path: Path, **kwargs: Any) -> Any:
    base = dict(vars(make_settings(tmp_path)))
    base["sticker_library"] = SimpleNamespace(
        enabled=kwargs.pop("enabled", True),
        dir=kwargs.pop("dir", "stickers"),
        max_files=kwargs.pop("max_files", 500),
        max_bytes=kwargs.pop("max_bytes", 5 * 1024 * 1024),
    )
    return SimpleNamespace(**base)


def _source(tmp_path: Path, payload: bytes = PNG) -> Path:
    path = tmp_path / "incoming.png"
    path.write_bytes(payload)
    return path


def sticker_message(path: str) -> Message:
    """真实链路的形态：消息 kind=sticker，但附件 kind=image。"""
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="wxg:g1",
        user_id="wxg:u1",
        nickname="阿猫",
        content="[表情:愉快的]",
        message_id="1:1",
        kind="sticker",
        attachments=[Attachment(kind="image", path=path)],
    )


# ---------------------------------------------------------------- 入站归档


def test_sticker_is_archived_and_indexed(tmp_path: Path) -> None:
    source = _source(tmp_path)
    msg = sticker_message(str(source))
    asyncio.run(materialize_attachments(msg, _settings(tmp_path)))

    library = tmp_path / "ws" / "stickers"
    files = sorted(path for path in library.iterdir() if path.suffix == ".png")
    assert len(files) == 1
    assert files[0].name == hashlib.md5(PNG).hexdigest()[:16] + ".png"
    assert files[0].read_bytes() == PNG

    index = (library / "index.md").read_text(encoding="utf-8")
    assert f"`stickers/{files[0].name}`" in index


def test_plain_image_is_not_archived(tmp_path: Path) -> None:
    msg = make_message(str(_source(tmp_path)), kind="image")
    asyncio.run(materialize_attachments(msg, _settings(tmp_path)))
    assert not (tmp_path / "ws" / "stickers").exists()


def test_disabled_library_does_not_touch_disk(tmp_path: Path) -> None:
    msg = sticker_message(str(_source(tmp_path)))
    asyncio.run(
        materialize_attachments(msg, _settings(tmp_path, enabled=False))
    )
    assert not (tmp_path / "ws" / "stickers").exists()


def test_oversized_sticker_skipped(tmp_path: Path) -> None:
    msg = sticker_message(str(_source(tmp_path)))
    asyncio.run(
        materialize_attachments(msg, _settings(tmp_path, max_bytes=8))
    )
    library = tmp_path / "ws" / "stickers"
    assert not library.exists() or not list(library.glob("*.png"))


def test_prune_keeps_newest(tmp_path: Path) -> None:
    library = tmp_path / "ws" / "stickers"
    library.mkdir(parents=True)
    now = time.time()
    old = library / "old.png"
    new = library / "new.png"
    old.write_bytes(PNG)
    new.write_bytes(PNG)
    os_utime(old, now - 3600)
    os_utime(new, now)

    media_module._prune_sticker_library(library, 1)

    assert not old.exists()
    assert new.exists()


def os_utime(path: Path, when: float) -> None:
    import os

    os.utime(path, (when, when))


# ---------------------------------------------------------------- 出站引用


def _resolve(path: str, settings: Any) -> list[Attachment]:
    return resolve_attachments(make_reply(Attachment(kind="image", path=path)), settings)


def test_resolve_allows_library_file(tmp_path: Path) -> None:
    library = tmp_path / "ws" / "stickers"
    library.mkdir(parents=True)
    target = library / "abc123.png"
    target.write_bytes(PNG)

    result = _resolve("stickers/abc123.png", _settings(tmp_path))

    assert len(result) == 1
    assert result[0].path == target.resolve().as_posix()
    assert result[0].kind == "image"


def test_resolve_rejects_escape_from_library(tmp_path: Path) -> None:
    (tmp_path / "ws" / "stickers").mkdir(parents=True)
    (tmp_path / "ws" / "secret.png").write_bytes(PNG)

    assert _resolve("stickers/../secret.png", _settings(tmp_path)) == []


def test_resolve_rejects_library_file_when_disabled(tmp_path: Path) -> None:
    library = tmp_path / "ws" / "stickers"
    library.mkdir(parents=True)
    (library / "abc.png").write_bytes(PNG)

    assert _resolve("stickers/abc.png", _settings(tmp_path, enabled=False)) == []


# ---------------------------------------------------------------- 配置校验


def test_build_defaults_when_section_absent() -> None:
    cfg = _build_sticker_library({})
    assert cfg == StickerLibraryConfig()


@pytest.mark.parametrize("value", ["a/b", "a\\b", "..", "", "../x"])
def test_build_rejects_nested_or_escaping_dir(value: str) -> None:
    with pytest.raises(ConfigError):
        _build_sticker_library({"dir": value})
