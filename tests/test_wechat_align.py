"""消息对齐：occurrence id、重复文本、上翻 resync、@ 与引用判定。"""

from __future__ import annotations

import pytest

from dafeiyu.gateway.wechat_bridge.align import (
    K,
    MIN_OVERLAP_RATIO,
    SessionAligner,
)

ACCOUNT = "小助手"


def make_aligner(**overrides) -> SessionAligner:
    options = {
        "account_nickname": ACCOUNT,
        "epoch": 7,
        "clock": lambda: 1000.0,
    }
    options.update(overrides)
    return SessionAligner("wxg:abc123", "测试群", **options)


def msg(nickname: str, text: str, **extra) -> dict:
    return {"nickname": nickname, "text": text, **extra}


def ids(result) -> list[str]:
    return [event["message_id"] for event in result.messages]


# --------------------------------------------------------------- occurrence id
def test_first_poll_is_baseline_without_events() -> None:
    aligner = make_aligner()

    result = aligner.poll([msg("阿猫", "历史一"), msg("阿狗", "历史二")])

    assert result.messages == ()
    assert result.resync is None


def test_duplicates_same_text_get_distinct_ids() -> None:
    """计划验收：同一文本连发 3 次 -> 3 个不同 message_id、3 个事件。"""
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "开场")])

    result = aligner.poll(
        [msg("阿猫", "开场"), msg("阿猫", "重"), msg("阿猫", "重"), msg("阿猫", "重")]
    )

    assert len(result.messages) == 3
    assert ids(result) == ["7:1", "7:2", "7:3"]
    assert len(set(ids(result))) == 3
    assert all(event["text"] == "重" for event in result.messages)


def test_duplicates_arriving_one_by_one_keep_counting() -> None:
    aligner = make_aligner()
    window = [msg("阿猫", "开场")]
    aligner.poll(window)

    seen: list[str] = []
    for _ in range(3):
        window.append(msg("阿猫", "重"))
        seen.extend(ids(aligner.poll(window)))

    assert seen == ["7:1", "7:2", "7:3"]


def test_stable_window_emits_nothing() -> None:
    aligner = make_aligner()
    window = [msg("阿猫", "一"), msg("阿狗", "二")]
    aligner.poll(window)

    result = aligner.poll(list(window))

    assert result.messages == ()
    assert result.resync is None
    assert aligner.epoch == 7
    assert aligner.seq == 0


def test_window_slide_emits_only_new_tail() -> None:
    aligner = make_aligner()
    window = [msg(f"人{i}", f"旧{i}") for i in range(K)]
    aligner.poll(window)

    window = window[1:] + [msg("新人", "新的一条")]
    result = aligner.poll(window)

    assert ids(result) == ["7:1"]
    assert result.messages[0]["text"] == "新的一条"


def test_seq_is_strictly_increasing_without_gaps() -> None:
    aligner = make_aligner()
    window = [msg("阿猫", "开场")]
    aligner.poll(window)

    seen: list[str] = []
    for index in range(5):
        window.append(msg("阿猫", f"第{index}条"))
        seen.extend(ids(aligner.poll(window)))

    assert seen == [f"7:{n}" for n in range(1, 6)]


# --------------------------------------------------------------------- resync
def test_scrolled_up_window_triggers_resync_without_messages() -> None:
    """计划 failure 场景：用户手动上翻导致末尾 3 条消失 -> 只 resync、不补发。"""
    aligner = make_aligner()
    window = [msg(f"人{i}", f"内容{i}") for i in range(10)]
    aligner.poll(window)

    result = aligner.poll(window[:-3])

    assert result.messages == ()
    assert result.resync is not None
    assert result.resync["type"] == "resync"
    assert result.resync["chat_id"] == "wxg:abc123"
    assert result.resync["session_epoch"] == result.epoch
    assert result.epoch != 7


def test_overlap_below_half_is_a_new_epoch() -> None:
    aligner = make_aligner()
    aligner.poll([msg(f"人{i}", f"旧{i}") for i in range(10)])

    # 只有 1 条重叠：1 / 10 < 0.5，必须视为断片而不是「发了 9 条新消息」。
    result = aligner.poll([msg("人9", "旧9")] + [msg("新", f"n{i}") for i in range(9)])

    assert result.messages == ()
    assert result.resync is not None
    assert result.resync["reason"] == "overlap_low"
    assert MIN_OVERLAP_RATIO == 0.5


def test_empty_window_triggers_resync() -> None:
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "在吗")])

    result = aligner.poll([])

    assert result.messages == ()
    assert result.resync["reason"] == "window_empty"


def test_resync_restarts_seq_under_new_epoch() -> None:
    aligner = make_aligner()
    window = [msg(f"人{i}", f"旧{i}") for i in range(6)]
    aligner.poll(window)
    window.append(msg("阿猫", "旧消息"))
    assert ids(aligner.poll(window)) == ["7:1"]

    lost = aligner.poll(window[:2])
    assert lost.resync is not None
    new_epoch = lost.epoch

    window = window[:2] + [msg("阿狗", "断片后的第一条")]
    after = aligner.poll(window)

    assert ids(after) == [f"{new_epoch}:1"]


# ---------------------------------------------------------------- at_bot / @
def test_at_bot_matches_exact_nickname() -> None:
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "开场")])

    result = aligner.poll(
        [
            msg("阿猫", "开场"),
            msg("阿猫", f"@{ACCOUNT} 在吗"),
            msg("阿猫", f"@{ACCOUNT}\u2005在吗"),
            msg("阿狗", f"@{ACCOUNT}2 这条不算"),
            msg("阿狗", "只是提到小助手，没有 @"),
        ]
    )

    assert [e["at_bot"] for e in result.messages] == [True, True, False, False]
    assert all(e["at_all"] is False for e in result.messages)


def test_at_all_is_not_at_bot() -> None:
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "开场")])

    result = aligner.poll([msg("阿猫", "开场"), msg("阿猫", "@所有人 开会了")])

    event = result.messages[0]
    assert event["at_bot"] is False
    assert event["at_all"] is True


def test_empty_account_nickname_never_sets_at_bot(caplog) -> None:
    """计划专项：account_nickname 为空时 @ 一律不算点名，且要 WARNING。"""
    aligner = make_aligner(account_nickname="")
    aligner.poll([msg("阿猫", "开场")])

    with caplog.at_level("WARNING", logger="dafeiyu.gateway.wechat_bridge.align"):
        result = aligner.poll([msg("阿猫", "开场"), msg("阿猫", "@小助手 在吗")])

    assert result.messages[0]["at_bot"] is False
    assert "account_nickname 为空" in caplog.text


# --------------------------------------------------------------- reply_to_bot
def test_reply_to_bot_uses_quote_author() -> None:
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "开场")])

    result = aligner.poll(
        [
            msg("阿猫", "开场"),
            msg("阿猫", "回答上面", reply_author=ACCOUNT, reply_to="机器人上一条"),
        ]
    )

    event = result.messages[0]
    assert event["reply_to_bot"] is True
    assert event["reply_to_text"] == "机器人上一条"


def test_reply_to_someone_else_is_not_a_reply_to_bot() -> None:
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "开场")])

    result = aligner.poll(
        [
            msg("阿猫", "开场"),
            msg("阿狗", "回阿猫", reply_author="阿猫", reply_to="阿猫的原话"),
        ]
    )

    event = result.messages[0]
    assert event["reply_to_bot"] is False
    assert event["reply_to_text"] is None


def test_quote_without_author_degrades_to_not_a_reply(caplog) -> None:
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "开场")])

    with caplog.at_level("DEBUG", logger="dafeiyu.gateway.wechat_bridge.align"):
        result = aligner.poll(
            [msg("阿猫", "开场"), msg("阿狗", "只有引用原文", reply_to="看不清是谁说的")]
        )

    event = result.messages[0]
    assert event["reply_to_bot"] is False
    assert event["reply_to_text"] is None
    assert "缺少作者" in caplog.text


# ------------------------------------------------------------------- 其余字段
def test_envelope_fields_and_own_messages() -> None:
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "开场")])

    result = aligner.poll([msg("阿猫", "开场"), msg(ACCOUNT, "我自己发的")])

    event = result.messages[0]
    assert event["v"] == 1
    assert event["type"] == "message"
    assert event["channel"] == "wechat"
    assert event["chat_type"] == "group"
    assert event["chat_id"] == "wxg:abc123"
    assert event["chat_name"] == "测试群"
    assert event["session_epoch"] == 7
    assert event["ts"] == 1000
    # 桥自己标记 bot 发言：重启后不会先自我回复一次。
    assert event["is_bot"] is True
    assert event["user_id"].startswith("wxg:")
    assert event["user_id"] != "wxg:self"


def test_user_id_is_stable_per_nickname() -> None:
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "开场")])

    seen: list[str] = []
    window = [msg("阿猫", "开场")]
    for text in ("一", "二"):
        window.append(msg("阿猫", text))
        seen.extend(e["user_id"] for e in aligner.poll(window).messages)
    window.append(msg("阿狗", "三"))
    seen.append(aligner.poll(window).messages[0]["user_id"])

    assert seen[0] == seen[1]
    assert seen[2] != seen[0]


def test_user_alias_hook_is_used() -> None:
    aligner = make_aligner(user_alias=lambda name: f"u{len(name)}")

    result = aligner.poll([msg("阿猫", "开场"), msg("阿猫", "新")])
    # 第一轮是基线；第二轮才会发事件。
    result = aligner.poll([msg("阿猫", "开场"), msg("阿猫", "新"), msg("阿狗", "尾巴")])

    assert result.messages[-1]["user_id"] == "wxg:u2"


def test_timestamp_falls_back_to_clock() -> None:
    aligner = make_aligner()

    result = aligner.poll([msg("阿猫", "有 ts", ts=123), msg("阿猫", "无 ts")])
    result = aligner.poll(
        [msg("阿猫", "有 ts", ts=123), msg("阿猫", "无 ts"), msg("阿猫", "补一条")]
    )

    assert result.messages[0]["ts"] == 1000


def test_invalid_chat_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        SessionAligner("abc123", "群", account_nickname=ACCOUNT)
    with pytest.raises(ValueError):
        SessionAligner("wxg:", "群", account_nickname=ACCOUNT)
    with pytest.raises(ValueError):
        SessionAligner("wxg:abc", "群", account_nickname=ACCOUNT, chat_type="channel")


def test_owner_entries_are_ordered_and_timestamped() -> None:
    """基线里的 ts 不参与对齐：同文本不同 ts 仍是同一条。"""
    aligner = make_aligner()
    aligner.poll([msg("阿猫", "开场", ts=1)])

    result = aligner.poll([msg("阿猫", "开场", ts=999), msg("阿猫", "新的", ts=1000)])

    assert ids(result) == ["7:1"]
