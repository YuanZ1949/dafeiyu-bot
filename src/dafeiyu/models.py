from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ChatType(str, Enum):
    GROUP = "group"
    PRIVATE = "private"


# 平台会话 id 统一带通道前缀存储，如 "qq:12345"、"wxg:abc"。
QQ_PREFIX = "qq:"
WECHAT_PREFIX = "wxg:"


def channel_of(chat_id: str) -> str:
    """从带前缀的会话 id 推导所属通道：这是唯一的派生点。"""
    if chat_id.startswith(QQ_PREFIX):
        channel, rest = "qq", chat_id[len(QQ_PREFIX):]
    elif chat_id.startswith(WECHAT_PREFIX):
        channel, rest = "wechat", chat_id[len(WECHAT_PREFIX):]
    else:
        channel, rest = "", ""
    if not channel or not rest:
        raise ValueError(f"chat_id 缺少通道前缀或前缀后为空: {chat_id!r}")
    return channel


@dataclass
class Segment:
    type: str
    data: dict


@dataclass
class Message:
    chat_type: ChatType
    chat_id: str
    user_id: str
    nickname: str
    content: str
    message_id: str
    segments: list[Segment] = field(default_factory=list)
    is_bot: bool = False
    at_bot: bool = False
    reply_to_bot: bool = False
    # 回复目标：id 用来回查昵称，uid 用来在消息被清理后退化成 QQ 号。
    reply_to_message_id: str | None = None
    reply_to_user_id: str | None = None
    timestamp: int = 0
    # 非空 = 这条消息已被撤回；时间线只显示占位，不再展示原文。
    recalled_at: str | None = None
    # 微信引用块的原文摘要（协议可选字段）：只进提示词，不入库。
    reply_to_text: str | None = None

    def to_prompt_line(self) -> str:
        return f"{self.nickname}: {self.content}"

    @property
    def channel(self) -> str:
        """所属通道（只读派生，不持久化）：转调唯一的派生点 `channel_of`。"""
        return channel_of(self.chat_id)


@dataclass(frozen=True)
class RecallEvent:
    """OneBot notice 里的撤回事件：只带定位信息，不带原文。"""

    chat_type: ChatType
    chat_id: str
    message_id: str
    operator_id: str = ""


@dataclass
class Impression:
    chat_type: ChatType
    chat_id: str
    subject_user_id: str | None
    kind: str
    content: str
    confidence: float
    # 抽取时记下的最新群昵称：注入时用来回答「这是谁」，消息被清理后也不丢。
    subject_nickname: str = ""
    source_message_id: str = ""
    source_excerpt: str = ""
    updated_at: str = ""
    id: int | None = None


@dataclass
class Summary:
    chat_type: ChatType
    chat_id: str
    date: str
    content: str
    updated_at: str = ""


@dataclass
class WeeklySummary:
    chat_type: ChatType
    chat_id: str
    week_start: str
    content: str
    updated_at: str = ""
