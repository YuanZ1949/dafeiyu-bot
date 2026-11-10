"""微信入站附件物化：把本地库发现的媒体复制进 ``<workspace>/images/``。

消息入库前只有 content 字符串能到达大脑，所以图片必须落盘并把 workspace 相对
路径写回 content；``Attachment`` 仍只是内存元数据，不入库、不改 schema。

设计约束（与 ``images.py`` 相同的退化原则，但职责不同）：

- 带本地路径的附件按原逻辑复制/解码；只有 URL 的附件（QQ 语音/视频、
  微信表情 `cdnurl`）先下载再处理；
- 只有 ``kind == "image"`` 会复制/解码；``kind == "file"`` 只追加文本注记；
- ``.dat`` 由调用方注入的 ``decode`` 还原，返回 ``None`` 就保留占位；
- 快路径（开关关闭 / 没有带路径的附件）零文件系统调用；
- 本模块永不抛异常：任何失败都保留原始占位，让入站继续。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx

from . import imaging, speech
from .config import Settings
from .imaging import locate as _locate
from .models import Attachment, Message, Segment
from .netssl import default_ssl_context

log = logging.getLogger(__name__)

_PLACEHOLDER = "[图片]"
_ANNOTATED = "[图片]（图存 {path}，可用 read 查看）"
_ANNOTATION_MARK = "（图存"
_FILE_PREFIX = "[文件]"

#: 视频原文件的占位与注记。注意：原文件先复制进 workspace，再抽帧。
_VIDEO_PLACEHOLDER = "[视频]"
_VIDEO_MARK_FRAMES = "（已抽帧"
_VIDEO_MARK_ORIGINAL = "（原文件"
_VIDEO_MARKS = (_VIDEO_MARK_FRAMES, _VIDEO_MARK_ORIGINAL)

#: 语音占位：QQ 语音段是 ``[语音]``，微信同样。注记独立于图片正则。
_VOICE_PLACEHOLDER = "[语音]"
#: 任何 ``[语音]（…）`` 都视为已注记，避免第二次调用重复替换。
_VOICE_ANNOTATION_MARK = "（"
_VOICE_PLACEHOLDER_RE = re.compile(re.escape(_VOICE_PLACEHOLDER))

#: QQ/微信语音段常见的下载地址后缀。
_VOICE_URL_SUFFIXES = frozenset(
    {".silk", ".amr", ".wav", ".mp3", ".m4a", ".ogg", ".aac", ".flac"}
)
_VIDEO_URL_SUFFIXES = frozenset(
    {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".flv", ".webm", ".wmv", ".3gp"}
)

_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)

#: 抽帧位置（时长百分比）与抽出来的文件名前缀。
_VIDEO_FRAME_PERCENTS: tuple[float, ...] = (0.10, 0.35, 0.60, 0.85)

#: 视频原文件上限：与 repository 的 MAX_VIDEO_BYTES 保持一致；这里只是
#: 防止异常大的文件被整块读进内存，不参与媒体发现。
_MAX_VIDEO_BYTES = 100 * 1024 * 1024

#: ffmpeg 单次调用上限：抽帧要解码整段视频，比表情转码宽松。
_VIDEO_TIMEOUT = 60.0

#: 超过这个大小的 ``.dat`` 不再尝试图片解密：纯 Python AES 会拖死事件循环。
_VIDEO_DECODE_LIMIT = 4 * 1024 * 1024

#: Windows 上不要为每次 ffmpeg 调用弹出控制台窗口。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_PLACEHOLDER_RE = re.compile(re.escape(_VIDEO_PLACEHOLDER))

#: 可被注记的图片/表情占位；表情可带名称或 md5 前缀。
_PLACEHOLDER_RE = re.compile(r"\[(?:图片|表情(?::[^\]\r\n]*)?)\]")

#: 微信表情缓存文件名：32 位 hex、无扩展名（见 media/emoticon）。
_HEX32_NAME_RE = re.compile(r"[0-9a-fA-F]{32}")

#: ``decode`` 允许同步或异步：表情解码要调 ffmpeg，实现方会放到线程里。
Decode = Callable[[bytes], bytes | None | Awaitable[bytes | None]]

#: QQ 语音/视频只有 URL 没有本地路径：下载函数可注入，签名与 images 一致。
FetchFn = Callable[[str, float, int], Awaitable[bytes]]


async def _default_fetch(url: str, timeout: float, max_bytes: int) -> bytes:
    """流式下载，边收边判上限，超限即断。"""
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, verify=default_ssl_context()
    ) as client, client.stream("GET", url) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"附件超过大小上限 {max_bytes} 字节")
            chunks.append(chunk)
        return b"".join(chunks)

#: 与 ``images.py`` 相同的魔数表。这里刻意复制一份：本模块只依赖 models，
#: 不调用另一个模块的私有函数（那会让两边的生命周期互相绑定）。
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def _sniff_ext(data: bytes) -> str:
    """按魔数猜扩展名；RIFF 需要再看 WEBP 标记。"""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def _sniff_video_ext(data: bytes) -> str:
    """按容器魔数猜视频扩展名；认不出返回空串。

    只用于「``.dat`` 解码后恰好拿到视频字节」这一少见分支；认不出时保留
    原始 ``.dat``，不猜格式。
    """
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return ".avi"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return ".mkv"
    if data.startswith(b"FLV"):
        return ".flv"
    return ""


def _target(directory: Path, source: str, data: bytes, suffix: str) -> Path:
    """生成落盘路径：时间戳 + 来源 md5 前 6 位 + 嗅探到的扩展名。"""
    digest = hashlib.md5(source.encode("utf-8", "ignore")).hexdigest()[:6]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = _sniff_ext(data) or suffix.lower() or ".jpg"
    return directory / f"{stamp}-{digest}{ext}"


def _annotate_first(content: str, annotation: str) -> str:
    """替换第一个**未**带注记的 ``[图片]``/``[表情...]``，避免重复注解已物化的占位。"""
    # 注记模板固定以 ``[图片]`` 开头；表情占位只换前缀，复用同一段后缀。
    suffix = (
        annotation[len(_PLACEHOLDER) :]
        if annotation.startswith(_PLACEHOLDER)
        else annotation
    )
    for match in _PLACEHOLDER_RE.finditer(content):
        tail = content[match.end() :]
        if not tail.startswith(_ANNOTATION_MARK):
            return content[: match.start()] + match.group(0) + suffix + tail
    return content


def _annotate_video(content: str, suffix: str) -> str:
    """把第一个未注记的 ``[视频]`` 换成本轮抽帧/原文件的注记。"""
    for match in _VIDEO_PLACEHOLDER_RE.finditer(content):
        tail = content[match.end() :]
        if not tail.startswith(_VIDEO_MARKS):
            return content[: match.start()] + match.group(0) + suffix + tail
    return content


def _annotate_voice(content: str, suffix: str) -> str:
    """把第一个未注记的 ``[语音]`` 换成本轮转写/原文件注记。"""
    for match in _VOICE_PLACEHOLDER_RE.finditer(content):
        tail = content[match.end() :]
        if not tail.startswith(_VOICE_ANNOTATION_MARK):
            return content[: match.start()] + match.group(0) + suffix + tail
    return content


def _ext_from_url(url: str, allowed: frozenset[str]) -> str:
    suffix = Path(url.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
    return suffix if suffix in allowed else ""


def _voice_ext(data: bytes, source: str) -> str:
    """按魔数猜语音扩展名；认不出时退回 URL 后缀，兜底 ``.bin``。"""
    if data.startswith((b"\x02#!SILK_V", b"#!SILK_V3")):
        return ".silk"
    if data.startswith(b"#!AMR"):
        return ".amr"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"ID3") or data[:2] in (
        b"\xff\xfb",
        b"\xff\xf3",
        b"\xff\xf2",
        b"\xff\xf1",
    ):
        return ".mp3"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".m4a"
    return _ext_from_url(source, _VOICE_URL_SUFFIXES) or ".bin"


def _voice_target(directory: Path, source: str, data: bytes) -> Path:
    """``voices/<时间戳>-<md5(来源)[:6]><扩展名>``。"""
    digest = hashlib.md5(source.encode("utf-8", "ignore")).hexdigest()[:6]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return directory / f"{stamp}-{digest}{_voice_ext(data, source)}"


def _has_media_source(attachment: Attachment) -> bool:
    """带本地路径的附件，或只有 http(s) 来源的语音/视频（QQ）。"""
    if attachment.path:
        return True
    if attachment.kind not in ("voice", "record", "video", "image"):
        return False
    return bool(_HTTP_RE.match((attachment.source or "").strip()))


async def _download_video(
    source: str,
    *,
    root: Path,
    settings: Settings,
    fetch: FetchFn,
) -> Path | None:
    """把 QQ 视频 URL 流式下载到临时文件；失败返回 ``None``。"""
    images_cfg = getattr(settings, "images", None)
    timeout = float(getattr(images_cfg, "timeout_seconds", 8.0) or 8.0)
    try:
        data = await fetch(source, timeout, _MAX_VIDEO_BYTES)
    except Exception:
        log.info("视频下载失败，保持占位：%s", source, exc_info=True)
        return None
    if not data or len(data) > _MAX_VIDEO_BYTES:
        log.info(
            "视频大小不可接受（%d 字节，上限 %d），保持占位：%s",
            len(data) if data else 0,
            _MAX_VIDEO_BYTES,
            source,
        )
        return None
    ext = (
        _sniff_video_ext(data)
        or _ext_from_url(source, _VIDEO_URL_SUFFIXES)
        or ".mp4"
    )
    digest = hashlib.md5(source.encode("utf-8", "ignore")).hexdigest()[:8]
    directory = root / "videos"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f".incoming-{digest}{ext}"
        await asyncio.to_thread(target.write_bytes, data)
    except OSError:
        log.info("视频临时文件写盘失败：%s", source, exc_info=True)
        return None
    return target


async def _download_image(
    source: str,
    *,
    settings: Settings,
    fetch: FetchFn,
    limit: int,
) -> bytes | None:
    """下载 URL 图片（微信表情 cdnurl）；失败/超限返回 ``None``。"""
    images_cfg = getattr(settings, "images", None)
    timeout = float(getattr(images_cfg, "timeout_seconds", 10.0) or 10.0)
    if limit <= 0:
        limit = _MAX_VIDEO_BYTES
    try:
        data = await fetch(source, timeout, limit)
    except Exception:
        log.info("图片下载失败，保持占位：%s", source, exc_info=True)
        return None
    if not data or len(data) > limit:
        log.info(
            "图片大小不可接受（%d 字节，上限 %d），保持占位：%s",
            len(data) if data else 0,
            limit,
            source,
        )
        return None
    return data


async def _materialize_voice(
    msg: Message,
    attachment: Attachment,
    *,
    root: Path,
    settings: Settings,
    fetch: FetchFn,
    annotate: bool,
) -> Attachment:
    """语音落盘 + 转写；任何失败都退回原附件、保留 ``[语音]``。"""
    cfg = getattr(settings, "speech", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        # 关闭时完全不处理：不下载、不落盘、不改 content。
        return attachment

    source_value = attachment.path or (attachment.source or "").strip()
    data: bytes | None = None
    if attachment.path:
        try:
            data = await asyncio.to_thread(Path(attachment.path).read_bytes)
        except OSError:
            log.info(
                "语音读取失败，保持占位：msg=%s", msg.message_id, exc_info=True
            )
            return attachment
    elif _HTTP_RE.match(source_value):
        max_bytes = int(getattr(cfg, "max_bytes", 0) or 0)
        timeout = float(getattr(cfg, "timeout_seconds", 10.0) or 10.0)
        try:
            data = await fetch(
                source_value, timeout, max_bytes or _MAX_VIDEO_BYTES
            )
        except Exception:
            log.info(
                "语音下载失败，保持占位：msg=%s", msg.message_id, exc_info=True
            )
            return attachment
    else:
        return attachment

    if not data:
        return attachment
    max_bytes = int(getattr(cfg, "max_bytes", 0) or 0)
    if max_bytes > 0 and len(data) > max_bytes:
        log.info(
            "语音大小不可接受（%d 字节，上限 %d），保持占位：msg=%s",
            len(data),
            max_bytes,
            msg.message_id,
        )
        return attachment

    directory = root / "voices"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = _voice_target(directory, source_value, data)
        await asyncio.to_thread(target.write_bytes, data)
    except OSError:
        log.info("语音写盘失败，保持占位：msg=%s", msg.message_id, exc_info=True)
        return attachment

    relative = _materialized_relative(target, root) or f"voices/{target.name}"
    duration = int(getattr(attachment, "duration", 0) or 0)
    max_seconds = int(getattr(cfg, "max_seconds", 0) or 0)
    if max_seconds > 0 and duration > max_seconds:
        suffix = (
            f"（太长，未转写；原文件 {relative}）"
            if annotate
            else "（太长，未转写）"
        )
    else:
        # 转写不受 annotate 影响：本地模型没有 read 工具也能直接读正文。
        text = await speech.transcribe(target, settings)
        if text:
            suffix = (
                f"（转写：{text}，原文件 {relative}）"
                if annotate
                else f"（转写：{text}）"
            )
        else:
            suffix = (
                f"（听不清，原文件 {relative}）" if annotate else "（听不清）"
            )
    msg.content = _annotate_voice(msg.content, suffix)
    return Attachment(
        kind=attachment.kind,
        source=attachment.source,
        path=relative,
        name=target.name,
        size=len(data),
        duration=attachment.duration,
    )


def _materialized_relative(path: Path, root: Path) -> str | None:
    """``path`` 已位于 workspace 内时返回其相对 posix 路径。"""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _cleanup(directory: Path, retention_days: int) -> None:
    """惰性清理：retention_days>0 时删掉过期文件。0 = 不清理。"""
    if retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:  # 并发删除/权限问题：清理失败不致命
            log.debug("清理过期图片失败：%s", entry, exc_info=True)


def _file_note(attachment: Attachment) -> str:
    name = attachment.name or Path(attachment.path).name
    return f"{_FILE_PREFIX} {name} ({attachment.path})"


def _probe_duration(ffmpeg: str, ffprobe: str | None, source: Path) -> float:
    """读视频时长（秒）；ffprobe 缺失/失败时退回解析 ffmpeg 的 ``Duration:``。"""
    if ffprobe:
        try:
            completed = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(source),
                ],
                check=False,
                capture_output=True,
                timeout=_VIDEO_TIMEOUT,
                creationflags=_NO_WINDOW,
            )
            text = (completed.stdout or b"").decode("ascii", "ignore").strip()
            if completed.returncode == 0 and text:
                value = float(text.splitlines()[0])
                if value > 0:
                    return value
        except (OSError, subprocess.SubprocessError, ValueError):
            log.debug("ffprobe 读时长失败，尝试 ffmpeg 输出：%s", source)

    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(source)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_VIDEO_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    match = _DURATION_RE.search((completed.stderr or b"").decode("utf-8", "ignore"))
    if match is None:
        return 0.0
    hours, minutes, seconds = match.groups()
    try:
        value = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return 0.0
    return value if value > 0 else 0.0


def _extract_frame(
    ffmpeg: str, source: Path, target: Path, offset: float
) -> bool:
    """在 ``offset`` 秒处抽 1 帧 jpg；失败返回 False，绝不抛异常。"""
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{offset:.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(target),
    ]
    try:
        completed = subprocess.run(
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_VIDEO_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ffmpeg 抽帧失败：%s", exc)
        return False
    if completed.returncode != 0:
        log.debug("ffmpeg 抽帧失败（exit=%s）", completed.returncode)
        return False
    try:
        return target.is_file() and target.stat().st_size > 0
    except OSError:
        return False


async def _extract_video_frames(
    root: Path, source: Path, digest: str
) -> list[str]:
    """按固定百分比抽帧到 ``images/``，返回 workspace 相对路径列表。

    ffmpeg 缺失、读不到时长、单帧失败都只跳过，不抛异常；最多 4 张。
    """
    ffmpeg = _locate("ffmpeg")
    if not ffmpeg:
        log.debug("ffmpeg 不可用，视频只保留原文件")
        return []
    duration = await asyncio.to_thread(
        _probe_duration, ffmpeg, _locate("ffprobe"), source
    )
    if duration <= 0:
        log.debug("读不到视频时长，跳过抽帧：%s", source.name)
        return []
    directory = root / "images"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.info("视频抽帧目录创建失败：%s", directory, exc_info=True)
        return []

    frames: list[str] = []
    for index, ratio in enumerate(_VIDEO_FRAME_PERCENTS, start=1):
        target = directory / f"video-{digest}-{index}.jpg"
        ok = await asyncio.to_thread(
            _extract_frame, ffmpeg, source, target, duration * ratio
        )
        if ok:
            frames.append(f"images/{target.name}")
    return frames


#: GIF 抽帧序号：第 0 帧经常是空白/纯色（微信动图实测），所以中段也取一张。
_GIF_FRAME_INDICES = (0, 8, 24)

#: 帧数不超过这个值就按静图处理：微信原图实测 2 帧且首帧纯白。
_STILL_GIF_FRAMES = 2

#: 静图判定时最多尝试的帧数（2 帧容器两帧都转，取体积最大者）。
_MAX_STILL_GIF_SAMPLES = 2


def _gif_frame_count(data: bytes) -> int:
    """本地数 GIF 图像帧数（不依赖 ffmpeg）；非 GIF 或坏数据返回 0。

    只做块结构遍历：扩展块按子块跳过，图像描述符计一帧。帧数未知时返回 0，
    调用方沿用旧的 ``0/8/24`` 抽帧策略。
    """
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return 0

    def skip_blocks(pos: int) -> int:
        while pos < len(data):
            length = data[pos]
            pos += 1
            if length == 0:
                return pos
            pos += length
        return len(data)

    pos = 13
    flags = data[10]
    if flags & 0x80:
        pos += 3 * (2 ** ((flags & 0x07) + 1))
    count = 0
    while pos < len(data):
        marker = data[pos]
        if marker == 0x3B:  # trailer
            break
        if marker == 0x2C:  # image descriptor
            if pos + 10 > len(data):
                break
            packed = data[pos + 9]
            pos += 10
            if packed & 0x80:
                pos += 3 * (2 ** ((packed & 0x07) + 1))
            pos = skip_blocks(pos + 1)  # 跳过 LZW 最小码长 + 子块
            count += 1
        elif marker == 0x21:  # 扩展块
            pos = skip_blocks(pos + 2)
        else:
            break
    return count


def _gif_frame_indices(count: int) -> tuple[int, ...]:
    """按真实帧数给出抽帧序号；未知（0）时沿用旧策略。"""
    if count <= 0:
        return _GIF_FRAME_INDICES
    if count <= _STILL_GIF_FRAMES:
        return tuple(range(min(count, _MAX_STILL_GIF_SAMPLES)))
    return _GIF_FRAME_INDICES


def _extract_gif_frame(ffmpeg: str, source: Path, target: Path, index: int) -> bool:
    """用 ffmpeg 的 select 滤镜取 GIF 第 ``index`` 帧；失败返回 False。"""
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"select=eq(n\\,{index})",
        "-vsync",
        "0",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(target),
    ]
    try:
        completed = subprocess.run(
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_VIDEO_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("GIF 抽帧失败：%s", exc)
        return False
    if completed.returncode != 0:
        log.debug("GIF 抽帧失败（exit=%s）", completed.returncode)
        return False
    try:
        return target.is_file() and target.stat().st_size > 0
    except OSError:
        return False


async def _extract_gif_frames(
    root: Path, source: Path, digest: str, *, count: int = 0
) -> list[str]:
    """把 GIF 的几帧抽成 jpg 存进 ``images/``，返回 workspace 相对路径。

    ffmpeg 缺失、单帧失败都只跳过；绝不抛异常。第一帧是空白时，后面几帧
    仍然能让模型看到动图内容。``count`` 是本地数出的真实帧数：``<= 2`` 视为
    静图，只在候选帧里保留体积最大的一张（细节最多），其余删掉。
    """
    ffmpeg = _locate("ffmpeg")
    if not ffmpeg:
        log.debug("ffmpeg 不可用，GIF 只保留原文件")
        return []
    directory = root / "images"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.info("GIF 抽帧目录创建失败：%s", directory, exc_info=True)
        return []
    frames: list[tuple[int, Path]] = []
    for order, index in enumerate(_gif_frame_indices(count), start=1):
        target = directory / f"gif-{digest}-{order}.jpg"
        ok = await asyncio.to_thread(
            _extract_gif_frame, ffmpeg, source, target, index
        )
        if ok:
            frames.append((order, target))
    if 0 < count <= _STILL_GIF_FRAMES:
        frames = _keep_best_frame(frames)
    return [f"images/{target.name}" for _order, target in frames]


def _keep_best_frame(frames: list[tuple[int, Path]]) -> list[tuple[int, Path]]:
    """静图候选里只留体积最大的一帧，其余删盘；失败时保守保留全部。"""
    if len(frames) <= 1:
        return frames
    best: tuple[int, Path] | None = None
    best_size = -1
    for item in frames:
        try:
            size = item[1].stat().st_size
        except OSError:
            size = -1
        if size > best_size:
            best, best_size = item, size
    if best is None:
        return frames
    for item in frames:
        if item is best:
            continue
        try:
            item[1].unlink(missing_ok=True)
        except OSError:
            log.debug("清理多余 GIF 帧失败：%s", item[1], exc_info=True)
    return [best]


async def _materialize_video(
    msg: Message,
    attachment: Attachment,
    *,
    root: Path,
    decode: Decode | None,
    annotate: bool,
) -> Attachment:
    """复制视频原文件并按需抽帧；任何失败都退回原附件。"""
    source = attachment.path
    source_path = Path(source)
    try:
        data = await asyncio.to_thread(source_path.read_bytes)
    except OSError:
        log.info(
            "视频读取失败，保持占位：msg=%s source=%s",
            msg.message_id,
            source,
            exc_info=True,
        )
        return attachment

    source_suffix = source_path.suffix.lower()
    suffix = source_suffix
    if decode is not None and source_suffix == ".dat" and len(data) <= _VIDEO_DECODE_LIMIT:
        # 视频 .dat 的加密格式与图片不同，decode 返回 None 是正常情况；
        # 只有确实解出可识别的视频容器时才采用解码结果。
        # 大文件不做尝试：图片解码器是纯 Python AES，几十 MB 会卡住事件循环。
        decoded = decode(data)
        if inspect.isawaitable(decoded):
            decoded = await decoded
        decoded_ext = _sniff_video_ext(decoded) if decoded else ""
        if decoded and decoded_ext:
            data, suffix = decoded, decoded_ext
        else:
            log.debug("视频 .dat 解码未得到视频，保留原始字节：msg=%s", msg.message_id)

    if not data or len(data) > _MAX_VIDEO_BYTES:
        log.info(
            "视频大小不可接受（%d 字节，上限 %d），保持占位：msg=%s",
            len(data),
            _MAX_VIDEO_BYTES,
            msg.message_id,
        )
        return attachment

    digest = hashlib.md5(source.encode("utf-8", "ignore")).hexdigest()[:6]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = suffix if suffix and suffix != ".dat" else ".dat"
    relative_video = f"videos/{stamp}-{digest}{ext}"
    stored = relative_video
    video_path: Path | None = None
    try:
        (root / "videos").mkdir(parents=True, exist_ok=True)
        target = root / relative_video
        target.write_bytes(data)
        video_path = target
    except OSError:
        # 复制失败不致命：源文件还在，至少把路径写进 content。
        log.info(
            "视频复制失败，只记源路径：msg=%s source=%s",
            msg.message_id,
            source,
            exc_info=True,
        )
        stored = source
        if source_suffix != ".dat":
            video_path = source_path

    frames: list[str] = []
    if annotate and video_path is not None:
        frames = await _extract_video_frames(root, video_path, digest)
    if annotate:
        suffix_text = (
            f"（已抽帧 {'、'.join(frames)}，可用 read 查看；原文件 {stored}）"
            if frames
            else f"（原文件 {stored}，无法抽帧）"
        )
        msg.content = _annotate_video(msg.content, suffix_text)

    return Attachment(
        kind=attachment.kind,
        source=attachment.source,
        path=stored,
        name=Path(stored).name,
        size=len(data),
        duration=attachment.duration,
    )


async def _read_image_bytes(
    path: Path,
    decode: Decode | None,
    path_max: int,
) -> tuple[bytes, Path] | None:
    """读取并（必要时）解码一个图片候选；不可用时返回 ``None``。

    ``decode`` 只对微信 ``.dat`` 与 32 位 hex 表情缓存生效；解码失败返回
    ``None``，调用方据此尝试下一个变体。``path_max <= 0`` 表示不限制大小。
    """
    try:
        # 单次读取：不文件存在性预检，减少 TOCTOU 与多余系统调用。
        data = await asyncio.to_thread(path.read_bytes)
    except OSError:
        return None
    if decode is not None and (
        path.suffix.lower() == ".dat" or _HEX32_NAME_RE.fullmatch(path.name)
    ):
        decoded = decode(data)
        if inspect.isawaitable(decoded):
            decoded = await decoded
        if decoded is None:
            return None
        data = decoded
    if not data:
        return None
    if path_max > 0 and len(data) > path_max:
        log.info("附件大小不可接受（%d 字节，上限 %d）：%s", len(data), path_max, path)
        return None
    return data, path


async def materialize_attachments(
    msg: Message,
    settings: Settings,
    *,
    decode: Decode | None = None,
    annotate: bool = True,
    fetch: FetchFn | None = None,
) -> int:
    """把附件物化进 workspace，返回成功写盘的图片数量。

    - 带本地路径的附件（微信）仍由 ``wechat.media.enabled`` 门控；
    - 只有 http(s) 来源的附件（QQ 语音/视频、微信表情）由 ``images.enabled``
      门控；
    - 语音（``voice`` / ``record``）落盘到 ``voices/`` 并转写；
    - 视频（``kind == "video"``）复制原文件到 ``videos/``，``annotate`` 为真
      时用 ffmpeg 抽帧到 ``images/``；URL 来源先下载到临时文件。

    ``decode`` 对微信 ``.dat`` 图片与表情缓存生效；``fetch`` 可注入下载函数。
    任何失败都保留原占位，绝不抛出。
    """
    try:
        workspace = getattr(settings, "workspace", None)
        # 快路径：workspace 关闭时连 Path 都不构造，避免文件系统探测。
        if not getattr(workspace, "enabled", False):
            return 0
        workspace_dir = getattr(workspace, "dir", "")
        if not workspace_dir:
            return 0

        media_cfg = getattr(getattr(settings, "wechat", None), "media", None)
        path_enabled = bool(getattr(media_cfg, "enabled", False))
        path_max = int(getattr(media_cfg, "max_bytes", 0) or 0)
        images_cfg = getattr(settings, "images", None)
        url_enabled = bool(getattr(images_cfg, "enabled", False))
        url_max = int(getattr(images_cfg, "max_bytes", 0) or 0)
        if not path_enabled and not url_enabled:
            return 0

        attachments = [item for item in msg.attachments if _has_media_source(item)]
        if not attachments:
            return 0

        fetch_fn = fetch or _default_fetch
        root = Path(workspace_dir)
        directory = root / "images"
        prepared = False
        done = 0
        updated: list[Attachment] = []

        for attachment in attachments:
            attachment_path = (attachment.path or "").strip()
            source_url = (attachment.source or "").strip()
            is_url = not attachment_path and bool(_HTTP_RE.match(source_url))

            if attachment_path:
                if not path_enabled or path_max <= 0:
                    updated.append(attachment)
                    continue
                source = attachment_path
                source_path: Path | None = Path(source)

                # 已经在 workspace 里的相对路径 = 本模块上一轮的产物（或
                # images.py 的产物）：不再复制、不再注解，保持幂等。
                if (
                    not source_path.is_absolute()
                    and (root / source_path).is_file()
                ):
                    updated.append(attachment)
                    continue
            elif is_url:
                if not url_enabled:
                    updated.append(attachment)
                    continue
                source = source_url
                source_path = None
            else:
                updated.append(attachment)
                continue

            if attachment.kind == "file":
                if source_path is None:
                    updated.append(attachment)
                    continue
                note = _file_note(attachment)
                if note not in msg.content:
                    msg.content = (
                        f"{msg.content}\n{note}" if msg.content else note
                    )
                updated.append(attachment)
                continue

            if attachment.kind in ("voice", "record"):
                try:
                    updated.append(
                        await _materialize_voice(
                            msg,
                            attachment,
                            root=root,
                            settings=settings,
                            fetch=fetch_fn,
                            annotate=annotate,
                        )
                    )
                except Exception:  # 单条语音失败不能拖累其它附件
                    log.warning(
                        "语音物化失败，保持占位：msg=%s",
                        msg.message_id,
                        exc_info=True,
                    )
                    updated.append(attachment)
                continue

            if attachment.kind == "video":
                try:
                    if source_path is not None:
                        updated.append(
                            await _materialize_video(
                                msg,
                                attachment,
                                root=root,
                                decode=decode,
                                annotate=annotate,
                            )
                        )
                    else:
                        temp = await _download_video(
                            source, root=root, settings=settings, fetch=fetch_fn
                        )
                        if temp is None:
                            updated.append(attachment)
                            continue
                        try:
                            downloaded = Attachment(
                                kind=attachment.kind,
                                source=attachment.source,
                                path=str(temp),
                                name=temp.name,
                                size=0,
                                duration=attachment.duration,
                            )
                            updated.append(
                                await _materialize_video(
                                    msg,
                                    downloaded,
                                    root=root,
                                    decode=decode,
                                    annotate=annotate,
                                )
                            )
                        finally:
                            try:
                                temp.unlink(missing_ok=True)
                            except OSError:
                                log.debug(
                                    "清理视频临时文件失败：%s", temp, exc_info=True
                                )
                except Exception:  # 单个视频失败不能拖累其它附件
                    log.warning(
                        "视频物化失败，保持占位：msg=%s",
                        msg.message_id,
                        exc_info=True,
                    )
                    updated.append(attachment)
                continue

            if attachment.kind != "image":
                updated.append(attachment)
                continue

            if source_path is not None:
                # 微信同一张图有多个变体（原图 / `_h` / `_t`）：原图是 `wxgf`
                # 容器，解不开时按 `fallbacks` 顺序退回，避免只剩 180px 缩略图。
                candidates = [source_path]
                candidates.extend(
                    Path(item)
                    for item in getattr(attachment, "fallbacks", ())
                    if item
                )
                picked: tuple[bytes, Path] | None = None
                for candidate in candidates:
                    picked = await _read_image_bytes(candidate, decode, path_max)
                    if picked is not None:
                        break
                if picked is None:
                    log.info(
                        "附件读取或解码失败，保持占位：msg=%s", msg.message_id
                    )
                    updated.append(attachment)
                    continue
                data, suffix = picked[0], picked[1].suffix
            elif is_url:
                # 微信表情只有 cdnurl（本地缓存未命中）：直接下载，先按魔数
                # 认图片；认不出再交给 decode（.dat 加密容器）尝试。
                fetched = await _download_image(
                    source,
                    settings=settings,
                    fetch=fetch_fn,
                    limit=url_max or _MAX_VIDEO_BYTES,
                )
                if fetched is None:
                    updated.append(attachment)
                    continue
                data = fetched
                suffix = _sniff_ext(data)
                if not suffix and decode is not None:
                    decoded = decode(data)
                    if inspect.isawaitable(decoded):
                        decoded = await decoded
                    if decoded:
                        decoded_ext = _sniff_ext(decoded)
                        if decoded_ext:
                            data, suffix = decoded, decoded_ext
                if not suffix:
                    log.info(
                        "URL 附件不是可识别图片，保持占位：msg=%s",
                        msg.message_id,
                    )
                    updated.append(attachment)
                    continue
            else:
                updated.append(attachment)
                continue

            if not prepared:
                # 目录与清理只在第一张真要落盘时才动，避免无图消息碰文件系统。
                directory.mkdir(parents=True, exist_ok=True)
                # workspace/images 与 images.py 共用，沿用同一保留期配置。
                retention_days = getattr(images_cfg, "retention_days", 0)
                _cleanup(directory, retention_days)
                prepared = True
            target = _target(directory, source, data, suffix)
            try:
                await asyncio.to_thread(target.write_bytes, data)
            except OSError:
                log.info(
                    "附件写盘失败，保持占位：msg=%s", msg.message_id, exc_info=True
                )
                updated.append(attachment)
                continue

            if target.suffix.lower() != ".gif":
                await asyncio.to_thread(
                    imaging.downscale, target, settings.images.max_edge
                )
            try:
                size = target.stat().st_size
            except OSError:
                size = len(data)
            relative = _materialized_relative(target, root) or (f"images/{target.name}")
            if target.suffix.lower() == ".gif":
                # 动图首帧常是空白（微信动图实测）：抽几帧 jpg 再给模型看。
                # 先本地数帧：<= 2 帧是「伪动图」（静态图被容器封装成两帧），
                # 只保留最佳帧，注记也不写「动图」，免得模型以为画面会动。
                gif_count = _gif_frame_count(data)
                frames = (
                    await _extract_gif_frames(
                        root, target, target.stem, count=gif_count
                    )
                    if annotate
                    else []
                )
                if frames and 0 < gif_count <= _STILL_GIF_FRAMES:
                    msg.content = _annotate_first(
                        msg.content,
                        f"[图片]（图存 {frames[0]}，可用 read 查看；"
                        f"原文件 {relative}）",
                    )
                elif frames:
                    msg.content = _annotate_first(
                        msg.content,
                        f"[图片]（动图，已抽帧 {'、'.join(frames)}，"
                        f"可用 read 查看；原文件 {relative}）",
                    )
                else:
                    msg.content = _annotate_first(
                        msg.content, _ANNOTATED.format(path=relative)
                    )
            else:
                msg.content = _annotate_first(
                    msg.content, _ANNOTATED.format(path=relative)
                )
            updated.append(
                Attachment(
                    kind=attachment.kind,
                    source=attachment.source,
                    path=relative,
                    name=target.name,
                    size=size,
                    duration=attachment.duration,
                )
            )
            done += 1

        msg.attachments = updated
        return done
    except Exception:
        # 任何意外都只降级，不影响入库与回复。
        log.warning(
            "附件物化失败，保持占位：msg=%s", getattr(msg, "message_id", ""),
            exc_info=True,
        )
        return 0


# --------------------------------------------------------- 跨群镜像：路径与物化


def _workspace_root(settings: Settings) -> Path | None:
    """从配置取 workspace 根目录；未配置时返回 ``None``（绝不回退 cwd）。"""
    workspace = getattr(settings, "workspace", None)
    raw = str(getattr(workspace, "dir", "") or "").strip()
    if not raw:
        return None
    return Path(raw)


def resolve_media_path(settings: Settings, attachment: Attachment) -> Path | None:
    """把附件路径解析成真实可读的绝对文件；不可用时返回 ``None``。

    这是跨群镜像发送前的唯一路径校验点（GAP-15/GAP-22）：

    - 空路径、或未配置 ``workspace.dir`` 一律 ``None``（绝不拼成 cwd）；
    - 相对路径先拒绝含 ``..`` 的段，再拼到 workspace 下，``resolve()`` 后
      必须仍在 workspace 内；
    - 绝对路径 ``resolve()`` 后要求是普通文件且非空；
    - 目录、缺失文件、空文件、逃逸一律 ``None``。
    """
    raw = (attachment.path or "").strip()
    if not raw:
        return None
    root = _workspace_root(settings)
    if root is None:
        return None

    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
    else:
        if ".." in candidate.parts:
            return None
        try:
            resolved = (root / candidate).resolve()
            root_resolved = root.resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(root_resolved):
            return None

    try:
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            return None
    except OSError:
        return None
    return resolved


def _clone_message(msg: Message) -> Message:
    """复制一份可安全改写的 ``Message``：content/segments/attachments 都不共享。"""
    return replace(
        msg,
        segments=[
            Segment(type=segment.type, data=dict(segment.data))
            for segment in msg.segments
        ],
        attachments=list(msg.attachments),
    )


def _without_raw_dat(attachments: list[Attachment]) -> list[Attachment]:
    """清空仍指向 ``.dat`` 的附件路径：解不开的微信原始文件绝不能发出去。"""
    cleaned: list[Attachment] = []
    for attachment in attachments:
        if attachment.path and Path(attachment.path).suffix.lower() == ".dat":
            cleaned.append(
                replace(attachment, path="", size=0, fallbacks=())
            )
        else:
            cleaned.append(attachment)
    return cleaned


async def materialize_relay_attachments(
    msg: Message,
    settings: Settings,
    *,
    fetch: FetchFn | None = None,
    decode: Decode | None = None,
) -> Message:
    """镜像转发前物化附件，在副本上操作，绝不修改调用方的 ``Message``。

    复用 :func:`materialize_attachments` 的下载/解码/落盘逻辑：

    - URL 来源的图片/语音/视频依赖 ``images.enabled`` + ``workspace.enabled``
      （语音/``record`` 另受 ``speech.enabled`` 门控，视频不受）；
    - 微信本地 ``.dat`` 走与 ``Service.handle`` 相同的 ``decode``，解不开就把
      路径清空，由出站侧发占位，绝不把原始 ``.dat`` 交给目标通道；
    - 任何失败只记日志并保留原附件，绝不抛异常。
    """
    copy = _clone_message(msg)
    try:
        workspace = getattr(settings, "workspace", None)
        if not getattr(workspace, "enabled", False):
            return copy
        if _workspace_root(settings) is None:
            return copy
        await materialize_attachments(
            copy,
            settings,
            decode=decode,
            annotate=False,
            fetch=fetch or _default_fetch,
        )
        # 物化会向副本 content 追加注记；镜像只需要附件，恢复原文避免噪音。
        copy.content = msg.content
        copy.attachments = _without_raw_dat(copy.attachments)
        return copy
    except Exception:
        log.warning(
            "镜像附件物化失败，保持原样：msg=%s", msg.message_id, exc_info=True
        )
        copy.content = msg.content
        copy.attachments = _without_raw_dat(copy.attachments)
        return copy
