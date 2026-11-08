"""出站语音合成（edge-tts）：回复文本 → mp3。

设计约束（与 ``speech.py`` 相同的退化原则）：

- **默认关闭**：``speech.tts_enabled=false`` 时直接返回 ``False``，不 import
  ``edge_tts``、不落盘；
- **永不抛异常**：可选依赖没装、目录不可写、合成报错、超时都只返回
  ``False``，由调用方退回纯文本；
- **懒加载**：``edge_tts`` 只在真正合成时 import，模块顶层不依赖它；
- **先写临时文件再原子替换**：``<out_path>.tmp`` 写完才 ``os.replace``，
  失败时清理临时文件，避免留下半截 mp3。

注意：启用后回复文本会被发送到云端 TTS 提供商（edge-tts）。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from pathlib import Path

from .config import SpeechConfig

log = logging.getLogger(__name__)

#: 单次合成上限（秒）；超时按失败处理（todo 27 的回复超时也按 30s 对齐）。
_TIMEOUT_SECONDS = 30.0

#: 同一时刻只合成一条：edge-tts 走网络，并发没有收益，还会抢带宽。
_SEMAPHORE = asyncio.Semaphore(1)


def _remove(path: Path) -> None:
    """尽力删除临时文件；删不掉也不抛。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("清理临时语音文件失败：%s", path, exc_info=True)


async def _synthesize_to(text: str, tmp: Path, cfg: SpeechConfig) -> bool:
    """懒加载 edge-tts 并写 ``tmp``；缺依赖返回 ``False``。"""
    try:
        # 按名字动态导入：可选依赖不能出现在模块顶层，也不让静态检查把
        # 「没装 edge-tts」当成语法层错误（运行时会退化成 False）。
        edge_tts = importlib.import_module("edge_tts")
    except Exception:
        log.debug("edge-tts 未安装，跳过语音合成")
        return False
    communicate = edge_tts.Communicate(
        text, voice=cfg.tts_voice, rate=cfg.tts_rate
    )
    await communicate.save(str(tmp))
    return True


async def synthesize(text: str, out_path: Path, cfg: SpeechConfig) -> bool:
    """把 ``text`` 合成为 ``out_path``（mp3）。

    返回 ``True`` 表示文件已就绪。任何前置条件不满足、依赖缺失、异常或
    超时都返回 ``False``，不抛异常，也不留下 ``.tmp`` 半成品。
    """
    if not cfg.tts_enabled:
        return False

    clean = text.strip() if isinstance(text, str) else ""
    if not clean:
        return False
    if cfg.tts_max_chars < 1 or len(clean) > cfg.tts_max_chars:
        log.debug(
            "文本过长，跳过语音合成（%d > %d 字）",
            len(clean),
            cfg.tts_max_chars,
        )
        return False

    tmp = out_path.with_name(f"{out_path.name}.tmp")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("语音输出目录不可写：%s", out_path.parent)
        return False

    try:
        async with _SEMAPHORE:
            ok = await asyncio.wait_for(
                _synthesize_to(clean, tmp, cfg), timeout=_TIMEOUT_SECONDS
            )
    except asyncio.TimeoutError:
        log.warning("语音合成超时（%.0fs）：%s", _TIMEOUT_SECONDS, out_path.name)
        _remove(tmp)
        return False
    except Exception:
        log.debug("语音合成失败：%s", out_path.name, exc_info=True)
        _remove(tmp)
        return False

    if not ok:
        _remove(tmp)
        return False

    try:
        if not tmp.is_file() or tmp.stat().st_size <= 0:
            log.debug("语音合成未产出有效文件：%s", out_path.name)
            _remove(tmp)
            return False
        os.replace(tmp, out_path)
    except OSError:
        log.warning("语音文件落盘失败：%s", out_path, exc_info=True)
        _remove(tmp)
        return False
    return True
