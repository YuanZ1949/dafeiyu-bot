"""文本宽度：按显示列换行（纯 stdlib）。

``display_width`` / ``wrap_by_width`` 只用 stdlib ``unicodedata``：东亚宽字符
（W/F）记 2 列、组合字符记 0 列、其余记 1 列；不引入 wcwidth 等第三方包。

气泡不再做像素级对齐（原因见 ``mirror._bubble``）：闭合框在任何一种字体比例
下都对不齐，所以这里也不再需要真实字体度量。
"""

from __future__ import annotations

import unicodedata

# 断行优先选在这些标点之后，避免下一行以标点开头。
_BREAK_AFTER = frozenset("，。！？；：、）】》」』…,.;:!?)]}>")
#: ASCII 词元里常见的符号：与字母数字一起构成不可切开的整体（URL、时间）。
_TOKEN_CHARS = frozenset(":/._-%#?=&@+~")
#: 允许在其后断行的空白（含全角空格）。
_SPACES = frozenset((" ", "\u3000"))


def display_width(s: str) -> int:
    """字符串在等宽终端里的显示列数（东亚宽 2、组合字符 0、其余 1）。"""
    total = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            total += 2
        else:
            total += 1
    return total


def wrap_by_width(text: str, width: int) -> list[str]:
    """按显示宽度把 ``text`` 切成若干行；组合字符不与基字拆开。

    已有的换行先按行拆，各自包宽。断点优先级（从高到低）：

    1. 最后一个可断标点之后（``，。！？`` 等，避免下一行以标点开头）；
    2. 最后一个空白之后；
    3. 任意不切开 ASCII 词元的位置（``6:45``、``https://x`` 这类不被腰斩）；
    4. 硬切（整行就是一个超长词元时只能如此）。

    ``"".join(wrap_by_width(text, width))`` 能还原原文（只把行尾空白留在上一行，
    不丢字符）。空行保留为空串，``width <= 0`` 时不切。
    """
    if width <= 0:
        return [text]
    lines: list[str] = []
    for segment in text.split("\n"):
        current: list[str] = []
        cols = 0
        for cluster in _clusters(segment):
            w = display_width(cluster)
            if current and cols + w > width:
                carried = _break_back(current, cluster)
                lines.append("".join(current[: len(current) - len(carried)]))
                current = carried
                cols = sum(display_width(part) for part in current)
            current.append(cluster)
            cols += w
        lines.append("".join(current))
    return lines


def _clusters(segment: str) -> list[str]:
    """把 ``segment`` 切成不可拆的字符簇：基字 + 跟随的组合字符。"""
    clusters: list[str] = []
    for ch in segment:
        if unicodedata.combining(ch) and clusters:
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def _is_token_char(ch: str) -> bool:
    """ASCII 字母数字或 URL/时间里常见的符号：切开它们就是把人家的词腰斩。"""
    return ch.isascii() and (ch.isalnum() or ch in _TOKEN_CHARS)


def _splits_token(prev: str, nxt: str) -> bool:
    """在这里断行会不会把一个 ASCII 词元腰斩。"""
    return _is_token_char(prev) and _is_token_char(nxt)


def _break_back(clusters: list[str], pending: str) -> list[str]:
    """按优先级选断点，返回要挪到下一行的簇；返回空表示就地硬切。

    ``pending`` 是当前行装不下、即将进入下一行的那个簇。只有两种情况需要
    回退：下一行会以标点开头（``pending`` 本身是可断标点），或溢出点会把
    ``6:45`` / URL 这类 ASCII 词元腰斩。其余情况直接切（贪心填满整行）。
    回退顺序：标点后 → 空白后 → 任意词元边界；整行就是一个超长词元时只能硬切。
    """
    if pending not in _BREAK_AFTER and not _splits_token(clusters[-1], pending):
        return []
    for index in range(len(clusters) - 1, 0, -1):
        if clusters[index - 1] in _BREAK_AFTER and not _splits_token(
            clusters[index - 1], clusters[index]
        ):
            return clusters[index:]
    for index in range(len(clusters) - 1, 0, -1):
        if clusters[index - 1] in _SPACES and not _splits_token(
            clusters[index - 1], clusters[index]
        ):
            return clusters[index:]
    for index in range(len(clusters) - 1, 0, -1):
        if not _splits_token(clusters[index - 1], clusters[index]):
            return clusters[index:]
    return []
