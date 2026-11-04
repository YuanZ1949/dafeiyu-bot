"""微信 ``.dat`` 图片解码：只做「能证明是图片」的还原，其余一律返回 None。

背景（公开格式知识）：

- 微信 4.x 的聊天图片缓存（``*.dat``）多数仍是**整文件单字节 XOR**，常见密钥
  为 ``0x37`` / ``0x38`` / ``0x39``；不同客户端版本/文件会落在这几个值上。
- 少数文件已经是明文（PNG/JPEG/GIF/WEBP/BMP），直接透传。
- 新版 AES 加密的容器不在本模块范围内：仓库没有可用的 AES 依赖，也没有可
  验证的格式；识别不出就返回 None，让调用方保留 ``[图片]`` 占位。

设计约束：纯函数、绝不抛异常、不在日志里打印文件内容或完整路径。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

__all__ = ["decode_dat"]

#: 单字节 XOR 候选；只保留公开文档里最常见的三个值。
_XOR_KEYS = (0x37, 0x38, 0x39)

#: 常见图片魔数（RIFF 需要再看 WEBP 标记）。
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_GIF87 = b"GIF87a"
_GIF89 = b"GIF89a"
_BMP = b"BM"
_RIFF = b"RIFF"
_WEBP = b"WEBP"


def _sniff_image(data: bytes) -> bool:
    """只认明确是图片的头部；不做解码，也不相信扩展名。"""
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


def decode_dat(data: bytes) -> bytes | None:
    """把微信 ``.dat`` 还原成图片字节；无法证明是图片时返回 ``None``。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    if len(raw) < 4:
        return None
    if _sniff_image(raw):
        return raw
    for key in _XOR_KEYS:
        decoded = bytes(byte ^ key for byte in raw)
        if _sniff_image(decoded):
            return decoded
    log.debug("无法识别的 .dat（%d 字节），保留占位", len(raw))
    return None
