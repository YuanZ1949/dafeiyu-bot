from dafeiyu.sender import (
    MAX_CHUNK,
    MAX_LONGFORM,
    MAX_PARTS,
    is_technical_question,
    segment_reply,
    shape_reply,
    shape_reply_parts,
)


def test_short_reply_passes_through() -> None:
    text = "鱼" * 100
    assert shape_reply(text) == [text]


def test_empty_returns_no_chunks() -> None:
    assert shape_reply("   ") == []


def test_soft_limit_no_longer_truncates() -> None:
    """120/300 只是提示词软目标：200 字无标点也不再被砍成 120。"""
    text = "鲸" * 200
    assert shape_reply(text) == [text]


def test_multi_sentence_reply_is_not_cut_at_soft_limit() -> None:
    text = "第一句话说完了。" + "继续说" * 40 + "。"
    assert shape_reply(text) == [text]


def test_technical_soft_limit_is_not_enforced() -> None:
    text = "码" * 200
    chunks = shape_reply(text, is_technical=True)
    assert chunks == [text]


def test_text_over_hard_part_is_split_at_500() -> None:
    text = "段" * 1200
    chunks = shape_reply(text)
    assert len(chunks) == 3
    assert all(len(c) <= MAX_CHUNK for c in chunks)
    assert "".join(chunks) == text


def test_model_newlines_become_message_boundaries() -> None:
    first = "结论是这个方案能跑，配置和依赖都没问题。"
    second = "原因有两个：开关没打开，网络也不稳。"
    assert shape_reply(first + "\n" + second) == [first, second]


def test_newline_heavy_string_reply_capped_at_max_parts() -> None:
    """换行多的字符串回复也必须守在 MAX_PARTS 以内，不能炸成几十条群消息。"""
    text = "\n".join(f"第{i}句很短。" for i in range(30))
    chunks = shape_reply(text)
    assert len(chunks) <= MAX_PARTS
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_long_string_reply_still_splits_by_paragraph() -> None:
    """不到上限时按原文换行分段，不能被过度合并成 1 条。"""
    text = (
        "第一段讲结论，这个方案今天就能上线。\n"
        "\n"
        "第二段讲原因，开关没打开而且网络不稳。\n"
        "\n"
        "第三段讲后续，明天再补一次回归测试。"
    )
    chunks = shape_reply(text)
    assert len(chunks) == 3
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_code_fence_is_not_split_across_messages() -> None:
    text = (
        "先看这段：\n"
        "```python\nprint('a。b')\nprint('c。d')\n```\n"
        "改完再跑。"
    )
    chunks = shape_reply(text)
    code = [chunk for chunk in chunks if "print" in chunk]
    assert len(code) == 1
    assert "print('a。b')\nprint('c。d')" in code[0]
    assert code[0].rstrip().endswith("```")


def test_shape_reply_parts_keeps_model_boundaries() -> None:
    assert shape_reply_parts(["第一条", "第二条", "   "]) == ["第一条", "第二条"]


def test_shape_reply_parts_ignores_non_strings() -> None:
    assert shape_reply_parts(["甲", 1, None, "乙"]) == ["甲", "乙"]  # type: ignore[list-item]


def test_shape_reply_parts_holds_hard_total() -> None:
    parts = ["鱼" * 600, "鲸" * 600, "虾" * 1200]
    chunks = shape_reply_parts(parts)
    assert len(chunks) > 1
    assert sum(len(chunk) for chunk in chunks) <= MAX_LONGFORM


def test_shape_reply_parts_merges_over_cap_without_losing_text() -> None:
    parts = [f"第{i}句。" for i in range(MAX_PARTS + 5)]
    chunks = shape_reply_parts(parts)
    assert len(chunks) == MAX_PARTS
    assert "".join(chunks).replace("\n", "") == "".join(parts)


def test_shape_reply_parts_merge_still_respects_hard_part() -> None:
    """极端分布（20 条 1 字 + 1 条长文）合并后也不能超单条上限。"""
    parts = ["鱼"] * 20 + ["鲸" * 1000]
    chunks = shape_reply_parts(parts)
    assert all(len(chunk) <= MAX_CHUNK for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == "".join(parts)


def test_longform_keeps_source_list_tail() -> None:
    """被点名要来源时不砍尾：链接和最后一条都得发出去。"""
    text = (
        "来了来了，本鱼的出处都在这，自己看：第一条是台积电最新季度财报，"
        "营收和毛利率都写得很清楚，管理层还给了下季度指引。"
        "第二条是费城半导体指数的成分股调整，从权重变化能看出资金往哪边走。"
        "第三条是两家券商对明年资本开支的判断，分歧主要落在存储和先进制程上，"
        "链接 https://example.com/semi 里有原文。"
    )
    assert len(text) > 120
    chunks = shape_reply(text, allow_longform=True)
    assert "".join(chunks) == text
    assert text.endswith("原文。")
    assert all(not chunk.endswith("…") for chunk in chunks)


def test_longform_still_capped_at_safety_limit() -> None:
    text = "鱼" * (MAX_LONGFORM + 100)
    chunks = shape_reply(text, allow_longform=True)
    joined = "".join(chunks)
    assert len(joined) <= MAX_LONGFORM
    assert joined.endswith("…")


def test_allow_longform_no_longer_changes_result() -> None:
    """旧参数保留兼容，但新策略下默认路径与 allow_longform 已一致。"""
    text = "第一句话说完了。" + "继续说" * 40 + "。"
    assert shape_reply(text) == [text]
    assert shape_reply(text, allow_longform=True) == [text]


def test_is_technical_question_detects_code_questions() -> None:
    assert is_technical_question("这段代码为什么报错？")
    assert is_technical_question("接口返回 500 怎么办")
    assert not is_technical_question("今天中午吃什么")


def test_segment_short_reply_stays_single() -> None:
    assert segment_reply("在呢，刚睡醒。") == ["在呢，刚睡醒。"]


def test_segment_empty_returns_no_parts() -> None:
    assert segment_reply("   ") == []


def test_segment_splits_two_sentences() -> None:
    text = (
        "结论是这个方案能跑，配置和依赖都没问题，今天就可以直接上线。"
        "原因有两个，一个是开关没打开，一个是网络不稳，这两点都确认过了。"
    )
    parts = segment_reply(text)
    assert len(parts) == 2
    assert "".join(parts) == text


def test_segment_caps_at_three_parts() -> None:
    text = "第一句。" * 20
    parts = segment_reply(text)
    assert len(parts) == 3
    assert "".join(parts) == text


def test_segment_merges_tiny_tail() -> None:
    text = (
        "这是一段足够长的开头句子，用来触发拆分逻辑，后面还有内容。"
        "嗯。"
        "最后再补一句正常长度的话，把这件事收个尾，别留半句，真的别留。"
    )
    parts = segment_reply(text)
    assert len(parts) == 2
    assert all(len(part) >= 8 for part in parts)
    assert "".join(parts) == text


def test_segment_splits_on_newline_and_drops_it() -> None:
    first = "结论是这个方案能跑，配置和依赖都没问题，今天就可以直接上线，不用再等。"
    second = "原因有两个，一个是开关没打开，一个是网络不稳，这两点都已经确认过了，放心。"
    parts = segment_reply(first + "\n" + second)
    assert parts == [first, second]


def test_segment_keeps_single_long_text_without_boundaries() -> None:
    text = "鱼" * 100
    assert segment_reply(text) == [text]


def test_segment_keeps_code_block_in_one_message() -> None:
    text = (
        "先看这段，问题在配置里。\n"
        "```python\nprint('hello。world')\nprint('again。')\n```\n"
        "把引号里的内容改掉再跑一次，应该就好了。"
    )
    assert segment_reply(text) == [text]
