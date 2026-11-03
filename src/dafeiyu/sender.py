"""把模型回复整形为适合群聊的短句与分片。"""

from __future__ import annotations

import re

SPLIT_AT = "。！？!?；;\n"
MAX_CHUNK = 500

# 拆成多条短消息：只有够长且真有多个句子才拆，避免把一句话拆碎。
# 省略号不是句子边界（否则会把括号里的 OS 从中间切开）。
SEGMENT_BOUNDARIES = "。！？!?\n"
SEGMENT_MIN_CHARS = 60
SEGMENT_MAX_PARTS = 3
SEGMENT_MIN_PART_CHARS = 8

# 成对符号：截断/分片时不能把引号、括号切成两半。
PAIRS = {
    "（": "）",
    "(": ")",
    "「": "」",
    "『": "』",
    "《": "》",
    "〈": "〉",
    "【": "】",
    "[": "]",
    "“": "”",
    "‘": "’",
}
_CLOSERS = frozenset(PAIRS.values())

TECHNICAL_MARKERS = (
    "代码",
    "报错",
    "错误",
    "异常",
    "函数",
    "接口",
    "编译",
    "部署",
    "配置",
    "日志",
    "算法",
    "数据库",
    "sql",
    "python",
    "javascript",
    "java",
    "api",
    "bug",
    "error",
    "traceback",
)


def is_technical_question(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in TECHNICAL_MARKERS)


def _depths(text: str) -> list[int]:
    """返回每个下标处「进入该字符之前」的嵌套深度。

    开符号在下标处深度不变、其后 +1；闭符号在 depth>0 时先 -1。
    多余的闭符号直接忽略，深度不会变负。
    """
    depths: list[int] = []
    depth = 0
    for char in text:
        depths.append(depth)
        if char in PAIRS:
            depth += 1
        elif char in _CLOSERS and depth > 0:
            depth -= 1
    return depths


def _open_stack(text: str) -> list[tuple[int, str]]:
    """返回仍未闭合的开符号位置栈（位置, 对应闭符号）。"""
    stack: list[tuple[int, str]] = []
    for index, char in enumerate(text):
        if char in PAIRS:
            stack.append((index, PAIRS[char]))
        elif char in _CLOSERS and stack:
            stack.pop()
    return stack


def _truncate(text: str, limit: int) -> str:
    """按 limit 截断，且只在括号/引号之外（深度 0）的句末切。

    窗口里没有可用的句末时，退回「去掉未闭合尾巴 + 省略号」，
    保证返回结果自身括号平衡、长度不超过 limit。
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    window = text[:limit]
    depths = _depths(window)
    cut = -1
    for index, char in enumerate(window):
        if char in SPLIT_AT and depths[index] == 0:
            cut = index
    if cut > 0:
        return window[: cut + 1]

    base = text[: limit - 1]
    stack = _open_stack(base)
    if stack:
        # 丢掉最后一个没闭合的尾巴，避免留下半句引号。
        base = base[: stack[-1][0]]
    result = base.rstrip()
    if not result:
        # 极端情况：整段都被一个未闭合的开符号包住。
        alt = text[: limit - 1].rstrip()
        alt_stack = _open_stack(alt)
        if alt_stack:
            alt = alt[: alt_stack[-1][0]].rstrip()
        result = alt
    if not result:
        return "…"
    result += "…"
    return result[:limit]


def _split(text: str, size: int = MAX_CHUNK) -> list[str]:
    chunks: list[str] = []
    rest = text
    while len(rest) > size:
        window = rest[:size]
        depths = _depths(window)
        cut = -1
        for index, char in enumerate(window):
            if char in SPLIT_AT and depths[index] == 0:
                cut = index
        if cut < size * 4 // 5:
            cut = size - 1
            # 硬切可能把括号切成两半：把这个开符号之后的残段挪到下一块。
            stack = _open_stack(window[: cut + 1])
            if stack and stack[-1][0] > 0:
                cut = stack[-1][0] - 1
        if cut < 0:
            cut = size - 1
        chunks.append(rest[: cut + 1])
        rest = rest[cut + 1 :]
    if rest:
        chunks.append(rest)
    return chunks


# ---- markdown 清理 ------------------------------------------------------

# 代码块 / 行内代码整段保留，不参与清理。
_CODE_RE = re.compile(r"```.*?(?:```|$)|`[^`\n]*`", re.DOTALL)

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_OS_ITALIC_RE = re.compile(r"\*(?P<body>[（(][^*\n]*[）)])\*")
_ITALIC_RE = re.compile(r"\*([^*\n]+?)\*")
_ITALIC_UNDERSCORE_RE = re.compile(
    r"(?<![A-Za-z0-9])_([^_\n]+?)_(?![A-Za-z0-9])"
)
_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+")
_QUOTE_RE = re.compile(r"(?m)^[ \t]{0,3}>[ \t]?")
_LIST_RE = re.compile(r"(?m)^[ \t]*[-*][ \t]+")


def _clean_prose(segment: str) -> str:
    segment = _BOLD_RE.sub(r"\1", segment)
    segment = _BOLD_UNDERSCORE_RE.sub(r"\1", segment)
    segment = _OS_ITALIC_RE.sub(r"\g<body>", segment)
    segment = _ITALIC_RE.sub(r"\1", segment)
    segment = _ITALIC_UNDERSCORE_RE.sub(r"\1", segment)
    segment = _HEADING_RE.sub("", segment)
    segment = _QUOTE_RE.sub("", segment)
    segment = _LIST_RE.sub("・", segment)
    return segment


def clean_markdown(text: str) -> str:
    """去掉微信/QQ 不渲染的 markdown 标记，代码内容原样保留。

    只删标记、不增删文字；清理后 strip 一次。
    """
    if not text:
        return text
    pieces: list[str] = []
    last = 0
    for match in _CODE_RE.finditer(text):
        pieces.append(_clean_prose(text[last : match.start()]))
        pieces.append(match.group(0))
        last = match.end()
    pieces.append(_clean_prose(text[last:]))
    return "".join(pieces).strip()


def shape_reply(
    text: str,
    max_chars: int = 120,
    max_chars_technical: int = 300,
    is_technical: bool = False,
) -> list[str]:
    text = clean_markdown(text.strip())
    if not text:
        return []
    limit = max_chars_technical if is_technical else max_chars
    chunks = [chunk for chunk in _split(_truncate(text, limit)) if chunk]
    return chunks


def _sentences(text: str) -> list[str]:
    """按深度 0 的句末边界切句；每段保持原文（允许 strip）。"""
    depths = _depths(text)
    parts: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in SEGMENT_BOUNDARIES and depths[index] == 0:
            piece = text[start : index + 1].strip()
            if piece:
                parts.append(piece)
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _units(sentences: list[str]) -> list[str]:
    """把内心 OS 段和紧跟的第一句绑成一个不可拆的单元。"""
    units: list[str] = []
    index = 0
    while index < len(sentences):
        sentence = sentences[index]
        if (
            sentence.lstrip().startswith(("（", "("))
            and index + 1 < len(sentences)
        ):
            units.append(sentence + sentences[index + 1])
            index += 2
        else:
            units.append(sentence)
            index += 1
    return units


def _partition(units: list[str], parts_count: int) -> list[str] | None:
    """在句子边界上分成 parts_count 段，段长尽量接近总长/段数。

    每段都不能短于 SEGMENT_MIN_PART_CHARS；无解时返回 None。
    """
    total = sum(len(unit) for unit in units)
    if parts_count < 2 or len(units) < parts_count:
        return None
    target = total / parts_count
    prefix = [0]
    for unit in units:
        prefix.append(prefix[-1] + len(unit))

    infinity = float("inf")
    size = len(units)
    costs = [[infinity] * (size + 1) for _ in range(parts_count + 1)]
    backs = [[-1] * (size + 1) for _ in range(parts_count + 1)]
    costs[0][0] = 0.0
    for part in range(1, parts_count + 1):
        for end in range(part, size + 1):
            for start in range(part - 1, end):
                if costs[part - 1][start] == infinity:
                    continue
                length = prefix[end] - prefix[start]
                if length < SEGMENT_MIN_PART_CHARS:
                    continue
                cost = costs[part - 1][start] + (length - target) ** 2
                if cost < costs[part][end]:
                    costs[part][end] = cost
                    backs[part][end] = start

    if costs[parts_count][size] == infinity:
        return None

    result: list[str] = []
    end = size
    for part in range(parts_count, 0, -1):
        start = backs[part][end]
        if start < 0:
            return None
        result.append("".join(units[start:end]))
        end = start
    result.reverse()
    return result


def segment_reply(text: str) -> list[str]:
    """把一段回复切成 1-3 条短消息：只在句末切，内容不增不减。

    太短、没有句末边界、或者切出来只剩一条时，原样返回一条。
    """
    body = text.strip()
    if not body:
        return []
    # 代码块/行内代码跨消息拆开就没法看了，整条发。
    if "`" in body:
        return [body]
    if len(body) < SEGMENT_MIN_CHARS:
        return [body]
    sentences = _sentences(body)
    if len(sentences) < 2:
        return [body]
    units = _units(sentences)

    parts: list[str] | None = None
    for count in range(min(SEGMENT_MAX_PARTS, len(units)), 1, -1):
        parts = _partition(units, count)
        if parts:
            break
    if not parts or len(parts) < 2:
        return [body]

    merged: list[str] = []
    for part in parts:
        if merged and len(part) < SEGMENT_MIN_PART_CHARS:
            merged[-1] += part
        else:
            merged.append(part)
    if merged and len(merged[0]) < SEGMENT_MIN_PART_CHARS and len(merged) > 1:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    if len(merged) < 2:
        return [body]
    return merged
