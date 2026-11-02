from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dafeiyu.memory.store import Store
from dafeiyu.memory.timeline import (
    TIMELINE_HINT,
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


def test_batch_prompt_renders_reply_names() -> None:
    from dafeiyu.memory.timeline import build_batch_prompt

    prompt = build_batch_prompt(
        [make_msg("我也觉得", "m2", reply_to_message_id="m1")],
        "",
        reply_names={"m1": "鱼片"},
    )
    assert "（回复 鱼片）" in prompt


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
