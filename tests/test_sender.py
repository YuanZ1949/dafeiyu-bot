from dafeiyu.sender import is_technical_question, shape_reply


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
