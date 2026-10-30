"""把模型回复整形为适合群聊的短句与分片。"""

from __future__ import annotations

SPLIT_AT = "。！？!?；;\n"
MAX_CHUNK = 500

# 拆成多条短消息：只有够长且真有多个句子才拆，避免把一句话拆碎。
SEGMENT_BOUNDARIES = "。！？!?…\n"
SEGMENT_MIN_CHARS = 60
SEGMENT_MAX_PARTS = 3
SEGMENT_MIN_PART_CHARS = 8

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


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(ch) for ch in SPLIT_AT)
    if cut > 0:
        return window[: cut + 1]
    return window[: limit - 1].rstrip() + "…"


def _split(text: str, size: int = MAX_CHUNK) -> list[str]:
    chunks: list[str] = []
    rest = text
    while len(rest) > size:
        window = rest[:size]
        cut = max(window.rfind(ch) for ch in SPLIT_AT)
        if cut < size * 4 // 5:
            cut = size - 1
        chunks.append(rest[: cut + 1])
        rest = rest[cut + 1 :]
    if rest:
        chunks.append(rest)
    return chunks


def shape_reply(
    text: str,
    max_chars: int = 120,
    max_chars_technical: int = 300,
    is_technical: bool = False,
) -> list[str]:
    text = text.strip()
    if not text:
        return []
    limit = max_chars_technical if is_technical else max_chars
    return _split(_truncate(text, limit))


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in SEGMENT_BOUNDARIES:
            piece = text[start : index + 1].strip()
            if piece:
                parts.append(piece)
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


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

    target = -(-len(body) // min(SEGMENT_MAX_PARTS, len(sentences)))
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        too_long = current and len(current) + len(sentence) > target
        if too_long and len(parts) < SEGMENT_MAX_PARTS - 1:
            parts.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        parts.append(current)

    merged: list[str] = []
    for part in parts:
        if merged and len(part) < SEGMENT_MIN_PART_CHARS:
            merged[-1] += part
        else:
            merged.append(part)
    if len(merged) < 2:
        return [body]
    return merged
