"""气泡像素对齐：真实聊天字体下的验收（没有字体时跳过）。

样例取自微信本地库 ``message_0.db`` 的 ``Msg_709359d5…`` 表：这三条消息
按显示列补空格时，同一气泡内像素宽度差 52–172px，右边框明显错位。
"""

from __future__ import annotations

import pytest

from dafeiyu import mirror
from dafeiyu.textwidth import _missing_glyph, load_chat_metrics

REAL_SAMPLES = (
    ["𓁹酸菜鱼：完成了"],
    [
        "𓁹酸菜鱼：",
        "我记得当时有些分子进化树的概念",
        "我也是半懂不懂的  只知道用工具",
        "把图做出来",
    ],
    [
        "𓁹酸菜鱼：",
        "打算让AI研究一下当年自己写的论",
        "文 [图片]",
    ],
)


@pytest.fixture(scope="module")
def metrics():
    loaded = load_chat_metrics(16)
    if loaded is None:
        pytest.skip("没有可用的聊天字体（未安装雅黑 / PingFang / Noto CJK）")
    return loaded


@pytest.mark.parametrize("lines", REAL_SAMPLES)
def test_real_bubble_rows_align_within_one_pixel(metrics, lines) -> None:
    box = mirror._pixel_bubble(lines, "╭╮╰╯", metrics)
    assert box is not None
    rows = box.splitlines()
    assert rows[0].startswith("╭") and rows[0].endswith("╮")
    assert rows[-1].startswith("╰") and rows[-1].endswith("╯")
    assert all(row.startswith("│") and row.endswith("│") for row in rows[1:-1])
    widths = [metrics.width(row) for row in rows]
    assert max(widths) - min(widths) <= 1
    # 每行补白最多 2 个 U+200A；就算客户端把它吞成零宽，最坏偏差也只有 4px。
    assert all(row.count("\u200a") <= 2 for row in rows)
    collapsed = [metrics.width(row.replace("\u200a", "")) for row in rows]
    assert max(collapsed) - min(collapsed) <= 4


def test_emoji_width_uses_system_fallback(metrics) -> None:
    """主字体没有 emoji 字形时按系统回退字体度量，而不是 .notdef 的 11px。"""
    if not any(not _missing_glyph(font, "😏") for font in metrics.fallbacks):
        pytest.skip("没有带 emoji 字形的回退字体")
    assert metrics.width("😏") > 11


class _Metrics:
    def __init__(self, table: dict[str, int], default: int = 16) -> None:
        self._table = table
        self._default = default

    def width(self, text: str) -> int:
        return sum(self._table.get(ch, self._default) for ch in text)


def test_pixel_bubble_refuses_mismatched_corner_widths() -> None:
    """自定义四角宽度不一致时压不到同一像素宽度：退回按列对齐而不是硬凑。"""
    metrics = _Metrics({"╭": 10, "╮": 10, "╰": 10, "╯": 11})
    assert mirror._pixel_bubble(["你好"], "╭╮╰╯", metrics) is None


def test_pixel_bubble_refuses_zero_width_space_font() -> None:
    metrics = _Metrics({" ": 0, "─": 11, "│": 11, "╭": 10, "╮": 10, "╰": 10, "╯": 10})
    assert mirror._pixel_bubble(["你好"], "╭╮╰╯", metrics) is None
