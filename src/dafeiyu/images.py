"""QQ 图片消息物化（方案 A：落盘 + content 带路径，大脑自己 read）。

消息进 service 时内存里 segments 还在，而入库后的 content 字符串是图片信息
到达大脑的唯一载体——所以在 ``store.add_message`` 之前把图片下载/复制到
``<workspace.dir>/images/``，并把 content 里的 ``[图片]`` 占位替换成带路径
的注记。此后批次时间线、单条历史、记忆注入全部自动带上路径，DB schema 与
brain 载荷零改动。

设计约束：
- 本模块**永不抛异常**——任何失败都优雅退化回 ``[图片]`` 占位；
- 快路径：没有 image 段的消息立即返回，不碰网络不建目录；
- 文件名自生成（时间戳 + 来源 md6），不采信 URL 路径段，无路径注入。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

import httpx

from .config import Settings
from .models import Attachment, Message
from .netssl import default_ssl_context

log = logging.getLogger(__name__)

_PLACEHOLDER = "[图片]"
_ANNOTATED = "[图片]（图存 {path}，可用 read 查看）"
_ANNOTATED_ANIMATED = "[图片]（图存 {path}，动图，可用 read 查看）"

_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)
_DATA_URI_RE = re.compile(r"^data:image/[\w.+-]+;base64,", re.IGNORECASE)

# 魔数 → 扩展名（顺序无关，逐前缀匹配）。
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)
_URL_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

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
                raise ValueError(f"图片超过大小上限 {max_bytes} 字节")
            chunks.append(chunk)
        return b"".join(chunks)


def _sniff_ext(data: bytes) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    return ""


def _ext_from_url(url: str) -> str:
    suffix = Path(url.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
    return suffix if suffix in _URL_SUFFIXES else ""


def _source(data: dict) -> tuple[str, str]:
    """从 image 段 data 里挑可用来源，返回 (kind, value)；无可用来源 ("", "")。

    优先级：http(s) url → http(s) file → 本地存在文件 → base64。
    """
    url = data.get("url")
    if isinstance(url, str) and _HTTP_RE.match(url.strip()):
        return "http", url.strip()
    file_value = data.get("file")
    if isinstance(file_value, str):
        file_value = file_value.strip()
        if _HTTP_RE.match(file_value):
            return "http", file_value
        if file_value:
            path = Path(file_value)
            if path.is_absolute() and path.is_file():
                return "local", file_value
    b64 = data.get("base64")
    if isinstance(b64, str) and b64.strip():
        return "base64", b64.strip()
    return "", ""


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


async def _obtain(
    kind: str,
    value: str,
    *,
    settings: Settings,
    fetch: FetchFn,
) -> bytes:
    cfg = settings.images
    if kind == "http":
        data = await fetch(value, cfg.timeout_seconds, cfg.max_bytes)
    elif kind == "local":
        data = Path(value).read_bytes()
    else:  # base64
        raw = _DATA_URI_RE.sub("", value)
        data = base64.b64decode(raw, validate=False)
    if len(data) > cfg.max_bytes:
        raise ValueError(f"图片超过大小上限 {cfg.max_bytes} 字节")
    return data


def _target(directory: Path, kind: str, value: str, data: bytes) -> Path:
    """生成落盘路径：时间戳 + 来源 md6 前 6 位；扩展名先魔数后 URL 后缀。"""
    digest = hashlib.md5(value.encode("utf-8", "ignore")).hexdigest()[:6]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = _sniff_ext(data) or (
        _ext_from_url(value) if kind == "http" else ""
    ) or ".jpg"
    return directory / f"{stamp}-{digest}{ext}"


async def materialize(
    msg: Message,
    settings: Settings,
    *,
    fetch: FetchFn | None = None,
    annotate: bool = True,
) -> int:
    """把 msg 里的 image 段落盘并把路径写进 content。返回成功张数。

    永不抛异常；无 image 段时零开销快路径。
    ``annotate=False``（大脑没有 read 工具）时直接保持 ``[图片]``：
    不下载、不写盘，避免把无用的文件路径塞进 prompt。
    """
    try:
        return await _materialize(
            msg, settings, fetch=fetch or _default_fetch, annotate=annotate
        )
    except Exception:
        log.warning("图片物化失败，回退占位：%s", msg.message_id, exc_info=True)
        return 0


async def _materialize(
    msg: Message,
    settings: Settings,
    *,
    fetch: FetchFn,
    annotate: bool,
) -> int:
    if not annotate or not settings.images.enabled:
        return 0
    image_segs = [s for s in msg.segments if s.type == "image"]
    if not image_segs:
        return 0
    if not settings.workspace.enabled:
        log.debug("workspace 已关闭，跳过图片物化：%s", msg.message_id)
        return 0

    directory = Path(settings.workspace.dir) / "images"
    prepared = False

    done = 0
    for seg in image_segs:
        if not isinstance(seg.data, dict):
            continue
        kind, value = _source(seg.data)
        if not kind:
            log.info(
                "图片无可用来源（NapCat 未给 url/file/base64）：msg=%s",
                msg.message_id,
            )
            continue
        try:
            data = await _obtain(kind, value, settings=settings, fetch=fetch)
        except Exception:
            log.info(
                "图片获取失败，保持占位：msg=%s kind=%s", msg.message_id, kind,
                exc_info=True,
            )
            continue
        if not prepared:
            # 目录与清理只在第一张真要落盘时才动，避免无图消息留空目录。
            directory.mkdir(parents=True, exist_ok=True)
            _cleanup(directory, settings.images.retention_days)
            prepared = True
        target = _target(directory, kind, value, data)
        try:
            target.write_bytes(data)
        except OSError:
            log.info(
                "图片写盘失败，保持占位：msg=%s", msg.message_id, exc_info=True
            )
            continue
        rel = target.as_posix()
        if _PLACEHOLDER in msg.content:
            template = (
                _ANNOTATED_ANIMATED if target.suffix.lower() == ".gif" else _ANNOTATED
            )
            msg.content = msg.content.replace(
                _PLACEHOLDER, template.format(path=rel), 1
            )
            # 附件只是内存元数据：path 用 workspace 相对路径（images/xxx），
            # 与 dashboard 的 /api/workspace/file?path=images/... 对齐；
            # 注记被跳过（content 里没有占位）时不追加，保持两者同步。
            try:
                msg.attachments.append(
                    Attachment(
                        kind="image",
                        source=value,
                        path=f"images/{target.name}",
                        name=target.name,
                        size=len(data),
                    )
                )
            # 追加附件失败不得影响图片物化本身。
            except Exception:
                log.debug("图片附件记录失败：msg=%s", msg.message_id, exc_info=True)
        done += 1
    if done:
        log.debug("图片物化完成：%s → %d 张", msg.message_id, done)
    return done
