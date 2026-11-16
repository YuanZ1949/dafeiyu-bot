"""helper 解析桥：脱敏包装、可用性探测与失败静默（使用真实 helper 目录）。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from dafeiyu.gateway.wechat_bridge.localdb import helper_parsers

ROOM = "70000000001@chatroom"
SELF = "wxid_bridge_self"
SENDER = "wxid_bridge_sender"

# 验收用撤回通知 XML（公开字段，不含凭据）。
REVOKE_XML = ('<sysmsg type="revokemsg"><revokemsg>'
              '<session>123@chatroom</session><newmsgid>42</newmsgid>'
              '</revokemsg></sysmsg>')

FRIEND_REQUEST_XML = (
    '<msg fromusername="%s" fromnickname="张三" scene="14" opcode="2" '
    'encryptusername="v3_bridge_test_user" ticket="v4_bridge_test_ticket" '
    'content="请加我好友">somewhere</msg>' % SENDER
)

JOIN_XML = (
    '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
    '<content_template type="tmpl_type_profile"><plain />'
    '<template>"$username$"加入了群聊</template>'
    '<link_list><link name="username" type="link_profile"><memberlist>'
    '<member><username>wxid_bridge_new_member</username>'
    '<nickname>李四</nickname></member>'
    '</memberlist></link></link_list>'
    '</content_template></sysmsgtemplate></sysmsg>'
)

FRIEND_REQUEST_KEYS = {"sender_id", "nickname", "verification", "scene", "opcode"}
CREDENTIAL_MARKS = ("v3_", "v4_", "encryptusername", "ticket")


def _markers_present(root: Path) -> bool:
    return ((root / "native" / "text_service.py").is_file()
            and (root / "wxbot_framework" / "friend_requests.py").is_file())


requires_helper = pytest.mark.skipif(
    sys.platform != "win32" or not _markers_present(Path(helper_parsers.DEFAULT_HELPER_ROOT)),
    reason="需要 Windows 上的 helper 目录")


@pytest.fixture
def bridge(monkeypatch, tmp_path_factory):
    """默认指向真实 helper；可通过 indirect 换成缺失标记的临时目录。"""
    root = helper_parsers.DEFAULT_HELPER_ROOT
    if not _markers_present(Path(root)):
        root = tmp_path_factory.mktemp("helper_stub")  # 缺标记：走不可用分支
    monkeypatch.setenv("WECHAT_HELPER_ROOT", str(root))
    helper_parsers._reset_bridge()
    yield helper_parsers
    helper_parsers._reset_bridge()


def _assert_no_credentials(text: str) -> None:
    for mark in CREDENTIAL_MARKS:
        assert mark not in text


@requires_helper
def test_helper_available_and_versions(bridge):
    assert bridge.helper_available() is True
    versions = bridge.supported_versions()
    assert isinstance(versions, tuple) and versions
    assert all(isinstance(version, str) and version for version in versions)


@requires_helper
def test_parse_friend_request_sanitized(bridge):
    result = bridge.parse_friend_request(FRIEND_REQUEST_XML)
    assert result is not None
    assert set(result) == FRIEND_REQUEST_KEYS
    assert result["sender_id"] == SENDER
    assert result["nickname"] == "张三"
    assert result["verification"] == "请加我好友"
    assert result["scene"] == 14
    assert result["opcode"] == 2
    for value in result.values():
        _assert_no_credentials(str(value))


@requires_helper
def test_parse_group_system_message_join(bridge):
    events = bridge.parse_group_system_message(JOIN_XML, 10002, ROOM, SELF)
    assert events == ({"kind": "join", "room_id": ROOM,
                       "member_id": "wxid_bridge_new_member",
                       "nickname": "李四", "source": "system_structured"},)


@requires_helper
def test_parse_recall_acceptance_xml(bridge):
    result = bridge.parse_recall(REVOKE_XML, chat_id="123@chatroom")
    assert result is not None
    assert result["message_id"] == "42"
    assert result["chat_id"] == "123@chatroom"
    assert result["notice"] == ""


@requires_helper
def test_adversarial_inputs_never_raise(bridge):
    assert bridge.helper_available() is True
    assert bridge.parse_friend_request("") is None
    assert bridge.parse_friend_request("<not-msg attr='1'/>") is None
    assert bridge.parse_friend_request("<msg><broken") is None
    assert bridge.parse_friend_request(
        FRIEND_REQUEST_XML.replace('opcode="2"', 'opcode="7"')) is None
    assert bridge.parse_friend_request(
        FRIEND_REQUEST_XML.replace('v3_bridge_test_user', 'short')) is None
    assert bridge.parse_group_system_message("", 10002, ROOM, SELF) == ()
    assert bridge.parse_group_system_message("plain text, no xml", 10002, ROOM, SELF) == ()
    assert bridge.parse_group_system_message("<broken", 10002, ROOM, SELF) == ()
    assert bridge.parse_group_system_message(JOIN_XML, 1, ROOM, SELF) == ()
    assert bridge.parse_group_system_message(JOIN_XML, 10002, "not_a_chatroom", SELF) == ()
    assert bridge.parse_recall("", chat_id=ROOM) is None
    assert bridge.parse_recall(REVOKE_XML, chat_id="999@chatroom") is None
    assert bridge.parse_recall("totally not xml", chat_id=ROOM) is None


def test_missing_marker_is_unavailable(monkeypatch, tmp_path, caplog):
    empty = tmp_path / "helper_missing"
    empty.mkdir()
    monkeypatch.setenv("WECHAT_HELPER_ROOT", str(empty))
    helper_parsers._reset_bridge()
    try:
        with caplog.at_level(logging.DEBUG, logger=helper_parsers.__name__):
            assert helper_parsers.helper_available() is False
            assert helper_parsers.supported_versions() == ()
            assert helper_parsers.parse_friend_request(FRIEND_REQUEST_XML) is None
            assert helper_parsers.parse_group_system_message(JOIN_XML, 10002, ROOM, SELF) == ()
            assert helper_parsers.parse_recall(REVOKE_XML, chat_id=ROOM) is None
        # 标记缺失时绝不导入 helper 模块。
        assert "wxbot_framework" not in sys.modules
        # 失败只记一条日志，且不携带凭据或原始 XML。
        records = [record for record in caplog.records
                   if record.name == helper_parsers.__name__]
        assert len(records) == 1
        text = records[0].getMessage()
        _assert_no_credentials(text)
        assert FRIEND_REQUEST_XML not in text
        assert JOIN_XML not in text
        assert REVOKE_XML not in text
    finally:
        helper_parsers._reset_bridge()
