"""摘要正文清洗与截断：写入/注入共用同一套规则。"""

from __future__ import annotations

from dafeiyu.memory.summary_text import clean_summary, clip_summary

# 线上真实污染样本（10-31 ~ 11-05 的日摘要开头）。
META_STEP_LIMIT = "本任务已达到最大步数上限，无法再调用工具；以下为已完成内容，可直接作为摘要使用：\n\n"

BODY = "梦幻天使先喊外号「大肥鱼」，Deepseek娘应声，随后聊到显卡和驱动。"


def test_clean_summary_drops_step_limit_preamble() -> None:
    assert clean_summary(META_STEP_LIMIT + BODY) == BODY


def test_clean_summary_drops_no_tool_preamble() -> None:
    text = "未使用任何工具，直接基于给定聊天记录完成整理；无需读取图片文件也可以完成摘要。\n\n摘要正文：\n\n" + BODY

    assert clean_summary(text) == BODY


def test_clean_summary_keeps_plain_body() -> None:
    assert clean_summary(BODY) == BODY


def test_clean_summary_returns_empty_for_blank() -> None:
    assert clean_summary("   ") == ""


def test_clean_summary_keeps_original_when_cleaning_would_gut_it() -> None:
    """清洗后太短就退回原文：宁可留着元文本，也不能把摘要清成空串。"""
    text = "已达到最大步数上限。"

    assert clean_summary(text) == text


def test_clean_summary_does_not_touch_meta_words_in_body() -> None:
    """「工具」「步数」出现在正文中段时不算元叙述，不能误删。"""
    body = "大家在讨论最大步数这个概念：" + "很长的正文" * 20

    assert clean_summary(body) == body


def test_clip_summary_marks_ellipsis_at_sentence_boundary() -> None:
    text = "甲" * 90 + "。" + "乙" * 100

    out = clip_summary(text, 120)

    assert out.endswith("…")
    assert out.startswith("甲" * 90 + "。")


def test_clip_summary_hard_cut_keeps_limit_and_marks_ellipsis() -> None:
    out = clip_summary("鱼" * 250, 200)

    assert len(out) == 200
    assert out.endswith("…")


def test_clip_summary_short_text_is_untouched() -> None:
    assert clip_summary("很短。", 200) == "很短。"


def test_clip_summary_zero_limit_returns_empty() -> None:
    assert clip_summary("x" * 10, 0) == ""


def test_clip_summary_never_exceeds_limit() -> None:
    """断点正好落在 limit-1 时也不能超出预算；三条路径都要守住 limit。"""
    cases = [
        ("123456789。" + "尾巴", 10),  # 句号正好在第 10 位，截断后不能再加出第 11 字
        ("很长的一句话。", 1),  # limit=1
        ("鱼" * 20, 5),  # 无标点，硬截断
    ]

    for text, limit in cases:
        assert len(clip_summary(text, limit)) <= limit
