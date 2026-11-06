"""微信入站附件物化：把本地库发现的媒体复制进 ``<workspace>/images/``。

消息入库前只有 content 字符串能到达大脑，所以图片必须落盘并把 workspace 相对
路径写回 content；``Attachment`` 仍只是内存元数据，不入库、不改 schema。

设计约束（与 ``images.py`` 相同的退化原则，但职责不同）：

- 只处理**已有本地路径**的附件；不下载、不访问网络；
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
import os
import re
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from .config import Settings
from .models import Attachment, Message

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


def _locate(program: str) -> str | None:
    """定位外部程序：``DAFEIYU_FFMPEG`` 优先，其次 ``PATH``。"""
    if program == "ffmpeg":
        override = str(os.environ.get("DAFEIYU_FFMPEG") or "").strip()
        if override:
            return override
    return shutil.which(program)


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


async def materialize_attachments(
    msg: Message,
    settings: Settings,
    *,
    decode: Decode | None = None,
    annotate: bool = True,
) -> int:
    """把带本地路径的附件物化进 workspace，返回成功写盘的图片数量。

    ``decode`` 对微信 ``.dat`` 图片与表情缓存（32 位 hex 文件名）生效；可以
    返回 awaitable（表情转码要起 ffmpeg 子进程）。传 ``None`` 表示不解码、
    按原字节复制。视频（``kind == "video"``）会复制原文件到 ``videos/``，
    并在 ``annotate`` 为真时用 ffmpeg 抽帧到 ``images/``。任何失败都保留原
    占位，绝不抛出。
    """
    try:
        workspace = getattr(settings, "workspace", None)
        media_cfg = getattr(getattr(settings, "wechat", None), "media", None)
        # 快路径：开关全关时连 Path 都不构造，避免无意义的文件系统探测。
        if not getattr(workspace, "enabled", False):
            return 0
        if not getattr(media_cfg, "enabled", False):
            return 0
        attachments = [item for item in msg.attachments if item.path]
        if not attachments:
            return 0

        max_bytes = int(getattr(media_cfg, "max_bytes", 0) or 0)
        if max_bytes <= 0:
            return 0
        workspace_dir = getattr(workspace, "dir", "")
        if not workspace_dir:
            return 0
        root = Path(workspace_dir)
        directory = root / "images"
        prepared = False
        done = 0
        updated: list[Attachment] = []

        for attachment in attachments:
            source = attachment.path
            source_path = Path(source)

            # 已经在 workspace 里的相对路径 = 本模块上一轮的产物（或 images.py
            # 的产物）：不再复制、不再注解，保持幂等。
            if (
                not source_path.is_absolute()
                and (root / source_path).is_file()
            ):
                updated.append(attachment)
                continue

            if attachment.kind == "file":
                note = _file_note(attachment)
                if note not in msg.content:
                    msg.content = (
                        f"{msg.content}\n{note}" if msg.content else note
                    )
                updated.append(attachment)
                continue

            if attachment.kind == "video":
                try:
                    updated.append(
                        await _materialize_video(
                            msg,
                            attachment,
                            root=root,
                            decode=decode,
                            annotate=annotate,
                        )
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

            try:
                # 单次读取：不文件存在性预检，减少 TOCTOU 与多余系统调用。
                data = source_path.read_bytes()
            except OSError:
                log.info(
                    "附件读取失败，保持占位：msg=%s kind=%s",
                    msg.message_id,
                    attachment.kind,
                    exc_info=True,
                )
                updated.append(attachment)
                continue

            if decode is not None and (
                source_path.suffix.lower() == ".dat"
                or _HEX32_NAME_RE.fullmatch(source_path.name)
            ):
                decoded = decode(data)
                if inspect.isawaitable(decoded):
                    decoded = await decoded
                data = decoded
                if data is None:
                    log.debug("附件解码失败，保持占位：msg=%s", msg.message_id)
                    updated.append(attachment)
                    continue

            if not data or len(data) > max_bytes:
                log.info(
                    "附件大小不可接受（%d 字节，上限 %d），保持占位：msg=%s",
                    len(data),
                    max_bytes,
                    msg.message_id,
                )
                updated.append(attachment)
                continue

            if not prepared:
                # 目录与清理只在第一张真要落盘时才动，避免无图消息碰文件系统。
                directory.mkdir(parents=True, exist_ok=True)
                # workspace/images 与 images.py 共用，沿用同一保留期配置。
                retention_days = getattr(
                    getattr(settings, "images", None), "retention_days", 0
                )
                _cleanup(directory, retention_days)
                prepared = True
            target = _target(directory, source, data, source_path.suffix)
            try:
                target.write_bytes(data)
            except OSError:
                log.info(
                    "附件写盘失败，保持占位：msg=%s", msg.message_id, exc_info=True
                )
                updated.append(attachment)
                continue

            relative = _materialized_relative(target, root) or (f"images/{target.name}")
            msg.content = _annotate_first(
                msg.content, _ANNOTATED.format(path=relative)
            )
            updated.append(
                Attachment(
                    kind=attachment.kind,
                    source=attachment.source,
                    path=relative,
                    name=target.name,
                    size=len(data),
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
