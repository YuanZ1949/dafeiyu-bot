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

from dafeiyu.gateway.qq import parse_event
from dafeiyu.models import ChatType

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
    assert m.content == "@群友 看这个"


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
