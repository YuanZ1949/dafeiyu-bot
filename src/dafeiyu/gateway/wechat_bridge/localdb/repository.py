"""微信 4.1 本地库 repository：把解密后的明文镜像 SQLite 读成桥的消息条目。

只依赖镜像对象的 ``path_for(rel) -> Path``（以及可选的 ``paths()``）；本模块
**不** import `sqlcipher` / `wal` / `mirror`：解密与镜像生命周期由另一层负责，
这里只读文件。

输出形状对齐 ``driver._normalize_entry``::

    {"nickname": str, "text": str, "kind": str,
     "reply_to": str, "reply_author": str, "ts": int}

分两层：

- :class:`ChatRef` 把群显示名解析成 ``(username, shard, table)``；
- :meth:`LocalMessageStore.recent` 读最近 N 条，按 ``local_id`` 翻成时间正序。

fail-open 是本模块的第一原则：镜像缺失、表缺失、schema 不符、文件损坏、权限
不足一律返回空结果并留一条 debug（poller 不能因为读库失败而停摆）。唯一会抛
`StoreError` 的是构造参数非法——那是集成代码的 bug，应该在启动时就暴露。
"""

from __future__ import annotations

import hashlib
import html
import importlib
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

#: 可选依赖：没装时压缩消息退化成空文本，不影响其余行。用 importlib 而非静态
#: import，避免「可选依赖不在类型检查器解释器里」变成硬错误。
_zstandard: Any
try:
    _zstandard = importlib.import_module("zstandard")
except ImportError:  # pragma: no cover - 依赖已列在运行环境里，仅作降级
    _zstandard = None

log = logging.getLogger(__name__)

__all__ = ["ChatRef", "LocalMessageStore", "StoreError"]

#: 群 username 的固定后缀。
CHATROOM_SUFFIX = "@chatroom"

#: 系统/官方会话：即使不是 ``@chatroom`` 也不能当好友私聊对象。
_SYSTEM_USERNAMES = frozenset(
    {
        "filehelper",
        "notifymessage",
        "weixin",
        "fmessage",
        "medianote",
        "floatbottle",
        "qqmail",
        "qmessage",
        "tmessage",
        "weixinreminder",
        "officialaccounts",
        "mphelper",
        "brandsessionholder",
        "snshelper",
    }
)

#: `local_type` 低 32 位是类型，高位是标志位。
_TYPE_MASK = 0xFFFFFFFF

#: `WCDB_CT_message_content == 4`：该列是 zstd 压缩的 BLOB。
_CONTENT_ZSTD = 4

#: 支持的稳定消息类型（10000/10002/未知类型一律丢弃）。
_SUPPORTED_TYPES = frozenset({1, 3, 34, 42, 43, 47, 48, 49, 50})

#: 非文本类型的占位文本；49（appmsg）单独取 `<title>`。
_PLACEHOLDER_BY_TYPE: dict[int, tuple[str, str]] = {
    3: ("image", "[图片]"),
    34: ("voice", "[语音]"),
    42: ("card", "[名片]"),
    43: ("video", "[视频]"),
    47: ("sticker", "[表情]"),
    48: ("location", "[位置]"),
    50: ("call", "[通话]"),
}

#: 表情 XML 里可作为可读名的属性，按优先级排列（实测大多为空）。
_EMOJI_NAME_ATTRS = ("desc", "attachedtext")

#: 表情 XML 的 md5 属性（缓存文件名与它一致）；XML 解析失败时用正则兜底。
_EMOJI_MD5_RE = re.compile(r'<emoji[^>]*\bmd5="([0-9a-fA-F]{32})"')
_HEX32_RE = re.compile(r"[0-9a-f]{32}")

#: appmsg 的引用回复类型。
_APPMSG_QUOTE = 57

#: appmsg 的合并转发类型。
_APPMSG_FORWARD = 19

#: 视频号分享 XML 的正则兜底（ElementTree 解析失败时用）。
_FINDER_NICK_RE = re.compile(r"<nickname[^>]*>(.*?)</nickname>", re.DOTALL)
_FINDER_DESC_RE = re.compile(r"<desc[^>]*>(.*?)</desc>", re.DOTALL)
_FINDER_DURATION_RE = re.compile(
    r"<videoPlayDuration[^>]*>\s*(\d+)\s*</videoPlayDuration>", re.DOTALL
)
_FINDER_MEDIA_COUNT_RE = re.compile(
    r"<mediaCount[^>]*>\s*(\d+)\s*</mediaCount>", re.DOTALL
)

#: 只认 `message/message_<数字>.db`；`message_fts.db` / `message_resource.db` /
#: `biz_message_0.db` / `*.kvdb` 因此天然被排除。
_MESSAGE_REL_RE = re.compile(r"^message/message_(\d+)\.db$")

#: 非自己发的群消息正文以 `"<发送者wxid>:\n"` 开头。wxid 里不会有冒号/换行。
_SENDER_PREFIX_RE = re.compile(r"^([^:\r\n]{1,128}):\r?\n")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL)
_APPMSG_TYPE_RE = re.compile(r"<type[^>]*>\s*(\d+)\s*</type>", re.DOTALL)
_REFER_AUTHOR_RE = re.compile(r"<displayname[^>]*>(.*?)</displayname>", re.DOTALL)
_REFER_CONTENT_RE = re.compile(r"<content[^>]*>(.*?)</content>", re.DOTALL)

#: `recent` 需要的最小列集合；少一列说明不是微信 4.1 的 schema。
_REQUIRED_COLUMNS = frozenset(
    {
        "local_id",
        "local_type",
        "real_sender_id",
        "create_time",
        "message_content",
        "WCDB_CT_message_content",
    }
)

#: 媒体落盘只处理这几类；47（表情）按图片处理。
_MEDIA_KIND_BY_TYPE: dict[int, str] = {3: "image", 34: "voice", 43: "video", 47: "image"}

#: 已知的微信媒体索引库（仓库里没有可验证的 schema，按「存在才读」适配）。
_MEDIA_REL = "message/message_resource.db"

#: 变体优先级：原图 > `_h` > `_t`。
#:
#: 4.x 的原图（无后缀 `.dat`）解密后多是微信私有的 ``wxgf`` 容器，解码层会
#: 把其中的 HEVC 转成高分辨率 PNG；只有转码失败（缺密钥/缺 ffmpeg）时才按
#: 顺序退回 `_h` / `_t`。早期按「模型能不能直接打开」排序，把 180px 的 `_t`
#: 排在最前，导致能拿到原图的消息也只有缩略图画质。
_VARIANT_ORDER: tuple[str, ...] = ("", "_h", "_t")

#: 从 `packed_info` protobuf blob 里捞 32 位 hex md5（前后不能再接 hex 字符）。
_MD5_IN_BLOB_RE = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")

#: 表名优先级；实际列名在运行时从 schema 读，绝不按固定 DDL 假设。
_MEDIA_TABLE_PRIORITY = (
    "message_resource",
    "MessageResourceInfo",
    "message_resource_info",
    "hardlink",
    "HardLinkInfo",
    "media",
)
_MEDIA_ID_COLUMNS = (
    "message_local_id",
    "local_id",
    "msg_local_id",
    "message_id",
    "msg_id",
)
_MEDIA_PATH_COLUMNS = ("local_path", "file_path", "media_path", "path")
_MEDIA_NAME_COLUMNS = ("file_name", "name")
_MEDIA_SIZE_COLUMNS = ("file_size", "size", "filesize")
_MEDIA_DURATION_COLUMNS = ("duration", "play_length", "voice_length")

#: 视频单独的大小上限：不复用图片的 ``media_max_bytes``（默认 10 MiB 对视频太小）。
MAX_VIDEO_BYTES = 100 * 1024 * 1024

#: 视频目录名：4.x 实测是大写 ``Video``，旧版本可能是小写，两者都试。
_VIDEO_DIR_NAMES = ("Video", "video")

#: 视频原文件常见后缀；匹配 ``<md5>*`` 时只用于排序，不限制文件必须叫这些名字。
_VIDEO_LIKE_SUFFIXES = frozenset(
    {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".dat"}
)


@dataclass(frozen=True)
class ChatRef:
    """一个已定位的会话：显示名 + username + 分库相对路径 + 消息表名。"""

    display_name: str
    username: str
    shard: str
    table: str
    #: "group" / "friend"；旧调用方按 4 个位置参数构造时默认按群处理。
    kind: str = "group"


class StoreError(RuntimeError):
    """本地库 repository 的构造参数/配置错误（运行时读失败不抛这个）。"""


class LocalMessageStore:
    """只读的本地库视图；实例可以跨线程复用（每次调用各自开连接）。"""

    def __init__(
        self,
        mirrors: Any,
        *,
        self_username: str,
        self_display_name: str,
        contact_rel: str = "contact/contact.db",
        message_rels: Sequence[str] | None = None,
        media_enabled: bool = False,
        media_max_bytes: int = 10 * 1024 * 1024,
        media_storage_root: str = "",
    ) -> None:
        if not callable(getattr(mirrors, "path_for", None)):
            raise StoreError("mirrors 必须提供 path_for(rel) -> Path")
        if not isinstance(self_username, str) or not self_username.strip():
            raise StoreError("self_username 不能为空")
        if not isinstance(self_display_name, str):
            raise StoreError("self_display_name 必须是 str")
        if not isinstance(contact_rel, str) or not contact_rel.strip():
            raise StoreError("contact_rel 不能为空")
        if not isinstance(media_enabled, bool):
            raise StoreError("media_enabled 必须是 bool")
        if int(media_max_bytes) <= 0:
            raise StoreError("media_max_bytes 必须大于 0")

        explicit: tuple[str, ...] | None = None
        if message_rels is not None:
            if isinstance(message_rels, (str, bytes)):
                raise StoreError("message_rels 必须是路径序列，不能是单个字符串")
            try:
                rels = tuple(str(rel) for rel in message_rels)
            except TypeError as exc:
                raise StoreError("message_rels 必须是可迭代的路径序列") from exc
            if not all(rel.strip() for rel in rels):
                raise StoreError("message_rels 里不能有空路径")
            explicit = rels

        self._mirrors = mirrors
        self._self_username = self_username.strip()
        # 群显示名与配置里的 account_nickname 同源，空值允许（只是自己的条目昵称为空）。
        self._self_display_name = self_display_name.strip()
        self._contact_rel = contact_rel.replace("\\", "/")
        self._explicit_rels = explicit
        self._warned_no_zstd = False
        self._media_enabled = media_enabled
        self._media_max_bytes = int(media_max_bytes)
        self._media_storage_root = str(media_storage_root or "").strip()

    # ------------------------------------------------------------- 公共 API
    def group_names(self) -> list[str]:
        """所有 ``@chatroom`` 的显示名（去重、排序）。"""
        names = {
            display
            for username, display in self._contact_rows()
            if _is_chatroom(username) and display
        }
        return sorted(names)

    def friend_names(self) -> list[str]:
        """所有好友（``wxid_*`` 私聊对象）的显示名（去重、排序）。

        只认 ``contact`` 表里的 wxid：官方号（``gh_``）、系统会话
        （filehelper / weixin / notifymessage …）和 ``@openim`` 都不算好友，
        避免把系统通知当成「有人私聊 bot」。
        """
        names = {
            display
            for username, display in self._contact_rows()
            if self._is_friend_username(username) and display
        }
        return sorted(names)

    def resolve(self, display_name: str, kind: str = "group") -> ChatRef | None:
        """按显示名解析 ``(username, shard, table)``；歧义/缺失返回 None。"""
        name = str(display_name or "").strip()
        if not name or kind not in ("group", "friend"):
            return None

        matched = [
            username
            for username, display in self._contact_rows()
            if self._match_username(username, kind) and display == name
        ]
        if not matched:
            log.debug(
                "%s显示名 %r 在 contact 里没有对应的 username",
                "群" if kind == "group" else "好友",
                name,
            )
            return None
        if len(set(matched)) > 1:
            # 同名 fail closed：猜错会话会把 A 的消息发进 B。
            log.warning(
                "%s显示名 %r 命中 %d 个 username，拒绝猜测：%s",
                "群" if kind == "group" else "好友",
                name,
                len(set(matched)),
                sorted(set(matched)),
            )
            return None

        username = matched[0]
        table = message_table_name(username)
        shard = self._find_shard(table)
        if shard is None:
            log.debug("会话 %r（%s）的消息表 %s 不在任何分库里", name, username, table)
            return None
        return ChatRef(
            display_name=name, username=username, shard=shard, table=table, kind=kind
        )

    def resolve_any(self, display_name: str) -> ChatRef | None:
        """群或好友任一命中；同名的群和好友同时存在时返回 None（不猜）。"""
        group = self.resolve(display_name, kind="group")
        friend = self.resolve(display_name, kind="friend")
        if group is not None and friend is not None:
            log.warning(
                "显示名 %r 同时命中群 %s 和好友 %s，拒绝猜测",
                display_name,
                group.username,
                friend.username,
            )
            return None
        return group or friend

    def kind_of(self, display_name: str) -> str | None:
        """显示名唯一对应的会话类型；不存在或群/好友重名时返回 None。"""
        name = str(display_name or "").strip()
        if not name:
            return None
        kinds = [kind for kind in ("group", "friend") if self.resolve(name, kind)]
        return kinds[0] if len(kinds) == 1 else None

    def _match_username(self, username: str, kind: str) -> bool:
        if kind == "group":
            return _is_chatroom(username)
        if kind == "friend":
            return self._is_friend_username(username)
        return False

    def _is_friend_username(self, username: str) -> bool:
        """微信 4.x 的私聊对象：``wxid_*``，排除自己和系统/官方号。"""
        value = str(username or "").strip()
        return (
            bool(value)
            and value.startswith("wxid_")
            and value != self._self_username
            and value not in _SYSTEM_USERNAMES
        )

    def recent(self, ref: ChatRef, limit: int = 50) -> list[dict[str, Any]]:
        """读 ``ref`` 最近的 ``limit`` 条消息，时间正序（旧 -> 新）。"""
        count = _positive_int(limit)
        if count is None:
            log.debug("recent 的 limit 非法（%r），返回空列表", limit)
            return []
        username = str(getattr(ref, "username", "") or "")
        shard = str(getattr(ref, "shard", "") or "")
        table = str(getattr(ref, "table", "") or "")
        if not username or not shard or not table:
            log.debug("recent 收到不完整的 ChatRef：%r", ref)
            return []

        path = self._mirror_path(shard)
        if path is None:
            return []
        try:
            con = _connect(path)
        except (sqlite3.Error, OSError) as exc:
            log.debug("打开消息分库 %s 失败：%s", shard, exc)
            return []
        with closing(con):
            if not _has_columns(con, table):
                log.debug("%s 里的表 %s 缺失或 schema 不符，返回空列表", shard, table)
                return []
            senders = _name2id(con)
            contacts, strangers = self._display_indexes()
            try:
                rows = con.execute(
                    f"SELECT local_id, local_type, real_sender_id, create_time, "
                    f"message_content, WCDB_CT_message_content "
                    f'FROM "{_quote_ident(table)}" ORDER BY local_id DESC LIMIT ?',
                    (count,),
                ).fetchall()
            except sqlite3.Error as exc:
                log.debug("读取 %s.%s 失败：%s", shard, table, exc)
                return []
            media_raw = (
                self._media_rows(
                    con,
                    [_safe_int(row[0]) for row in rows],
                    username,
                    {
                        _safe_int(row[0]): _base_type(row[1]) or 0
                        for row in rows
                    },
                )
                if self._media_enabled
                else {}
            )

        entries: list[dict[str, Any]] = []
        # SQL 取的是最新 N 条（DESC），这里翻成时间正序（旧 -> 新）。
        for row in reversed(rows):
            raw = media_raw.get(_safe_int(row[0]))
            entry = self._entry_from_row(row, senders, contacts, strangers, raw)
            if entry is not None:
                entries.append(entry)
        return entries

    def find_self_message(
        self,
        ref: ChatRef,
        text: str,
        *,
        since: float,
        limit: int = 50,
    ) -> dict[str, Any] | None:
        """回读自己刚发的消息：同群 + 同文本 + ``create_time >= since``。

        只认 ``self_username`` 对应的条目（其昵称由本模块填成
        ``self_display_name``）。没有配置显示名时无法区分 self 与他人，
        fail-closed 返回 None——宁可回执停在 ``typed``，也不把别人的话当成
        自己的送达证据。
        """
        body = "" if text is None else str(text)
        if not body or not self._self_display_name:
            return None
        try:
            threshold = float(since)
        except (TypeError, ValueError):
            return None
        for entry in reversed(self.recent(ref, limit)):
            ts = entry.get("ts")
            if not isinstance(ts, int) or isinstance(ts, bool) or ts < threshold:
                continue
            if entry.get("text") != body:
                continue
            if entry.get("nickname") != self._self_display_name:
                continue
            return entry
        return None

    # ------------------------------------------------------------- 行规整
    def _entry_from_row(
        self,
        row: Sequence[Any],
        senders: dict[int, str],
        contacts: dict[str, str],
        strangers: dict[str, str],
        media_raw: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        local_type = _base_type(row[1])
        if local_type not in _SUPPORTED_TYPES:
            # 系统消息（10000/10002）与未知类型不进入对齐序列。
            return None

        sender_username = senders.get(_safe_int(row[2]), "")
        content = self._decode_content(row[4], _safe_int(row[5]))
        prefix_username, stripped = _strip_sender_prefix(content)
        # 只有前缀确实属于这条消息的发件人才剥；否则正文里出现的 "xxx:\n"
        # 可能是用户自己打的（例如 "http://x:8080\n"），剥了就是丢数据。
        body = (
            stripped
            if prefix_username and prefix_username == sender_username
            else content
        )

        kind, text, reply_to, reply_author = _render_body(local_type, body)

        if local_type == 47 and media_raw is None and self._media_enabled:
            # 贴纸的 md5 在消息 XML 里，比媒体索引可靠；缓存目录按 md5 分片。
            media_raw = self._emoticon_media(body)

        if sender_username == self._self_username:
            nickname = self._self_display_name
        else:
            nickname = contacts.get(sender_username) or strangers.get(sender_username) or ""
            if not nickname and prefix_username and (
                not sender_username or prefix_username == sender_username
            ):
                # 联系人表里没有这个人：正文前缀里的 wxid 是唯一可靠的身份，
                # 但仍不把它当显示名「猜」——它就是 wxid 本身。
                nickname = prefix_username

        return {
            "nickname": nickname,
            "text": text,
            "kind": kind,
            "reply_to": reply_to,
            "reply_author": reply_author,
            "ts": _safe_int(row[3]),
        } | (
            {"media": media}
            if (media := self._media_meta(local_type, media_raw)) is not None
            else {}
        )

    def _decode_content(self, value: Any, content_type: int) -> str:
        """按 ``WCDB_CT_message_content`` 解正文；任何失败都退化成空串。"""
        if value is None:
            return ""
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            if content_type != _CONTENT_ZSTD:
                return data.decode("utf-8", "replace")
            return self._decompress(data)
        if isinstance(value, str):
            if content_type == _CONTENT_ZSTD:
                log.debug("WCDB_CT_message_content=4 但正文已是文本，按明文处理")
            return value
        return str(value)

    def _decompress(self, data: bytes) -> str:
        if _zstandard is None:
            if not self._warned_no_zstd:
                self._warned_no_zstd = True
                log.warning("zstandard 不可用：压缩消息正文按空文本处理")
            return ""
        try:
            raw = _zstandard.ZstdDecompressor().decompress(data)
        except Exception as exc:  # noqa: BLE001 - 单条坏数据不能打断整批
            log.debug("zstd 解压失败（%d 字节）：%s", len(data), exc)
            return ""
        return raw.decode("utf-8", "replace")

    # ------------------------------------------------------------- 库/表定位
    def _mirror_path(self, rel: str) -> Path | None:
        try:
            raw = self._mirrors.path_for(rel)
        except Exception as exc:  # noqa: BLE001 - 镜像层故障由调用方 fail-open
            log.debug("mirrors.path_for(%r) 失败：%s", rel, exc)
            return None
        if raw is None:
            return None
        try:
            return Path(raw)
        except TypeError as exc:
            log.debug("mirrors.path_for(%r) 返回了非路径值 %r：%s", rel, raw, exc)
            return None

    def _shard_rels(self) -> tuple[str, ...]:
        """分库相对路径：显式参数 > ``mirrors.paths()`` > 默认 ``message_0``。"""
        if self._explicit_rels is not None:
            return self._explicit_rels

        found: set[str] = set()
        paths = getattr(self._mirrors, "paths", None)
        if callable(paths):
            try:
                raw_paths = list(cast(Any, paths)())
            except Exception as exc:  # noqa: BLE001 - 枚举失败退回默认分库
                log.debug("枚举镜像路径失败：%s", exc)
            else:
                for item in raw_paths:
                    rel = _message_rel(item)
                    if rel is not None:
                        found.add(rel)
        if found:
            return tuple(sorted(found, key=_shard_order))

        log.debug("没有发现 message/message_<N>.db，只尝试 message/message_0.db")
        return ("message/message_0.db",)

    def _find_shard(self, table: str) -> str | None:
        for rel in self._shard_rels():
            path = self._mirror_path(rel)
            if path is None:
                continue
            try:
                with closing(_connect(path)) as con:
                    exists = con.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                        (table,),
                    ).fetchone()
            except (sqlite3.Error, OSError) as exc:
                log.debug("检查分库 %s 失败，跳过：%s", rel, exc)
                continue
            if exists:
                return rel
        return None

    # ------------------------------------------------------------- 联系人
    def _contact_rows(self) -> list[tuple[str, str]]:
        """`contact` 里的 ``(username, remark or nick_name)``，失败返回空。"""
        return self._read_display_rows("contact")

    def _display_indexes(self) -> tuple[dict[str, str], dict[str, str]]:
        contacts = dict(self._read_display_rows("contact"))
        strangers = dict(self._read_display_rows("stranger"))
        return contacts, strangers

    def _read_display_rows(self, table: str) -> list[tuple[str, str]]:
        if table not in ("contact", "stranger"):  # 表名只可能来自本模块常量
            return []
        path = self._mirror_path(self._contact_rel)
        if path is None:
            return []
        try:
            with closing(_connect(path)) as con:
                rows = con.execute(
                    f'SELECT username, remark, nick_name FROM "{table}"'
                ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            log.debug("读取 %s.%s 失败：%s", self._contact_rel, table, exc)
            return []
        out: list[tuple[str, str]] = []
        for username, remark, nick_name in rows:
            name = str(username or "").strip()
            if name:
                out.append((name, _display_name(remark, nick_name)))
        return out

    # ------------------------------------------------------------- 媒体元数据
    def _media_rows(
        self,
        con: sqlite3.Connection,
        local_ids: Sequence[int],
        username: str = "",
        types: dict[int, int] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """``local_id -> {path,name,size,duration}``；任何失败都返回空。

        schema 不可验证（仓库里没有真实媒体库）：只在**确实存在**的表里按列名
        适配，读不到就当没有媒体，绝不猜路径。除旧库的路径列外，再按 4.x 的
        ``MessageResourceInfo.packed_info`` + attach 目录约定补一份来源；新来源
        只补缺，不覆盖已经从路径列读到的结果。``types``（``local_id -> 基础
        local_type``）决定按图片还是视频约定解析候选路径。
        """
        wanted = {_safe_int(value) for value in local_ids}
        wanted.discard(0)
        if not wanted:
            return {}
        found = self._query_media_tables(con, wanted)
        resource = self._mirror_path(_MEDIA_REL)
        if resource is not None:
            try:
                with closing(_connect(resource)) as resource_con:
                    found.update(self._query_media_tables(resource_con, wanted))
            except (sqlite3.Error, OSError) as exc:
                log.debug("打开媒体索引库失败：%s", exc)
        for key, value in self._resource_media(wanted, username, types).items():
            found.setdefault(key, value)
        return found

    def _resource_media(
        self,
        ids: set[int],
        username: str,
        types: dict[int, int] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """从 ``message_resource.db`` 的 md5 推 attach 路径；失败一律返回空。

        4.x 的实际布局是 ``msg/attach/<md5(chat)>/<YYYY-MM>/Img/<md5>[_h|_t].dat``，
        视频则放在同级的 ``Video/``（也可能是小写 ``video/``）或 ``msg/video/``
        下。索引库里没有路径列，只有 ``packed_info`` blob 里的 md5。这里只负责
        「算出候选路径并确认磁盘上确实有文件」，最终是否放行仍交给
        :meth:`_media_meta`（root 越界/大小校验都在那里）。
        """
        if not username or not ids:
            return {}
        try:
            resource = self._mirror_path(_MEDIA_REL)
            if resource is None:
                return {}
            with closing(_connect(resource)) as con:
                columns_by_table = _table_columns(con)
                info_table = _table_named(columns_by_table, "MessageResourceInfo")
                if info_table is None:
                    return {}
                info_columns = {name.lower(): name for name in columns_by_table[info_table]}
                local_column = info_columns.get("message_local_id")
                packed_column = info_columns.get("packed_info")
                if local_column is None or packed_column is None:
                    return {}
                message_id_column = info_columns.get("message_id")
                chat_column = info_columns.get("chat_id")

                placeholders = ",".join("?" for _ in ids)
                quoted_local = f'"{_quote_ident(local_column)}"'
                quoted_packed = f'"{_quote_ident(packed_column)}"'
                quoted_message_id = (
                    f'"{_quote_ident(message_id_column)}"'
                    if message_id_column is not None
                    else "NULL"
                )
                where = f"{quoted_local} IN ({placeholders})"
                params: list[Any] = sorted(ids)
                # `message_local_id` 只在单个会话内唯一；能定位到 chat_id 时必须
                # 一起过滤，否则会把别的会话的同号媒体串过来。定位失败则退回
                # 只按 local_id 查（fail-open，最多多一次磁盘探测）。
                chat_id = (
                    self._chat_rowid(con, columns_by_table, username)
                    if chat_column is not None
                    else None
                )
                if chat_column is not None and chat_id is not None:
                    where += f' AND "{_quote_ident(chat_column)}" = ?'
                    params.append(chat_id)
                rows = con.execute(
                    f"SELECT {quoted_local}, {quoted_message_id}, {quoted_packed} "
                    f'FROM "{_quote_ident(info_table)}" '
                    f"WHERE {where}",
                    tuple(params),
                ).fetchall()

                root = self._media_root()
                if root is None:
                    return {}
                base = (
                    Path(root)
                    / "msg"
                    / "attach"
                    / hashlib.md5(username.encode("utf-8")).hexdigest()
                )
                found: dict[int, dict[str, Any]] = {}
                for local_id, _message_id, packed in rows:
                    key = _safe_int(local_id)
                    digest = _blob_md5(packed)
                    if not key or not digest:
                        continue
                    local_type = (types or {}).get(key)
                    if _MEDIA_KIND_BY_TYPE.get(local_type or 0) == "video":
                        primary = _video_file(root, username, digest)
                        paths = [primary] if primary is not None else []
                    else:
                        paths = _attach_files(base, digest)
                    if paths:
                        found[key] = {
                            "path": str(paths[0]),
                            "name": paths[0].name,
                            "size": 0,
                            "duration": 0,
                        }
                        if len(paths) > 1:
                            # 原图解不开时按优先级退回 `_h` / `_t`。
                            found[key]["fallbacks"] = [str(item) for item in paths[1:]]
                return found
        except Exception as exc:  # noqa: BLE001 - 媒体发现永远不能打断消息读取
            log.debug("从 message_resource.db 推导媒体路径失败：%s", exc)
            return {}

    def _chat_rowid(
        self,
        con: sqlite3.Connection,
        columns_by_table: dict[str, set[str]],
        username: str,
    ) -> int | None:
        """``ChatName2Id`` 里 ``username`` 对应的 rowid（即 ``chat_id``）。

        表结构是 ``(user_name, update_time)``，会话 id 就是 rowid。缺表/缺列/
        查不到都返回 None，调用方据此跳过会话过滤。
        """
        table = _table_named(columns_by_table, "ChatName2Id")
        if table is None:
            return None
        columns = {name.lower(): name for name in columns_by_table[table]}
        name_column = columns.get("user_name") or columns.get("username")
        if name_column is None:
            return None
        try:
            row = con.execute(
                f'SELECT rowid FROM "{_quote_ident(table)}" '
                f'WHERE "{_quote_ident(name_column)}" = ? LIMIT 1',
                (username,),
            ).fetchone()
        except sqlite3.Error as exc:
            log.debug("解析 ChatName2Id 失败：%s", exc)
            return None
        return _safe_int(row[0]) if row is not None else None

    def _query_media_tables(
        self, con: sqlite3.Connection, ids: set[int]
    ) -> dict[int, dict[str, Any]]:
        columns_by_table = _table_columns(con)
        placeholders = ",".join("?" for _ in ids)
        parameters = tuple(sorted(ids))
        for table, layout in _media_tables(columns_by_table):
            id_column = layout["id"]
            select = [id_column, layout["path"]]
            for key in ("name", "size", "duration"):
                if layout.get(key):
                    select.append(layout[key])
            quoted = ", ".join(f'"{_quote_ident(col)}"' for col in select)
            sql = (
                f'SELECT {quoted} FROM "{_quote_ident(table)}" '
                f'WHERE "{_quote_ident(id_column)}" IN ({placeholders})'
            )
            try:
                rows = con.execute(sql, parameters).fetchall()
            except sqlite3.Error as exc:
                log.debug("读取媒体表 %s 失败：%s", table, exc)
                continue
            found: dict[int, dict[str, Any]] = {}
            for row in rows:
                values = dict(zip(select, row, strict=False))
                key = _safe_int(values.get(id_column))
                if key:
                    found[key] = {
                        "path": values.get(layout["path"]),
                        "name": values.get(layout["name"]) if layout.get("name") else "",
                        "size": values.get(layout["size"]) if layout.get("size") else 0,
                        "duration": (
                            values.get(layout["duration"])
                            if layout.get("duration")
                            else 0
                        ),
                    }
            if found:
                return found
        return {}

    def _media_meta(
        self, local_type: int, raw: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """把索引行落成文档化的 media dict；证明不了文件存在就返回 None。

        ``fallbacks`` 里的备选路径同样逐条校验（越界/缺失/超限一律丢弃），
        只保留能安全读取的候选，顺序不变。
        """
        kind = _MEDIA_KIND_BY_TYPE.get(local_type)
        if kind is None or not isinstance(raw, dict):
            return None
        text = str(raw.get("path") or "").strip()
        if not text:
            return None
        root = self._media_root()
        if root is None:
            log.debug("媒体根目录未知，跳过媒体发现")
            return None
        try:
            root_resolved = root.resolve()
        except (OSError, ValueError) as exc:
            log.debug("媒体根目录不可用：%s", exc)
            return None
        primary = self._checked_media_path(text, root_resolved, kind)
        if primary is None:
            return None
        candidate, size = primary
        name = str(raw.get("name") or "").strip() or candidate.name
        meta: dict[str, Any] = {
            "kind": kind,
            "path": str(candidate),
            "name": name,
            "size": int(size),
            "duration": _safe_int(raw.get("duration")),
        }
        fallbacks: list[str] = []
        for item in raw.get("fallbacks") or ():
            fallback = self._checked_media_path(str(item), root_resolved, kind)
            if fallback is not None and fallback[0] != candidate:
                fallbacks.append(str(fallback[0]))
        if fallbacks:
            meta["fallbacks"] = fallbacks
        return meta

    def _checked_media_path(
        self, text: str, root_resolved: Path, kind: str
    ) -> tuple[Path, int] | None:
        """校验单个媒体候选：必须在 root 内、存在、大小可接受。"""
        text = (text or "").strip()
        if not text:
            return None
        try:
            candidate = Path(text)
            if not candidate.is_absolute():
                candidate = root_resolved / candidate
            candidate = candidate.resolve()
            if not candidate.is_relative_to(root_resolved):
                log.debug("媒体路径越界，忽略：%s", candidate.name)
                return None
            if not candidate.is_file():
                log.debug("媒体文件缺失，保留占位：%s", candidate.name)
                return None
            size = candidate.stat().st_size
        except (OSError, ValueError) as exc:
            log.debug("媒体路径不可用：%s", exc)
            return None
        if size <= 0 or size > self._media_limit(kind):
            log.debug("媒体文件大小不可接受（%s）：%d 字节", candidate.name, size)
            return None
        return candidate, size

    def _media_limit(self, kind: str) -> int:
        """按媒体类型返回大小上限：视频独立，图片/语音沿用配置值。"""
        if kind == "video":
            return MAX_VIDEO_BYTES
        return self._media_max_bytes

    def _emoticon_media(self, body: str) -> dict[str, Any] | None:
        """从表情 XML 的 md5 找到 ``cache/<YYYY-MM>/Emoticon/...`` 缓存文件。

        只返回候选路径；文件存在性/大小/越界校验仍交给 :meth:`_media_meta`。
        """
        digest = _emoji_md5(body)
        if not digest:
            return None
        root = self._media_root()
        if root is None:
            return None
        path = _emoticon_file(root, digest)
        if path is None:
            log.debug("表情缓存文件缺失：%s", digest)
            return None
        return {"path": str(path), "name": path.name, "size": 0, "duration": 0}

    def _media_root(self) -> Path | None:
        if self._media_storage_root:
            return Path(self._media_storage_root)
        root = getattr(self._mirrors, "db_root", None)
        if root is None:
            return None
        try:
            # `db_root` 是 `db_storage`；4.x 的 attach 在账号根（它的父目录）下。
            return Path(root).parent
        except TypeError:
            return None


# ----------------------------------------------------------------- 工具函数
def message_table_name(username: str) -> str:
    """``Msg_`` + md5(username utf-8) 的 hexdigest。"""
    digest = hashlib.md5(username.encode("utf-8")).hexdigest()
    return f"Msg_{digest}"


def _is_chatroom(username: str) -> bool:
    return username.endswith(CHATROOM_SUFFIX)


def _display_name(remark: Any, nick_name: Any) -> str:
    """与微信 UI 一致：备注优先，否则昵称。"""
    return str(remark or "").strip() or str(nick_name or "").strip()


def _base_type(value: Any) -> int | None:
    try:
        return int(value) & _TYPE_MASK
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _strip_sender_prefix(text: str) -> tuple[str, str]:
    """返回 ``(前缀里的 wxid, 去掉前缀的正文)``；不匹配时前缀为空。"""
    match = _SENDER_PREFIX_RE.match(text or "")
    if match is None:
        return "", text or ""
    return match.group(1), text[match.end() :]


def _render_body(local_type: int, body: str) -> tuple[str, str, str, str]:
    """返回 ``(kind, text, reply_to, reply_author)``。"""
    if local_type == 1:
        return "text", body, "", ""
    if local_type == 47:
        # 表情仍按 sticker 归类，但正文尽量带上可读标识（名称或 md5）。
        return "sticker", _render_emoji(body), "", ""
    if local_type == 49:
        return _render_appmsg(body)
    kind, placeholder = _PLACEHOLDER_BY_TYPE[local_type]
    return kind, placeholder, "", ""


def _render_emoji(body: str) -> str:
    """从表情 XML 生成 ``[表情:<name>]`` / ``[表情:<md5 前 8>]``。

    节点形如 ``<emoji md5="..." desc="..." attachedtext="..." .../>``，实测
    desc/attachedtext 多为空，md5 才稳定可区分。解析失败或无 emoji 节点时退回
    纯占位；绝不抛异常。
    """
    placeholder = _PLACEHOLDER_BY_TYPE[47][1]
    root = _parse_xml(body)
    if root is None:
        return placeholder
    node = root.find(".//emoji")
    if node is None:
        return placeholder
    name = ""
    for attr in _EMOJI_NAME_ATTRS:
        name = str(node.get(attr) or "").strip()
        if name:
            break
    if name:
        # 去换行并截断，避免占位把单行正文撑破。
        name = re.sub(r"\s+", " ", name).strip()[:20]
        if name:
            return f"[表情:{name}]"
    md5 = str(node.get("md5") or "").strip()
    if md5:
        return f"[表情:{md5[:8]}]"
    return placeholder


def _render_appmsg(body: str) -> tuple[str, str, str, str]:
    title, appmsg_type, refer_author, refer_content = _appmsg_fields(body)
    if appmsg_type == str(_APPMSG_QUOTE):
        # 引用回复仍属于 appmsg；kind 用 quote 让下游能区分普通链接。
        return "quote", title, refer_content, refer_author
    if appmsg_type == str(_APPMSG_FORWARD):
        # 合并转发（聊天记录）没有可展开的子消息：只把标题标出来，避免模型
        # 把它当成普通链接。kind 保持 link，timeline/附件语义不变。
        return "link", f"[合并转发] {title}".strip(), "", ""
    finder = _finder_share(body)
    if finder is not None:
        # 视频号的 <title> 是「当前版本不支持展示该内容」这类模板文案，真正
        # 内容在 <finderFeed> 里；kind 仍按链接归类（无附件、无媒体下载）。
        return "link", finder, "", ""
    # 解析失败/没有 title 就是空文本：宁可这条气泡没有正文，也不伪造 "[链接]"。
    return "link", title, "", ""


def _finder_share(body: str) -> str | None:
    """把视频号分享渲染成一行可读文本；不是视频号分享返回 ``None``。

    真实结构（4.x 本地库实测）::

        <appmsg><type>51</type>
          <title>当前版本不支持展示该内容，请升级到最新版本。</title>
          <finderFeed>
            <nickname>…</nickname><desc>…#话题#…</desc>
            <mediaCount>1</mediaCount>
            <mediaList><media><mediaType>4</mediaType>
              <videoPlayDuration>12</videoPlayDuration></media></mediaList>
          </finderFeed>
        </appmsg>

    只做文本可读化：``<media><url>`` 是带 token 的临时直链、封面也只有 200px
    级，下载它们既不稳定也没有画质收益，因此视频本体/封面一律不取。
    """
    if "<finderFeed" not in body:
        return None
    root = _parse_xml(body)
    if root is None:
        nickname = _regex_text(_FINDER_NICK_RE, body)
        desc = _regex_text(_FINDER_DESC_RE, body)
        duration = _regex_int(_FINDER_DURATION_RE, body)
        media_count = _regex_int(_FINDER_MEDIA_COUNT_RE, body)
    else:
        nickname = _node_text(root, ".//finderFeed/nickname")
        desc = _node_text(root, ".//finderFeed/desc")
        duration = max(
            (
                _safe_int(node.text)
                for node in root.iterfind(
                    ".//finderFeed/mediaList/media/videoPlayDuration"
                )
            ),
            default=0,
        )
        media_count = _safe_int(_node_text(root, ".//finderFeed/mediaCount"))
    nickname = re.sub(r"\s+", " ", nickname).strip()
    desc = re.sub(r"\s+", " ", desc).strip()
    if not nickname and not desc:
        return None
    head = f"[视频号] {nickname}：{desc}" if nickname and desc else ""
    if not head:
        head = f"[视频号] {nickname or desc}"
    if duration > 0:
        return f"{head}（视频时长 {duration} 秒）"
    if media_count > 1:
        return f"{head}（共 {media_count} 张图）"
    return head


def _regex_int(pattern: re.Pattern[str], text: str) -> int:
    match = pattern.search(text or "")
    if match is None:
        return 0
    return _safe_int(match.group(1))


def _appmsg_fields(body: str) -> tuple[str, str, str, str]:
    """从 appmsg XML 取 ``(title, type, refer displayname, refer content)``。

    ElementTree 优先；微信 XML 常有未转义字符/未闭合标签，解析失败时回退正则；
    回退也拿不到就返回空串——绝不抛异常。
    """
    root = _parse_xml(body)
    if root is not None:
        return (
            _node_text(root, ".//appmsg/title"),
            _node_text(root, ".//appmsg/type"),
            _node_text(root, ".//refermsg/displayname"),
            _node_text(root, ".//refermsg/content"),
        )
    return (
        _regex_text(_TITLE_RE, body),
        _regex_text(_APPMSG_TYPE_RE, body),
        _regex_text(_REFER_AUTHOR_RE, body),
        _regex_text(_REFER_CONTENT_RE, body),
    )


def _parse_xml(text: str) -> ET.Element | None:
    if not text or "<" not in text:
        return None
    try:
        return ET.fromstring(text)
    except (ET.ParseError, ValueError) as exc:
        log.debug("微信 XML 解析失败，回退正则：%s", exc)
        return None


def _node_text(root: ET.Element, path: str) -> str:
    node = root.find(path)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _regex_text(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text or "")
    if match is None:
        return ""
    return html.unescape(match.group(1)).strip()


def _message_rel(value: Any) -> str | None:
    """把 ``paths()`` 的返回值规整成 ``message/message_N.db`` 相对路径。"""
    text = str(value).replace("\\", "/")
    index = text.find("message/")
    if index < 0:
        return None
    candidate = text[index:]
    return candidate if _MESSAGE_REL_RE.match(candidate) else None


def _shard_order(rel: str) -> tuple[int, str]:
    match = _MESSAGE_REL_RE.match(rel)
    return (int(match.group(1)) if match else 0, rel)


def _has_columns(con: sqlite3.Connection, table: str) -> bool:
    try:
        info = con.execute(f'PRAGMA table_info("{_quote_ident(table)}")').fetchall()
    except sqlite3.Error as exc:
        log.debug("读取表 %s 的 schema 失败：%s", table, exc)
        return False
    if not info:
        return False
    return _REQUIRED_COLUMNS.issubset({str(row[1]) for row in info})


def _table_columns(con: sqlite3.Connection) -> dict[str, set[str]]:
    """``表名 -> 列名集合``；读不到 schema 的表直接不出现在结果里。"""
    try:
        names = [
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        log.debug("枚举媒体库表失败：%s", exc)
        return {}
    out: dict[str, set[str]] = {}
    for name in names:
        try:
            info = con.execute(f'PRAGMA table_info("{_quote_ident(name)}")').fetchall()
        except sqlite3.Error:
            continue
        columns = {str(row[1]) for row in info if len(row) > 1}
        if columns:
            out[name] = columns
    return out


def _media_tables(
    columns_by_table: dict[str, set[str]],
) -> list[tuple[str, dict[str, str]]]:
    """按已知优先级挑出「有 id 列 + 有路径列」的表，列名运行时适配。"""

    def rank(name: str) -> tuple[int, str]:
        try:
            return (_MEDIA_TABLE_PRIORITY.index(name), name)
        except ValueError:
            return (len(_MEDIA_TABLE_PRIORITY), name)

    chosen: list[tuple[str, dict[str, str]]] = []
    for name in sorted(columns_by_table, key=rank):
        columns = columns_by_table[name]
        id_column = _first_present(columns, _MEDIA_ID_COLUMNS)
        path_column = _first_present(columns, _MEDIA_PATH_COLUMNS)
        if id_column is None or path_column is None:
            continue
        layout = {"id": id_column, "path": path_column}
        for key, candidates in (
            ("name", _MEDIA_NAME_COLUMNS),
            ("size", _MEDIA_SIZE_COLUMNS),
            ("duration", _MEDIA_DURATION_COLUMNS),
        ):
            match = _first_present(columns, candidates)
            if match is not None:
                layout[key] = match
        chosen.append((name, layout))
    return chosen


def _first_present(columns: set[str], candidates: Sequence[str]) -> str | None:
    lowered = {column.lower(): column for column in columns}
    for candidate in candidates:
        found = lowered.get(candidate.lower())
        if found is not None:
            return found
    return None


def _table_named(columns_by_table: dict[str, set[str]], name: str) -> str | None:
    """按大小写不敏感的表名精确匹配；找不到返回 None。"""
    wanted = name.lower()
    for table in columns_by_table:
        if table.lower() == wanted:
            return table
    return None


def _blob_md5(value: Any) -> str:
    """从 ``packed_info`` protobuf blob 中取第一个 32 位 hex md5（小写）。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    elif isinstance(value, str):
        data = value.encode("utf-8", "ignore")
    else:
        return ""
    match = _MD5_IN_BLOB_RE.search(data)
    return match.group(0).decode("ascii").lower() if match is not None else ""


def _emoji_md5(body: str) -> str:
    """从表情 XML 取 ``<emoji md5="...">``；拿不到 32 位 hex 返回空串。"""
    root = _parse_xml(body)
    if root is not None:
        node = root.find(".//emoji")
        if node is not None:
            value = str(node.get("md5") or "").strip().lower()
            if _HEX32_RE.fullmatch(value):
                return value
    match = _EMOJI_MD5_RE.search(body or "")
    return match.group(1).lower() if match is not None else ""


def _emoticon_file(root: Path, digest: str) -> Path | None:
    """在 ``<root>/cache/<YYYY-MM>/Emoticon/<md5[:2]>/<md5>`` 下找表情缓存。"""
    if not _HEX32_RE.fullmatch(digest):
        return None
    try:
        matches = sorted((root / "cache").glob(f"*/Emoticon/{digest[:2]}/{digest}"))
    except OSError as exc:
        log.debug("枚举表情缓存目录失败：%s", exc)
        return None
    for match in matches:
        try:
            if match.is_file():
                return match
        except OSError as exc:
            log.debug("检查表情缓存文件失败：%s", exc)
    return None


def _attach_files(base: Path, digest: str) -> list[Path]:
    """按变体优先级返回**全部**存在的候选：原图 → ``_h`` → ``_t``。

    每个变体只取排序后的第一个文件（同一 md5 理论上只有一个）。返回列表而
    不是单个路径：原图可能是 ``wxgf``，解不开时要由下游按顺序退回。
    """
    found: list[Path] = []
    for suffix in _VARIANT_ORDER:
        try:
            matches = sorted(base.glob(f"*/Img/{digest}{suffix}.dat"))
        except OSError as exc:
            log.debug("枚举 attach 目录失败：%s", exc)
            continue
        for match in matches:
            try:
                if match.is_file():
                    found.append(match)
                    break
            except OSError as exc:
                log.debug("检查 attach 文件失败：%s", exc)
    return found


def _attach_file(base: Path, digest: str) -> Path | None:
    """首选变体路径（原图优先）；只需要单路径的调用方使用。"""
    files = _attach_files(base, digest)
    return files[0] if files else None


def _video_file(root: Path, username: str, digest: str) -> Path | None:
    """按 4.x 布局找视频原文件；找不到返回 ``None``。

    候选顺序（前一组命中就不再往后找）：

    1. ``msg/attach/<md5(当前会话)>/<YYYY-MM>/{Video,video}/<md5>*``；
    2. ``msg/attach/<任意会话 md5>/<YYYY-MM>/{Video,video}/<md5>*``（会话目录
       对不上时的兜底；同组内按路径排序保证确定性）；
    3. ``msg/{video,Video}/**/<md5>*``（旧版本把视频单独放在 ``msg/video``）。

    同组内多个匹配按「非 ``.dat`` 优先 → 常见视频后缀优先 → 更大的文件优先」
    排序：``.dat`` 还要解密，能让 ffmpeg 直接吃的原文件更值得先试。
    """
    if not _HEX32_RE.fullmatch(digest):
        return None

    bases: list[Path] = []
    attach = root / "msg" / "attach"
    if username:
        bases.append(attach / hashlib.md5(username.encode("utf-8")).hexdigest())
    try:
        bases.extend(
            sorted(
                path
                for path in attach.glob("*")
                if path.is_dir() and path not in bases
            )
        )
    except OSError as exc:
        log.debug("枚举视频 attach 目录失败：%s", exc)

    for base in bases:
        for folder in _VIDEO_DIR_NAMES:
            for pattern in (f"*/{folder}/{digest}*", f"{folder}/{digest}*"):
                path = _pick_video(_glob_files(base, pattern))
                if path is not None:
                    return path

    for folder in _VIDEO_DIR_NAMES:
        path = _pick_video(_glob_files(root / "msg" / folder, f"**/{digest}*"))
        if path is not None:
            return path
    return None


def _glob_files(directory: Path, pattern: str) -> list[Path]:
    try:
        return list(directory.glob(pattern))
    except OSError as exc:
        log.debug("枚举视频目录失败（%s）：%s", pattern, exc)
        return []


def _pick_video(candidates: Sequence[Path]) -> Path | None:
    """从候选里挑一个真实存在且最适合交给 ffmpeg 的文件。"""
    files: list[Path] = []
    for path in candidates:
        try:
            if path.is_file():
                files.append(path)
        except OSError as exc:
            log.debug("检查视频文件失败：%s", exc)
    if not files:
        return None
    return min(files, key=_video_preference)


def _video_preference(path: Path) -> tuple[int, int, int, str]:
    """排序键：非 ``.dat`` > 常见视频后缀 > 更大文件 > 路径稳定。"""
    suffix = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return (
        1 if suffix == ".dat" else 0,
        0 if suffix in _VIDEO_LIKE_SUFFIXES else 1,
        -size,
        path.as_posix(),
    )


def _name2id(con: sqlite3.Connection) -> dict[int, str]:
    """``Name2Id`` 的 rowid -> username；表缺失时返回空映射。"""
    try:
        rows = con.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error as exc:
        log.debug("读取 Name2Id 失败：%s", exc)
        return {}
    out: dict[int, str] = {}
    for rowid, user_name in rows:
        name = str(user_name or "")
        if name:
            out[_safe_int(rowid)] = name
    return out


def _quote_ident(name: str) -> str:
    return name.replace('"', '""')


def _connect(path: Path) -> sqlite3.Connection:
    """只读 URI 打开；路径里的中文/空格/``#``/``?`` 都要正确转义。"""
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)
