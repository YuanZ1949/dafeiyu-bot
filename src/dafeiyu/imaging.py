"""图片尺寸探测与降采样（ffmpeg/ffprobe）。

入站大图先探测最长边，超过 ``images.max_edge`` 时用 ffmpeg 等比缩到上限，
再原子替换原文件。任何失败都保留原图并返回 ``False``——降采样只是省 token，
不能影响入站。

设计约束：

- 不引入 Pillow/OpenCV，只用外部 ffmpeg/ffprobe；
- GIF 是动图，降采样会被 ``-frames:v 1`` 压成单帧，直接跳过；WEBP 也可能是
  多帧动图，所以 ``-frames:v 1`` 只加在静态容器上（``.jpg/.jpeg/.png/.bmp``）；
- 临时文件与源同目录、名字形如 ``a.tmp.png``：ffmpeg 靠最后一个后缀选 muxer，
  ``a.png.tmp`` 会因无法识别输出格式而失败；
- 本模块函数不抛异常（``locate`` 除外，它只做查找）。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

#: ffmpeg/ffprobe 单次调用上限：降采样要完整解码一张图，留足余量。
_PROBE_TIMEOUT = 60.0

#: 只有静态容器才加 ``-frames:v 1``：WEBP 可能是多帧动图，压成单帧等于丢动画。
_STATIC_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp"})

#: Windows 上不要为每次 ffmpeg 调用弹出控制台窗口。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def locate(program: str) -> str | None:
    """定位外部程序：``DAFEIYU_FFMPEG`` 优先，其次 ``PATH``。"""
    if program == "ffmpeg":
        override = str(os.environ.get("DAFEIYU_FFMPEG") or "").strip()
        if override:
            return override
    return shutil.which(program)


def _locate_ffprobe(ffmpeg: str) -> str | None:
    """优先用 ffmpeg 同目录的 ffprobe，其次查 ``PATH``。"""
    directory = Path(ffmpeg).parent
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = directory / name
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return locate("ffprobe")


def probe_size(path: Path, ffprobe: str) -> tuple[int, int] | None:
    """用 ffprobe 读第一路视频流的 ``(width, height)``；失败返回 ``None``。"""
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=False,
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    text = (completed.stdout or b"").decode("ascii", "ignore")
    for line in text.splitlines():
        parts = [part.strip() for part in line.strip().split(",")]
        if len(parts) < 2:
            continue
        try:
            width, height = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if width > 0 and height > 0:
            return width, height
    return None


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("清理图片临时文件失败：%s", path, exc_info=True)


def downscale(path: Path, max_edge: int) -> bool:
    """超过 ``max_edge`` 的图片等比缩小并原地替换；成功返回 ``True``。

    任何前置条件不满足（关闭降采样、GIF、ffmpeg/ffprobe 缺失、读不到尺寸、
    本来就不超限）或执行失败都返回 ``False``，原文件保持不动。静态容器只取
    首帧；WEBP 不动帧数（多帧动图原样交给 ffmpeg 重编码）。
    """
    tmp: Path | None = None
    try:
        suffix = path.suffix.lower()
        if max_edge <= 0:
            return False
        if suffix == ".gif":
            return False
        ffmpeg = locate("ffmpeg")
        if not ffmpeg:
            return False
        ffprobe = _locate_ffprobe(ffmpeg)
        if not ffprobe:
            return False
        size = probe_size(path, ffprobe)
        if size is None or max(size) <= max_edge:
            return False

        tmp = path.with_name(f"{path.stem}.tmp{path.suffix}")
        # JPEG 二次编码会叠加有损压缩：mjpeg 默认量化偏保守，显式给 q=3。
        # PNG/GIF/WEBP 走各自的默认参数，避免误传 qscale 改变编码语义。
        quality = ["-q:v", "3"] if suffix in (".jpg", ".jpeg") else []
        # 静态容器只留首帧；WEBP 交给 ffmpeg 保留全部帧（动图压成单帧等于丢动画）。
        # 编码器不支持多帧时 ffmpeg 会失败，调用方保留原图——不会比原来更差。
        frame_limit = ["-frames:v", "1"] if suffix in _STATIC_SUFFIXES else []
        completed = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(path),
                "-vf",
                (
                    f"scale=min({max_edge}\\,iw):min({max_edge}\\,ih)"
                    ":force_original_aspect_ratio=decrease"
                ),
                *frame_limit,
                *quality,
                str(tmp),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_PROBE_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
        if completed.returncode != 0:
            log.debug(
                "ffmpeg 降采样失败（exit=%s）：%s", completed.returncode, path
            )
            _discard(tmp)
            return False
        try:
            if not tmp.is_file() or tmp.stat().st_size <= 0:
                _discard(tmp)
                return False
            os.replace(tmp, path)
        except OSError:
            _discard(tmp)
            return False
        return True
    except Exception:
        log.debug("图片降采样异常，保留原图：%s", path, exc_info=True)
        if tmp is not None:
            _discard(tmp)
        return False
