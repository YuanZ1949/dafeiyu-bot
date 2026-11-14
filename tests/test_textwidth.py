"""T4：display_width / wrap_by_width —— 只用 stdlib 的显示宽度工具。"""

from __future__ import annotations

from dafeiyu.textwidth import display_width, pad_to_width, wrap_by_width


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
