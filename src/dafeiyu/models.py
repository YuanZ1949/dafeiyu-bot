from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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


@dataclass(frozen=True)
class Attachment:
    """消息附件（图片/语音/视频/文件等）。

    仅存在于内存中，用于提示词与出站发送；永不写入 messages 表或任何持久化存储。
    """

    kind: str
    source: str = ""
    path: str = ""
    name: str = ""
    size: int = 0
    duration: int = 0
    #: 合并转发节点（仅出站协议使用），元素形如 ``{"name": "...", "content": "..."}``。
    #: 追加在末尾，旧的位置参数构造保持可用。
    nodes: tuple[dict[str, Any], ...] = ()
    #: 同一媒体的备选本地路径（微信原图解不开时按顺序退回 ``_h`` / ``_t``）。
    #: 其它通道不填，保持空元组。
    fallbacks: tuple[str, ...] = ()


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
    # 消息类型与附件：同样只在内存中流转，不持久化（content 仍是唯一持久载体）。
    kind: str = "text"
    attachments: list[Attachment] = field(default_factory=list)
    # 微信引用块的作者（协议可选字段）：只进提示词，不入库。
    reply_to_author: str | None = None
    # @所有人：不是点名，但时间线要能看出这是广播。
    at_all: bool = False
    # @ 的是别的群友：这句在叫别人，不是叫 bot。和 at_all 一样只在内存里，
    # 不落库；下一次从库里回读时会丢失这个标记（只影响旧消息的展示）。
    at_others: bool = False
    # 跨群镜像写入的行：以 is_bot=1 落库，但不进目标群提示词、不参与
    # 「上一条是不是 bot 提问」判定；仅保留在回声种子里。
    mirror: bool = False

    def to_prompt_line(self) -> str:
        """裸「昵称: 内容」。

        **不要在这里加「带标记」分支**：标记渲染在
        `memory.timeline.format_message_line`，而 timeline 本身 import 了
        `models` —— 反向 import（哪怕放在函数体内）会被 basedpyright 记成
        `reportImportCycles` 新增 error。需要标记的调用点（历史重放）直接
        调 `format_message_line(message)`。
        """
        return f"{self.nickname}: {self.content}"

    def attachment_paths(self) -> tuple[str, ...]:
        """返回非空 path 的附件路径，保持原顺序。"""
        return tuple(a.path for a in self.attachments if a.path)

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


@dataclass(frozen=True)
class NoticeEvent:
    """OneBot notice 里的非撤回事件（戳一戳/进退群/禁言）。

    ``kind`` 统一成 ``group_poke`` / ``friend_poke`` / ``group_increase`` /
    ``group_decrease`` / ``group_ban``；``user_id`` 是事件主体（被戳者、进/退群者、
    被禁言者），``operator_id`` 是操作者或发起者。仅用于日志与后续接入，不触发模型。
    """

    kind: str
    chat_type: ChatType
    chat_id: str
    user_id: str = ""
    operator_id: str = ""
    sub_type: str = ""
    duration: int = 0


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


@dataclass(frozen=True)
class Participant:
    """episode 的参与者：只留 id 和当时的群昵称，够注入时叫人就行。"""

    user_id: str
    nickname: str = ""


@dataclass
class Episode:
    """一段有起止的对话（「正在聊什么」）。

    Observer 每 10 分钟或每 8–20 条消息重算一次，落库时带 ``id`` 更新同一行；
    ``status`` 只有 open 的会进注入。``started_at`` 用 epoch 秒，和
    ``messages.timestamp`` 同一口径（不要写 TEXT）。
    """

    chat_type: ChatType
    chat_id: str
    start_message_id: str
    last_message_id: str
    topic: str
    summary: str
    participants: tuple[Participant, ...] = ()
    # opening|discussion|climax|winding|closed；未知值落库前归一成 discussion。
    phase: str = "discussion"
    mood: str = ""
    importance: float = 0.5
    # open|closed|stale
    status: str = "open"
    started_at: int = 0
    updated_at: str = ""
    id: int | None = None


@dataclass
class OpenLoop:
    """没有下文的事：谁问了没接、谁答应了没做、群里在等什么。"""

    chat_type: ChatType
    chat_id: str
    kind: str
    content: str
    subject_user_id: str | None = None
    source_message_id: str = ""
    # open|done|dropped
    status: str = "open"
    created_at: str = ""
    closed_at: str | None = None
    id: int | None = None


@dataclass
class BotState:
    """角色的当前状态（单行、跨群共享）：只留「在意什么」和「一句心情」。

    刻意不做数值情绪与随机波动：宁可少，不要飘。``focus`` 由每晚 Reflector
    覆盖，``mood_note`` 由规则在「被夸/被怼/被冷落」时改写。
    """

    focus: str = ""
    mood_note: str = ""
    updated_at: str = ""


@dataclass
class BotStance:
    """跨群共享的「我的立场」：话题 → 观点。

    与 ``bot_state`` 不同，立场不是瞬时状态而是稳定观点：A 群讨论过的
    结论，到 B 群被问起时应该还记得。Reflector 每晚更新，注入按
    ``confidence`` 取靠前的几条。
    """

    topic: str
    stance: str
    confidence: float = 0.5
    source_message_id: str = ""
    updated_at: str = ""
    id: int | None = None


@dataclass
class RelationshipCard:
    """关系卡：聚合出来的「这个人是谁、聊过多少、上次怎么回的」。

    不落表，每次按当前批次发言人现算（impressions + messages 聚合）。
    """

    user_id: str
    nickname: str = ""
    facts: tuple[str, ...] = ()
    message_count: int = 0
    last_seen_ts: int = 0
    last_bot_reply: str = ""


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
