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
    timestamp: int = 0

    def to_prompt_line(self) -> str:
        return f"{self.nickname}: {self.content}"


@dataclass
class Impression:
    chat_type: ChatType
    chat_id: int
    subject_user_id: int | None
    kind: str
    content: str
    confidence: float
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
