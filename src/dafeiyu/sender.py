"""把模型回复整形为适合群聊的短句与分片。"""

from __future__ import annotations

SPLIT_AT = "。！？!?；;\n"
MAX_CHUNK = 500

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
