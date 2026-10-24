from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType

CONTEXT_BUDGET = 2500
OUTPUT_RESERVE = 300
SYSTEM_MAX_TOKENS = 600

IMPRESSION_HEADER = "【群友印象】"
SUMMARY_HEADER = "【昨日摘要】"


@dataclass(frozen=True)
class ContextBundle:
    system: str
    messages: list[dict[str, str]]
    estimated_tokens: int


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return math.ceil(cjk * 0.7 + other / 4)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _yesterday(today: str) -> str:
    return (date.fromisoformat(today) - timedelta(days=1)).isoformat()


def _compose_system(base: str, impressions: list[str], summary: str | None) -> str:
    parts = [base]
    if impressions:
        parts.append("\n\n" + IMPRESSION_HEADER + "\n" + "\n".join(impressions))
    if summary:
        parts.append("\n\n" + SUMMARY_HEADER + "\n" + summary)
    return "".join(parts)


def build_context(
    store: Store,
    chat_type: ChatType,
    chat_id: int,
    window_size: int,
    impression_topk: int,
    system: str,
    today: str,
) -> ContextBundle:
    history = store.recent_messages(chat_type, chat_id, window_size)
    messages = [
        {
            "role": "assistant" if m.is_bot else "user",
            "content": m.to_prompt_line(),
        }
        for m in history
    ]

    impression_lines = [
        f"{imp.content}" for imp in store.list_impressions(chat_type, chat_id, impression_topk)
    ]
    summary = store.get_summary(chat_type, chat_id, _yesterday(today))

    def total(system_text: str, msgs: list[dict[str, str]]) -> int:
        return estimate_tokens(system_text) + sum(estimate_tokens(m["content"]) for m in msgs)

    available = CONTEXT_BUDGET - OUTPUT_RESERVE

    # 1) 先裁窗口，但永远保留最新的一条（当前消息）
    while total(_compose_system(system, impression_lines, summary), messages) > available and len(messages) > 1:
        messages.pop(0)

    system_text = _compose_system(system, impression_lines, summary)
    # 2) 再逐条去掉印象
    while total(system_text, messages) > available and impression_lines:
        impression_lines.pop()
        system_text = _compose_system(system, impression_lines, summary)

    # 3) 最后去摘要
    if total(system_text, messages) > available and summary is not None:
        summary = None
        system_text = _compose_system(system, impression_lines, summary)

    # 4) 兜底：system 自身超长时截断
    system_text = _truncate_to_tokens(system_text, SYSTEM_MAX_TOKENS)

    return ContextBundle(
        system=system_text,
        messages=messages,
        estimated_tokens=total(system_text, messages),
    )
