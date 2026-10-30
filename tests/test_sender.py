from dafeiyu.sender import is_technical_question, segment_reply, shape_reply


def test_short_reply_passes_through() -> None:
    text = "鱼" * 100
    assert shape_reply(text) == [text]


def test_empty_returns_no_chunks() -> None:
    assert shape_reply("   ") == []


def test_long_text_without_punctuation_is_hard_cut() -> None:
    chunks = shape_reply("鲸" * 200)
    assert len(chunks) == 1
    assert chunks[0].endswith("…")
    assert len(chunks[0]) <= 120


def test_long_text_cuts_at_last_sentence_end() -> None:
    text = "第一句话说完了。" + "继续说" * 40 + "。"
    chunks = shape_reply(text)
    assert chunks == ["第一句话说完了。"]


def test_technical_limit_is_300() -> None:
    text = "码" * 200
    chunks = shape_reply(text, is_technical=True)
    assert chunks == [text]


def test_very_long_text_is_split_into_500_char_chunks() -> None:
    text = ("段" * 250 + "\n") * 3 + "尾" * 49
    chunks = shape_reply(text, max_chars=1000)
    assert len(chunks) == 2
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(chunks) == text


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
