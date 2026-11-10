from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection
from http import HTTPStatus
from itertools import count
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from dafeiyu.faces import face_name
from dafeiyu.gateway.base import GatewayError
from dafeiyu.models import (
    QQ_PREFIX,
    Attachment,
    ChatType,
    Message,
    NoticeEvent,
    RecallEvent,
    Segment,
)

logger = logging.getLogger(__name__)

_PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "poke": "[戳一戳]",
    "shake": "[窗口抖动]",
    "anonymous": "[匿名消息]",
    "keyboard": "[卡片]",
    "template": "[卡片]",
    "qqmusic": "[音乐]",
    "node": "[合并转发]",
}

_RPS_NAMES = {1: "石头", 2: "剪刀", 3: "布"}

_CARD_FALLBACK = "[小程序卡片]"
_XML_FALLBACK = "[卡片]"
_FORWARD_FALLBACK = "[合并转发]"
_FORWARD_MAX_DEPTH = 2
_FORWARD_MAX_NODES = 10
_FORWARD_NODE_CLIP = 60
# 一条事件最多主动拉取 1 个合并转发，避免放大网络请求。
_FORWARD_FETCH_LIMIT = 1
_ATTACHMENT_NAME_CLIP = 60


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _qq_id(value: Any) -> str:
    """OneBot 的裸数字 id -> 带通道前缀的会话 id；解析失败返回空串。"""
    number = _as_int(value)
    return f"{QQ_PREFIX}{number}" if number else ""


def _bare_id(chat_id: str) -> int:
    """send_*_msg 的 params 要原始数字：去掉通道前缀再转 int。"""
    text = str(chat_id)
    text = text.removeprefix(QQ_PREFIX)
    try:
        return int(text)
    except (TypeError, ValueError) as exc:
        raise GatewayError(f"非法的 QQ 会话 id：{chat_id!r}") from exc


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _render_json_card(data: dict[str, Any]) -> str:
    """json 段（小程序/分享卡）：浅层两轮查找可读标题，失败回退占位。"""
    try:
        raw = data.get("data")
        if isinstance(raw, str):
            parsed = json.loads(raw)
        elif isinstance(raw, dict):
            parsed = raw
        else:
            return _CARD_FALLBACK
        if not isinstance(parsed, dict):
            return _CARD_FALLBACK
        title = _find_card_title(parsed, depth=2)
        if not title:
            return _CARD_FALLBACK
        return f"[卡片] {_clip(title, 80)}"
    # 畸形卡片段渲染降级为占位符，绝不让解析异常冒泡。
    except Exception:  # noqa: BLE001
        return _CARD_FALLBACK


def _find_card_title(node: Any, depth: int) -> str | None:
    """在 JSON 里限深查找 title/desc 文本（meta.newsApp.title 之类）。"""
    if depth < 0 or not isinstance(node, dict):
        return None
    for key in ("title", "desc"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in node.values():
        found = _find_card_title(value, depth - 1)
        if found:
            return found
    return None


def _render_xml_card(data: dict[str, Any]) -> str:
    """xml 段：用 QQ 客户端生成的 summary，缺失回退占位。"""
    try:
        summary = str(data.get("summary", "")).strip()
        if not summary:
            return _XML_FALLBACK
        return f"{_XML_FALLBACK} {_clip(summary, 80)}"
    # 畸形 XML 段渲染降级为占位符，绝不让解析异常冒泡。
    except Exception:  # noqa: BLE001
        return _XML_FALLBACK


def _clip_first(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return _clip(text, limit) if text else ""


def _render_face(data: dict[str, Any]) -> str:
    """face 段：表内 id 渲染名称，表外数字保留 id；缺失或非数字回退占位。"""
    face_id = str(data.get("id") or "").strip()
    if not face_id.isdigit():
        logger.info("QQ 表情段缺少数字 id：%r", data)
        return _PLACEHOLDERS["face"]
    name = face_name(int(face_id))
    if name:
        return f"[表情:{name}]"
    logger.info("QQ 表情表未收录：id=%s data=%r", face_id, data)
    return f"[表情:{face_id}]"


def _render_mface(data: dict[str, Any]) -> str:
    """mface 段：用商店表情的 summary 区分；去换行并截断，空则回退占位。"""
    summary = " ".join(str(data.get("summary") or "").split())
    summary = _clip(summary, 20)
    if summary:
        return f"[表情:{summary}]"
    logger.info("QQ 商店表情无 summary：%r", data)
    return _PLACEHOLDERS["mface"]


def _render_at(data: dict[str, Any], bot_qq: int) -> str:
    """@ 段渲染成文字：@你 / @全体成员 / @群友。

    OneBot 的 at 段只带 QQ 号、不带昵称，所以非 bot 的 @ 不把裸号塞进
    模型上下文，只保留「这句话在点名某人」的结构。
    """
    target = str(data.get("qq") or "").strip()
    if target == "all":
        return "@全体成员 "
    if bot_qq and target == str(bot_qq):
        return "@你 "
    return "@群友 "


def _render_rich_segment(seg_type: str, data: dict[str, Any]) -> str:
    """分享/位置/名片/音乐/骰子/猜拳/markdown 这类消息的可读摘要。"""
    if seg_type == "share":
        title = _clip_first(data.get("title"), 60) or _clip_first(
            data.get("url"), 60
        )
        return f"[分享] {title}".rstrip()
    if seg_type == "location":
        title = _clip_first(data.get("title") or data.get("content"), 60)
        return f"[位置] {title}".rstrip()
    if seg_type == "contact":
        name = _clip_first(data.get("name") or data.get("nickname"), 40)
        return f"[名片] {name}".rstrip()
    if seg_type == "music":
        title = _clip_first(data.get("title"), 60)
        return f"[音乐] {title}".rstrip()
    if seg_type == "dice":
        return f"[骰子:{data.get('result', '?')}]"
    if seg_type == "rps":
        result = data.get("result")
        try:
            name = _RPS_NAMES.get(int(result), str(result))
        except (TypeError, ValueError):
            name = str(result or "?")
        return f"[猜拳:{name}]"
    if seg_type == "markdown":
        body = _clip_first(data.get("content") or data.get("text"), 200)
        return f"[Markdown] {body}".rstrip()
    return ""


def _pending_forward_ids(event: dict[str, Any]) -> list[str]:
    """找出需要主动拉取的合并转发段 id（只有 id、没有内联节点）。

    一条消息里最多取 1 个，避免一次事件触发过多网络请求；已带非空
    ``content`` 的段（能直接渲染）与其它段类型一律忽略。
    """
    raw_message = event.get("message")
    if not isinstance(raw_message, list):
        return []
    ids: list[str] = []
    for item in raw_message:
        if not isinstance(item, dict) or item.get("type") != "forward":
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        content = data.get("content")
        if isinstance(content, list) and content:
            continue
        forward_id = data.get("id")
        if not isinstance(forward_id, str) or not forward_id.strip():
            continue
        forward_id = forward_id.strip()
        if forward_id in ids:
            continue
        ids.append(forward_id)
        if len(ids) >= _FORWARD_FETCH_LIMIT:
            break
    return ids


def _render_forward(
    data: dict[str, Any], depth: int, bot_qq: int = 0
) -> str:
    """合并转发：内联节点递归渲染成「昵称: 内容」，超限回退占位。"""
    if depth >= _FORWARD_MAX_DEPTH:
        return _FORWARD_FALLBACK
    try:
        content = data.get("content")
        if not isinstance(content, list) or not content:
            return _FORWARD_FALLBACK
        lines: list[str] = []
        for node in content[:_FORWARD_MAX_NODES]:
            if not isinstance(node, dict):
                continue
            sender = node.get("sender")
            name = ""
            if isinstance(sender, dict):
                name = str(sender.get("nickname") or sender.get("user_id") or "").strip()
            name = name or "群友"
            message = node.get("message", "")
            if isinstance(message, str):
                body = message.strip()
            elif isinstance(message, list):
                body, _segs, _rid, _rqq, _atts = _render_segments(
                    message, depth + 1, bot_qq=bot_qq
                )
            else:
                body = ""
            lines.append(f"{name}: {_clip(body, _FORWARD_NODE_CLIP)}")
        if not lines:
            return _FORWARD_FALLBACK
        return "[聊天记录]\n" + "\n".join(lines)
    # 转发记录渲染降级为占位符，绝不让解析异常冒泡。
    except Exception:  # noqa: BLE001
        return _FORWARD_FALLBACK


def _pick_media_source(
    data: dict[str, Any], keys: tuple[str, ...] = ("url", "file", "base64")
) -> str:
    """按给定优先级挑第一个非空字符串来源；没有可用来源返回空串。

    与 ``images._source`` 同思路，但不 import images（避免网关 ↔ 物化的循环依赖）。
    """
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _attachment_from_segment(seg_type: str, data: dict[str, Any]) -> Attachment | None:
    """单个段 → 附件；无可记录信息时返回 None。"""
    if seg_type == "image":
        source = _pick_media_source(data)
        return Attachment(kind="image", source=source) if source else None
    if seg_type in ("record", "video"):
        source = _pick_media_source(data, ("url", "file"))
        duration = _as_int(data.get("duration"))
        if not source and not duration:
            return None
        return Attachment(kind=seg_type, source=source, duration=duration)
    if seg_type == "file":
        name = _clip_first(
            data.get("name") or data.get("file"), _ATTACHMENT_NAME_CLIP
        )
        if not name:
            return None
        return Attachment(
            kind="file", name=name, size=_as_int(data.get("size"))
        )
    if seg_type == "face":
        name = _clip_first(data.get("id"), _ATTACHMENT_NAME_CLIP)
        return Attachment(kind="sticker", name=name) if name else None
    if seg_type == "mface":
        name = _clip_first(data.get("summary"), _ATTACHMENT_NAME_CLIP)
        return Attachment(kind="sticker", name=name) if name else None
    if seg_type == "node":
        # 摘要已经由 content 里的 [合并转发] 占位承载，这里只留类型标记。
        return Attachment(kind="forward")
    return None


def _attachments_from_segments(raw_segments: list[Any]) -> list[Attachment]:
    """把可识别的媒体段归一化成附件；未知/畸形段静默跳过，永不抛异常。"""
    attachments: list[Attachment] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        seg_type = str(item.get("type", ""))
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        try:
            attachment = _attachment_from_segment(seg_type, data)
        # 单个畸形段不得影响整条消息的解析。
        except Exception:  # noqa: BLE001
            attachment = None
        if attachment is not None:
            attachments.append(attachment)
    return attachments


def _render_segments(
    raw_segments: list[Any],
    depth: int = 0,
    *,
    bot_qq: int = 0,
) -> tuple[str, list[Segment], str | None, int | None, list[Attachment]]:
    parts: list[str] = []
    segments: list[Segment] = []
    reply_id: str | None = None
    reply_qq: int | None = None

    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        seg_type = str(item.get("type", ""))
        data = item.get("data")
        if not isinstance(data, dict):
            data = {}
        segments.append(Segment(type=seg_type, data=data))

        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            parts.append(_render_at(data, bot_qq))
        elif seg_type == "file":
            name = _clip_first(data.get("name") or data.get("file"), 60)
            parts.append(f"[文件] {name}".rstrip())
        elif seg_type == "face":
            parts.append(_render_face(data))
        elif seg_type == "mface":
            parts.append(_render_mface(data))
        elif seg_type in _PLACEHOLDERS:
            parts.append(_PLACEHOLDERS[seg_type])
        elif seg_type == "reply":
            reply_id = str(data.get("id", ""))
            reply_qq = _as_int(data.get("qq")) or None
        elif seg_type == "json":
            parts.append(_render_json_card(data))
        elif seg_type == "xml":
            parts.append(_render_xml_card(data))
        elif seg_type == "forward":
            parts.append(_render_forward(data, depth, bot_qq))
        elif seg_type in (
            "share",
            "location",
            "contact",
            "music",
            "dice",
            "rps",
            "markdown",
        ):
            parts.append(_render_rich_segment(seg_type, data))
        # 其余未知段类型一律忽略，不抛异常

    return (
        "".join(parts).strip(),
        segments,
        reply_id,
        reply_qq,
        _attachments_from_segments(raw_segments),
    )


def _parse_recall(event: dict[str, Any]) -> RecallEvent | None:
    """notice 里的撤回事件；其他 notice 一律忽略。"""
    notice_type = event.get("notice_type")
    if notice_type == "group_recall":
        chat_type = ChatType.GROUP
        chat_id = _qq_id(event.get("group_id"))
    elif notice_type == "friend_recall":
        chat_type = ChatType.PRIVATE
        chat_id = _qq_id(event.get("user_id"))
    else:
        return None
    message_id = str(event.get("message_id") or "")
    if not chat_id or not message_id:
        return None
    return RecallEvent(
        chat_type=chat_type,
        chat_id=chat_id,
        message_id=message_id,
        operator_id=_qq_id(event.get("operator_id")),
    )


def _parse_notice(event: dict[str, Any]) -> NoticeEvent | None:
    """解析非撤回 notice（戳一戳/进退群/禁言）；未知类型返回 None。

    只产出结构化事件供上层记日志，不触发模型回复。``poke`` 在 OneBot 里
    有两种写法（``notice_type=notify`` + ``sub_type=poke``，或直接
    ``group_poke``/``friend_poke``），两种都收。
    """
    notice_type = str(event.get("notice_type") or "")
    sub_type = str(event.get("sub_type") or "")
    if notice_type == "notify" and sub_type == "poke":
        kind = "group_poke" if event.get("group_id") else "friend_poke"
    elif notice_type in ("group_poke", "friend_poke"):
        kind = notice_type
    elif notice_type in ("group_increase", "group_decrease", "group_ban"):
        kind = notice_type
    else:
        return None

    group_id = _qq_id(event.get("group_id"))
    user_id = _qq_id(event.get("user_id"))
    # poke 事件里 user_id 是发起者、target_id 是被戳者；其余事件 user_id 是主体。
    if kind.endswith("_poke"):
        subject = _qq_id(event.get("target_id") or event.get("user_id"))
        operator = user_id
    else:
        subject = user_id
        operator = _qq_id(event.get("operator_id"))

    if kind == "group_poke":
        chat_type, chat_id = ChatType.GROUP, group_id
    elif kind == "friend_poke":
        chat_type, chat_id = ChatType.PRIVATE, operator or subject
    else:
        chat_type, chat_id = ChatType.GROUP, group_id
    if not chat_id:
        return None

    try:
        duration = int(event.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return NoticeEvent(
        kind=kind,
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=subject,
        operator_id=operator,
        sub_type=sub_type,
        duration=max(0, duration),
    )


def parse_event(
    event: dict[str, Any],
    bot_qq: int,
    bot_message_ids: Collection[str] = (),
) -> Message | RecallEvent | NoticeEvent | None:
    if not isinstance(event, dict):
        return None
    if event.get("post_type") == "notice":
        return _parse_recall(event) or _parse_notice(event)
    if event.get("post_type") != "message":
        return None

    message_type = event.get("message_type")
    if message_type == "group":
        chat_type = ChatType.GROUP
        chat_id = _qq_id(event.get("group_id"))
    elif message_type == "private":
        chat_type = ChatType.PRIVATE
        chat_id = _qq_id(event.get("user_id"))
    else:
        return None

    # 昵称兜底仍用裸 QQ 号：带前缀的 id 写进昵称会污染提示词。
    raw_user_id = _as_int(event.get("user_id"))
    user_id = _qq_id(event.get("user_id"))
    sender = event.get("sender")
    if not isinstance(sender, dict):
        sender = {}
    nickname = str(sender.get("card") or sender.get("nickname") or raw_user_id)

    raw_message = event.get("message", "")
    reply_id: str | None = None
    reply_qq: int | None = None
    segments: list[Segment] = []
    attachments: list[Attachment] = []

    if isinstance(raw_message, str):
        content = raw_message.strip()
        at_bot = False
        at_all = False
        at_others = False
    elif isinstance(raw_message, list):
        content, segments, reply_id, reply_qq, attachments = _render_segments(
            raw_message, bot_qq=bot_qq
        )
        at_bot = any(
            seg.type == "at" and str(seg.data.get("qq", "")) == str(bot_qq) for seg in segments
        )
        at_all = any(
            seg.type == "at" and str(seg.data.get("qq", "")).strip() == "all"
            for seg in segments
        )
        # @ 了别的群友不是点名 bot：时间线要标出来，避免抢答。
        # @全体成员 归 at_all，也不算「@了别的群友」。
        at_others = any(
            seg.type == "at"
            and str(seg.data.get("qq", "")).strip() not in ("", "all", str(bot_qq))
            for seg in segments
        )
    else:
        content = ""
        at_bot = False
        at_all = False
        at_others = False

    reply_to_bot = (
        reply_id is not None and reply_id in {str(i) for i in bot_message_ids}
    ) or (reply_qq is not None and reply_qq == bot_qq)

    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=str(event.get("message_id", "")),
        segments=segments,
        # 两边都是带前缀的字符串；留成 int == str 会恒为 False（回声过滤失效）。
        is_bot=user_id == _qq_id(bot_qq),
        at_bot=at_bot,
        at_all=at_all,
        at_others=at_others,
        reply_to_bot=reply_to_bot,
        reply_to_message_id=reply_id or None,
        reply_to_user_id=_qq_id(reply_qq) if reply_qq else None,
        timestamp=_as_int(event.get("time")),
        attachments=attachments,
    )


class QQAdapter:
    """OneBot v11 反向 WebSocket 服务端：NapCat 主动连入。"""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        bot_qq: int,
        send_timeout: float = 10.0,
        max_bot_message_ids: int = 500,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._bot_qq = bot_qq
        self._send_timeout = send_timeout
        self.bot_message_ids: deque[str] = deque(maxlen=max_bot_message_ids)
        self._handler: Callable[[Message], Awaitable[None]] | None = None
        self._recall_handler: Callable[[RecallEvent], Awaitable[None]] | None = None
        self._notice_handler: Callable[[NoticeEvent], Awaitable[None]] | None = None
        self._server: Server | None = None
        self._ws: ServerConnection | None = None
        self._pending: dict[str, tuple[asyncio.Future[dict[str, Any]], str]] = {}
        self._echo = count(1)
        self._tasks: set[asyncio.Task[None]] = set()
        self._bound_port = port
        # 只记录时间戳，供健康快照使用；不保留消息内容。
        self.last_event_at: float | None = None
        self.last_disconnect_at: float | None = None
        self.last_send_at: float | None = None

    @property
    def port(self) -> int:
        return self._bound_port

    @property
    def bot_qq(self) -> str:
        """自己的 QQ 号（带通道前缀）：与 Message.user_id 保持同一形态。"""
        return self.bot_id

    @property
    def bot_id(self) -> str:
        return f"{QQ_PREFIX}{self._bot_qq}"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def on_message(self, handler: Callable[[Message], Awaitable[None]]) -> None:
        self._handler = handler

    def on_recall(self, handler: Callable[[RecallEvent], Awaitable[None]]) -> None:
        self._recall_handler = handler

    def on_notice(self, handler: Callable[[NoticeEvent], Awaitable[None]]) -> None:
        self._notice_handler = handler

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await serve(
            self._handle_connection,
            self._host,
            self._port,
            process_request=self._process_request,
        )
        sockets = self._server.sockets
        if sockets:
            self._bound_port = sockets[0].getsockname()[1]
        logger.info("QQ 网关已监听 %s:%s", self._host, self._bound_port)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        self._fail_pending(GatewayError("QQ 网关已关闭"))

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str | None:
        return await self._send_message(chat_type, chat_id, text, chars=len(text))

    async def send_segments(
        self, chat_type: ChatType, chat_id: str, segments: list[dict[str, Any]]
    ) -> str | None:
        """按 OneBot 消息段数组发送（引用回复、表情等）。

        `send` 的纯文本参数结构不变；这里只把 `message` 换成段数组，走同一条
        请求/回执通路，失败语义也一样（抛 `GatewayError`，消息未送达）。
        """
        chars = sum(
            len(str(segment.get("data", {}).get("text", "")))
            for segment in segments
            if isinstance(segment, dict)
        )
        return await self._send_message(chat_type, chat_id, segments, chars=chars)

    async def upload_file(
        self, chat_type: ChatType, chat_id: str, path: str, name: str = ""
    ) -> None:
        """把本地文件上传到群/私聊；失败抛 `GatewayError`，绝不吞掉。

        `name` 是对方看到的文件名，空则取路径 basename。日志只记文件名与
        会话，不把完整本地路径写到 INFO 以上。
        """
        bare_id = _bare_id(chat_id)
        filename = name or Path(path).name
        if chat_type is ChatType.GROUP:
            action = "upload_group_file"
            params: dict[str, Any] = {
                "group_id": bare_id,
                "file": path,
                "name": filename,
            }
        else:
            action = "upload_private_file"
            params = {"user_id": bare_id, "file": path, "name": filename}

        target = f"{chat_type.value}/{bare_id}"
        try:
            await self._request(action, params)
        except GatewayError as exc:
            logger.warning("文件上传失败 %s %s name=%s：%s", action, target, filename, exc)
            raise
        logger.info("文件已上传 %s %s name=%s", action, target, filename)

    async def send_forward(
        self, chat_type: ChatType, chat_id: str, nodes: list[dict[str, Any]]
    ) -> str | None:
        """发合并转发；节点由调用方构造，这里只负责协议动作与 id 记账。"""
        bare_id = _bare_id(chat_id)
        if chat_type is ChatType.GROUP:
            action = "send_group_forward_msg"
            params: dict[str, Any] = {"group_id": bare_id, "messages": nodes}
        else:
            action = "send_private_forward_msg"
            params = {"user_id": bare_id, "messages": nodes}

        target = f"{chat_type.value}/{bare_id}"
        try:
            data = await self._request(action, params)
        except GatewayError as exc:
            logger.warning("合并转发失败 %s %s：%s", action, target, exc)
            raise

        self.last_send_at = time.time()
        message_id = data.get("message_id")
        if message_id is None:
            return None
        value = str(message_id)
        self.bot_message_ids.append(value)
        logger.info("合并转发已发送 %s %s msg_id=%s", action, target, value)
        return value

    async def set_msg_emoji_like(
        self, chat_type: ChatType, chat_id: str, message_id: str, emoji_id: str
    ) -> bool:
        """给已发出的消息贴表情回应；任何失败都返回 False，不抛给调用方。

        ``chat_type``/``chat_id`` 只用于日志定位：OneBot 的 ``set_msg_emoji_like``
        按 ``message_id`` 找消息。真实 NapCat 的 emoji id 取值需上线前验证。
        """
        if not message_id or not emoji_id:
            return False
        try:
            await self._request(
                "set_msg_emoji_like",
                {"message_id": _bare_id(message_id), "emoji_id": str(emoji_id)},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 表情回应是加法项
            logger.warning(
                "表情回应失败 %s %s msg_id=%s emoji=%s：%s",
                chat_type.value,
                chat_id,
                message_id,
                emoji_id,
                exc,
            )
            return False
        return True

    async def call_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """通用 OneBot 动作调用；只读查询（如 `get_group_info`）也走这里。"""
        return await self._request(action, params)

    async def _send_message(
        self, chat_type: ChatType, chat_id: str, message: Any, *, chars: int
    ) -> str | None:
        # 内部一律用带前缀的字符串 id；OneBot 协议参数仍是裸数字。
        bare_id = _bare_id(chat_id)
        if chat_type is ChatType.GROUP:
            action, params = "send_group_msg", {"group_id": bare_id, "message": message}
        else:
            action, params = "send_private_msg", {"user_id": bare_id, "message": message}

        target = f"{chat_type.value}/{bare_id}"
        try:
            data = await self._request(action, params)
        except GatewayError as exc:
            # 留痕：未连接/超时/retcode 都在这里落一条，调用方只知道"失败"。
            logger.warning("消息发送失败 %s %s chars=%d：%s", action, target, chars, exc)
            raise

        self.last_send_at = time.time()
        message_id = data.get("message_id")
        # 送达留痕：真实 message_id 是唯一能和 QQ 服务端对账的凭据，必须落日志。
        logger.info(
            "消息已发送 %s %s msg_id=%s chars=%d", action, target, message_id, chars
        )
        if message_id is None:
            return None
        # 对账凭据：真实 id 返回给上层入库，同时记入回声过滤表。
        value = str(message_id)
        self.bot_message_ids.append(value)
        return value

    async def _request(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """发一个 OneBot 动作并等回执；失败统一抛 `GatewayError`。"""
        ws = self._ws
        if ws is None:
            raise GatewayError("QQ 网关未连接")

        echo = str(next(self._echo))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = (future, action)
        try:
            await ws.send(
                json.dumps(
                    {"action": action, "params": params, "echo": echo}, ensure_ascii=False
                )
            )
            return await asyncio.wait_for(future, self._send_timeout)
        except TimeoutError as exc:
            raise GatewayError(f"{action} 等待响应超时") from exc
        finally:
            self._pending.pop(echo, None)

    def _authorized(self, request: Any) -> bool:
        if not self._token:
            return True
        return request.headers.get("Authorization") == f"Bearer {self._token}"

    def _process_request(self, connection: ServerConnection, request: Any) -> Any:
        if self._authorized(request):
            return None
        return connection.respond(HTTPStatus.FORBIDDEN, "unauthorized\n")

    async def _handle_connection(self, ws: ServerConnection) -> None:
        self._ws = ws
        logger.info("NapCat 已连接")
        try:
            async for raw in ws:
                await self._handle_frame(raw)
        except ConnectionClosed:
            pass
        finally:
            self.last_disconnect_at = time.time()
            if self._ws is ws:
                self._ws = None
            self._fail_pending(GatewayError("QQ 网关连接已断开"))
            logger.info("NapCat 连接已断开")

    async def _handle_frame(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("收到无法解析的 OneBot 帧")
            return
        if not isinstance(data, dict):
            return

        if "post_type" in data:
            # 只有 id 的合并转发需要回包拉取节点；接收循环不能在这里 await
            # 网络请求（会等不到 echo 而卡死），必须另起 task。
            pending_forward_ids = _pending_forward_ids(data)
            if pending_forward_ids:
                task = asyncio.create_task(
                    self._resolve_forwards_and_dispatch(data, pending_forward_ids)
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return
            self._dispatch_event(data)
            return

        echo = data.get("echo")
        if echo is None:
            return
        entry = self._pending.pop(str(echo), None)
        if entry is None:
            return
        future, action = entry
        if future.done():
            return
        if data.get("status") != "ok" or data.get("retcode") != 0:
            future.set_exception(
                GatewayError(f"{action} 失败：retcode={data.get('retcode')} status={data.get('status')}")
            )
            return
        result = data.get("data")
        future.set_result(result if isinstance(result, dict) else {})

    def _dispatch_event(self, data: dict[str, Any]) -> None:
        """解析并派发一条事件；普通路径与转发补拉路径共用。"""
        event = parse_event(data, self._bot_qq, self.bot_message_ids)
        if event is None:
            return
        if isinstance(event, NoticeEvent):
            logger.info(
                "QQ notice %s %s/%s user=%s operator=%s sub=%s duration=%s",
                event.kind,
                event.chat_type.value,
                event.chat_id,
                event.user_id,
                event.operator_id,
                event.sub_type,
                event.duration,
            )
            # 未注册处理器时只记日志（安全默认）；注册后交给 Service 分发。
            if self._notice_handler is None:
                return
            task = asyncio.create_task(self._run_notice(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        self.last_event_at = time.time()
        if isinstance(event, RecallEvent):
            if self._recall_handler is None:
                return
            task = asyncio.create_task(self._run_recall(event))
        else:
            if self._handler is None:
                return
            task = asyncio.create_task(self._run_handler(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve_forwards_and_dispatch(
        self, data: dict[str, Any], forward_ids: list[str]
    ) -> None:
        """为只带 id 的合并转发补拉节点，然后按普通事件派发。

        任何失败都只降级成原有的 ``[合并转发]`` 占位，不影响消息进入模型
        链路；只展开收到的那一层，不递归拉取嵌套转发。
        """
        for forward_id in forward_ids:
            try:
                result = await self.call_action("get_forward_msg", {"id": forward_id})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 拉取失败必须保持占位
                logger.warning("合并转发拉取失败 id=%s：%s", forward_id, exc)
                continue
            messages = result.get("messages")
            if not isinstance(messages, list):
                logger.warning("合并转发拉取结果缺少 messages id=%s", forward_id)
                continue
            self._apply_forward_content(data, forward_id, messages)
        self._dispatch_event(data)

    @staticmethod
    def _apply_forward_content(
        data: dict[str, Any], forward_id: str, messages: list[Any]
    ) -> None:
        """把拉回来的节点写回原始段；只匹配同一层的顶层段。"""
        raw_message = data.get("message")
        if not isinstance(raw_message, list):
            return
        for item in raw_message:
            if not isinstance(item, dict) or item.get("type") != "forward":
                continue
            segment_data = item.get("data")
            if not isinstance(segment_data, dict):
                continue
            if str(segment_data.get("id") or "").strip() != forward_id:
                continue
            existing = segment_data.get("content")
            if isinstance(existing, list) and existing:
                continue
            segment_data["content"] = messages

    async def _run_handler(self, event: Message) -> None:
        assert self._handler is not None
        try:
            await self._handler(event)
        except Exception:
            logger.exception("消息处理器异常")

    async def _run_recall(self, event: RecallEvent) -> None:
        assert self._recall_handler is not None
        try:
            await self._recall_handler(event)
        except Exception:
            logger.exception("撤回处理器异常")

    async def _run_notice(self, event: NoticeEvent) -> None:
        assert self._notice_handler is not None
        try:
            await self._notice_handler(event)
        except Exception:
            logger.exception("notice 处理器异常")

    def _fail_pending(self, exc: Exception) -> None:
        for future, _action in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
