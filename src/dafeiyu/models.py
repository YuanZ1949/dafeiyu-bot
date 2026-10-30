from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ChatType(str, Enum):
    GROUP = "group"
    PRIVATE = "private"


@dataclass
class Segment:
    type: str
    data: dict


@dataclass
class Message:
    chat_type: ChatType
    chat_id: int
    user_id: int
    nickname: str
    content: str
    message_id: str
    segments: list[Segment] = field(default_factory=list)
    is_bot: bool = False
    at_bot: bool = False
    reply_to_bot: bool = False
    # 回复目标：id 用来回查昵称，uid 用来在消息被清理后退化成 QQ 号。
    reply_to_message_id: str | None = None
    reply_to_user_id: int | None = None
    timestamp: int = 0
    # 非空 = 这条消息已被撤回；时间线只显示占位，不再展示原文。
    recalled_at: str | None = None

    def to_prompt_line(self) -> str:
        return f"{self.nickname}: {self.content}"


@dataclass(frozen=True)
class RecallEvent:
    """OneBot notice 里的撤回事件：只带定位信息，不带原文。"""

    chat_type: ChatType
    chat_id: int
    message_id: str
    operator_id: int = 0


@dataclass
class Impression:
    chat_type: ChatType
    chat_id: int
    subject_user_id: int | None
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
    chat_id: int
    date: str
    content: str
    updated_at: str = ""


@dataclass
class WeeklySummary:
    chat_type: ChatType
    chat_id: int
    week_start: str
    content: str
    updated_at: str = ""
