from __future__ import annotations

from collections.abc import Collection
from typing import Any

from dafeiyu.models import ChatType, Message, Segment

_PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _render_segments(raw_segments: list[Any]) -> tuple[str, list[Segment], str | None]:
    parts: list[str] = []
    segments: list[Segment] = []
    reply_id: str | None = None

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
        elif seg_type in _PLACEHOLDERS:
            parts.append(_PLACEHOLDERS[seg_type])
        elif seg_type == "reply":
            reply_id = str(data.get("id", ""))
        # 其余段类型（json/xml/未知）一律忽略，不抛异常

    return "".join(parts).strip(), segments, reply_id


def parse_event(
    event: dict[str, Any],
    bot_qq: int,
    bot_message_ids: Collection[str] = (),
) -> Message | None:
    if not isinstance(event, dict) or event.get("post_type") != "message":
        return None

    message_type = event.get("message_type")
    if message_type == "group":
        chat_type = ChatType.GROUP
        chat_id = _as_int(event.get("group_id"))
    elif message_type == "private":
        chat_type = ChatType.PRIVATE
        chat_id = _as_int(event.get("user_id"))
    else:
        return None

    user_id = _as_int(event.get("user_id"))
    sender = event.get("sender")
    if not isinstance(sender, dict):
        sender = {}
    nickname = str(sender.get("card") or sender.get("nickname") or user_id)

    raw_message = event.get("message", "")
    reply_id: str | None = None
    segments: list[Segment] = []

    if isinstance(raw_message, str):
        content = raw_message.strip()
        at_bot = False
    elif isinstance(raw_message, list):
        content, segments, reply_id = _render_segments(raw_message)
        at_bot = any(
            seg.type == "at" and str(seg.data.get("qq", "")) == str(bot_qq) for seg in segments
        )
    else:
        content = ""
        at_bot = False

    reply_to_bot = reply_id is not None and reply_id in {str(i) for i in bot_message_ids}

    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=str(event.get("message_id", "")),
        segments=segments,
        is_bot=user_id == bot_qq,
        at_bot=at_bot,
        reply_to_bot=reply_to_bot,
        timestamp=_as_int(event.get("time")),
    )
