import dataclasses
from typing import Any

import pytest

from dafeiyu.models import Attachment, ChatType, Message

MSG_KWARGS: dict[str, Any] = {
    "chat_type": ChatType.GROUP,
    "chat_id": "qq:123",
    "user_id": "456",
    "nickname": "测试",
    "content": "你好",
    "message_id": "m-1",
}


def test_legacy_construction_defaults_to_text_and_empty_attachments():
    msg = Message(**MSG_KWARGS)
    assert msg.kind == "text"
    assert msg.attachments == []


def test_attachment_paths_filters_empty_paths_and_preserves_order():
    msg = Message(
        **MSG_KWARGS,
        attachments=[
            Attachment(kind="image"),
            Attachment(kind="image", path="b.png"),
            Attachment(kind="file"),
            Attachment(kind="image", path="a.png"),
        ],
    )
    assert msg.attachment_paths() == ("b.png", "a.png")


def test_attachment_paths_empty_when_no_attachments():
    assert Message(**MSG_KWARGS).attachment_paths() == ()


def test_attachment_is_frozen():
    att = Attachment(kind="image", path="a.png")
    field_name = "path"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(att, field_name, "b.png")


def test_message_channel_still_derives_from_chat_id():
    assert Message(**MSG_KWARGS).channel == "qq"
    assert Message(chat_id="wxg:abc", **{k: v for k, v in MSG_KWARGS.items() if k != "chat_id"}).channel == "wechat"
