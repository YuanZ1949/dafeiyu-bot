"""T4：display_width / wrap_by_width —— 只用 stdlib 的显示宽度工具。"""

from __future__ import annotations

from dafeiyu.textwidth import (
    PixelMetrics,
    display_width,
    pad_to_width,
    wrap_by_width,
)


def test_display_width_pure_chinese_is_two_columns_per_char() -> None:
    assert display_width("你好") == 4
    assert display_width("字" * 800) == 1600


def test_display_width_pure_ascii_is_one_column_per_char() -> None:
    assert display_width("hello") == 5
    assert display_width("42") == 2


def test_display_width_mixed_scripts() -> None:
    assert display_width("ab好c") == 5
    assert display_width("试试 hello 42 世界") == 18


def test_display_width_fullwidth_punctuation_counts_two() -> None:
    assert display_width("！") == 2
    assert display_width("：") == 2
    assert display_width("，。") == 4


def test_display_width_combining_marks_count_zero() -> None:
    # e + U+0301（组合尖音符）占 1 列；组合字符本身记 0。
    assert display_width("e\u0301") == 1
    assert display_width("\u0301") == 0


def test_display_width_empty_string() -> None:
    assert display_width("") == 0


def test_wrap_by_width_respects_display_width() -> None:
    lines = wrap_by_width("字" * 20, 12)
    assert lines == ["字" * 6, "字" * 6, "字" * 6, "字" * 2]
    assert all(display_width(line) <= 12 for line in lines)


def test_wrap_by_width_never_splits_combining_marks() -> None:
    text = "e\u0301" * 12  # 每个基字 + 组合字符占 1 列
    lines = wrap_by_width(text, 4)
    assert lines == ["e\u0301" * 4] * 3
    # 组合字符不与基字拆开：没有行以 U+0301 开头。
    assert all(not line.startswith("\u0301") for line in lines)


def test_wrap_by_width_breaks_after_punctuation_when_possible() -> None:
    text = "好好好，哈哈哈"  # 宽度 2+2+2+2+2+2+2 = 14
    lines = wrap_by_width(text, 8)
    # 第 4 个字会超宽：优先在「，」之后断行。
    assert lines == ["好好好，", "哈哈哈"]


def test_wrap_by_width_keeps_existing_newlines_and_blank_lines() -> None:
    lines = wrap_by_width("ab\ncd\n", 10)
    assert lines == ["ab", "cd", ""]


def test_wrap_by_width_width_zero_returns_text_verbatim() -> None:
    assert wrap_by_width("任意\n文本", 0) == ["任意\n文本"]


def test_pad_to_width_pads_with_spaces_to_display_width() -> None:
    assert pad_to_width("好", 4) == "好  "
    assert pad_to_width("ab", 4) == "ab  "
    assert pad_to_width("好", 2) == "好"
    assert display_width(pad_to_width("好 c", 6)) == 6


# ------------------------------------------------------- 真实字体像素度量


class _FakeMask:
    def __init__(self, payload: bytes) -> None:
        self.size = (8, 8)
        self._payload = payload

    def __bytes__(self) -> bytes:
        return self._payload


class _FakeFont:
    """只实现 getlength / getmask：宽度表里有的算有字形，否则是 .notdef。"""

    def __init__(self, widths: dict[str, int], notdef: int = 11) -> None:
        self._widths = widths
        self._notdef = notdef

    def getlength(self, text: str) -> float:
        return float(sum(self._widths.get(ch, self._notdef) for ch in text))

    def getmask(self, ch: str) -> _FakeMask:
        if ch in self._widths:
            return _FakeMask(b"glyph")
        return _FakeMask(b"notdef")


def test_pixel_metrics_measures_with_primary_font() -> None:
    metrics = PixelMetrics(_FakeFont({"a": 4, "阿": 16}))
    assert metrics.width("a阿") == 20
    assert metrics.width("") == 0


def test_pixel_metrics_corrects_missing_glyph_with_fallback() -> None:
    primary = _FakeFont({"a": 4})
    emoji = _FakeFont({"😏": 22}, notdef=10)
    metrics = PixelMetrics(primary, (emoji,))
    # 主字体按 .notdef 11px 计，再修正成回退字体的 22px。
    assert metrics.width("a😏") == 4 + 22


def test_pixel_metrics_corrects_every_occurrence() -> None:
    """同一缺字字符出现多次时，每次都要修正（不是每个字符只改一次）。"""
    primary = _FakeFont({"a": 4})
    emoji = _FakeFont({"😏": 22}, notdef=10)
    metrics = PixelMetrics(primary, (emoji,))
    assert metrics.width("😏😏") == 44


def test_pixel_metrics_uses_first_fallback_that_has_the_glyph() -> None:
    primary = _FakeFont({})
    empty = _FakeFont({})
    symbol = _FakeFont({"𓁹": 16}, notdef=10)
    metrics = PixelMetrics(primary, (empty, symbol))
    assert metrics.width("𓁹") == 16


def test_pixel_metrics_ignores_variation_selectors() -> None:
    metrics = PixelMetrics(_FakeFont({"a": 4}))
    # U+FE0F 是变体选择符：不占宽度，也不触发缺字回退。
    assert metrics.width("a\ufe0f") == 4
