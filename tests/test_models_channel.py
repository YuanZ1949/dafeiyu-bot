import pytest

from dafeiyu.models import ChatType, Message, channel_of

MSG_KWARGS = {
    "chat_type": ChatType.GROUP,
    "user_id": "456",
    "nickname": "测试",
    "content": "你好",
    "message_id": "m-1",
}


def _message(chat_id: str) -> Message:
    return Message(chat_id=chat_id, **MSG_KWARGS)


def test_channel_of_qq_prefix():
    assert channel_of("qq:123") == "qq"
    assert channel_of("qq:123:456") == "qq"


def test_channel_of_wechat_prefix():
    assert channel_of("wxg:abc") == "wechat"


def test_channel_of_rejects_unprefixed_id():
    with pytest.raises(ValueError):
        channel_of("123")


def test_channel_of_rejects_empty_id():
    with pytest.raises(ValueError):
        channel_of("")


def test_channel_of_rejects_bare_prefixes():
    with pytest.raises(ValueError):
        channel_of("qq:")
    with pytest.raises(ValueError):
        channel_of("wxg:")


def test_message_channel_derives_from_chat_id_prefix():
    assert _message("qq:123").channel == "qq"
    assert _message("wxg:abc").channel == "wechat"


def test_message_channel_matches_channel_of():
    # 行为断言：Message.channel 与直接调用 channel_of 一致（委托单一派生点）。
    for chat_id in ("qq:123", "wxg:abc"):
        msg = _message(chat_id)
        assert msg.channel == channel_of(msg.chat_id)


def test_message_channel_raises_for_unprefixed_id():
    msg = _message("123")
    with pytest.raises(ValueError):
        _ = msg.channel


def test_channel_is_not_a_persisted_dataclass_field():
    assert "channel" not in Message.__dataclass_fields__


def test_channel_not_serialized_in_asdict():
    import dataclasses

    assert "channel" not in dataclasses.asdict(_message("qq:123"))
