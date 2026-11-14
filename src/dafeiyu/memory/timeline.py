"""把「模型还没看过的消息」拼成一段带时间线的 prompt。

会话型大脑（OpenCode）只接收增量文本，被冷却/抽样跳过的消息如果直接丢弃，
模型就会以为群里没人说话，把用户的追问误解成「你那边卡了」。这里补上缺口：
逐条投递记录之外的按时间正序一次性交给模型，并告诉它哪些已经送达过。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from dafeiyu.memory.store import Store
from dafeiyu.models import Message
from dafeiyu.timeutil import SHANGHAI

TIMELINE_LIMIT = 20
TIMELINE_HINT = (
    "以上是自你上次开口以来的新消息，按时间顺序排列。"
    "通读整段再决定要不要开口：@了你或引用回复你才必须回；"
    "如果 @ 的是别的群友（行尾标了「@了别的群友」），那是在叫别人，别抢答；"
    "其余情况只在你对整段讨论有自己的看法时才开口，回就回你的观点，不是逐条应答。"
)
BATCH_HEADER = "—— 以下是自你上次开口以来的新消息 ——"
BATCH_BLOCK_HEADER = "【本批】"
# 话题锚点取本批第一句的前 40 字，足够认出「在聊哪件事」，又不至于把整句搬进去。
TOPIC_ANCHOR_CHARS = 40
CONTINUATION_HEADER = "【接续】"
EMPTY_CONTENT = "（无文字内容）"

#: 两条消息间隔超过这么久，提示模型「中间空了一段」，别把旧话题硬接过来。
PAUSE_NOTICE_SECONDS = 30 * 60
PAUSE_LINE_TEMPLATE = (
    "【停顿 {duration}】上一条消息到现在隔了这么久，中间群里没有新消息。"
    "上一段话题可能已经过去，也可能还连着：先看这批消息是不是接着同一件事说，"
    "别把旧话题硬接过来。"
)
GAP_LINE_TEMPLATE = "（这里隔了 {duration}没有消息）"


def gap_text(seconds: float) -> str:
    """把秒数写成「N 分钟 / N 小时 / N 天」，用于停顿提示。"""
    total = int(max(0.0, seconds))
    if total < 3600:
        return f"{max(1, total // 60)} 分钟"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")
    days, rest = divmod(total, 86400)
    hours = rest // 3600
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


def time_label(timestamp: int) -> str:
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, SHANGHAI).strftime("%m-%d %H:%M")


QUOTE_TEXT_LIMIT = 40


def _quote_mark(msg: Message, reply_names: dict[str, str] | None) -> str:
    """引用块的渲染：优先显示作者 + 原文，其次退回「回复 谁」。"""
    text = (msg.reply_to_text or "").replace("\n", " ").strip()
    if len(text) > QUOTE_TEXT_LIMIT:
        text = text[:QUOTE_TEXT_LIMIT].rstrip() + "…"
    if msg.reply_to_bot:
        return f"（引用你：{text}）" if text else "（引用回复你）"
    if text and msg.reply_to_author:
        return f"（引用 {msg.reply_to_author}：{text}）"
    if text:
        return f"（引用：{text}）"
    if reply_names and msg.reply_to_message_id:
        target = reply_names.get(msg.reply_to_message_id, "")
        if target:
            return f"（回复 {target}）"
    if msg.reply_to_author:
        return f"（引用 {msg.reply_to_author}）"
    return ""


def format_message_line(
    msg: Message,
    *,
    current: Message | None = None,
    reply_names: dict[str, str] | None = None,
    mention_ids: frozenset[str] | None = None,
) -> str:
    """一行一条消息；标出引用、@、@所有人 与「只是提到你」。

    群友之间的互回是判断「这句话在接谁」的关键线索；旧实现只标当前消息
    对 bot 的 @/引用，A 回 B 的关系在时间线里完全丢失。bot 自己的行统一用
    「你：」，避免模型把自己的旧发言当成群友发言。
    """
    if msg.recalled_at:
        content = "[已撤回]"
    else:
        content = msg.content.strip() or EMPTY_CONTENT
    label = time_label(msg.timestamp)
    prefix = f"[{label}] " if label else ""
    marks: list[str] = []
    if msg.at_all:
        marks.append("（@所有人）")
    if msg.at_others:
        marks.append("（@了别的群友）")
    if msg.reply_to_bot or msg.reply_to_text or msg.reply_to_author:
        quote = _quote_mark(msg, reply_names)
        if quote:
            marks.append(quote)
    elif reply_names and msg.reply_to_message_id:
        target = reply_names.get(msg.reply_to_message_id, "")
        if target:
            marks.append(f"（回复 {target}）")
    if msg.at_bot:
        marks.append("（@了你）")
    if mention_ids and msg.message_id in mention_ids:
        marks.append("（提到你，不确定）")
    speaker = "你" if msg.is_bot else msg.nickname
    return f"{prefix}{speaker}：{content}{''.join(marks)}"


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
    # 库里不存引用原文/作者；当前这条用内存里的对象替换，别把引用信息丢掉。
    pending = [msg if item.message_id == msg.message_id else item for item in pending]
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


def _batch_block(
    messages: list[Message],
    *,
    mirror_count: int,
    mirror_speakers: Sequence[str],
    addressed: Sequence[Message],
) -> str:
    """【本批】块：谁在说、谁在叫你、在聊什么、几条是转发的。

    build_batch_prompt 是纯函数：镜像条数由调用方（service）查库后传入，
    这里只渲染，绝不查库；addressed 也是调用方用 router.addressed_source
    判定好的点名消息，本函数不重新发明判定。
    """
    speakers = list(dict.fromkeys(m.nickname for m in messages if m.nickname))
    lines = [
        f"{BATCH_BLOCK_HEADER}这一批共 {len(messages)} 条消息，说话人：{'、'.join(speakers)}。"
    ]
    if addressed:
        who = list(dict.fromkeys(m.nickname for m in addressed if m.nickname))
        lines.append(f"- 在跟你说话：{len(addressed)} 条（{'、'.join(who)}）。")
    anchor = (messages[0].content or "").strip().replace("\n", " ")
    if anchor:
        clipped = anchor[:TOPIC_ANCHOR_CHARS]
        ellipsis = "…" if len(anchor) > TOPIC_ANCHOR_CHARS else ""
        lines.append(f"- 话题锚点：本批开头是「{clipped}{ellipsis}」。")
    if mirror_count > 0:
        who = "、".join(dict.fromkeys(mirror_speakers))
        suffix = f"（{who}）" if who else ""
        lines.append(f"- 跨群转发：{mirror_count} 条{suffix}。")
    return "\n".join(lines)


def build_batch_prompt(
    messages: list[Message],
    injection: str,
    *,
    continuation: str | None = None,
    omitted: int = 0,
    reply_names: dict[str, str] | None = None,
    mention_ids: frozenset[str] | None = None,
    gap_seconds: float = 0.0,
    mirror_count: int = 0,
    mirror_speakers: Sequence[str] = (),
    addressed: Sequence[Message] = (),
) -> str:
    """批次 prompt：【本批】→ 注入段 →（接续）→ 新消息时间线。

    三段注入必须留在最前、时间线留在最后（模型对末尾更敏感）；
    【本批】是整段的第一块，给模型一个「这批是谁在跟谁聊什么」的总览。
    单条且无人点名的批次不产出【本批】（避免噪声）。
    at_bot / reply_to_bot 随消息一起落库，每条按自己的标志渲染标记，
    模型才知道哪几句是对它说的、是 @ 还是引用回复。
    单条路径复用同一构建器，omitted 是更早未展开的消息数（由 pending 查询给出）。
    reply_names 是「被回复消息 id → 昵称」，用来渲染群友之间的「（回复 X）」。
    gap_seconds 是这一批第一条消息与上一批之间的空档：够长就明确提示「停顿」，
    让模型重新判断旧话题是否已经过去，而不是顺着很久以前的话头接。
    mirror_count / mirror_speakers 是本批窗口内跨群镜像行的条数与发言人：
    pending_batch 只取 is_bot=0 的行而镜像入库是 is_bot=1，所以必须由调用方
    另查 recent_messages(include_mirror=True) 后传入，默认 0/空（eval 调用点不用改）。
    addressed 是调用方用 router.addressed_source 判出的点名消息；不传时按
    at_bot / reply_to_bot 退化判定（alias 点名只有 service 层能判）。
    """
    addressed_msgs = (
        list(addressed)
        if addressed
        else [m for m in messages if m.at_bot or m.reply_to_bot]
    )
    parts: list[str] = []
    if messages and (len(messages) >= 2 or addressed_msgs):
        parts.append(
            _batch_block(
                messages,
                mirror_count=mirror_count,
                mirror_speakers=mirror_speakers,
                addressed=addressed_msgs,
            )
        )
    if injection.strip():
        parts.append(injection.strip())
    if continuation:
        parts.append(f"{CONTINUATION_HEADER}\n{continuation}")
    if gap_seconds >= PAUSE_NOTICE_SECONDS:
        parts.append(PAUSE_LINE_TEMPLATE.format(duration=gap_text(gap_seconds)))

    if messages:
        lines: list[str] = []
        if omitted:
            lines.append(f"（更早还有 {omitted} 条消息未展开）")
        lines.append(BATCH_HEADER)
        previous_ts = 0
        for msg in messages:
            if (
                previous_ts
                and msg.timestamp
                and msg.timestamp - previous_ts >= PAUSE_NOTICE_SECONDS
            ):
                lines.append(
                    GAP_LINE_TEMPLATE.format(
                        duration=gap_text(msg.timestamp - previous_ts)
                    )
                )
            if msg.timestamp:
                previous_ts = msg.timestamp
            current = msg if (msg.at_bot or msg.reply_to_bot) else None
            lines.append(
                format_message_line(
                    msg,
                    current=current,
                    reply_names=reply_names,
                    mention_ids=mention_ids,
                )
            )
        if len(messages) > 1:
            lines.append(TIMELINE_HINT)
        parts.append("\n".join(lines))

    return "\n\n".join(parts)
