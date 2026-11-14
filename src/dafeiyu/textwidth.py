"""按终端显示宽度处理文本（镜像消息气泡的对齐用）。

只用 stdlib ``unicodedata``：东亚宽字符（W/F）记 2 列、组合字符记 0 列、
其余记 1 列；不引入 wcwidth 等第三方包。
"""

from __future__ import annotations

import unicodedata

# 断行优先选在这些标点之后，避免下一行以标点开头。
_BREAK_AFTER = frozenset("，。！？；：、）】》」』…,.;:!?)]}>")


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

    已有的换行先按行拆，各自包宽；每行尽量在标点后断，找不到就硬切。
    空行保留为空串，``width <= 0`` 时不切。
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
                carried = _break_back(current)
                lines.append("".join(current[: len(current) - len(carried)]))
                current = carried
                cols = sum(display_width(part) for part in current)
            current.append(cluster)
            cols += w
        lines.append("".join(current))
    return lines


def pad_to_width(text: str, width: int) -> str:
    """用空格把 ``text`` 右侧补到给定显示宽度（宽度不足时不动）。"""
    return text + " " * (width - display_width(text))


def _clusters(segment: str) -> list[str]:
    """把 ``segment`` 切成不可拆的字符簇：基字 + 跟随的组合字符。"""
    clusters: list[str] = []
    for ch in segment:
        if unicodedata.combining(ch) and clusters:
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def _break_back(clusters: list[str]) -> list[str]:
    """把最后一个可断标点之后的簇挪到下一行；没有就硬切（返回空）。"""
    for index in range(len(clusters) - 1, 0, -1):
        if clusters[index - 1] in _BREAK_AFTER:
            return clusters[index:]
    return []
