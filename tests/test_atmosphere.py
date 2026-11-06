"""群气氛（P3）：确定性统计，不调模型。"""

from __future__ import annotations

from dafeiyu.memory.atmosphere import describe_atmosphere
from dafeiyu.models import ChatType, Message

NOW = 1_800_000_000.0
GROUP = "qq:1001"


def make_msg(
    content: str,
    *,
    user_id: str = "qq:1",
    seconds_ago: int = 60,
    is_bot: bool = False,
    timestamp: int | None = None,
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=GROUP,
        user_id=user_id,
        nickname=user_id,
        content=content,
        message_id=f"m-{content}-{user_id}-{seconds_ago}",
        is_bot=is_bot,
        timestamp=int(NOW - seconds_ago) if timestamp is None else timestamp,
    )


def test_empty_or_bot_only_returns_empty() -> None:
    assert describe_atmosphere([], now_ts=NOW) == ""
    assert describe_atmosphere([make_msg("嗯", is_bot=True)], now_ts=NOW) == ""


def test_busy_group_reports_heat_and_crowd() -> None:
    messages = [
        make_msg("为什么？", user_id=f"qq:{index % 5}", seconds_ago=120 - index * 5)
        for index in range(16)
    ]

    text = describe_atmosphere(messages, now_ts=NOW)

    assert "消息刷得很快" in text
    assert "好几个人在场" in text
    assert "问句多" in text


def test_short_burst_is_detected() -> None:
    messages = [
        make_msg("哈哈", user_id=f"qq:{index % 3}", seconds_ago=60 - index)
        for index in range(6)
    ]

    text = describe_atmosphere(messages, now_ts=NOW)

    assert "短句连刷" in text


def test_quiet_group_reports_quiet() -> None:
    messages = [make_msg("在", user_id="qq:1", seconds_ago=90)]

    assert describe_atmosphere(messages, now_ts=NOW) == "比较安静"


def test_long_silence_is_reported() -> None:
    messages = [make_msg("睡觉了", user_id="qq:1", seconds_ago=45 * 60)]

    text = describe_atmosphere(messages, now_ts=NOW)

    assert "已经安静 45 分钟" in text


def test_messages_without_timestamps_still_work() -> None:
    messages = [
        make_msg("在吗", user_id=f"qq:{index}", timestamp=0) for index in range(4)
    ]

    text = describe_atmosphere(messages, now_ts=NOW)

    assert "有人零星在聊" in text
    assert "已经安静" not in text
