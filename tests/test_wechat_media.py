"""todo 11：`.dat` 解码与 align 的 media 透传（纯本地，不碰真微信）。"""

from __future__ import annotations

import os
from typing import cast

import pytest

from dafeiyu.gateway.wechat_bridge import align as align_module
from dafeiyu.gateway.wechat_bridge.align import Entry, SessionAligner
from dafeiyu.gateway.wechat_bridge.media.dat import decode_dat

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 32
MEDIA = {
    "kind": "image",
    "path": "C:/wx/media/a.png",
    "name": "a.png",
    "size": 64,
    "duration": 0,
}


# ------------------------------------------------------------------ .dat 解码
def test_plain_png_passes_through() -> None:
    assert decode_dat(PNG) == PNG


@pytest.mark.parametrize("key", [0x37, 0x38, 0x39])
def test_xor_encoded_png_round_trips(key: int) -> None:
    encoded = bytes(byte ^ key for byte in PNG)

    assert decode_dat(encoded) == PNG


def test_xor_of_jpeg_is_recognized() -> None:
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    encoded = bytes(byte ^ 0x37 for byte in jpeg)

    assert decode_dat(encoded) == jpeg


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x89P",
        b"\x89PN",
        b"PK\x03\x04" + b"\x00" * 20,  # ZIP
        b"%PDF-1.7" + b"\x00" * 20,
        os.urandom(100),
    ],
)
def test_unknown_payload_returns_none(payload: bytes) -> None:
    assert decode_dat(payload) is None


def test_never_raises_on_wrong_type() -> None:
    assert decode_dat(cast(bytes, cast(object, None))) is None
    assert decode_dat(cast(bytes, cast(object, "not bytes"))) is None


# ---------------------------------------------------------------- align 透传
def _entry_raw(
    text: str, *, media: dict[str, str | int] | None = None
) -> dict[str, object]:
    raw: dict[str, object] = {
        "nickname": "阿猫",
        "text": text,
        "kind": "image",
        "ts": 1,
    }
    if media is not None:
        raw["media"] = media
    return raw


def test_entry_media_survives_export_and_seed() -> None:
    aligner = SessionAligner(
        "wxg:abc", "测试群", account_nickname="小助手", clock=lambda: 1.0
    )
    aligner.poll([_entry_raw("旧消息")])  # 第一轮只建基线

    result = aligner.poll(
        [_entry_raw("旧消息"), _entry_raw("新图片", media=MEDIA)]
    )

    assert len(result.messages) == 1
    assert result.messages[0]["media"] == MEDIA

    exported = aligner.export()
    assert exported[-1]["media"] == MEDIA

    entry = align_module._entry_from(exported[-1])
    assert isinstance(entry, Entry)
    assert entry.media == MEDIA

    # 重启续跑：seed 后再来一条，media 仍然进事件信封。
    restarted = SessionAligner(
        "wxg:abc", "测试群", account_nickname="小助手", clock=lambda: 2.0
    )
    restarted.seed(exported)
    again = restarted.poll(
        [_entry_raw("旧消息"), _entry_raw("新图片", media=MEDIA), _entry_raw("又一张", media=MEDIA)]
    )

    assert [message["media"] for message in again.messages] == [MEDIA]


def test_non_dict_media_is_ignored() -> None:
    entry = align_module._entry_from(
        {"nickname": "阿猫", "text": "[图片]", "kind": "image", "media": "oops"}
    )

    assert entry.media == {}
