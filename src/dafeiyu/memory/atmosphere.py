"""群气氛（P3）：确定性的轻量统计，不调模型、不引入随机。

只回答「这个群现在热不热闹」：最近窗口内的消息量、发言人数、问句比例、
短句连刷和冷场时长。输出一行中文，由 ``service._scene_hint`` 拼进「此刻」。
没有可用样本时返回空串，调用方整句省略。

刻意不做「氛围评分→自动调阈值」：接不接话仍然交给模型判断，这里只是把
它本来要翻消息才能数出来的事实提前说清楚。
"""

from __future__ import annotations

from collections.abc import Sequence

from dafeiyu.models import Message

#: 只统计最近 10 分钟：更早的消息描述的是「刚才」，不是「现在」。
WINDOW_SECONDS = 600
#: 单次最多看最近 60 条，避免每次回复都扫长窗口。
MAX_SAMPLE = 60
BUSY_MESSAGES = 15
ACTIVE_MESSAGES = 8
QUIET_MESSAGES = 3
MANY_SPEAKERS = 4
SHORT_CHARS = 6
SILENCE_MINUTES = 30
MAX_CHARS = 80

QUESTION_MARKERS = ("？", "?", "吗", "呢", "怎么", "为什么", "如何", "哪个", "谁", "啥")


def _is_question(text: str) -> bool:
    return any(marker in text for marker in QUESTION_MARKERS)


def describe_atmosphere(
    messages: Sequence[Message], *, now_ts: float
) -> str:
    """把一批消息概括成一句群气氛；统计不出东西时返回空串。

    ``messages`` 按时间正序传入（``Store.recent_messages`` 的口径）。时间戳
    为 0 的消息（老数据/测试）无法参与窗口计算，退化成「全量样本」。
    """
    human = [m for m in messages if not m.is_bot and not m.recalled_at]
    if not human:
        return ""
    human = human[-MAX_SAMPLE:]

    stamped = [m for m in human if float(m.timestamp) > 0]
    if stamped:
        recent = [
            m
            for m in human
            if float(m.timestamp) > 0 and now_ts - float(m.timestamp) <= WINDOW_SECONDS
        ]
        last_ts = max(float(m.timestamp) for m in stamped)
        silence = int(now_ts - last_ts) // 60
    else:
        recent = human
        silence = -1

    parts: list[str] = []
    if recent:
        count = len(recent)
        speakers = {m.user_id for m in recent}
        texts = [m.content.strip() for m in recent if m.content.strip()]
        questions = sum(1 for text in texts if _is_question(text))
        shorts = sum(1 for text in texts if len(text) <= SHORT_CHARS)

        if count >= BUSY_MESSAGES:
            parts.append("消息刷得很快")
        elif count >= ACTIVE_MESSAGES:
            parts.append("聊得正热")
        elif count >= QUIET_MESSAGES:
            parts.append("有人零星在聊")
        else:
            parts.append("比较安静")

        if len(speakers) >= MANY_SPEAKERS:
            parts.append("好几个人在场")
        if texts and questions >= 3 and questions * 2 >= len(texts):
            parts.append("问句多")
        if texts and shorts >= 5 and shorts * 2 >= len(texts):
            parts.append("短句连刷")

    if silence >= SILENCE_MINUTES:
        parts.append(f"已经安静 {silence} 分钟")
    if not parts:
        return ""
    return ("，".join(parts))[:MAX_CHARS]
