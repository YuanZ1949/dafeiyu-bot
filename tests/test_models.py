import dataclasses
from typing import Any

import pytest

from dafeiyu.models import Attachment, ChatType, Message, NoticeEvent

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


def test_attachment_and_notice_event_locator_fields_default_to_empty_strings():
    att = Attachment("file")
    assert att.file_id == ""
    assert att.busid == ""

    notice = NoticeEvent("friend_request", ChatType.PRIVATE, "qq:1")
    assert (notice.nickname, notice.comment, notice.event_id) == ("", "", "")


def test_attachment_and_notice_event_locator_fields_round_trip():
    att = Attachment(kind="file", file_id="fid", busid="7")
    assert (att.file_id, att.busid) == ("fid", "7")

    notice = NoticeEvent(
        kind="group_request",
        chat_type=ChatType.GROUP,
        chat_id="qq:1",
        nickname="n",
        comment="c",
        event_id="flag",
    )
    assert (notice.nickname, notice.comment, notice.event_id) == ("n", "c", "flag")


def test_legacy_positional_construction_still_works():
    # 旧位置参数顺序不变：Attachment(kind, source, path, name, size, duration, nodes, fallbacks)
    att = Attachment("image", "src", "a.png", "a.png", 3, 0)
    assert (att.kind, att.source, att.path, att.name, att.size, att.duration) == (
        "image",
        "src",
        "a.png",
        "a.png",
        3,
        0,
    )
    # 新字段在末尾追加，默认值不受影响。
    assert (att.file_id, att.busid) == ("", "")

    # NoticeEvent(kind, chat_type, chat_id, user_id, operator_id, sub_type, duration)
    notice = NoticeEvent("group_poke", ChatType.GROUP, "qq:1", "u2", "u3", "style", 5)
    assert (notice.kind, notice.user_id, notice.operator_id, notice.sub_type, notice.duration) == (
        "group_poke",
        "u2",
        "u3",
        "style",
        5,
    )
    assert (notice.nickname, notice.comment, notice.event_id) == ("", "", "")


def test_unknown_extra_payload_keys_raise_type_error_upstream_filters():
    # 模型是严格 dataclass：未知的 payload 键会被上游网关过滤后才进模型；
    # 这里固定「未知键会被拒绝」这一契约，防止日后悄悄加 **kwargs 吞键。
    payload: dict[str, Any] = {"unknown_key": "x"}
    _ = pytest.raises(TypeError, NoticeEvent, "group_poke", ChatType.GROUP, "qq:1", **payload)
