"""微信 ``.dat`` 图片解码：只做「能证明是图片」的还原，其余一律返回 None。

背景（公开格式知识 + 本机实测）：

- 微信 4.x 的聊天图片缓存（``*.dat``）多数仍是**整文件单字节 XOR**，常见密钥
  为 ``0x37`` / ``0x38`` / ``0x39``；不同客户端版本/文件会落在这几个值上。
- 少数文件已经是明文（PNG/JPEG/GIF/WEBP/BMP/WXGF），直接透传。
- 新版 4.x 的 **V2 容器**：前 15 字节是头部，之后是 AES-128-ECB 加密的图片头
  和单字节 XOR 的尾部。AES 密钥由调用方传入，拿不到时安全降级返回 ``None``。

V2 头部（小端）::

    [0:6]    签名 07 08 56 32 08 07（"\\x07\\x08V2\\x08\\x07"）
    [6:10]   aes_size（u32）
    [10:14]  xor_size（u32）
    [14]     1 字节填充（忽略）

布局有三种真实形态（统一按「AES 头 + 明文 middle + XOR 尾」解析）::

    旧：15B 头 + AES(aligned) + XOR(xor_size)
    新：15B 头 + AES(aligned) + 16B 明文分隔 + XOR(xor_size)
    大图：15B 头 + AES(aligned) + 任意长明文 middle + XOR(≤1 MiB)

大图的 XOR 段被客户端限制在 1 MiB 左右，剩余明文直接夹在 AES 头与 XOR 尾
之间；因此不能要求 ``total == 头 + aligned + xor_size``，只校验长度足够。

AES 区长度是 **PKCS7 对齐**，且永远严格大于 ``aes_size``::

    aligned = ((aes_size // 16) + 1) * 16

中间的明文分隔（如果有）不写死长度，统一落在 ``raw_middle`` 里原样透传。
AES 密钥由调用方按微信规则派生后传入（16 个 ASCII 十六进制字符）。

设计约束：纯函数、绝不抛异常、不在日志里打印文件内容或完整路径。
"""

from __future__ import annotations

import logging

from .aes import CipherError, aes_ecb_decrypt

log = logging.getLogger(__name__)

__all__ = ["decode_dat"]

#: 单字节 XOR 候选；只保留公开文档里最常见的三个值。
_XOR_KEYS = (0x37, 0x38, 0x39)

#: V2 容器签名与头部长度。
_V2_SIGNATURE = b"\x07\x08V2\x08\x07"
_V2_HEADER_SIZE = 15

#: 常见图片魔数（RIFF 需要再看 WEBP 标记）。
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_GIF87 = b"GIF87a"
_GIF89 = b"GIF89a"
_BMP = b"BM"
_RIFF = b"RIFF"
_WEBP = b"WEBP"


def _sniff_image(data: bytes) -> bool:
    """只认明确是图片、且 ``read`` 工具能打开的头部。

    微信 4.x 的原图常是私有 ``wxgf`` 容器：解密成功也不返回，否则会把一个
    读不了的 ``.dat`` 物化进 workspace。真正的可读版本在 ``_h`` / ``_t``。
    """
    if data.startswith(_PNG):
        return True
    if data.startswith(_JPEG):
        return True
    if data.startswith((_GIF87, _GIF89)):
        return True
    if data.startswith(_BMP):
        return True
    # RIFF....WEBP
    return len(data) >= 12 and data.startswith(_RIFF) and data[8:12] == _WEBP


def _unpad_pkcs7(data: bytes) -> bytes:
    """去掉 AES 明文尾部的 PKCS7 填充；不像填充就原样返回。"""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def _valid_key(aes_key: bytes | bytearray | memoryview | None) -> bytes | None:
    """校验调用方传入的 AES 密钥；不合法时返回 ``None``。"""
    if not isinstance(aes_key, (bytes, bytearray, memoryview)):
        return None
    key = bytes(aes_key)
    if len(key) not in (16, 24, 32):
        return None
    return key


def _valid_xor_key(xor_key: int | None) -> int | None:
    """校验单字节 XOR 密钥；不合法时返回 ``None``。"""
    if not isinstance(xor_key, int):
        return None
    if not 0 <= xor_key <= 0xFF:
        return None
    return xor_key


def _decode_v2(raw: bytes, aes_key: bytes | None, xor_key: int | None) -> bytes | None:
    """解析 V2 容器；缺密钥、长度不符或解密失败一律返回 ``None``。"""
    key = _valid_key(aes_key)
    xor = _valid_xor_key(xor_key)
    if key is None or xor is None:
        return None
    if len(raw) < _V2_HEADER_SIZE:
        return None

    aes_size = int.from_bytes(raw[6:10], "little")
    xor_size = int.from_bytes(raw[10:14], "little")
    if aes_size <= 0:
        return None

    total = len(raw)
    # AES 区是「整块 PKCS7」对齐，即使 aes_size 本身已是 16 的倍数也要多补
    # 一整块 0x10（实测微信 4.2 的 aes_size=1024 时密文占 1040 字节）。
    aligned = ((aes_size // 16) + 1) * 16
    middle_start = _V2_HEADER_SIZE + aligned
    tail_start = total - xor_size
    # 大图允许任意长的明文 middle（见模块文档），所以只校验尾巴装得下。
    if xor_size < 0 or middle_start > total or tail_start < middle_start:
        return None

    # 分隔符（如果有）自然落进 raw_middle。

    try:
        plain_head = _unpad_pkcs7(aes_ecb_decrypt(key, raw[_V2_HEADER_SIZE:middle_start]))
    except (CipherError, ValueError, TypeError, OSError):
        return None

    middle = raw[middle_start:tail_start]
    tail = bytes(byte ^ xor for byte in raw[tail_start:])
    return plain_head + middle + tail


def decode_dat(
    data: bytes,
    *,
    aes_key: bytes | None = None,
    xor_key: int | None = None,
) -> bytes | None:
    """把微信 ``.dat`` 还原成图片字节；无法证明是图片时返回 ``None``。

    ``aes_key`` / ``xor_key`` 只对 V2 容器有效，且必须同时提供；缺任一项或
    密钥错误时安全降级返回 ``None``，绝不向调用方抛异常。
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    if len(raw) < 4:
        return None
    if _sniff_image(raw):
        return raw

    if raw.startswith(_V2_SIGNATURE):
        try:
            decoded = _decode_v2(raw, aes_key, xor_key)
        except Exception:  # 防御性兜底：解析层永远不向调用方抛异常
            log.debug("V2 .dat 解码异常（%d 字节），保留占位", len(raw))
            return None
        if decoded is not None and _sniff_image(decoded):
            return decoded
        log.debug("V2 .dat 无法还原为图片（%d 字节），保留占位", len(raw))
        return None

    for key in _XOR_KEYS:
        decoded = bytes(byte ^ key for byte in raw)
        if _sniff_image(decoded):
            return decoded
    log.debug("无法识别的 .dat（%d 字节），保留占位", len(raw))
    return None
