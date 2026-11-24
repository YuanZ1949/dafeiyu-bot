"""回复媒体：附件安全解析、OneBot 消息段拼装、@-名字解析。

这里是模型输出进入发送链路前的安全边界：附件路径一律不允许逃出
``<workspace>/replies/``——形态检查（绝对路径/盘符/``..``）在 resolve
之前，包含性检查在 resolve 之后（顺带挡住指向目录外的软链）。

除 :func:`download_remote_attachments` 外，全部函数纯、无副作用：不建目录、
不读文件内容、不抛错，拒绝一律留痕。远端附件（``remote_media.enabled``）
先由 :func:`resolve_attachments` 做纯形态校验，再由下载函数落盘——SSRF
检查（协议/凭据/私网地址/重定向逐跳）与大小上限都在下载侧执行。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from dafeiyu.brain.client import BrainReply
from dafeiyu.config import Settings
from dafeiyu.faces import face_id
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
# 远端附件：只认这两个协议；其它（file/ftp/data）一律拒绝。
_REMOTE_SCHEMES = frozenset({"http", "https"})
# 主机名后缀：这些保留域永远指向本机/内网，不做 DNS 解析直接拒绝。
_INTERNAL_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".lan",
    ".home.arpa",
)
# 图片魔数 → 落盘后缀；Content-Type 不可信，最终以字节为准。
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)
# 未声明 Content-Type 时也接受：图片解码由魔数兜底。
_REMOTE_ACCEPTED_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "application/octet-stream", ""}
)
_MAX_REDIRECTS = 3
_REMOTE_UA = "dafeiyu-bot/1 (+reply media)"


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
    """校验全部通过后的 Attachment 组装：kind 归一，source 清空。

    sticker 的空 name 保持为空：path-only 贴纸在 QQ 侧发不出 face/mface
    时应直接跳过，而不是把 GIF 文件名伪装成表情摘要（mface）。
    """
    kind = str(getattr(candidate, "kind", "") or "").strip().lower() or "file"
    name = str(getattr(candidate, "name", "") or "")
    if not name and kind != "sticker":
        name = target.name
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


def is_remote_url(raw: str) -> bool:
    """``raw`` 是否是以 http/https 开头的远端附件地址。"""
    value = str(raw or "").strip()
    if not value:
        return False
    try:
        scheme = urllib.parse.urlsplit(value).scheme.lower()
    except ValueError:
        return False
    return scheme in _REMOTE_SCHEMES


def _host_allowed(host: str, allowed_hosts: Sequence[str]) -> bool:
    """主机白名单：精确匹配，或 ``*.example.com`` 通配子域；空名单放行全部。"""
    if not allowed_hosts:
        return True
    if host in allowed_hosts:
        return True
    for pattern in allowed_hosts:
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            if host.endswith(suffix) and host != suffix[1:]:
                return True
    return False


#: ``ipaddress.is_private`` 不覆盖的保留段：运营商级 NAT、IETF/TEST-NET、
#: 基准测试网段，一律当成不可达的目标拒绝。
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "100.64.0.0/10",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
    )
)


def _address_reject(host: str) -> str | None:
    """字面量 IP 的内网判定；域名返回 None（由下载侧 DNS 解析后再查）。"""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return "地址属于私网/环回/保留段"
    if any(address in network for network in _BLOCKED_NETWORKS):
        return "地址属于保留段"
    return None


def _remote_url_reject(url: str, allowed_hosts: Sequence[str]) -> str | None:
    """远端 URL 的纯形态校验（不解析 DNS、不联网）。"""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "URL 无法解析"
    if parsed.scheme.lower() not in _REMOTE_SCHEMES:
        return "协议不在 http/https"
    if parsed.username is not None or parsed.password is not None:
        return "URL 含凭据"
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return "URL 缺少主机"
    if not _host_allowed(host, allowed_hosts):
        return "主机不在白名单"
    lowered = host
    if lowered in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return "主机指向本机"
    if any(lowered.endswith(suffix) for suffix in _INTERNAL_HOST_SUFFIXES):
        return "主机名指向内网"
    try:
        port = parsed.port
    except ValueError:
        return "端口非法"
    if port is not None and not 1 <= port <= 65535:
        return "端口非法"
    return _address_reject(host)


def _resolve_reject(host: str, port: int) -> str | None:
    """DNS 解析后再校验每个地址，挡住 ``internal.example.com → 127.0.0.1`` 这类。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return f"DNS 解析失败：{type(exc).__name__}"
    addresses = {str(info[4][0]) for info in infos if info[4]}
    if not addresses:
        return "DNS 未返回地址"
    for address in addresses:
        reason = _address_reject(address)
        if reason:
            return reason
    return None


class _RemoteReject(Exception):
    """远端下载被安全策略拒绝（区别于网络错误）。"""


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """重定向逐跳做同样的形态校验，避免 302 到内网。"""

    max_redirections = _MAX_REDIRECTS

    def __init__(self, allowed_hosts: Sequence[str]) -> None:
        self._allowed_hosts = tuple(allowed_hosts)

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        reason = _remote_url_reject(newurl, self._allowed_hosts)
        if reason:
            raise _RemoteReject(f"重定向目标被拒绝（{reason}）")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sniff_image(data: bytes) -> str | None:
    """按魔数判定图片后缀；``RIFF....WEBP`` 单独处理。"""
    for magic, suffix in _IMAGE_MAGIC:
        if data.startswith(magic):
            return suffix
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _read_limited(response: Any, max_bytes: int) -> bytes:
    """流式读取并封顶；超过上限立刻中止（不把超大响应读进内存）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise _RemoteReject(f"响应超过上限 {max_bytes} 字节")
        chunks.append(chunk)
    return b"".join(chunks)


def _remote_short(url: str) -> str:
    """日志里不出现完整 query（可能带签名 token）。"""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "<非法 URL>"
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{base}?<{len(parsed.query)} 字符>" if parsed.query else base


def _download_one(
    url: str,
    destination_dir: Path,
    *,
    max_bytes: int,
    timeout: float,
    allowed_hosts: Sequence[str],
    opener_factory: Any = None,
) -> Attachment | None:
    """同步下载单个远端图片到 ``destination_dir``；任何失败只留痕并返回 None。"""
    reason = _remote_url_reject(url, allowed_hosts)
    if reason:
        log.warning("拒绝远端附件（%s）url=%s", reason, _remote_short(url))
        return None
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    # 字面量 IP 已校验；域名再解析一次，挡住「域名 → 内网 IP」。
    if _address_reject(host) is None:
        dns_reason = _resolve_reject(host, port)
        if dns_reason:
            log.warning("拒绝远端附件（%s）url=%s", dns_reason, _remote_short(url))
            return None
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _REMOTE_UA, "Accept": "image/*"},
        method="GET",
    )
    opener = (
        opener_factory(allowed_hosts)
        if opener_factory is not None
        else urllib.request.build_opener(_RedirectGuard(allowed_hosts))
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            if not 200 <= status < 300:
                raise _RemoteReject(f"HTTP {status}")
            content_type = (
                (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            )
            if content_type not in _REMOTE_ACCEPTED_TYPES:
                raise _RemoteReject(f"Content-Type {content_type!r} 不是图片")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > max_bytes:
                        raise _RemoteReject(f"Content-Length 超过上限 {max_bytes} 字节")
                except ValueError:
                    pass
            data = _read_limited(response, max_bytes)
    except _RemoteReject as exc:
        log.warning("拒绝远端附件（%s）url=%s", exc, _remote_short(url))
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("远端附件下载失败（%s）url=%s", exc, _remote_short(url))
        return None
    except Exception as exc:  # noqa: BLE001 - opener 可注入，保底不崩发送链路
        log.warning("远端附件下载异常（%s）url=%s", exc, _remote_short(url))
        return None

    suffix = _sniff_image(data)
    if suffix is None:
        log.warning("拒绝远端附件（内容不是可识别的图片）url=%s", _remote_short(url))
        return None
    if not data:
        log.warning("拒绝远端附件（内容为空）url=%s", _remote_short(url))
        return None
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / f"remote-{uuid.uuid4().hex}{suffix}"
        target.write_bytes(data)
    except OSError as exc:
        log.warning("远端附件落盘失败（%s）url=%s", exc, _remote_short(url))
        return None
    return Attachment(kind="image", source="", path=target.as_posix(), size=len(data))


async def download_remote_attachments(
    attachments: Sequence[Attachment],
    settings: Settings,
    *,
    opener_factory: Any = None,
) -> list[Attachment]:
    """把 ``resolve_attachments`` 产出的远端占位附件下载成真实文件。

    只处理「有 ``source`` URL、没有 ``path``」的条目；下载失败/被安全策略
    拒绝的条目直接丢弃（正文照发）。总条数受 ``remote_media.max_items``
    限制，按顺序下载，不并发（远端域名不可控，避免放大请求）。
    """
    remote = getattr(settings, "remote_media", None)
    pending = [
        item
        for item in attachments
        if str(getattr(item, "source", "") or "").strip()
        and not str(getattr(item, "path", "") or "").strip()
    ]
    if not pending:
        return list(attachments)
    if remote is None or not remote.enabled:
        log.warning("忽略 %d 个远端附件（remote_media 未启用）", len(pending))
        return [item for item in attachments if item not in pending]
    root = Path(settings.workspace.dir) if settings.workspace.enabled else None
    if root is None:
        log.warning("忽略 %d 个远端附件（工作区未启用）", len(pending))
        return [item for item in attachments if item not in pending]
    destination = root / REPLIES_DIRNAME
    budget = int(remote.max_items)
    resolved: list[Attachment] = []
    for item in attachments:
        url = str(getattr(item, "source", "") or "").strip()
        if not url or str(getattr(item, "path", "") or "").strip():
            resolved.append(item)
            continue
        if budget <= 0:
            log.warning("远端附件超过单条上限 %d，丢弃 url=%s", remote.max_items, _remote_short(url))
            continue
        budget -= 1
        try:
            downloaded = await asyncio.to_thread(
                _download_one,
                url,
                destination,
                max_bytes=int(remote.max_bytes),
                timeout=float(remote.timeout_seconds),
                allowed_hosts=tuple(remote.allowed_hosts),
                opener_factory=opener_factory,
            )
        except Exception as exc:  # noqa: BLE001 - 线程池异常同样只丢附件
            log.warning("远端附件下载任务异常（%s）url=%s", exc, _remote_short(url))
            continue
        if downloaded is None:
            continue
        # 保留模型给的 kind/name/duration，只把路径与大小换成落盘结果。
        resolved.append(
            replace(
                downloaded,
                kind=str(getattr(item, "kind", "") or "image").strip().lower() or "image",
                name=str(getattr(item, "name", "") or ""),
                duration=int(getattr(item, "duration", 0) or 0),
            )
        )
    return resolved


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
    ``sticker`` 无 path 时只校验非空 name 后原样透传；带 path 的贴纸走
    与图片相同的媒体解析，被拒但有 name 时回退为 name-only 贴纸。

    ``remote_media.enabled`` 时，``http(s)://`` 开头的 path 不落盘、只做
    纯形态校验后产出 ``kind="image"``、``source=<URL>`` 的占位附件，交给
    :func:`download_remote_attachments` 下载；未启用时与历史行为一致（拒绝）。
    """
    candidates: tuple[Attachment, ...] = reply.attachments
    if not candidates:
        return []
    root = Path(settings.workspace.dir) if settings.workspace.enabled else None
    base = (root / REPLIES_DIRNAME).resolve() if root is not None else None
    images = (root / IMAGES_DIRNAME).resolve() if root is not None else None
    # 贴纸库目录（启用时）：与 replies/ 一样恒允许，不需要会话归属。
    stickers_dirname = ""
    stickers: Path | None = None
    sticker_cfg = getattr(settings, "sticker_library", None)
    if (
        root is not None
        and sticker_cfg is not None
        and getattr(sticker_cfg, "enabled", False)
    ):
        name = str(getattr(sticker_cfg, "dir", "") or "").strip().strip("/\\")
        if name and "/" not in name and "\\" not in name and ".." not in name:
            stickers_dirname = name.lower()
            stickers = (root / name).resolve()
    remote_used = 0

    resolved: list[Attachment] = []
    for candidate in candidates:
        kind = str(getattr(candidate, "kind", "") or "").strip().lower()
        raw = str(getattr(candidate, "path", "") or "")
        fallback_name = ""
        if kind == "sticker":
            fallback_name = str(getattr(candidate, "name", "") or "").strip()
            if not raw.strip():
                # 无 path 的贴纸保持原行为：只按 name 透传（face/mface）。
                if not fallback_name:
                    log.info("忽略空 name 的表情附件")
                    continue
                resolved.append(
                    Attachment(kind="sticker", name=fallback_name[:_MAX_NAME_CHARS])
                )
                continue
            # 有 path 的贴纸落到下方通用媒体解析；被拒时按 name 兜底。
        elif kind == "forward":
            forward = _forward_attachment(candidate)
            if forward is None:
                log.info("忽略没有有效节点的合并转发附件")
                continue
            resolved.append(forward)
            continue
        elif not raw.strip():
            # 形残但无害：空路径不配 warning，info 留痕即可。
            log.info("忽略空路径的模型附件 kind=%r", getattr(candidate, "kind", "") or "")
            continue
        if is_remote_url(raw):
            # 远端附件只在这里做纯形态校验；下载与 DNS/重定向检查在下载侧。
            remote = getattr(settings, "remote_media", None)
            if remote is None or not remote.enabled:
                log.warning("拒绝模型附件（remote_media 未启用）path=%r", raw)
            elif root is None:
                log.warning("工作区未启用，拒绝远端模型附件 url=%r", raw)
            elif remote_used >= int(remote.max_items):
                log.warning(
                    "拒绝模型附件（远端附件超过上限 %d）url=%r",
                    remote.max_items,
                    raw,
                )
            else:
                reason = _remote_url_reject(raw, tuple(remote.allowed_hosts))
                if reason:
                    log.warning("拒绝模型附件（%s）url=%r", reason, raw)
                else:
                    remote_used += 1
                    resolved.append(
                        Attachment(
                            kind="image",
                            source=raw,
                            name=fallback_name[:_MAX_NAME_CHARS],
                        )
                    )
                    continue
            if kind == "sticker" and fallback_name:
                resolved.append(
                    Attachment(kind="sticker", name=fallback_name[:_MAX_NAME_CHARS])
                )
            continue
        if root is None or base is None or images is None:
            log.warning("工作区未启用，拒绝模型附件 path=%r", raw)
            item = None
        else:
            # 允许的根目录：replies / 贴纸库恒允许；images 仅当会话归属成立时放行。
            allowed: dict[str, Path] = {REPLIES_DIRNAME: base}
            if stickers_dirname and stickers is not None:
                allowed[stickers_dirname] = stickers
            if _lead_dirname(raw) == IMAGES_DIRNAME:
                rel = PurePosixPath(raw.replace("\\", "/")).as_posix()
                if not _images_owned(rel, settings, store, chat_type, chat_id):
                    log.warning("拒绝模型附件（images 未获本会话归属）path=%r", raw)
                    allowed = {}
                else:
                    allowed[IMAGES_DIRNAME] = images
            if not allowed:
                item = None
            else:
                try:
                    item = _resolve_one(candidate, raw, root, allowed)
                except (OSError, ValueError) as exc:
                    # stat/resolve 撞上损坏的软链、权限、非法字符：按拒绝处理。
                    log.warning("拒绝模型附件（解析出错：%s）path=%r", exc, raw)
                    item = None
        if item is not None:
            resolved.append(item)
        elif kind == "sticker" and fallback_name:
            # 贴纸文件被拒（越界/不在白名单/超限等）仍有 name 兜底：
            # QQ 发 face/mface，不至于整条丢表情。
            resolved.append(Attachment(kind="sticker", name=fallback_name[:_MAX_NAME_CHARS]))
    return resolved


#: face 段的 id 形态：纯数字（QQ 内置表情编号）；其余按 mface 文本摘要处理。
_FACE_ID_RE = re.compile(r"\d{1,6}")


def _sticker_segment(name: str) -> dict[str, Any] | None:
    """表情附件 → ``face`` 或 ``mface`` 段；非法 id / 空值返回 None。

    模型只给 ``name``：纯数字（1–6 位、非 0）视为 QQ 内置表情 id；
    其它文本先按内置表情名称反查（如 ``微笑``），命中出 face 段；
    未命中当作表情包摘要（mface）。
    """
    value = name.strip()
    if not value:
        return None
    if value.isdigit():
        if not _FACE_ID_RE.fullmatch(value) or int(value) <= 0:
            return None
        return {"type": "face", "data": {"id": value}}
    resolved = face_id(value)
    if resolved is not None:
        return {"type": "face", "data": {"id": resolved}}
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
