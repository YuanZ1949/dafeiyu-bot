"""入站语音转写：SILK / AMR / 常见音频 → faster-whisper 文本。

设计约束（与 ``images.py`` / ``media.py`` 相同的退化原则）：

- **永不抛异常**：依赖没装、ffmpeg 缺失、解码失败、模型超时都只返回
  ``False`` / ``""``，让入站继续；
- **懒加载**：``pysilk`` 与 ``faster_whisper`` 只在真正要转写时才 import，
  没装可选依赖时 ``import dafeiyu.speech`` 仍然成功；
- **不阻塞事件循环**：解码（含 ffmpeg 子进程）与模型加载/推理全部放进
  ``asyncio.to_thread``；并发由模块级信号量限制。

QQ 的语音段通常是 ``.amr`` 或 SILK（腾讯前缀 ``\\x02#!SILK_V``），微信的
语音段是本地文件；两种来源都由 ``media.py`` 物化后调用这里。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

#: Windows 上不要为每次 ffmpeg 调用弹出控制台窗口。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: SILK 文件头：普通 ``#!SILK_V3`` 与腾讯的 ``\x02#!SILK_V`` 前缀。
_SILK_MAGIC: tuple[bytes, ...] = (b"\x02#!SILK_V", b"#!SILK_V3")
_AMR_MAGIC = b"#!AMR"

#: SILK 解码出来的裸 PCM 参数（pysilk 固定按源采样率输出）。
_PCM_RATE = 24000
#: faster-whisper 要求的采样率。
_WHISPER_RATE = 16000

#: 单次 ffmpeg 转码上限（秒）。
_FFMPEG_TIMEOUT = 60.0

#: 模型单例：键为 ``(model, device, compute_type)``。faster-whisper 的加载
#: 是同步且第一次可能几分钟，所以整个进程只加载一次。
_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()

#: 每个事件循环各一份信号量：``asyncio.Semaphore`` 绑定循环，测试里跨
#: ``asyncio.run`` 复用会报 "attached to a different loop"。这里以循环对象本身
#: 作弱引用键，循环被回收后不会因为 ``id()`` 复用而拿到绑在旧循环上的信号量。
_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _locate(program: str) -> str | None:
    """定位外部程序：``DAFEIYU_FFMPEG`` 优先，其次 ``PATH``。"""
    if program == "ffmpeg":
        override = str(os.environ.get("DAFEIYU_FFMPEG") or "").strip()
        if override:
            return override
    return shutil.which(program)


def _semaphore(limit: int) -> asyncio.Semaphore:
    """按 (事件循环, 并发数) 复用信号量；只在事件循环线程里调用。"""
    loop = asyncio.get_running_loop()
    per_loop = _SEMAPHORES.get(loop)
    if per_loop is None:
        per_loop = {}
        _SEMAPHORES[loop] = per_loop
    semaphore = per_loop.get(limit)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        per_loop[limit] = semaphore
    return semaphore


def _header(src: Path) -> bytes:
    try:
        with src.open("rb") as handle:
            return handle.read(16)
    except OSError:
        return b""


def _run_ffmpeg(
    ffmpeg: str, source: Path, target: Path, *, raw_pcm: bool
) -> bool:
    """ffmpeg 转 16k 单声道 wav；失败只记 debug，绝不抛。"""
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if raw_pcm:
        args += ["-f", "s16le", "-ar", str(_PCM_RATE), "-ac", "1"]
    args += [
        "-i",
        str(source),
        "-ar",
        str(_WHISPER_RATE),
        "-ac",
        "1",
        str(target),
    ]
    try:
        completed = subprocess.run(
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_FFMPEG_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ffmpeg 转 wav 失败：%s", exc)
        return False
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", "ignore")[:200]
        log.debug("ffmpeg 转 wav 失败（exit=%s）：%s", completed.returncode, stderr)
        return False
    try:
        return target.is_file() and target.stat().st_size > 0
    except OSError:
        return False


def decode_to_wav(src: Path, dst: Path) -> bool:
    """把 SILK / AMR / wav / mp3 / m4a / ogg 转成 16k 单声道 wav。

    返回 ``True`` 表示 ``dst`` 已生成。任何依赖缺失、ffmpeg 失败、超时都
    返回 ``False``，不抛异常。
    """
    try:
        if not src.is_file():
            return False
        ffmpeg = _locate("ffmpeg")
        if not ffmpeg:
            log.debug("ffmpeg 不可用，无法转写语音：%s", src)
            return False

        header = _header(src)
        if header.startswith(_SILK_MAGIC):
            try:
                import pysilk  # noqa: PLC0415 - 可选依赖，必须懒加载
            except Exception:
                log.debug("pysilk 未安装，无法解码 SILK：%s", src)
                return False
            pcm = dst.with_name(f"{dst.stem}-pcm.tmp")
            try:
                with src.open("rb") as in_handle, pcm.open("wb") as out_handle:
                    pysilk.decode(in_handle, out_handle, _PCM_RATE)
                return _run_ffmpeg(ffmpeg, pcm, dst, raw_pcm=True)
            except Exception:
                log.debug("SILK 解码失败：%s", src, exc_info=True)
                return False
            finally:
                try:
                    pcm.unlink(missing_ok=True)
                except OSError:
                    log.debug("清理临时 PCM 失败：%s", pcm, exc_info=True)

        # AMR（``#!AMR``）与 wav/mp3/m4a/ogg 都交给 ffmpeg；ffmpeg 没有
        # silk 解码器，所以 SILK 必须先走 pysilk。
        return _run_ffmpeg(ffmpeg, src, dst, raw_pcm=False)
    except Exception:
        log.debug("语音解码失败：%s", src, exc_info=True)
        return False


def _load_model(cfg: Any) -> Any:
    """按 (model, device, compute_type) 复用 WhisperModel；失败返回 None。"""
    model_name = str(getattr(cfg, "model", "base") or "base").strip() or "base"
    device = str(getattr(cfg, "device", "cpu") or "cpu").strip() or "cpu"
    compute_type = (
        str(getattr(cfg, "compute_type", "int8") or "int8").strip() or "int8"
    )
    key = (model_name, device, compute_type)
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        endpoint = str(getattr(cfg, "hf_endpoint", "") or "").strip()
        if endpoint:
            # 本机直连 huggingface.co 会超时；镜像必须走环境变量。
            os.environ.setdefault("HF_ENDPOINT", endpoint)
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            model = WhisperModel(
                model_name, device=device, compute_type=compute_type
            )
        except Exception:
            log.debug("faster-whisper 模型加载失败：%s", model_name, exc_info=True)
            return None
        _MODEL_CACHE[key] = model
        return model


def _transcribe_sync(path: Path, cfg: Any) -> str:
    """线程内执行：解码 → 转写 → 回收临时 wav。"""
    wav = path.with_name(
        f"{path.stem}-{os.getpid()}-{threading.get_ident()}.wav"
    )
    try:
        if not decode_to_wav(path, wav):
            return ""
        model = _load_model(cfg)
        if model is None:
            return ""
        language = str(getattr(cfg, "language", "zh") or "").strip() or None
        beam_size = int(getattr(cfg, "beam_size", 1) or 1)
        initial_prompt = str(getattr(cfg, "initial_prompt", "") or "").strip()
        options: dict[str, Any] = {
            "language": language,
            "beam_size": beam_size,
            "vad_filter": True,
        }
        if initial_prompt:
            # 引导输出简体中文：实测 base 模型默认会吐繁体。
            options["initial_prompt"] = initial_prompt
        segments, _info = model.transcribe(str(wav), **options)
        # faster-whisper 是惰性生成器：必须消费完才有结果。
        text = "".join(str(getattr(segment, "text", "") or "") for segment in segments)
        return text.strip()
    except Exception:
        log.debug("语音转写失败：%s", path, exc_info=True)
        return ""
    finally:
        try:
            wav.unlink(missing_ok=True)
        except OSError:
            log.debug("清理临时 wav 失败：%s", wav, exc_info=True)


def _speech_disabled_by_env() -> bool:
    value = str(os.environ.get("DAFEIYU_SPEECH_DISABLE", "") or "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _workspace_ready(settings: Settings) -> bool:
    workspace = getattr(settings, "workspace", None)
    if not getattr(workspace, "enabled", False):
        return False
    return bool(str(getattr(workspace, "dir", "") or "").strip())


async def transcribe(path: Path, settings: Settings) -> str:
    """把语音文件转成文本；任何失败/超时都返回空串。

    前置条件：``speech.enabled``、未被 ``DAFEIYU_SPEECH_DISABLE`` 禁用、
    workspace 可用、文件存在且不超过 ``speech.max_bytes``。
    """
    try:
        cfg = getattr(settings, "speech", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            return ""
        if _speech_disabled_by_env():
            log.debug("DAFEIYU_SPEECH_DISABLE 已设置，跳过语音转写")
            return ""
        if not _workspace_ready(settings):
            return ""

        try:
            if not path.is_file():
                return ""
            size = path.stat().st_size
        except OSError:
            return ""
        max_bytes = int(getattr(cfg, "max_bytes", 0) or 0)
        if size <= 0 or (max_bytes > 0 and size > max_bytes):
            log.debug("语音文件大小不可接受（%d 字节）：%s", size, path)
            return ""

        timeout = float(getattr(cfg, "timeout_seconds", 0.0) or 0.0)
        if timeout <= 0:
            return ""
        concurrency = max(1, int(getattr(cfg, "concurrency", 1) or 1))
        started = time.monotonic()
        async with _semaphore(concurrency):
            text = await asyncio.wait_for(
                asyncio.to_thread(_transcribe_sync, path, cfg), timeout=timeout
            )
        log.debug(
            "语音转写完成：%s（%.1fs，%d 字）",
            path.name,
            time.monotonic() - started,
            len(text),
        )
        return text
    except asyncio.TimeoutError:
        log.info("语音转写超时：%s", path)
        return ""
    except Exception:
        log.debug("语音转写失败：%s", path, exc_info=True)
        return ""
