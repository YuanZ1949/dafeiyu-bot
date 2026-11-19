"""点名补丁的纯判定：纠正句绕过冷场 backoff、假称查过的检测。

两个函数都在 service.py 模块级，与 Service 解耦，方便离线回归：

- ``looks_like_correction``：bot 刚说完话时的短纠正句也算在跟它说话，
  否则 11-19 23:13「好像不是这个 是个德语名字」会被 silence_backoff 攒住；
- ``claims_search_without_tool``：回复声称「查过」但本轮没有检索工具调用，
  只做日志/决策流水标记，不改写要发出去的话。
"""

from dafeiyu.models import ChatType, Message
from dafeiyu.service import (
    CORRECTION_WINDOW_SECONDS,
    FRESH_LOOKUP_HINT,
    claims_search_without_tool,
    fresh_lookup_hint,
    looks_like_correction,
)


def _msg(content: str, *, is_bot: bool = False) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="qq:1",
        user_id="2",
        nickname="阿鱼",
        content=content,
        message_id="m1",
        segments=[],
        is_bot=is_bot,
        timestamp=0,
    )


def test_short_correction_right_after_bot_counts() -> None:
    assert looks_like_correction(
        "好像不是这个 是个德语名字", seconds_since_bot_spoke=20
    )


def test_correction_without_context_or_too_late_does_not_count() -> None:
    assert not looks_like_correction("好像不是这个", seconds_since_bot_spoke=None)
    assert not looks_like_correction(
        "好像不是这个", seconds_since_bot_spoke=CORRECTION_WINDOW_SECONDS + 1
    )


def test_long_message_is_not_a_quick_correction() -> None:
    assert not looks_like_correction(
        "不是这个 " + "啊" * 80, seconds_since_bot_spoke=10
    )


def test_plain_chat_without_correction_marker_does_not_count() -> None:
    assert not looks_like_correction("今天天气不错", seconds_since_bot_spoke=10)


def test_fresh_lookup_hint_for_asking_more_info() -> None:
    batch = [_msg("ANT上市前有个匿名模型反超了Fable  大肥鱼有没有更多信息")]
    assert fresh_lookup_hint(batch) == FRESH_LOOKUP_HINT


def test_no_hint_for_plain_chat_or_when_latest_message_moved_on() -> None:
    assert fresh_lookup_hint([_msg("今天天气不错")]) == ""
    assert fresh_lookup_hint([_msg("大肥鱼有没有更多信息"), _msg("算了不问了")]) == ""
    assert fresh_lookup_hint([_msg("有没有更多信息", is_bot=True)]) == ""


def test_claim_of_search_without_search_tool() -> None:
    assert claims_search_without_tool("我这边查了一圈，能查到实锤", [])
    assert claims_search_without_tool("我查过了", ["read"])


def test_claim_is_fine_when_search_tool_ran() -> None:
    assert not claims_search_without_tool("我查了一圈", ["websearch"])
    assert not claims_search_without_tool("查到 1463", ["webfetch"])


def test_negated_search_claim_is_not_a_claim() -> None:
    assert not claims_search_without_tool("这个本鱼没查到，凭印象说", [])
    assert not claims_search_without_tool("查不到可靠来源", [])
