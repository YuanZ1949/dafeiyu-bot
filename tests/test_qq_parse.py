import json

from fixtures.onebot_events import (
    at,
    face,
    forward,
    forward_node,
    group_event,
    image,
    json_card,
    private_event,
    reply,
    text,
    xml_card,
)

from dafeiyu.gateway.qq import (
    _attachments_from_segments,
    _pending_forward_ids,
    parse_event,
)
from dafeiyu.models import ChatType, Message, NoticeEvent

BOT_QQ = 10001


def test_group_text_message() -> None:
    event = group_event(segments=[text("大家好")], card="群名片")
    m = parse_event(event, bot_qq=BOT_QQ)
    assert m is not None
    assert m.chat_type is ChatType.GROUP
    assert m.chat_id == "qq:30003"
    assert m.user_id == "qq:20002"
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
    assert m.content == "@全体成员 在吗"


def test_at_other_is_rendered_as_group_member() -> None:
    m = parse_event(group_event(segments=[at(20002), text("看这个")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.at_bot is False
    assert m.at_others is True
    assert m.content == "@群友 看这个"


def test_at_all_is_not_at_others() -> None:
    m = parse_event(group_event(segments=[at("all"), text("开会了")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.at_all is True
    assert m.at_others is False


def test_at_bot_is_not_at_others() -> None:
    m = parse_event(group_event(segments=[at(BOT_QQ), text("你好")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.at_bot is True
    assert m.at_others is False


def test_at_other_and_bot_keeps_both_flags() -> None:
    """同时 @ 别人和 @ 你：两个标记都要留，模型才知道这句也点了我。"""
    m = parse_event(
        group_event(segments=[at(20002), at(BOT_QQ), text("你俩看看")]),
        bot_qq=BOT_QQ,
    )
    assert m is not None
    assert m.at_bot is True
    assert m.at_others is True


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


def test_reply_keeps_target_id_and_qq() -> None:
    event = group_event(segments=[reply("888", qq=20002), text("这个怎么弄")])
    m = parse_event(event, bot_qq=BOT_QQ)
    assert m is not None
    assert m.reply_to_message_id == "888"
    assert m.reply_to_user_id == "qq:20002"


def test_message_without_reply_has_no_target() -> None:
    m = parse_event(group_event(segments=[text("随便说说")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.reply_to_message_id is None
    assert m.reply_to_user_id is None


def test_private_message() -> None:
    m = parse_event(private_event(segments=[text("在吗")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.chat_type is ChatType.PRIVATE
    assert m.chat_id == "qq:20002"
    assert m.content == "在吗"


def test_non_message_event_returns_none() -> None:
    # 缺 group_id 的 notice 没有会话可归属，仍按未知事件丢弃。
    assert parse_event({"post_type": "notice", "notice_type": "group_increase"}, bot_qq=BOT_QQ) is None


def test_group_poke_notify_shape_is_parsed() -> None:
    event = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "group_id": 30003,
        "user_id": 20002,
        "target_id": BOT_QQ,
    }
    notice = parse_event(event, bot_qq=BOT_QQ)

    assert isinstance(notice, NoticeEvent)
    assert notice.kind == "group_poke"
    assert notice.chat_type is ChatType.GROUP
    assert notice.chat_id == "qq:30003"
    assert notice.user_id == "qq:10001"  # 被戳者
    assert notice.operator_id == "qq:20002"  # 发起者
    assert notice.sub_type == "poke"


def test_friend_poke_uses_private_chat() -> None:
    event = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "user_id": 20002,
        "target_id": BOT_QQ,
    }
    notice = parse_event(event, bot_qq=BOT_QQ)

    assert isinstance(notice, NoticeEvent)
    assert notice.kind == "friend_poke"
    assert notice.chat_type is ChatType.PRIVATE
    assert notice.chat_id == "qq:20002"


def test_direct_poke_notice_types_are_parsed() -> None:
    group = parse_event(
        {
            "post_type": "notice",
            "notice_type": "group_poke",
            "group_id": 30003,
            "user_id": 20002,
            "target_id": BOT_QQ,
        },
        bot_qq=BOT_QQ,
    )
    friend = parse_event(
        {
            "post_type": "notice",
            "notice_type": "friend_poke",
            "user_id": 20002,
            "target_id": BOT_QQ,
        },
        bot_qq=BOT_QQ,
    )

    assert isinstance(group, NoticeEvent) and group.kind == "group_poke"
    assert isinstance(friend, NoticeEvent) and friend.kind == "friend_poke"


def test_group_increase_decrease_ban_notices() -> None:
    increase = parse_event(
        {
            "post_type": "notice",
            "notice_type": "group_increase",
            "sub_type": "invite",
            "group_id": 30003,
            "user_id": 20002,
            "operator_id": 20003,
        },
        bot_qq=BOT_QQ,
    )
    decrease = parse_event(
        {
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": "kick",
            "group_id": 30003,
            "user_id": 20002,
            "operator_id": 20003,
        },
        bot_qq=BOT_QQ,
    )
    ban = parse_event(
        {
            "post_type": "notice",
            "notice_type": "group_ban",
            "sub_type": "ban",
            "group_id": 30003,
            "user_id": 20002,
            "operator_id": 20003,
            "duration": "600",
        },
        bot_qq=BOT_QQ,
    )

    assert isinstance(increase, NoticeEvent)
    assert (increase.kind, increase.user_id, increase.operator_id) == (
        "group_increase",
        "qq:20002",
        "qq:20003",
    )
    assert isinstance(decrease, NoticeEvent) and decrease.kind == "group_decrease"
    assert isinstance(ban, NoticeEvent)
    assert (ban.kind, ban.duration, ban.sub_type) == ("group_ban", 600, "ban")


def test_recall_still_wins_over_notice_parser() -> None:
    from dafeiyu.models import RecallEvent

    event = {
        "post_type": "notice",
        "notice_type": "group_recall",
        "group_id": 30003,
        "user_id": 20002,
        "message_id": 40004,
    }
    parsed = parse_event(event, bot_qq=BOT_QQ)

    assert isinstance(parsed, RecallEvent)


def test_unknown_notice_type_still_returns_none() -> None:
    event = {
        "post_type": "notice",
        "notice_type": "group_card",
        "group_id": 30003,
        "user_id": 20002,
    }
    assert parse_event(event, bot_qq=BOT_QQ) is None


def test_mixed_segments_render_placeholders() -> None:
    event = group_event(segments=[text("文字"), image(), face()])
    m = parse_event(event, bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "文字[图片][表情:撇嘴]"


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


# --- json 卡片（小程序/分享卡） ---

def test_json_segment_renders_title() -> None:
    embedded = json.dumps({
        "app": "com.tencent.miniapp",
        "meta": {"newsApp": {"title": "今日份的猫猫视频"}},
    }, ensure_ascii=False)
    m = parse_event(group_event(segments=[text("看这个"), json_card(embedded)]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "看这个[卡片] 今日份的猫猫视频"


def test_json_segment_deep_title_found() -> None:
    embedded = json.dumps({"meta": {"detail": {"title": "深夜食堂"}}}, ensure_ascii=False)
    m = parse_event(group_event(segments=[json_card(embedded)]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[卡片] 深夜食堂"


def test_json_segment_broken_falls_back() -> None:
    m = parse_event(group_event(segments=[json_card("{这不是json")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[小程序卡片]"


def test_json_segment_no_title_falls_back() -> None:
    embedded = json.dumps({"app": "com.tencent.foo", "prompt": "[应用] 某应用"}, ensure_ascii=False)
    m = parse_event(group_event(segments=[json_card(embedded)]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[小程序卡片]"


def test_json_segment_non_dict_data_falls_back() -> None:
    m = parse_event(group_event(segments=[{"type": "json", "data": "oops"}]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[小程序卡片]"


# --- xml 卡片 ---

def test_xml_segment_uses_summary() -> None:
    m = parse_event(group_event(segments=[xml_card("[分享] 某网页标题")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[卡片] [分享] 某网页标题"


def test_xml_segment_without_summary_falls_back() -> None:
    m = parse_event(group_event(segments=[{"type": "xml", "data": {"data": "<msg/>"}}]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[卡片]"


# --- 合并转发 ---

def test_forward_inline_nodes_rendered() -> None:
    node = forward_node("张三", [text("明天下雨吗")])
    node2 = forward_node("李四", [text("应该不会"), image()])
    m = parse_event(group_event(segments=[forward(node, node2)]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[聊天记录]\n张三: 明天下雨吗\n李四: 应该不会[图片]"


def test_forward_node_string_message() -> None:
    node = forward_node("王五", "直接文本")
    m = parse_event(group_event(segments=[forward(node)]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[聊天记录]\n王五: 直接文本"


def test_forward_id_only_falls_back() -> None:
    m = parse_event(group_event(segments=[forward(forward_id="abc123")]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[合并转发]"


def test_pending_forward_ids_finds_only_id_segments() -> None:
    inline = forward(forward_node("甲", [text("内联")]))
    event = group_event(
        segments=[
            text("看这个"),
            inline,
            forward(forward_id="abc123"),
        ]
    )
    assert _pending_forward_ids(event) == ["abc123"]


def test_pending_forward_ids_ignores_other_shapes() -> None:
    inline = forward(forward_node("甲", [text("内联")]))
    assert _pending_forward_ids(group_event(segments=[inline])) == []
    assert _pending_forward_ids(group_event(segments=[text("hi")])) == []
    assert _pending_forward_ids({"post_type": "notice"}) == []
    assert _pending_forward_ids({"message": "plain text"}) == []
    # 有 content 键但为空列表、且没有 id：仍没有可拉取项。
    assert (
        _pending_forward_ids(
            {"message": [{"type": "forward", "data": {"content": []}}]}
        )
        == []
    )


def test_pending_forward_ids_dedupes_and_limits_to_one() -> None:
    event = group_event(
        segments=[
            forward(forward_id="dup"),
            forward(forward_id="dup"),
            forward(forward_id="other"),
        ]
    )
    assert _pending_forward_ids(event) == ["dup"]


def test_forward_empty_content_falls_back() -> None:
    m = parse_event(group_event(segments=[{"type": "forward", "data": {"content": []}}]), bot_qq=BOT_QQ)
    assert m is not None
    assert m.content == "[合并转发]"


def test_forward_nested_depth_capped() -> None:
    # 深度上限 = 展开 2 层：表层(0)展开 → 外层(1)展开 → 中层(2)达上限回退占位
    level2 = {"type": "forward", "data": forward(forward_node("中层", [text("中层发言")]))["data"]}
    level1 = {"type": "forward", "data": forward(forward_node("外层", [text("外层发言"), level2]))["data"]}
    outer = forward(forward_node("表层", [text("转来的"), level1]))
    m = parse_event(group_event(segments=[outer]), bot_qq=BOT_QQ)
    assert m is not None
    assert "表层: 转来的" in m.content
    assert "外层: 外层发言" in m.content
    # 中层的 forward 达到深度上限：不再展开，回退占位
    assert "中层发言" not in m.content
    assert "[合并转发]" in m.content


def test_forward_node_count_capped() -> None:
    nodes = [forward_node(f"群友{i}", [text(f"消息{i}")]) for i in range(15)]
    m = parse_event(group_event(segments=[forward(*nodes)]), bot_qq=BOT_QQ)
    assert m is not None
    assert "群友9: 消息9" in m.content
    assert "群友10" not in m.content
    assert m.content.count("\n") <= 10


def test_forward_node_text_clipped() -> None:
    long_text = "很长的发言" * 100
    m = parse_event(group_event(segments=[forward(forward_node("话痨", [text(long_text)]))]), bot_qq=BOT_QQ)
    assert m is not None
    assert "很长的发言" in m.content
    assert len(m.content) < 400


def test_card_segments_mixed_with_text_do_not_raise() -> None:
    m = parse_event(
        group_event(segments=[text("标题党"), xml_card("摘要"), {"type": "mystery", "data": {}}]),
        bot_qq=BOT_QQ,
    )
    assert m is not None
    assert m.content.startswith("标题党[卡片] 摘要")


# --- 更多消息类型 ---

def test_file_segment_includes_name() -> None:
    m = parse_event(
        group_event(segments=[{"type": "file", "data": {"name": "报告.pdf"}}]),
        bot_qq=BOT_QQ,
    )
    assert m is not None
    assert m.content == "[文件] 报告.pdf"


def test_dice_and_rps_segments() -> None:
    dice = parse_event(
        group_event(segments=[{"type": "dice", "data": {"result": 4}}]),
        bot_qq=BOT_QQ,
    )
    rps = parse_event(
        group_event(segments=[{"type": "rps", "data": {"result": 2}}]),
        bot_qq=BOT_QQ,
    )
    assert dice is not None and dice.content == "[骰子:4]"
    assert rps is not None and rps.content == "[猜拳:剪刀]"


def test_share_location_contact_music_segments() -> None:
    event = group_event(
        segments=[
            {"type": "share", "data": {"title": "一篇文章"}},
            {"type": "location", "data": {"title": "公司楼下"}},
            {"type": "contact", "data": {"name": "阿猫"}},
            {"type": "music", "data": {"title": "海底"}},
        ]
    )
    m = parse_event(event, bot_qq=BOT_QQ)
    assert m is not None
    assert "[分享] 一篇文章" in m.content
    assert "[位置] 公司楼下" in m.content
    assert "[名片] 阿猫" in m.content
    assert "[音乐] 海底" in m.content


def test_markdown_segment_is_readable() -> None:
    m = parse_event(
        group_event(segments=[{"type": "markdown", "data": {"content": "**重点**"}}]),
        bot_qq=BOT_QQ,
    )
    assert m is not None
    assert m.content == "[Markdown] **重点**"


# --- 结构化附件（content 必须逐字节不变） ---


def test_media_segments_become_attachments() -> None:
    event = group_event(
        segments=[
            text("看图 "),
            {"type": "image", "data": {"url": "https://x/a.png"}},
            {"type": "record", "data": {"file": "x.amr", "duration": "3"}},
            {"type": "file", "data": {"name": "a.pdf", "size": "12"}},
        ]
    )
    m = parse_event(event, bot_qq=BOT_QQ)
    assert isinstance(m, Message)
    # 附件不改变任何渲染结果
    assert m.content == "看图 [图片][语音][文件] a.pdf"
    assert [a.kind for a in m.attachments] == ["image", "record", "file"]
    image_att, record_att, file_att = m.attachments
    assert image_att.source == "https://x/a.png"
    assert record_att.source == "x.amr"
    assert record_att.duration == 3
    assert file_att.name == "a.pdf"
    assert file_att.size == 12
    # 入站附件只带 source，绝不带文件系统路径
    assert all(a.path == "" for a in m.attachments)


def test_unknown_and_malformed_segments_have_no_attachments() -> None:
    m = parse_event(
        group_event(
            segments=[{"type": "mystery", "data": {}}, {"type": "image", "data": None}]
        ),
        bot_qq=BOT_QQ,
    )
    assert isinstance(m, Message)
    assert m.attachments == []

    assert (
        _attachments_from_segments(
            [
                None,
                42,
                {"type": "image", "data": None},
                {"type": "file", "data": {}},
                {"type": "record", "data": {}},
                {"type": "face", "data": {}},
                {"type": "mface", "data": {}},
                "not-a-dict",
            ]
        )
        == []
    )


def test_node_segment_becomes_forward_attachment() -> None:
    node = {"type": "node", "data": {"content": [forward_node("张三", [text("转来的")])]}}
    m = parse_event(group_event(segments=[node]), bot_qq=BOT_QQ)
    assert isinstance(m, Message)
    assert m.content == "[合并转发]"
    assert len(m.attachments) == 1
    assert m.attachments[0].kind == "forward"


def test_face_mface_and_video_mappings() -> None:
    m = parse_event(
        group_event(
            segments=[
                face(),
                {"type": "mface", "data": {"summary": "猫猫表情"}},
                {"type": "video", "data": {"file": "v.mp4", "duration": "7"}},
            ]
        ),
        bot_qq=BOT_QQ,
    )
    assert isinstance(m, Message)
    assert m.content == "[表情:撇嘴][表情:猫猫表情][视频]"
    assert [(a.kind, a.name, a.duration) for a in m.attachments] == [
        ("sticker", "1", 0),
        ("sticker", "猫猫表情", 0),
        ("video", "", 7),
    ]


def test_face_renders_known_name_and_keeps_unknown_id() -> None:
    known = parse_event(group_event(segments=[face()]), bot_qq=BOT_QQ)
    unknown = parse_event(
        group_event(segments=[{"type": "face", "data": {"id": "9999"}}]),
        bot_qq=BOT_QQ,
    )
    assert isinstance(known, Message)
    assert known.content == "[表情:撇嘴]"
    assert isinstance(unknown, Message)
    assert unknown.content == "[表情:9999]"


def test_face_without_numeric_id_falls_back() -> None:
    m = parse_event(
        group_event(segments=[{"type": "face", "data": {"id": "abc"}}]),
        bot_qq=BOT_QQ,
    )
    assert isinstance(m, Message)
    assert m.content == "[表情]"


def test_mface_renders_summary() -> None:
    m = parse_event(
        group_event(segments=[{"type": "mface", "data": {"summary": "猫猫表情"}}]),
        bot_qq=BOT_QQ,
    )
    assert isinstance(m, Message)
    assert m.content == "[表情:猫猫表情]"


def test_mface_without_summary_falls_back() -> None:
    m = parse_event(
        group_event(segments=[{"type": "mface", "data": {}}]),
        bot_qq=BOT_QQ,
    )
    assert isinstance(m, Message)
    assert m.content == "[表情]"

