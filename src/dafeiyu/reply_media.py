"""回复媒体：附件安全解析、OneBot 消息段拼装、@-名字解析。

这里是模型输出进入发送链路前的安全边界：附件路径一律不允许逃出
``<workspace>/replies/``——形态检查（绝对路径/盘符/``..``）在 resolve
之前，包含性检查在 resolve 之后（顺带挡住指向目录外的软链）。全部
函数纯、无副作用：不建目录、不读文件内容、不抛错，拒绝一律留痕。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from dafeiyu.brain.client import BrainReply
from dafeiyu.config import Settings
from dafeiyu.models import Attachment, ChatType

log = logging.getLogger(__name__)

# 附件大小上限：10 MiB，与下游 OneBot 本地文件发送的实际能力对齐。
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
# 允许发出的后缀白名单（比较时不区分大小写）。
ALLOWED_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mp3", ".wav", ".pdf", ".txt", ".zip"}
)
REPLIES_DIRNAME = "replies"
IMAGES_DIRNAME = "images"
#: images/ 归属校验只回看本会话最近 200 条消息；窗口外的旧图不可引用。
_OWNERSHIP_WINDOW = 200
#: 合并转发节点的上限与单节点正文长度（与 brain.client 的解析上限一致）。
MAX_FORWARD_NODES = 5
MAX_FORWARD_TEXT = 200

# 名字截断与 @ 封顶：模型编造超长名字/点名过多时收敛到安全规模。
_MAX_NAME_CHARS = 60
_MAX_AT_NAMES = 3
# 盘符形态：字符串任意位置出现的 `C:`（含 "replies/C:/x.png" 这类夹带）。
_DRIVE_RE = re.compile(r"[A-Za-z]:")


def _shape_reject_reason(raw: str) -> str | None:
    """resolve 之前的路径形态检查：越界形态直接拒绝，不碰文件系统。"""
    if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        return "绝对路径"
    if PureWindowsPath(raw).drive or _DRIVE_RE.search(raw):
        return "路径含盘符"
    if ".." in PureWindowsPath(raw).parts:
        return "路径含 .."
    return None


def _build_attachment(candidate: Any, target: Path, size: int) -> Attachment:
    """校验全部通过后的 Attachment 组装：kind 归一，source 清空。"""
    name = str(getattr(candidate, "name", "") or "") or target.name
    kind = str(getattr(candidate, "kind", "") or "").strip().lower() or "file"
    try:
        duration = int(getattr(candidate, "duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    return Attachment(
        kind=kind,
        source="",
        path=target.as_posix(),
        name=name[:_MAX_NAME_CHARS],
        size=size,
        duration=duration,
    )


def _lead_dirname(raw: str) -> str:
    """候选路径声称的第一级目录（小写）；空路径返回空串。"""
    posix = PurePosixPath(raw.replace("\\", "/"))
    return posix.parts[0].lower() if posix.parts else ""


def _resolve_one(
    candidate: Any, raw: str, root: Path, bases: Mapping[str, Path]
) -> Attachment | None:
    """单个候选的完整校验；拒绝时 warning 留痕并返回 None。

    ``bases`` 是「第一级目录名 → 允许根目录」映射：调用方决定哪些目录
    本次放行（replies 恒允许，images 仅在会话归属成立时加入）。
    """
    reason = _shape_reject_reason(raw)
    if reason is None:
        target = (root / raw).resolve(strict=False)
        # resolve(strict=False) 已跟随符号链接：逃逸出允许目录的软链
        # 在 resolve 之后必然落在 base 之外，这条包含性检查就是软链防线。
        base = bases.get(_lead_dirname(raw))
        if base is None or not target.is_relative_to(base):
            reason = "路径越出允许目录"
        elif not target.is_file():
            reason = "目标不是普通文件"
        elif (suffix := target.suffix.lower()) not in ALLOWED_SUFFIXES:
            reason = f"后缀 {suffix!r} 不在白名单"
        else:
            size = target.stat().st_size
            if size <= 0:
                reason = "文件为空"
            elif size > MAX_ATTACHMENT_BYTES:
                reason = f"{size} 字节超过上限 {MAX_ATTACHMENT_BYTES}"
            else:
                return _build_attachment(candidate, target, size)
    log.warning("拒绝模型附件（%s）path=%r", reason, raw)
    return None


def _forward_attachment(candidate: Any) -> Attachment | None:
    """合并转发：只保留非空正文的节点并限长；没有有效节点返回 None。"""
    nodes: list[dict[str, Any]] = []
    for node in getattr(candidate, "nodes", ()) or ():
        if len(nodes) >= MAX_FORWARD_NODES:
            break
        if not isinstance(node, dict):
            continue
        name = str(node.get("name") or "").strip()
        content = str(node.get("content") or "").strip()
        if not content:
            continue
        nodes.append(
            {"name": name[:_MAX_NAME_CHARS], "content": content[:MAX_FORWARD_TEXT]}
        )
    if not nodes:
        return None
    return Attachment(kind="forward", nodes=tuple(nodes))


def _images_owned(
    rel: str,
    settings: Settings,
    store: Any,
    chat_type: ChatType | None,
    chat_id: str,
) -> bool:
    """``images/<name>`` 是否出现在本会话最近消息正文里（会话内归属）。

    入站注记写入的是工作区全路径（images.py: ``rel = target.as_posix()``），
    所以匹配前必须把 content 归一化：先统一成 posix 斜杠，再剥掉 workspace
    根前缀；只对副本做替换，绝不改写消息本身（注记的全路径是给大脑 read 用的）。
    归属检查的任何异常按「未归属」处理。
    """
    if store is None or not chat_type or not chat_id:
        return False
    token = re.compile(rf"(?<![\w./-]){re.escape(rel)}(?![\w./-])")
    try:
        recent = store.recent_messages(chat_type, chat_id, _OWNERSHIP_WINDOW)
    except Exception:
        # 记忆层故障只丢 images/ 附件，不得影响 replies/ 与正文。
        return False
    root_prefix = Path(settings.workspace.dir).as_posix().rstrip("/") + "/"
    for message in recent:
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        normalized = content.replace("\\", "/").replace(root_prefix, "")
        if token.search(normalized):
            return True
    return False


def resolve_attachments(
    reply: BrainReply,
    settings: Settings,
    *,
    store: Any = None,
    chat_type: ChatType | None = None,
    chat_id: str = "",
) -> list[Attachment]:
    """把 BrainReply.attachments 的候选解析成安全附件，拒绝项跳过。

    只接受 ``workspace.dir`` 下 ``replies/`` 目录内、后缀与大小都合规的
    普通文件；``images/<name>`` 还必须出现在本会话最近消息正文里
    （提供 store/chat_type/chat_id 三者才做归属校验，缺一即拒绝）。
    工作区关闭、路径越界、文件缺失等一律留痕后跳过，绝不抛错。
    ``sticker`` 不读文件（表情没有本地路径），只校验非空 name 后原样透传。
    """
    candidates: tuple[Attachment, ...] = reply.attachments
    if not candidates:
        return []
    root = Path(settings.workspace.dir) if settings.workspace.enabled else None
    base = (root / REPLIES_DIRNAME).resolve() if root is not None else None
    images = (root / IMAGES_DIRNAME).resolve() if root is not None else None

    resolved: list[Attachment] = []
    for candidate in candidates:
        kind = str(getattr(candidate, "kind", "") or "").strip().lower()
        if kind == "sticker":
            name = str(getattr(candidate, "name", "") or "").strip()
            if not name:
                log.info("忽略空 name 的表情附件")
                continue
            resolved.append(Attachment(kind="sticker", name=name[:_MAX_NAME_CHARS]))
            continue
        if kind == "forward":
            forward = _forward_attachment(candidate)
            if forward is None:
                log.info("忽略没有有效节点的合并转发附件")
                continue
            resolved.append(forward)
            continue
        raw = str(getattr(candidate, "path", "") or "")
        if not raw.strip():
            # 形残但无害：空路径不配 warning，info 留痕即可。
            log.info("忽略空路径的模型附件 kind=%r", getattr(candidate, "kind", "") or "")
            continue
        if root is None or base is None or images is None:
            log.warning("工作区未启用，拒绝模型附件 path=%r", raw)
            continue
        # 允许的根目录：replies 恒允许；images 仅当会话归属成立时放行。
        allowed: dict[str, Path] = {REPLIES_DIRNAME: base}
        if _lead_dirname(raw) == IMAGES_DIRNAME:
            rel = PurePosixPath(raw.replace("\\", "/")).as_posix()
            if not _images_owned(rel, settings, store, chat_type, chat_id):
                log.warning("拒绝模型附件（images 未获本会话归属）path=%r", raw)
                continue
            allowed[IMAGES_DIRNAME] = images
        try:
            item = _resolve_one(candidate, raw, root, allowed)
        except (OSError, ValueError) as exc:
            # stat/resolve 撞上损坏的软链、权限、非法字符：按拒绝处理。
            log.warning("拒绝模型附件（解析出错：%s）path=%r", exc, raw)
            continue
        if item is not None:
            resolved.append(item)
    return resolved


#: face 段的 id 形态：纯数字（QQ 内置表情编号）；其余按 mface 文本摘要处理。
_FACE_ID_RE = re.compile(r"\d{1,6}")


def _sticker_segment(name: str) -> dict[str, Any] | None:
    """表情附件 → ``face`` 或 ``mface`` 段；非法 id / 空值返回 None。

    模型只给 ``name``：纯数字（1–6 位、非 0）视为 QQ 内置表情 id，
    其它文本当作表情包摘要（mface）。
    """
    value = name.strip()
    if not value:
        return None
    if value.isdigit():
        if not _FACE_ID_RE.fullmatch(value) or int(value) <= 0:
            return None
        return {"type": "face", "data": {"id": value}}
    return {"type": "mface", "data": {"summary": value}}


def build_onebot_segments(
    text: str,
    *,
    quote_id: str | None = None,
    at_qq_ids: Sequence[str] = (),
    attachments: Sequence[Attachment] = (),
) -> list[dict[str, Any]]:
    """按固定顺序拼 OneBot 消息段：reply -> at -> text -> 媒体。

    ``image`` 出 image 段，``record``/``voice`` 出 record 段，
    ``video`` 出 video 段，``sticker`` 出 face（纯数字 name）或 mface
    （文本 name）段；``file`` 走独立上传接口、``forward`` 需要节点结构，
    两者不在这里出段（静默跳过）；空文本不出段。
    """
    segments: list[dict[str, Any]] = []
    if quote_id:
        segments.append({"type": "reply", "data": {"id": str(quote_id)}})
    for qq_id in at_qq_ids:
        value = str(qq_id or "").strip()
        if value:
            segments.append({"type": "at", "data": {"qq": value}})
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    for item in attachments:
        kind = str(getattr(item, "kind", "") or "").strip().lower() or "file"
        if kind == "sticker":
            sticker = _sticker_segment(str(getattr(item, "name", "") or ""))
            if sticker is not None:
                segments.append(sticker)
            continue
        path = str(getattr(item, "path", "") or "")
        if not path:
            continue
        if kind == "image":
            segments.append({"type": "image", "data": {"file": path}})
        elif kind in ("record", "voice"):
            segments.append({"type": "record", "data": {"file": path}})
        elif kind == "video":
            segments.append({"type": "video", "data": {"file": path}})
    return segments


def resolve_at_names(names: Sequence[str], participants: Iterable[Any]) -> list[str]:
    """把模型点名的名字收敛成本批次在场成员：去重保序，最多 3 个。

    participants 的元素可以是 Message 对象（读 ``.nickname``）或裸昵称
    字符串；比较是去除首尾空白后的大小写敏感精确匹配，奇形输入不抛错。
    """
    # 非字符串的奇形输入一律忽略，绝不做 str() 兜底——否则数字/容器
    # 对象会伪装成某个昵称，凭空 @ 到无关成员。
    pool: set[str] = set()
    for item in participants:
        if isinstance(item, str):
            nick = item
        else:
            nick = getattr(item, "nickname", None)
            if not isinstance(nick, str):
                continue
        nick = nick.strip()
        if nick:
            pool.add(nick)

    kept: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            continue
        candidate = name.strip()
        if not candidate or candidate in seen or candidate not in pool:
            continue
        seen.add(candidate)
        kept.append(candidate)
        if len(kept) >= _MAX_AT_NAMES:
            break
    return kept
