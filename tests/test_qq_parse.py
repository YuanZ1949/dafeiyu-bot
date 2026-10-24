from fixtures.onebot_events import at, face, group_event, image, private_event, reply, text

from dafeiyu.gateway.qq import parse_event
from dafeiyu.models import ChatType

BOT_QQ = 10001


def test_group_text_message() -> None:
    event = group_event(segments=[text("大家好")], card="群名片")
    m = parse_event(event, bot_qq=BOT_QQ)
    assert m is not None
    assert m.chat_type is ChatType.GROUP
    assert m.chat_id == 30003
    assert m.user_id == 20002
    assert m.nickname == "群名片"
    assert m.content == "大家好"
    assert m.message_id == "40004"
    assert m.at_bot is False
    assert m.reply_to_bot is False
    assert m.is_bot is False


def test_at_bot_sets_flag() -> None:
    m = parse_event(group_event(segments=[at(BOT_QQ), text("你好")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.at_bot is True
    assert "你好" in m.content


def test_at_all_is_not_at_bot() -> None:
    m = parse_event(group_event(segments=[at("all"), text("在吗")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.at_bot is False
    assert m.content == "在吗"


def test_reply_to_bot_sets_flag() -> None:
    event = group_event(segments=[reply("777"), text("这个怎么弄")])
    m = parse_event(event, bot_qq=BOT_QQ, bot_message_ids={"777"})
    assert m is not None
    assert m.reply_to_bot is True
    assert m.at_bot is False


def test_reply_to_other_is_not_reply_to_bot() -> None:
    event = group_event(segments=[reply("888"), text("这个怎么弄")])
    m = parse_event(event, bot_qq=BOT_QQ, bot_message_ids={"777"})
    assert m is not None
    assert m.reply_to_bot is False


def test_private_message() -> None:
    m = parse_event(private_event(segments=[text("在吗")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.chat_type is ChatType.PRIVATE
    assert m.chat_id == 20002
    assert m.content == "在吗"


def test_non_message_event_returns_none() -> None:
    assert parse_event({"post_type": "notice", "notice_type": "group_increase"}, bot_qq=BOT_QQ) is None


def test_mixed_segments_render_placeholders() -> None:
    event = group_event(segments=[text("文字"), image(), face()])
    m = parse_event(event, bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "文字[图片][表情]"


def test_unknown_segment_does_not_raise() -> None:
    event = group_event(segments=[{"type": "mystery", "data": {}}, text("还在")])
    m = parse_event(event, bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "还在"


def test_plain_string_message_is_used_as_content() -> None:
    event = group_event(segments=[])
    event["message"] = "纯文本"
    m = parse_event(event, bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "纯文本"


def test_bot_own_message_flagged() -> None:
    event = group_event(segments=[text("我自己")], user_id=BOT_QQ)
    m = parse_event(event, bot_qq=BOT_QQ)
    assert m is not None
    assert m.is_bot is True


def test_to_prompt_line() -> None:
    m = parse_event(group_event(segments=[text("你好")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.to_prompt_line() == "鱼片: 你好"
