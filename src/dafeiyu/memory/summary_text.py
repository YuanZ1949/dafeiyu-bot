"""摘要正文的清洗与截断：写入与注入共用同一套规则。

模型侧偶尔会把「已达到最大步数」「未调用工具」这类 agent 元叙述写进摘要，
或者把半句话直接截断。这里统一清掉元文本、给截断补上省略号，避免脏数据
进库，更避免它被当成「群友说过的话」再注入下一轮上下文。
"""

from __future__ import annotations

import re

#: 出现在摘要开头、说明「这是 agent 自述」而不是群聊内容的词。
META_KEYWORDS = (
    "步数",
    "最大步",
    "未再调用",
    "无法再调用",
    "未使用任何工具",
    "无需调用工具",
    "无需读取图片",
    "以下为",
    "可直接作为摘要",
    "摘要正文",
    "已完成的整理",
    "草拟摘要",
)
#: 「摘要正文：」这类分隔标记：正文在它之后。
_BODY_MARKERS = ("摘要正文：", "摘要正文:", "摘要：", "摘要:")
#: 单段「元叙述：正文」只有在整段以这些自述开头时才切；防止正文里正常
#: 出现的「最大步数」「工具」把真正的摘要切掉。
_META_PREFIXES = (
    "已达到",
    "已到达",
    "本任务",
    "本代理",
    "本次",
    "未使用",
    "未再",
    "无法再",
    "无需",
    "（说明",
    "(说明",
    "说明：",
    "说明:",
    "摘要正文",
)
#: 元叙述与正文挤在同一段时的切分点，取最靠后的那个。
_CUT_MARKS = ("——", "：", ":")
#: 截断时优先落在句末，避免把最后一句话切成半句。
CLIP_BOUNDARIES = "。！？；!?;\n"
#: 清洗后至少要有这么多字才认；否则宁可保留原文，别把摘要清成空串。
_MIN_BODY_CHARS = 20


def _looks_like_meta(text: str) -> bool:
    return any(word in text for word in META_KEYWORDS)


def clean_summary(text: str) -> str:
    """去掉模型在摘要前面写的 agent 元叙述，只留群聊正文。

    只在前 120 字里判断，避免把正文中正常出现的「工具」「步数」误删；
    清洗后过短就退回原文。
    """
    body = (text or "").strip()
    if not body:
        return ""
    original = body
    for marker in _BODY_MARKERS:
        index = body.rfind(marker)
        if index == -1:
            continue
        rest = body[index + len(marker) :].strip()
        if len(rest) >= _MIN_BODY_CHARS:
            body = rest
            break

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    while len(paragraphs) > 1 and _looks_like_meta(paragraphs[0]):
        paragraphs.pop(0)
    body = "\n\n".join(paragraphs).strip()

    head = body[:120]
    if head and body.startswith(_META_PREFIXES) and _looks_like_meta(head):
        best = -1
        best_len = 0
        for mark in _CUT_MARKS:
            index = head.rfind(mark)
            if index > best:
                best, best_len = index, len(mark)
        if best != -1:
            rest = body[best + best_len :].strip()
            if len(rest) >= _MIN_BODY_CHARS:
                body = rest

    return body if len(body) >= _MIN_BODY_CHARS else original


def clip_summary(text: str, limit: int) -> str:
    """把摘要收敛到 ``limit`` 字；只要发生了截断就一定以「…」收尾。"""
    body = (text or "").strip()
    if limit <= 0:
        return ""
    if len(body) <= limit:
        return body
    window = body[:limit]
    cut = max(window.rfind(char) for char in CLIP_BOUNDARIES)
    # 断点太靠前（不足一半）说明第一句本身很短，截到那里会丢掉大部分内容，
    # 此时宁可硬截断。
    if cut + 1 >= limit // 2:
        return window[: cut + 1].rstrip() + "…"
    return window[: limit - 1].rstrip() + "…"
