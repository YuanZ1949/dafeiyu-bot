"""把「模型还没看过的消息」拼成一段带时间线的 prompt。

会话型大脑（OpenCode）只接收增量文本，被冷却/抽样跳过的消息如果直接丢弃，
模型就会以为群里没人说话，把用户的追问误解成「你那边卡了」。这里补上缺口：
逐条投递记录之外的按时间正序一次性交给模型，并告诉它哪些已经送达过。
"""

from __future__ import annotations

from datetime import datetime

from dafeiyu.memory.store import Store
from dafeiyu.models import Message
from dafeiyu.timeutil import SHANGHAI

TIMELINE_LIMIT = 20
TIMELINE_HINT = (
    "以上是自你上次开口以来的新消息，按时间顺序排列。"
    "通读整段再决定要不要开口：被 @ 或引用必须回；"
    "其余情况只在你对整段讨论有自己的看法时才开口，回就回你的观点，不是逐条应答。"
)
BATCH_HEADER = "—— 以下是自你上次开口以来的新消息 ——"
CONTINUATION_HEADER = "【接续】"
EMPTY_CONTENT = "（无文字内容）"


def time_label(timestamp: int) -> str:
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, SHANGHAI).strftime("%m-%d %H:%M")


def format_message_line(
    msg: Message,
    *,
    current: Message | None = None,
    reply_names: dict[str, str] | None = None,
) -> str:
    """一行一条消息；当前这条额外标出 @ 与引用，其他消息补「回复 谁」。

    群友之间的互回是判断「这句话在接谁」的关键线索；旧实现只标当前消息
    对 bot 的 @/引用，A 回 B 的关系在时间线里完全丢失。
    """
    if msg.recalled_at:
        content = "[已撤回]"
    else:
        content = msg.content.strip() or EMPTY_CONTENT
    label = time_label(msg.timestamp)
    prefix = f"[{label}] " if label else ""
    marks = ""
    if current is not None and msg.message_id == current.message_id:
        if current.reply_to_bot:
            marks = "（引用回复你）"
        elif current.at_bot:
            marks = "（@了你）"
    elif reply_names and msg.reply_to_message_id:
        target = reply_names.get(msg.reply_to_message_id, "")
        if target:
            marks = f"（回复 {target}）"
    return f"{prefix}{msg.nickname}：{content}{marks}"


def build_delivery(
    store: Store, msg: Message, limit: int = TIMELINE_LIMIT
) -> tuple[str, list[str]]:
    """返回 (prompt, 本次真正送达模型的消息 id 列表)。

    送达 id 用于逐条标记已投递：一次补投 3 条时，3 条都要记账，
    否则下次会把中间那两条再喂一遍。
    """
    omitted, pending = store.pending_messages(
        msg.chat_type, msg.chat_id, msg.message_id, limit
    )
    if len(pending) <= 1:
        single = pending[0] if pending else msg
        return format_message_line(single, current=msg), [single.message_id]

    lines: list[str] = []
    if omitted:
        lines.append(f"（更早还有 {omitted} 条消息未展开）")
    lines.extend(format_message_line(m, current=msg) for m in pending)
    lines.append(TIMELINE_HINT)
    return "\n".join(lines), [m.message_id for m in pending]


def build_prompt(store: Store, msg: Message, limit: int = TIMELINE_LIMIT) -> str:
    return build_delivery(store, msg, limit)[0]


def build_batch_prompt(
    messages: list[Message],
    injection: str,
    *,
    continuation: str | None = None,
    omitted: int = 0,
    reply_names: dict[str, str] | None = None,
) -> str:
    """批次 prompt：注入段 →（接续）→ 新消息时间线。

    三段注入必须留在最前、时间线留在最后（模型对末尾更敏感）。
    at_bot / reply_to_bot 随消息一起落库，每条按自己的标志渲染标记，
    模型才知道哪几句是对它说的、是 @ 还是引用回复。
    单条路径复用同一构建器，omitted 是更早未展开的消息数（由 pending 查询给出）。
    reply_names 是「被回复消息 id → 昵称」，用来渲染群友之间的「（回复 X）」。
    """
    parts: list[str] = []
    if injection.strip():
        parts.append(injection.strip())
    if continuation:
        parts.append(f"{CONTINUATION_HEADER}\n{continuation}")

    if messages:
        lines: list[str] = []
        if omitted:
            lines.append(f"（更早还有 {omitted} 条消息未展开）")
        lines.append(BATCH_HEADER)
        for msg in messages:
            current = msg if (msg.at_bot or msg.reply_to_bot) else None
            lines.append(
                format_message_line(msg, current=current, reply_names=reply_names)
            )
        if len(messages) > 1:
            lines.append(TIMELINE_HINT)
        parts.append("\n".join(lines))

    return "\n\n".join(parts)
