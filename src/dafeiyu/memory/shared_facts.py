"""跨群复用的「查证结论」缓存。

群聊里被问到需要检索的问题时，模型往往要在不同群里把同一件事重新查一遍
（实测：12:58 在 A 群查清「云朵哒哒哒」的出处，13:09 在 B 群又从零查了一次）。
这里把「问题 + 结论」按归一化后的问句去重存下来，下次任何群问到相似问题时
直接作为参考注入 prompt。

隐私边界（硬约束）：

- 只收群聊，私聊一律不入库；
- 问题或回答里出现手机号、身份证、银行卡、邮箱、住址/密码/验证码等字样时整条丢弃；
- 命中缓存的回答只当「参考」，prompt 里明确提示可能过时、拿不准就重查。

这里只做纯函数，落库与检索在 :class:`dafeiyu.memory.store.Store` 里。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# 太短的问题/回答没有复用价值；太长的多半是整段聊天记录，不适合当缓存键。
MIN_QUESTION_CHARS = 4
MAX_QUESTION_CHARS = 200
MIN_ANSWER_CHARS = 8
MAX_ANSWER_CHARS = 800

# 相似度阈值：交集/较短一侧的 bigram 数。低于这个值宁可重查也不要污染上下文。
MATCH_THRESHOLD = 0.72
# 至少共享几个 bigram 才算候选：2 个字符的巧合太多（「是的」「什么」都会撞）。
MIN_OVERLAP = 3

# 「没查到」这类回答没有复用价值，存进去只会让下一次误以为已有结论。
_FAILURE_MARKERS = (
    "没查到",
    "查不到",
    "搜不到",
    "不知道",
    "无法确认",
    "没有找到",
)

_QUESTION_MARKS = ("?", "？")
_QUESTION_WORDS = (
    "什么",
    "怎么",
    "为什么",
    "为啥",
    "哪",
    "多少",
    "是不是",
    "有没有",
    "能不能",
    "如何",
    "谁",
    "吗",
    "呢",
    "求",
    "请问",
)

_PRIVATE_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # 中国大陆手机号
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),  # 18 位身份证
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),  # 银行卡（16-19 位纯数字）
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),  # 邮箱
)

# 出现这些词就按隐私处理：宁可少记一条，也不要把私事带到别的群。
_PRIVATE_WORDS = (
    "身份证",
    "银行卡",
    "手机号码",
    "手机号",
    "电话号码",
    "家庭住址",
    "住址",
    "门牌",
    "密码",
    "验证码",
    "学号",
    "工号",
    "定位",
)


@dataclass(frozen=True)
class SharedFact:
    """一条可跨群复用的查证结论。"""

    id: int
    question_text: str
    answer_text: str
    source_chat_id: str
    created_at: str


def normalize(text: str) -> str:
    """归一化问句：全半角统一、大小写折叠、去掉所有标点/空白/符号。

    只保留文字与数字，目的是让「云朵 哒哒哒 是什么梗」和
    「云朵哒哒哒哒哒是什么梗？」落到相近的 bigram 集合上。
    """
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(
        ch
        for ch in folded
        if unicodedata.category(ch)[0] not in ("P", "Z", "S", "C")
    )


def bigrams(text: str) -> frozenset[str]:
    """字符 bigram 集合；单字退化成单元素集合，避免返回空集。"""
    if len(text) < 2:
        return frozenset({text}) if text else frozenset()
    return frozenset(text[index : index + 2] for index in range(len(text) - 1))


def similarity(left: str, right: str) -> float:
    """问句相似度：共享 bigram / 较短一侧的 bigram 数。

    用「较短一侧」做分母，是为了让「原神是什么梗」这种短问句也能命中
    「原神里云朵哒哒哒是什么梗」这种长问句；否则长句永远赢不了。
    """
    a, b = bigrams(normalize(left)), bigrams(normalize(right))
    if not a or not b:
        return 0.0
    shared = len(a & b)
    if shared < MIN_OVERLAP:
        return 0.0
    return shared / min(len(a), len(b))


def is_private(text: str) -> bool:
    """疑似个人信息：手机号/身份证/银行卡/邮箱/住址/密码等。"""
    body = str(text or "")
    if any(pattern.search(body) for pattern in _PRIVATE_PATTERNS):
        return True
    return any(word in body for word in _PRIVATE_WORDS)


def looks_like_question(text: str) -> bool:
    """粗判提问：问号结尾，或含常见疑问词。只用于决定「值不值得缓存」。"""
    body = str(text or "").strip()
    if not body:
        return False
    if body.endswith(_QUESTION_MARKS):
        return True
    return any(word in body for word in _QUESTION_WORDS)


def extract(question: str, answer: str) -> tuple[str, str] | None:
    """把一问一答收敛成可缓存的一对；不合格返回 None。

    不合格的情形：长度越界、涉及隐私、回答本身是「没查到」。
    """
    ask = str(question or "").strip()
    reply = str(answer or "").strip()
    if not (MIN_QUESTION_CHARS <= len(ask) <= MAX_QUESTION_CHARS):
        return None
    if not (MIN_ANSWER_CHARS <= len(reply) <= MAX_ANSWER_CHARS):
        return None
    if is_private(ask) or is_private(reply):
        return None
    if any(marker in reply for marker in _FAILURE_MARKERS):
        return None
    return ask, reply


def pick_best(
    facts: list[dict[str, Any]],
    query: str,
    *,
    threshold: float = MATCH_THRESHOLD,
) -> SharedFact | None:
    """在一批候选里挑相似度最高的一条；同分时优先新记录。"""
    best: tuple[float, dict[str, Any]] | None = None
    for row in facts:
        question = str(row.get("question_text") or "")
        if not question or is_private(question):
            continue
        answer = str(row.get("answer_text") or "")
        if not answer or is_private(answer):
            continue
        score = similarity(query, question)
        if score < threshold:
            continue
        if best is None or score > best[0]:
            best = (score, row)
    if best is None:
        return None
    row = best[1]
    return SharedFact(
        id=int(row.get("id") or 0),
        question_text=str(row.get("question_text") or ""),
        answer_text=str(row.get("answer_text") or ""),
        source_chat_id=str(row.get("source_chat_id") or ""),
        created_at=str(row.get("created_at") or ""),
    )
