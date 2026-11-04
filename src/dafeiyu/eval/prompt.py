"""从 case 构造 prompt：复用真实批次构建器，注入段由 context 确定性拼出。"""

from __future__ import annotations

from dafeiyu.memory.context import (
    INJECTION_IMPRESSION_HEADER,
    INJECTION_SUMMARY_HEADER,
)
from dafeiyu.memory.timeline import build_batch_prompt
from dafeiyu.models import ChatType, Message

from .models import EvalCase, EvalContext


def build_injection(context: EvalContext) -> str:
    """把 case 的摘要/印象按固定顺序拼成注入段。

    顺序与真实注入一致：群友印象在前、昨日摘要在后；空段整段省略。
    """
    parts: list[str] = []
    if context.impressions:
        parts.append(
            f"{INJECTION_IMPRESSION_HEADER}\n" + "\n".join(context.impressions)
        )
    if context.summary:
        parts.append(f"{INJECTION_SUMMARY_HEADER}\n{context.summary}")
    return "\n\n".join(parts)


def to_messages(case: EvalCase) -> list[Message]:
    chat_type = ChatType(case.chat_type)
    return [
        Message(
            chat_type=chat_type,
            chat_id=case.chat_id,
            user_id=item.user_id,
            nickname=item.nickname,
            content=item.content,
            message_id=f"{case.id}:{index}",
            at_bot=item.at_bot,
            reply_to_bot=item.reply_to_bot,
            timestamp=item.timestamp,
        )
        for index, item in enumerate(case.messages)
    ]


def build_prompt(case: EvalCase) -> str:
    return build_batch_prompt(
        to_messages(case),
        build_injection(case.context),
        continuation=case.context.continuation,
    )
