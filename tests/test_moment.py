"""接话时机信号：问句识别、冷场/纯附和惩罚、隐式接话。"""

from __future__ import annotations

from dafeiyu.models import ChatType, Message
from dafeiyu.moment import (
    analyze,
    answers_bot_question,
    follows_bot_expression,
    is_question,
    is_short_only,
    is_sticker_content,
    reopened_by_question,
    requests_continuation,
)


def make_msg(
    content: str,
    *,
    message_id: str = "m1",
    is_bot: bool = False,
    timestamp: int = 1000,
    nickname: str = "阿猫",
    user_id: int = 1,
    at_bot: bool = False,
    reply_to_bot: bool = False,
    reply_to_message_id: str | None = None,
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=1,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=message_id,
        is_bot=is_bot,
        timestamp=timestamp,
        at_bot=at_bot,
        reply_to_bot=reply_to_bot,
        reply_to_message_id=reply_to_message_id,
    )


def test_question_detection() -> None:
    assert is_question("这个怎么弄？")
    assert is_question("为什么又崩了")
    assert is_question("有没有人知道 traceback 怎么读")
    assert not is_question("今天天气不错")
    assert reopened_by_question("求个链接")


def test_short_only_detection() -> None:
    assert is_short_only([make_msg("哈哈"), make_msg("草")])
    assert is_short_only([make_msg("[图片]")])
    assert not is_short_only([make_msg("这个方案我觉得可以，但得先确认依赖")])


def test_sticker_content_detection() -> None:
    assert is_sticker_content("[表情]")
    assert is_sticker_content("[表情:狗头]")
    assert is_sticker_content("[表情:狗头]（图存 images/dog.png，可用 read 查看）")
    # 表情后面还跟着别的内容，就不是单纯一个表情了。
    assert not is_sticker_content("[表情][图片]")
    assert not is_sticker_content("哈哈")
    assert not is_sticker_content("[表情:狗头] 你也来一个")


def test_short_only_sticker_hint_is_soft() -> None:
    signals = analyze([make_msg("[表情:狗头]")], now_ts=1000)
    assert signals.short_only is True
    assert signals.sticker is True
    assert "有同感" in signals.hint()
    assert "倾向不说" not in signals.hint()


def test_short_only_without_sticker_keeps_old_hint() -> None:
    signals = analyze([make_msg("哈哈")], now_ts=1000)
    assert signals.short_only is True
    assert signals.sticker is False
    assert signals.hint() == "【时机】这批没什么可接的内容，倾向不说。"


def test_follows_bot_expression() -> None:
    bot = make_msg("发个图给你看看", is_bot=True, timestamp=1000, nickname="溟月")
    sticker = make_msg("[表情:狗头]", timestamp=1005)

    assert follows_bot_expression(sticker, bot) is True
    # 上一条是人、或者隔得太久，都只当普通刷屏。
    assert follows_bot_expression(sticker, make_msg("哈哈", timestamp=1000)) is False
    assert follows_bot_expression(
        make_msg("[表情:狗头]", timestamp=2000), bot
    ) is False


def test_analyze_question_scores_higher() -> None:
    question = analyze([make_msg("这个报错怎么处理？")], now_ts=1000)
    chatter = analyze([make_msg("今天天气不错")], now_ts=1000)
    assert question.question is True
    assert question.score > chatter.score
    assert "提问" in question.hint()


def test_analyze_hot_chat_penalized_without_question() -> None:
    messages = [
        make_msg(f"这是第{i}条比较长的聊天内容", message_id=f"m{i}")
        for i in range(6)
    ]
    signals = analyze(messages, now_ts=1000)
    assert signals.hot is True
    assert signals.score < 0
    assert "聊得热" in signals.hint()


def test_analyze_addressed_never_penalized_to_silence() -> None:
    messages = [make_msg("哈哈", at_bot=True)]
    signals = analyze(messages, now_ts=1000, addressed=True)
    assert signals.score > 0
    assert "接了你上一句" not in signals.hint()


def test_analyze_followed_up_hint() -> None:
    signals = analyze([make_msg("4060")], now_ts=1000, followed_up=True)
    assert signals.followed_up is True
    assert "接了你上一句" in signals.hint()
    assert signals.worth_reopen is True


def test_answers_bot_question_only_for_direct_short_reply() -> None:
    bot = make_msg("你用的什么卡？", is_bot=True, timestamp=1000, nickname="溟月")
    user = make_msg("4060", timestamp=1005)

    assert answers_bot_question(user, bot) is True
    # 中间隔太久、或者上一句不是问句，都不算隐式接话。
    assert answers_bot_question(make_msg("4060", timestamp=2000), bot) is False
    assert answers_bot_question(user, make_msg("在吗", is_bot=True, timestamp=1000)) is False
    # 长回答不再当隐式接话：更像是在跟别人聊。
    long = make_msg(
        "这个卡是 4060 但我觉得驱动也得换一下，而且电源可能也不太够，"
        "最好先看看整机功耗再决定要不要升级",
        timestamp=1005,
    )
    assert answers_bot_question(long, bot) is False


def test_requests_continuation_recognizes_short_followups() -> None:
    for text in ("继续说", "接着说", "然后呢", "还有呢", "继续说下去", "别停"):
        assert requests_continuation(text) is True, text


def test_requests_continuation_ignores_long_or_unrelated_text() -> None:
    assert requests_continuation("今天中午吃什么") is False
    assert requests_continuation("") is False
    # 长文里只是顺带出现「继续」不算在催它接着说。
    assert requests_continuation("我继续觉得这个方案不太行，原因是成本太高了") is False
