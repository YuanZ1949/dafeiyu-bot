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

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .config import Settings
from .models import Attachment, Message

log = logging.getLogger(__name__)

_PLACEHOLDER = "[图片]"
_ANNOTATED = "[图片]（图存 {path}，可用 read 查看）"
_ANNOTATION_MARK = "（图存"
_FILE_PREFIX = "[文件]"

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


def _target(directory: Path, source: str, data: bytes, suffix: str) -> Path:
    """生成落盘路径：时间戳 + 来源 md5 前 6 位 + 嗅探到的扩展名。"""
    digest = hashlib.md5(source.encode("utf-8", "ignore")).hexdigest()[:6]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = _sniff_ext(data) or suffix.lower() or ".jpg"
    return directory / f"{stamp}-{digest}{ext}"


def _annotate_first(content: str, annotation: str) -> str:
    """替换第一个**未**带注记的 ``[图片]``，避免重复注解已物化的占位。"""
    start = 0
    while True:
        index = content.find(_PLACEHOLDER, start)
        if index < 0:
            return content
        tail = content[index + len(_PLACEHOLDER) :]
        if not tail.startswith(_ANNOTATION_MARK):
            return content[:index] + annotation + tail
        start = index + len(_PLACEHOLDER)


def _materialized_relative(path: Path, root: Path) -> str | None:
    """``path`` 已位于 workspace 内时返回其相对 posix 路径。"""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _file_note(attachment: Attachment) -> str:
    name = attachment.name or Path(attachment.path).name
    return f"{_FILE_PREFIX} {name} ({attachment.path})"


async def materialize_attachments(
    msg: Message,
    settings: Settings,
    *,
    decode: Callable[[bytes], bytes | None] | None = None,
) -> int:
    """把带本地路径的附件物化进 workspace，返回成功写盘的图片数量。

    ``decode`` 只对 ``.dat`` 源生效；传 ``None`` 表示不解码、按原字节复制。
    任何失败都保留原占位，绝不抛出。
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

            if source_path.suffix.lower() == ".dat" and decode is not None:
                data = decode(data)
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
                directory.mkdir(parents=True, exist_ok=True)
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
