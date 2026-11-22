"""辩论账本与自动 @ 的纯函数契约（不读库、不调模型）。"""

from __future__ import annotations

import json
from typing import Any

from dafeiyu.debate import (
    CLAIM_CHARS,
    LEDGER_MAX,
    LEDGER_TTL_SECONDS,
    pick_auto_at_target,
    pick_target,
    render_ledger,
    update_ledger,
)
from dafeiyu.models import ChatType, Message

NOW = 1_800_000_000


def make_msg(
    content: str,
    message_id: str,
    *,
    nickname: str = "阿猫",
    at_bot: bool = False,
    reply_to_bot: bool = False,
    reply_to_message_id: str | None = None,
    reply_to_author: str | None = None,
    is_bot: bool = False,
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="1",
        user_id="7",
        nickname=nickname,
        content=content,
        message_id=message_id,
        at_bot=at_bot,
        reply_to_bot=reply_to_bot,
        reply_to_message_id=reply_to_message_id,
        reply_to_author=reply_to_author,
        is_bot=is_bot,
    )


def entries(raw: str) -> list[dict[str, Any]]:
    return json.loads(raw)


# ---- pick_target ----------------------------------------------------------


def test_pick_target_prefers_last_addressed_message() -> None:
    batch = [
        make_msg("先说一句", "m1", nickname="阿猫"),
        make_msg("@溟月 你怎么看", "m2", nickname="小北", at_bot=True),
        make_msg("我也想问", "m3", nickname="橙子"),
    ]
    assert pick_target(batch) == ("小北", "@溟月 你怎么看")


def test_pick_target_falls_back_to_last_speaker() -> None:
    batch = [
        make_msg("甲", "m1", nickname="阿猫"),
        make_msg("乙", "m2", nickname="小北"),
    ]
    assert pick_target(batch) == ("小北", "乙")


def test_pick_target_ignores_bot_only_batch() -> None:
    assert pick_target([make_msg("本鱼在呢", "b1", nickname="溟月", is_bot=True)]) is None


def test_pick_target_clips_long_claim() -> None:
    target = pick_target([make_msg("长" * 80, "m1")])
    assert target is not None
    claim = target[1]
    assert claim == "长" * CLAIM_CHARS + "…"


# ---- update_ledger --------------------------------------------------------


def test_concession_is_recorded() -> None:
    batch = [make_msg("@溟月 你不懂", "m1", nickname="阿猫", at_bot=True)]
    raw = update_ledger("", batch, "你说得对，这点我认。", NOW)
    assert entries(raw) == [
        {"kind": "conceded", "who": "阿猫", "claim": "@溟月 你不懂", "at": NOW}
    ]


def test_open_dispute_is_recorded() -> None:
    batch = [make_msg("这插件就是不行", "m1", nickname="小北", reply_to_bot=True)]
    raw = update_ledger("", batch, "不对，你只看了截图。", NOW)
    assert entries(raw)[0]["kind"] == "open"


def test_plain_reply_does_not_touch_ledger() -> None:
    batch = [make_msg("今天好热", "m1")]
    assert update_ledger("", batch, "确实热。", NOW) == ""
    assert update_ledger("", batch, "确实热。", NOW) == ""


def test_same_speaker_same_kind_replaces_previous_claim() -> None:
    batch = [make_msg("第一版说法", "m1", nickname="阿猫", at_bot=True)]
    raw = update_ledger("", batch, "我不同意。", NOW)
    later = [make_msg("改口后的说法", "m2", nickname="阿猫", at_bot=True)]
    raw = update_ledger(raw, later, "问题在于，你换了概念。", NOW + 10)
    got = entries(raw)
    assert len(got) == 1
    assert got[0]["claim"] == "改口后的说法"
    assert got[0]["at"] == NOW + 10


def test_conceded_and_open_coexist_for_same_speaker() -> None:
    batch = [make_msg("A 说法", "m1", nickname="阿猫", at_bot=True)]
    raw = update_ledger("", batch, "这点我认。", NOW)
    later = [make_msg("B 说法", "m2", nickname="阿猫", at_bot=True)]
    raw = update_ledger(raw, later, "但是 B 说法不对。", NOW + 5)
    kinds = {item["kind"]: item["claim"] for item in entries(raw)}
    assert kinds == {"conceded": "A 说法", "open": "B 说法"}


def test_expired_entries_are_dropped() -> None:
    old = json.dumps(
        [
            {
                "kind": "open",
                "who": "阿猫",
                "claim": "旧主张",
                "at": NOW - LEDGER_TTL_SECONDS - 1,
            }
        ],
        ensure_ascii=False,
    )
    assert render_ledger(old, NOW) == ""
    fresh = update_ledger(old, [make_msg("新主张", "m1", at_bot=True)], "不对。", NOW)
    assert [item["claim"] for item in entries(fresh)] == ["新主张"]


def test_ledger_is_capped_and_keeps_newest() -> None:
    raw = ""
    for index in range(LEDGER_MAX + 3):
        batch = [
            make_msg(f"说法{index}", f"m{index}", nickname=f"人{index}", at_bot=True)
        ]
        raw = update_ledger(raw, batch, "不对。", NOW + index)
    got = entries(raw)
    assert len(got) == LEDGER_MAX
    assert [item["who"] for item in got] == [
        f"人{index}" for index in range(3, LEDGER_MAX + 3)
    ]


def test_bad_json_is_treated_as_empty_ledger() -> None:
    assert render_ledger("{not json", NOW) == ""
    raw = update_ledger("{not json", [make_msg("主张", "m1", at_bot=True)], "不对。", NOW)
    assert len(entries(raw)) == 1


# ---- render_ledger --------------------------------------------------------


def test_render_ledger_splits_conceded_and_unresolved() -> None:
    raw = json.dumps(
        [
            {"kind": "conceded", "who": "阿猫", "claim": "插件能装", "at": NOW},
            {"kind": "open", "who": "小北", "claim": "插件不行", "at": NOW},
        ],
        ensure_ascii=False,
    )
    block = render_ledger(raw, NOW)
    assert block.startswith("【辩论账本】")
    assert "已让步：阿猫「插件能装」" in block
    assert "未解决：小北「插件不行」" in block
    assert "先复述对方主张再反驳" in block


def test_render_ledger_empty_without_entries() -> None:
    assert render_ledger("", NOW) == ""
    assert render_ledger("[]", NOW) == ""


# ---- pick_auto_at_target --------------------------------------------------


def test_auto_at_needs_multiple_speakers() -> None:
    batch = [make_msg("你这话不对", "m1", nickname="阿猫", at_bot=True)]
    assert pick_auto_at_target(batch, "不对，你搞错了。") == ""


def test_auto_at_needs_rebuttal_tone() -> None:
    batch = [
        make_msg("今天好热", "m1", nickname="阿猫"),
        make_msg("是啊", "m2", nickname="小北"),
    ]
    assert pick_auto_at_target(batch, "确实热。") == ""


def test_auto_at_prefers_last_addressed_speaker() -> None:
    batch = [
        make_msg("先说一句", "m1", nickname="阿猫"),
        make_msg("@溟月 你看呢", "m2", nickname="小北", at_bot=True),
    ]
    assert pick_auto_at_target(batch, "你问的这个我不同意。") == "小北"


def test_auto_at_falls_back_to_last_speaker() -> None:
    batch = [
        make_msg("甲", "m1", nickname="阿猫"),
        make_msg("乙", "m2", nickname="小北"),
    ]
    assert pick_auto_at_target(batch, "但是你说得不对。") == "小北"
