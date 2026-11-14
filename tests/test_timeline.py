from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dafeiyu.memory.store import Store
from dafeiyu.memory.timeline import (
    TIMELINE_HINT,
    build_batch_prompt,
    build_prompt,
    format_message_line,
    time_label,
)
from dafeiyu.models import ChatType, Message

SHANGHAI = timezone(timedelta(hours=8))
GROUP = 20001


def at(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 10, 24, hour, minute, second, tzinfo=SHANGHAI).timestamp())


def make_store(tmp_path) -> Store:
    store = Store(str(tmp_path / "timeline.db"))
    store.init_schema()
    return store


def make_msg(
    content: str,
    message_id: str,
    *,
    timestamp: int = 0,
    nickname: str = "梦幻天使",
    user_id: int = 504567327,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: int = GROUP,
    at_bot: bool = False,
    reply_to_bot: bool = False,
    reply_to_message_id: str | None = None,
    reply_to_text: str | None = None,
    reply_to_author: str | None = None,
    at_all: bool = False,
    at_others: bool = False,
    is_bot: bool = False,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=message_id,
        at_bot=at_bot,
        reply_to_bot=reply_to_bot,
        reply_to_message_id=reply_to_message_id,
        reply_to_text=reply_to_text,
        reply_to_author=reply_to_author,
        at_all=at_all,
        at_others=at_others,
        is_bot=is_bot,
        timestamp=timestamp,
    )


def test_time_label_uses_shanghai_clock() -> None:
    assert time_label(at(21, 4)) == "10-24 21:04"
    assert time_label(0) == ""


def test_first_delivery_does_not_backfill_history(tmp_path) -> None:
    """游标缺失时只投当前这条，不把老历史再灌一遍。"""
    store = make_store(tmp_path)
    store.add_message(make_msg("旧消息一", "h1", timestamp=at(20, 0)))
    store.add_message(make_msg("旧消息二", "h2", timestamp=at(20, 1)))
    current = make_msg("大肥鱼在么", "h3", timestamp=at(20, 2))

    assert build_prompt(store, current) == "[10-24 20:02] 梦幻天使：大肥鱼在么"


def test_skipped_messages_are_backfilled_with_timestamps(tmp_path) -> None:
    store = make_store(tmp_path)
    first = make_msg("大肥鱼在么", "m1", timestamp=at(21, 4, 13))
    store.add_message(first)
    store.mark_delivered(ChatType.GROUP, GROUP, "m1")

    store.add_message(make_msg("群里谁说话你都会应么", "m2", timestamp=at(21, 4, 40)))
    store.add_message(make_msg("今天天气不错", "m3", timestamp=at(21, 5, 0)))
    current = make_msg("我问了你什么问题", "m4", timestamp=at(21, 9, 2))
    store.add_message(current)

    prompt = build_prompt(store, current)
    assert "[10-24 21:04] 梦幻天使：群里谁说话你都会应么" in prompt
    assert "[10-24 21:05] 梦幻天使：今天天气不错" in prompt
    assert "[10-24 21:09] 梦幻天使：我问了你什么问题" in prompt
    assert prompt.endswith(TIMELINE_HINT)
    # 时间顺序：22:04 的那条在 21:05 之前
    assert prompt.index("21:04") < prompt.index("21:05") < prompt.index("21:09")


def test_hint_asks_for_synthesis_not_last_message_reply() -> None:
    """情商改造：hint 不再催「回应最后一条」，改为通读整段、综合判断。"""
    assert "回应最后一条" not in TIMELINE_HINT
    assert "整段" in TIMELINE_HINT
    assert "不是逐条应答" in TIMELINE_HINT


def test_cursor_advances_after_delivery(tmp_path) -> None:
    store = make_store(tmp_path)
    store.add_message(make_msg("旧甲", "m1", timestamp=at(21, 0)))
    store.mark_delivered(ChatType.GROUP, GROUP, "m1")

    current = make_msg("旧乙", "m2", timestamp=at(21, 1))
    store.add_message(current)
    assert "旧甲" not in build_prompt(store, current)

    store.mark_delivered(ChatType.GROUP, GROUP, "m2")
    store.add_message(make_msg("新丙", "m3", timestamp=at(21, 2)))
    later = make_msg("新丁", "m4", timestamp=at(21, 3))
    store.add_message(later)
    prompt = build_prompt(store, later)
    assert "新丙" in prompt
    assert "旧甲" not in prompt and "旧乙" not in prompt


def test_overflow_reports_omitted_count(tmp_path) -> None:
    store = make_store(tmp_path)
    store.add_message(make_msg("起点", "m0", timestamp=at(21, 0)))
    store.mark_delivered(ChatType.GROUP, GROUP, "m0")
    for index in range(5):
        store.add_message(make_msg(f"话{index}", f"m{index + 1}", timestamp=at(21, index + 1)))
    current = make_msg("最后一句", "m9", timestamp=at(21, 9))
    store.add_message(current)

    prompt = build_prompt(store, current, limit=3)
    assert prompt.startswith("（更早还有 3 条消息未展开）")
    assert "最后一句" in prompt
    assert "话4" in prompt


def test_bot_messages_are_not_backfilled(tmp_path) -> None:
    store = make_store(tmp_path)
    store.add_message(make_msg("起点", "m0", timestamp=at(21, 0)))
    store.mark_delivered(ChatType.GROUP, GROUP, "m0")
    store.add_message(
        make_msg("本鱼在呢", "b1", timestamp=at(21, 1), nickname="溟月", is_bot=True)
    )
    current = make_msg("继续", "m2", timestamp=at(21, 2))
    store.add_message(current)

    prompt = build_prompt(store, current)
    assert "本鱼在呢" not in prompt
    assert "继续" in prompt


def test_current_message_marks_addressing(tmp_path) -> None:
    store = make_store(tmp_path)
    store.add_message(make_msg("起点", "m0", timestamp=at(21, 0)))
    store.mark_delivered(ChatType.GROUP, GROUP, "m0")

    at_msg = make_msg("在吗", "m1", timestamp=at(21, 1), at_bot=True)
    store.add_message(at_msg)
    assert format_message_line(at_msg, current=at_msg).endswith("（@了你）")

    reply_msg = make_msg("继续", "m2", timestamp=at(21, 2), reply_to_bot=True)
    store.add_message(reply_msg)
    assert format_message_line(reply_msg, current=reply_msg).endswith("（引用回复你）")


def test_group_reply_target_is_rendered(tmp_path) -> None:
    """群友之间「谁回谁」要进时间线，模型才能判断这句话在接谁。"""
    line = format_message_line(
        make_msg("对，就是那个", "m2", reply_to_message_id="m1"),
        reply_names={"m1": "阿猫"},
    )
    assert line.endswith("（回复 阿猫）")


def test_quote_renders_author_and_original_text() -> None:
    """引用块要带作者与原文，模型才能看出「这句在接谁、接的什么」。"""
    line = format_message_line(
        make_msg(
            "对，就是那个",
            "m2",
            reply_to_author="阿猫",
            reply_to_text="这个方案能不能跑",
        )
    )
    assert line.endswith("（引用 阿猫：这个方案能不能跑）")


def test_quote_to_bot_uses_quote_text() -> None:
    line = format_message_line(
        make_msg(
            "继续",
            "m2",
            reply_to_bot=True,
            reply_to_author="大肥鱼",
            reply_to_text="上一句结论",
        )
    )
    assert line.endswith("（引用你：上一句结论）")


def test_at_all_is_rendered() -> None:
    line = format_message_line(make_msg("@所有人 开会了", "m1", at_all=True))
    assert line.endswith("（@所有人）")
    assert "（@了你）" not in line


def test_at_others_is_rendered() -> None:
    line = format_message_line(make_msg("@群友 你看这个", "m1", at_others=True))
    assert line.endswith("（@了别的群友）")
    assert "（@了你）" not in line


def test_batch_prompt_marks_long_pause_before_batch() -> None:
    from dafeiyu.memory.timeline import build_batch_prompt

    prompt = build_batch_prompt(
        [make_msg("新话题", "m2", timestamp=at(21, 30))],
        "",
        gap_seconds=2 * 3600,
    )

    assert "【停顿 2 小时】" in prompt
    assert "别把旧话题硬接过来" in prompt


def test_batch_prompt_marks_pause_between_messages() -> None:
    from dafeiyu.memory.timeline import build_batch_prompt

    prompt = build_batch_prompt(
        [
            make_msg("旧的", "m1", timestamp=at(18, 0)),
            make_msg("隔了很久才回", "m2", timestamp=at(19, 30)),
        ],
        "",
    )

    assert "（这里隔了 1 小时 30 分钟没有消息）" in prompt


def test_batch_prompt_skips_short_gap_notice() -> None:
    from dafeiyu.memory.timeline import build_batch_prompt

    prompt = build_batch_prompt(
        [
            make_msg("一", "m1", timestamp=at(18, 0)),
            make_msg("二", "m2", timestamp=at(18, 5)),
        ],
        "",
        gap_seconds=300,
    )

    assert "【停顿" not in prompt
    assert "这里隔了" not in prompt


def test_bot_own_line_uses_second_person() -> None:
    line = format_message_line(make_msg("本鱼在呢", "b1", nickname="溟月", is_bot=True))
    assert "溟月：" not in line
    assert "你：本鱼在呢" in line


def test_alias_mention_is_marked_uncertain() -> None:
    msg = make_msg("我刚才看到大肥鱼在别的地方冒泡", "m1")
    line = format_message_line(msg, mention_ids=frozenset({"m1"}))
    assert line.endswith("（提到你，不确定）")


def test_batch_prompt_renders_reply_names() -> None:
    prompt = build_batch_prompt(
        [make_msg("我也觉得", "m2", reply_to_message_id="m1")],
        "",
        reply_names={"m1": "鱼片"},
    )
    assert "（回复 鱼片）" in prompt


# ---- T3：批次头部【本批】------------------------------------------------


def test_batch_block_has_four_elements() -> None:
    """三条消息（1 条 @机器人、1 条镜像计数、1 条原生）：四要素齐。"""
    messages = [
        make_msg("大肥鱼今天怎么不理人", "m1", timestamp=at(21, 0), nickname="梦幻天使"),
        make_msg("他是不是在忙", "m2", timestamp=at(21, 1), nickname="阿猫"),
        make_msg("@溟月 出来玩", "m3", timestamp=at(21, 2), nickname="阿狗", at_bot=True),
    ]
    prompt = build_batch_prompt(
        messages,
        "",
        mirror_count=1,
        mirror_speakers=("阿猫",),
        addressed=[messages[2]],
    )
    # 【本批】是整段提示词的第一块（在注入串之前）。
    assert prompt.startswith("【本批】")
    # ① 说话人（去重、按出现顺序）
    assert "说话人：梦幻天使、阿猫、阿狗" in prompt
    # ② 点名者
    assert "在跟你说话：1 条（阿狗）" in prompt
    # ③ 话题锚点：本批第一句前 40 字
    assert "话题锚点：本批开头是「大肥鱼今天怎么不理人」" in prompt
    # ④ 转发条数与发言人
    assert "跨群转发：1 条（阿猫）" in prompt


def test_batch_block_without_addressed_param_falls_back_to_flags() -> None:
    """不传 addressed（eval 调用点）时按 at_bot / reply_to_bot 退化判定。"""
    prompt = build_batch_prompt(
        [
            make_msg("先说一句", "m1", timestamp=at(21, 0)),
            make_msg("@溟月 在么", "m2", timestamp=at(21, 1), at_bot=True),
        ],
        "",
    )
    assert "在跟你说话：1 条（梦幻天使）" in prompt
    assert "跨群转发" not in prompt


def test_single_unaddressed_batch_has_no_batch_block() -> None:
    """单条且无人点名：不产出【本批】，避免噪声。"""
    prompt = build_batch_prompt([make_msg("随便说说", "m1")], "")
    assert "【本批】" not in prompt


def test_single_addressed_batch_has_batch_block() -> None:
    prompt = build_batch_prompt(
        [make_msg("@溟月 在么", "m1", at_bot=True)],
        "",
        addressed=[],
    )
    assert prompt.startswith("【本批】")


def test_empty_content_is_labelled(tmp_path) -> None:
    store = make_store(tmp_path)
    store.add_message(make_msg("起点", "m0", timestamp=at(21, 0)))
    store.mark_delivered(ChatType.GROUP, GROUP, "m0")
    current = make_msg("", "m1", timestamp=at(21, 1))
    store.add_message(current)

    assert "（无文字内容）" in build_prompt(store, current)


def test_forget_chat_clears_cursor(tmp_path) -> None:
    store = make_store(tmp_path)
    store.add_message(make_msg("一", "m1", timestamp=at(21, 0)))
    store.mark_delivered(ChatType.GROUP, GROUP, "m1")
    assert store.last_delivered_id(ChatType.GROUP, GROUP) is not None

    store.forget_chat(ChatType.GROUP, GROUP)
    assert store.last_delivered_id(ChatType.GROUP, GROUP) is None
