from __future__ import annotations

from datetime import date, datetime
from typing import Awaitable, Callable

from dafeiyu.memory.store import SessionStats, Store
from dafeiyu.models import ChatType, Message

Summarizer = Callable[..., Awaitable[str]]


def needs_rotation(
    stats: SessionStats,
    *,
    now: datetime,
    rotation_messages: int,
    rotation_days: int,
    rotation_tokens: int,
) -> bool:
    """条数、token、天数任一到达阈值就该换 session。

    created_at 是上海日期（YYYY-MM-DD），按自然日差算；缺失时不参与天数判断。
    """
    if stats.delivered_count >= rotation_messages:
        return True
    if stats.token_estimate >= rotation_tokens:
        return True
    if not stats.created_at:
        return False
    try:
        started = date.fromisoformat(stats.created_at)
    except ValueError:
        return False
    return (now.date() - started).days >= rotation_days


async def apply_rotation(
    brain,
    store: Store,
    summarize: Summarizer,
    chat_type: ChatType,
    chat_id: str,
    messages: list[Message],
) -> str | None:
    """生成接续摘要后换 session；摘要失败则什么都不做，返回 None。"""
    summary = (await summarize(brain, store, chat_type, chat_id, messages) or "").strip()
    if not summary:
        return None
    brain.reset_session(chat_type, chat_id)
    store.reset_session_stats(chat_type, chat_id)
    return f"这是你在本会话的新 session。上次聊到：{summary}。"
