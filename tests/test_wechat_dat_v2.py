"""微信 4.x V2 ``.dat``（AES-ECB 头 + 单字节 XOR 尾）解码测试。

AES 是仓库内的纯 Python 实现，不依赖 Windows CNG，因此这些用例在任何
平台都会真跑；旧的「非 Windows 跳过」标记已删除。
"""

from __future__ import annotations

import pytest

from dafeiyu.gateway.wechat_bridge.media.aes import (
    aes_ecb_decrypt,
    aes_ecb_encrypt,
)
from dafeiyu.gateway.wechat_bridge.media.dat import decode_dat, decode_dat_container

SIGNATURE = b"\x07\x08V2\x08\x07"
# 真实 V2 密钥是 md5(...).hexdigest()[:16] 的 16 个 ASCII 字节。
AES_KEY = b"0123456789abcdef"
WRONG_KEY = b"fedcba9876543210"
XOR_KEY = 0x5A

# 32 字节 JPEG 头（末尾 0x1b，不会误判成 PKCS7 填充）与 32 字节尾部。
HEAD = b"\xff\xd8\xff\xe0" + bytes(range(28))
TAIL = bytes(range(32, 64))
assert len(HEAD) == 32 and len(TAIL) == 32


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    """补 PKCS7 到整块；长度已是整块时补一整块 0x10。"""
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def _build_v2(head: bytes, middle: bytes, tail: bytes) -> bytes:
    """按真实布局拼一个 V2 文件：头 + AES(PKCS7(head)) + 明文 middle + XOR(tail)。"""
    header = (
        SIGNATURE
        + len(head).to_bytes(4, "little")
        + len(tail).to_bytes(4, "little")
        + b"\x00"
    )
    cipher = aes_ecb_encrypt(AES_KEY, _pkcs7_pad(head))
    return header + cipher + middle + bytes(byte ^ XOR_KEY for byte in tail)


# ---------------------------------------------------------------- AES 原语
@pytest.mark.parametrize("key_size", [16, 24, 32])
def test_aes_ecb_round_trips_all_key_sizes(key_size: int) -> None:
    key = bytes(range(key_size))
    plain = bytes(range(32))

    assert aes_ecb_decrypt(key, aes_ecb_encrypt(key, plain)) == plain


@pytest.mark.parametrize(
    ("key_hex", "expected_hex"),
    [
        ("000102030405060708090a0b0c0d0e0f", "69c4e0d86a7b0430d8cdb78070b4c55a"),
        (
            "000102030405060708090a0b0c0d0e0f1011121314151617",
            "dda97ca4864cdfe06eaf70a0ec0d7191",
        ),
        (
            "000102030405060708090a0b0c0d0e0f"
            "101112131415161718191a1b1c1d1e1f",
            "8ea2b7ca516745bfeafc49904b496089",
        ),
    ],
    ids=["aes-128", "aes-192", "aes-256"],
)
def test_aes_ecb_fips197_vectors(key_hex: str, expected_hex: str) -> None:
    key = bytes.fromhex(key_hex)
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected = bytes.fromhex(expected_hex)

    assert aes_ecb_encrypt(key, plain) == expected
    assert aes_ecb_decrypt(key, expected) == plain


def test_aes_ecb_rejects_unaligned_data() -> None:
    with pytest.raises(ValueError):
        aes_ecb_encrypt(AES_KEY, b"\x00" * 15)
    with pytest.raises(ValueError):
        aes_ecb_decrypt(AES_KEY, b"\x00" * 17)


def test_aes_ecb_rejects_bad_key_size() -> None:
    with pytest.raises(ValueError):
        aes_ecb_decrypt(b"short", b"\x00" * 16)


# ---------------------------------------------------------------- V2 解码
@pytest.mark.parametrize("aes_size", [32, 30], ids=["aligned-16", "unaligned"])
@pytest.mark.parametrize(
    "middle",
    [b"", b"\x00" * 16],
    ids=["no-middle", "16b-middle"],
)
def test_v2_round_trips_known_layouts(aes_size: int, middle: bytes) -> None:
    head = HEAD[:aes_size]
    plain = head + middle + TAIL
    payload = _build_v2(head, middle, TAIL)
    # 构造结果必须符合「PKCS7 对齐」的长度公式。
    aligned = ((aes_size // 16) + 1) * 16
    assert len(payload) == 15 + aligned + len(middle) + len(TAIL)

    assert decode_dat(payload, aes_key=AES_KEY, xor_key=XOR_KEY) == plain


def test_v2_large_middle_is_passed_through() -> None:
    """真实大图：XOR 段被限制在 1 MiB，剩余明文任意长地夹在中间。"""
    middle = bytes(range(256)) * 9  # 2304 字节，远大于 16
    payload = _build_v2(HEAD, middle, TAIL)

    assert decode_dat(payload, aes_key=AES_KEY, xor_key=XOR_KEY) == (
        HEAD + middle + TAIL
    )


def test_v2_without_keys_returns_none() -> None:
    payload = _build_v2(HEAD, b"", TAIL)

    assert decode_dat(payload) is None
    assert decode_dat(payload, aes_key=AES_KEY) is None
    assert decode_dat(payload, xor_key=XOR_KEY) is None


def test_v2_with_invalid_keys_returns_none() -> None:
    payload = _build_v2(HEAD, b"", TAIL)

    assert decode_dat(payload, aes_key=b"x" * 15, xor_key=XOR_KEY) is None
    assert decode_dat(payload, aes_key=AES_KEY, xor_key=0x100) is None


def test_v2_with_wrong_aes_key_returns_none() -> None:
    payload = _build_v2(HEAD, b"", TAIL)

    assert decode_dat(payload, aes_key=WRONG_KEY, xor_key=XOR_KEY) is None


def test_v2_truncated_returns_none() -> None:
    payload = _build_v2(HEAD, b"", TAIL)

    assert decode_dat(payload[:-1], aes_key=AES_KEY, xor_key=XOR_KEY) is None
    assert decode_dat(payload[:20], aes_key=AES_KEY, xor_key=XOR_KEY) is None


def test_v2_corrupted_sizes_return_none() -> None:
    payload = bytearray(_build_v2(HEAD, b"", TAIL))
    payload[6:10] = (10**6).to_bytes(4, "little")

    assert decode_dat(bytes(payload), aes_key=AES_KEY, xor_key=XOR_KEY) is None


def test_wxgf_is_not_treated_as_readable_image() -> None:
    """原图容器的 ``wxgf`` 解密成功也不落盘：``read`` 打不开它。"""
    head = b"wxgf" + bytes(28)
    payload = _build_v2(head, b"", TAIL)

    assert decode_dat(payload, aes_key=AES_KEY, xor_key=XOR_KEY) is None


def test_container_returns_wxgf_plaintext_for_transcoding() -> None:
    """容器解码保留 ``wxgf`` 明文，供上层转成高分辨率 PNG。"""
    head = b"wxgf" + bytes(28)
    payload = _build_v2(head, b"", TAIL)

    assert decode_dat_container(payload, aes_key=AES_KEY, xor_key=XOR_KEY) == (
        head + TAIL
    )
    # 判据不变：decode_dat 仍拒绝 wxgf，避免把不可读容器物化进 workspace。
    assert decode_dat(payload, aes_key=AES_KEY, xor_key=XOR_KEY) is None


def test_container_requires_v2_and_keys() -> None:
    payload = _build_v2(b"wxgf" + bytes(28), b"", TAIL)

    assert decode_dat_container(b"\x89PNG\r\n\x1a\n" + bytes(16)) is None
    assert decode_dat_container(payload) is None
    assert decode_dat_container(payload, aes_key=AES_KEY) is None
    assert decode_dat_container(payload, xor_key=XOR_KEY) is None
    # 容器层不做「密钥对不对」的判定：错密钥解出的乱码由上层 wxgf/图片魔数
    # 校验拦下（decode_dat 已覆盖）。这里只确认不抛异常。
    assert decode_dat_container(
        payload, aes_key=WRONG_KEY, xor_key=XOR_KEY
    ) is not None
    assert decode_dat(payload, aes_key=WRONG_KEY, xor_key=XOR_KEY) is None


# ---------------------------------------------------------------- 旧格式回归
def test_legacy_xor_0x37_still_decodes() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    encoded = bytes(byte ^ 0x37 for byte in png)

    assert decode_dat(encoded) == png
