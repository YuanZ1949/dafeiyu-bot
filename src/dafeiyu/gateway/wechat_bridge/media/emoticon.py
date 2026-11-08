"""微信 4.2 表情缓存（``cache/<YYYY-MM>/Emoticon/...``）的解码。

收到的贴纸（``local_type == 47``）在本地缓存里是一个**裸 AES 文件**，没有头部：

- 文件名就是 32 位十六进制 md5（与消息 XML 的 ``<emoji md5="...">`` 一致）；
- 算法是 AES-128-CBC，``IV == key``，整文件单流，尾部 PKCS7；
- 明文是 ``wxgf`` 容器：``wxgf`` 魔数 + 元数据头，之后才是裸 HEVC Annex B。
  头的长度随版本不同（v0x12 为 22 字节，v0x13/v0x14 会内嵌数百字节），因此
  这里按「第一个参数集 NAL（VPS/SPS/PPS）」定位真正的码流起点。

``cfgDword`` 与图片 ``.dat`` 同源（见 :mod:`keygen`），但密钥派生多了
``EMOTICON`` 后缀且取 raw digest。模块只做「解密 + 转码」，不猜路径、
不抛异常；任何一步失败都返回 ``None``，调用方保留 ``[表情]`` 占位。

HEVC 需要外部 ``ffmpeg``：优先环境变量 ``DAFEIYU_FFMPEG``，否则查 ``PATH``。
缺失时安全降级（表情仍然是占位），不影响入站。多帧贴纸转 GIF，单帧转 PNG。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .aes import CipherError, aes_cbc_decrypt

log = logging.getLogger(__name__)

__all__ = [
    "decode_emoticon",
    "decrypt_emoticon",
    "is_emoticon_name",
    "wxgf_hevc",
    "wxgf_to_image",
]

#: 表情缓存文件名：32 位十六进制 md5，无扩展名。
_EMOTICON_NAME_RE = re.compile(r"[0-9a-fA-F]{32}")

_WXGF_MAGIC = b"wxgf"

#: HEVC Annex B 起始码。
_START_CODE = b"\x00\x00\x00\x01"

#: ``wxgf`` 头长度随版本增长：v0x12 是 22 字节，v0x13/v0x14 会多出
#: 一段几百字节到 1 KB 以上的内嵌数据（静态缩略图/附加参数）。实测真实
#: 贴纸的 VPS 出现在 22、894、961、1303 字节处，因此扫描上限取 64 KiB。
_MAX_HEADER_SCAN = 64 * 1024

#: HEVC NAL 类型：VPS=32、SPS=33、PPS=34。``nal_unit_type = (byte >> 1) & 0x3F``。
_PARAM_SET_TYPES = frozenset({32, 33, 34})

#: 单次转码的墙钟上限：贴纸很小，超时说明 ffmpeg 卡住了。本机最重的
#: 96 帧贴纸暖机约 3s，但首次冷启动被杀软扫描时实测到 19.98s，因此留到 30s。
_DEFAULT_TIMEOUT = 30.0

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_GIF_MAGIC = (b"GIF87a", b"GIF89a")

#: Windows 上不要为每次转码弹出控制台窗口。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_emoticon_name(name: str) -> bool:
    """``name`` 是否像表情缓存文件名（32 位十六进制，无扩展名）。"""
    return bool(_EMOTICON_NAME_RE.fullmatch(str(name or "")))


def _unpad_pkcs7(data: bytes) -> bytes:
    """去掉 PKCS7 填充；不像填充就原样返回。"""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decrypt_emoticon(
    raw: bytes | bytearray | memoryview,
    key: bytes | bytearray | memoryview,
) -> bytes | None:
    """AES-128-CBC 解密表情缓存，返回明文；不像表情就返回 ``None``。

    表情没有可校验的头部，因此用「解密后必须是 ``wxgf`` 容器、或本身就是
    PNG/JPEG/GIF」作为判据：随机数据同时命中这些魔数的概率可以忽略。
    少数缓存文件（本机实测约 1/8）存的直接是 PNG，以前被 ``wxgf`` 判据误杀。
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return None
    data = bytes(raw)
    if not data or len(data) % 16:
        return None
    if not isinstance(key, (bytes, bytearray, memoryview)):
        return None
    aes_key = bytes(key)
    if len(aes_key) != 16:
        return None
    try:
        plain = aes_cbc_decrypt(aes_key, aes_key, data)
    except (CipherError, ValueError, TypeError):
        return None
    plain = _unpad_pkcs7(plain)
    if _looks_like_image(plain) or plain.startswith(_WXGF_MAGIC):
        return plain
    return None


def _find_parameter_set(payload: bytes, limit: int) -> int:
    """找到第一个参数集（VPS/SPS/PPS）NAL 的起始码位置；没有返回 -1。

    不能只认「第一个起始码」：v0x13/v0x14 的内嵌数据里会出现形如
    ``00 00 00 01`` 的字节（实测 4d2c… 在 795 处就有一个，但 NAL 类型是
    保留值），从那里截断会让 ffmpeg 解不出帧。参数集才是可解码起点。
    """
    position = payload.find(_START_CODE, len(_WXGF_MAGIC), limit)
    while position >= 0:
        header = position + len(_START_CODE)
        if header < len(payload):
            nal_type = (payload[header] >> 1) & 0x3F
            if nal_type in _PARAM_SET_TYPES:
                return position
        position = payload.find(_START_CODE, header, limit)
    return -1


def wxgf_hevc(payload: bytes) -> bytes | None:
    """从 ``wxgf`` 明文里取出裸 HEVC 流；格式不符返回 ``None``。"""
    if not payload.startswith(_WXGF_MAGIC):
        return None
    limit = min(len(payload), _MAX_HEADER_SCAN)
    position = _find_parameter_set(payload, limit)
    if position < 0:
        return None
    stream = payload[position:]
    return stream if len(stream) > len(_START_CODE) else None


def _locate(program: str) -> str | None:
    """定位外部程序：``DAFEIYU_FFMPEG`` 优先，其次 ``PATH``。"""
    if program == "ffmpeg":
        override = str(os.environ.get("DAFEIYU_FFMPEG") or "").strip()
        if override:
            return override
    return shutil.which(program)


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def _frame_count(probe: str | None, source: Path, timeout: float) -> int:
    """用 ``ffprobe`` 数帧；缺失/失败/未知都返回 0（按单帧处理）。"""
    if not probe:
        return 0
    args = [
        probe,
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "csv=p=0",
        str(source),
    ]
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    output = (completed.stdout or b"").decode("ascii", "ignore").strip()
    if not output:
        return 0
    try:
        return int(output.splitlines()[0])
    except (ValueError, IndexError):
        return 0


def _looks_like_image(data: bytes) -> bool:
    return (
        data.startswith(_PNG_MAGIC)
        or data.startswith(_JPEG_MAGIC)
        or data.startswith(_GIF_MAGIC)
    )


def transcode(
    stream: bytes,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bytes | None:
    """把裸 HEVC 流转成 ``read`` 能打开的图片字节；失败返回 ``None``。"""
    binary = ffmpeg or _locate("ffmpeg")
    if not binary:
        log.debug("ffmpeg 不可用，表情保留占位")
        return None
    probe = ffprobe if ffprobe is not None else _locate("ffprobe")
    try:
        with tempfile.TemporaryDirectory(prefix="dafeiyu-emo-") as tmp:
            source = Path(tmp) / "in.hevc"
            source.write_bytes(stream)
            animated = _frame_count(probe, source, timeout) > 1
            target = Path(tmp) / ("out.gif" if animated else "out.png")
            args = [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "hevc",
                "-i",
                str(source),
            ]
            if animated:
                args += ["-loop", "0"]
            else:
                # 不加 ``-update 1`` 时 image2 会按序列模式报错。
                args += ["-frames:v", "1", "-update", "1"]
            args.append(str(target))
            completed = _run(args, timeout)
            if completed.returncode != 0:
                log.debug(
                    "ffmpeg 转码表情失败（exit=%s）", completed.returncode
                )
                return None
            data = target.read_bytes()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("表情转码失败：%s", exc)
        return None
    if not _looks_like_image(data):
        log.debug("ffmpeg 输出不是图片，表情保留占位")
        return None
    return data


def wxgf_to_image(
    payload: bytes,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bytes | None:
    """把 ``wxgf`` 明文里的 HEVC 流转成 PNG/GIF；不是 ``wxgf`` 返回 ``None``。

    微信 4.x 的聊天图片**原图**解密后也是 ``wxgf`` 容器，因此这个函数同时
    服务于图片原图与表情缓存。
    """
    stream = wxgf_hevc(payload)
    if stream is None:
        log.debug("wxgf 里没有找到 HEVC 起始码")
        return None
    return transcode(stream, ffmpeg=ffmpeg, ffprobe=ffprobe, timeout=timeout)


def decode_emoticon(
    raw: bytes | bytearray | memoryview,
    key: bytes | bytearray | memoryview,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bytes | None:
    """解密并转码一个表情缓存文件；任何失败都返回 ``None``。"""
    payload = decrypt_emoticon(raw, key)
    if payload is None:
        log.debug("表情缓存解密失败或不是 wxgf/图片，保留占位")
        return None
    if _looks_like_image(payload):
        # 少数缓存文件存的直接是 PNG/JPEG/GIF，无需 ffmpeg 二次转码。
        return payload
    return wxgf_to_image(payload, ffmpeg=ffmpeg, ffprobe=ffprobe, timeout=timeout)
