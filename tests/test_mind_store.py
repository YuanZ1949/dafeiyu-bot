"""心智层地基：bot_state / observer_state / reply_outcomes / 关系卡 / mind 配置。

这些是 Observer、Reflector、关系卡与结果打标共用的底层读写，先把口径钉死：
* bot_state 只有一行，部分更新不能把另一个字段清空；
* observer 游标与每日计数互不覆盖，跨天自动归零；
* 关系卡只给批次里真的说话的人，生人不臆测；
* 结果打标可重复回算（ON CONFLICT 覆盖），不重复插入。
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace

import pytest

from dafeiyu.config import ConfigError, MindConfig, _build_injection, _build_mind
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message, OpenLoop

GROUP = ChatType.GROUP
CHAT = "qq:1"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "mind.db")
    s.init_schema()
    yield s
    s.close()


def msg(
    user_id: str = "qq:2",
    content: str = "在吗",
    message_id: str = "m1",
    *,
    nickname: str = "阿猫",
    is_bot: bool = False,
    timestamp: int = 1000,
    reply_to_user_id: str | None = None,
) -> Message:
    return Message(
        GROUP,
        CHAT,
        user_id,
        nickname,
        content,
        message_id,
        is_bot=is_bot,
        timestamp=timestamp,
        reply_to_user_id=reply_to_user_id,
    )


# ---- bot_state ------------------------------------------------------------


def test_bot_state_defaults_to_empty(store: Store) -> None:
    state = store.get_bot_state()
    assert state.focus == ""
    assert state.mood_note == ""


def test_set_bot_state_only_touches_given_field(store: Store) -> None:
    store.set_bot_state(focus="显卡驱动那个坑", mood_note="有点懒")
    updated = store.set_bot_state(mood_note="被夸了一句，尾巴翘着")

    assert updated.focus == "显卡驱动那个坑"
    assert updated.mood_note == "被夸了一句，尾巴翘着"
    assert store.get_bot_state().focus == "显卡驱动那个坑"


def test_same_mood_is_suppressed(store: Store) -> None:
    assert store.note_bot_mood("被夸了，有点飘") is True
    # 同一句话连续出现就抑制，避免变成固定口癖。
    assert store.note_bot_mood("被夸了，有点飘") is False
    assert store.note_bot_mood("被怼了，不太高兴") is True


def test_forget_all_clears_bot_state(store: Store) -> None:
    store.set_bot_state(focus="在意的事", mood_note="心情")
    store.forget_all()
    assert store.get_bot_state().focus == ""


# ---- observer_state -------------------------------------------------------


def test_observer_cursor_and_daily_calls_are_independent(store: Store) -> None:
    store.set_observer_cursor(GROUP, CHAT, 42)
    store.bump_observer_calls(GROUP, CHAT, "2026-11-04")

    assert store.observer_cursor(GROUP, CHAT) == 42
    assert store.observer_calls_today(GROUP, CHAT, "2026-11-04") == 1


def test_observer_daily_calls_reset_on_new_day(store: Store) -> None:
    for _ in range(3):
        store.bump_observer_calls(GROUP, CHAT, "2026-11-04")

    assert store.bump_observer_calls(GROUP, CHAT, "2026-11-05") == 1
    assert store.observer_calls_today(GROUP, CHAT, "2026-11-05") == 1


def test_observer_state_is_chat_scoped(store: Store) -> None:
    store.set_observer_cursor(GROUP, "qq:1", 10)
    store.set_observer_cursor(GROUP, "qq:2", 20)

    assert store.observer_cursor(GROUP, "qq:1") == 10
    assert store.observer_cursor(GROUP, "qq:2") == 20
    assert store.observer_cursor(GROUP, "qq:3") == 0


# ---- mind_state（自省标记）--------------------------------------------------

def test_mind_state_roundtrip_and_scope_cleanup(store: Store) -> None:
    key = store.reflector_key(GROUP, CHAT)
    store.set_mind_state(key, "2026-11-04")
    assert store.get_mind_state(key) == "2026-11-04"
    assert store.get_mind_state("missing", "none") == "none"

    store.forget_chat(GROUP, CHAT)
    assert store.get_mind_state(key) == ""


def test_forget_all_clears_mind_state(store: Store) -> None:
    store.set_mind_state("reflector/group/qq:1", "2026-11-04")
    store.forget_all()
    assert store.get_mind_state("reflector/group/qq:1") == ""


# ---- 关系卡 ----------------------------------------------------------------


def test_relationship_card_aggregates_impressions_and_messages(store: Store) -> None:
    now = int(time.time())
    store.upsert_impression(
        GROUP, CHAT, "qq:2", "nickname", "外号鱼片", 0.9, subject_nickname="阿猫"
    )
    store.upsert_impression(
        GROUP, CHAT, "qq:2", "preference", "喜欢猫", 0.8, subject_nickname="阿猫"
    )
    store.add_message(msg(content="今天猫又拆家了", message_id="m1", timestamp=now - 60))
    store.add_message(msg(content="猫粮买哪种", message_id="m2", timestamp=now - 30))
    store.add_message(
        msg(
            user_id="qq:999",
            nickname="大肥鱼",
            content="买冻干吧",
            message_id="b1",
            is_bot=True,
            timestamp=now - 20,
            reply_to_user_id="qq:2",
        )
    )

    cards = store.relationship_cards(GROUP, CHAT, ["qq:2"], now_ts=now)

    card = cards["qq:2"]
    assert card.nickname == "阿猫"
    assert "外号鱼片" in card.facts and "喜欢猫" in card.facts
    assert card.message_count == 2
    assert card.last_seen_ts == now - 30
    assert card.last_bot_reply == "买冻干吧"


def test_relationship_card_keeps_strangers_bare(store: Store) -> None:
    now = int(time.time())
    store.add_message(msg(user_id="qq:7", nickname="路人", message_id="m1", timestamp=now))

    card = store.relationship_cards(GROUP, CHAT, ["qq:7"], now_ts=now)["qq:7"]

    assert card.nickname == "路人"
    assert card.facts == ()
    assert card.last_bot_reply == ""


def test_relationship_cards_without_speakers(store: Store) -> None:
    assert store.relationship_cards(GROUP, CHAT, [], now_ts=0) == {}


# ---- 结果打标 --------------------------------------------------------------


def test_pending_outcome_skips_fresh_and_already_tagged(store: Store) -> None:
    store.add_message(
        msg(user_id="qq:999", is_bot=True, message_id="b1", timestamp=1000)
    )
    store.add_message(
        msg(user_id="qq:999", is_bot=True, message_id="b2", timestamp=5000)
    )
    store.upsert_reply_outcome(
        GROUP,
        CHAT,
        "b1",
        1000,
        180,
        engaged=True,
        quoted=False,
        replied=True,
        silence_after=False,
    )

    pending = store.bot_messages_pending_outcome(until_ts=4000)

    assert [m.message_id for m in pending] == []


def test_pending_outcome_excludes_mirror(store: Store) -> None:
    store.add_message(
        msg(user_id="qq:999", is_bot=True, message_id="b1", timestamp=1000)
    )
    # 同一条回复在另一通道的镜像副本：不算独立发言，不该重复进待结算候选。
    store.add_message(
        replace(
            msg(user_id="qq:999", is_bot=True, message_id="b1", timestamp=1000),
            chat_id="wechat:1",
            mirror=True,
        )
    )

    pending = store.bot_messages_pending_outcome(until_ts=4000)

    assert [(m.chat_id, m.message_id, m.mirror) for m in pending] == [
        ("qq:1", "b1", False)
    ]


def test_upsert_outcome_is_idempotent(store: Store) -> None:
    store.add_message(
        msg(user_id="qq:999", is_bot=True, message_id="b1", timestamp=1000)
    )

    for engaged in (False, True):
        store.upsert_reply_outcome(
            GROUP,
            CHAT,
            "b1",
            1000,
            180,
            engaged=engaged,
            quoted=False,
            replied=engaged,
            silence_after=not engaged,
        )

    assert store.reply_outcome_stats(GROUP, CHAT, now_ts=10_000) == {
        "total": 1,
        "engaged": 1,
        "quoted": 0,
        "replied": 1,
        "silent": 0,
        "engaged_rate": 1.0,
    }


def test_outcome_stats_ignore_old_rows(store: Store) -> None:
    store.add_message(
        msg(user_id="qq:999", is_bot=True, message_id="b1", timestamp=1000)
    )
    store.upsert_reply_outcome(
        GROUP,
        CHAT,
        "b1",
        1000,
        180,
        engaged=True,
        quoted=False,
        replied=False,
        silence_after=False,
    )

    stats = store.reply_outcome_stats(GROUP, CHAT, days=1, now_ts=10_000_000)
    assert stats["total"] == 0


# ---- mind 配置 -------------------------------------------------------------


def test_mind_section_is_optional() -> None:
    mind = _build_mind({})
    assert mind == MindConfig()
    assert mind.enabled is False


def test_mind_section_parses_values() -> None:
    mind = _build_mind(
        {
            "enabled": True,
            "observer_enabled": True,
            "observer_interval_seconds": 300,
            "observer_min_messages": 5,
            "observer_daily_calls": 6,
            "reflection_hour": 4,
        }
    )
    assert mind.enabled is True
    assert mind.observer_enabled is True
    assert mind.observer_interval_seconds == 300
    assert mind.observer_min_messages == 5
    assert mind.observer_daily_calls == 6
    assert mind.reflection_hour == 4


def test_mind_section_ignores_deprecated_observer_provider(caplog) -> None:
    """旧配置的 observer_provider 不再生效，但要告警提醒删掉。"""
    with caplog.at_level(logging.WARNING):
        mind = _build_mind({"observer_provider": "local"})

    assert mind == MindConfig()
    assert "observer_provider" in caplog.text


def test_mind_section_rejects_bad_values() -> None:
    with pytest.raises(ConfigError):
        _build_mind({"reflection_hour": 24})


def test_injection_mind_switches_follow_master_switch() -> None:
    off = _build_injection({}, {})
    on = _build_injection({}, {"state_chars": 80}, MindConfig(enabled=True))

    assert off.inject_state is False and off.inject_relations is False
    assert on.inject_state is True and on.inject_relations is True
    assert on.state_chars == 80


# ---- open_loops: latest_open_loop -----------------------------------------


def test_latest_open_loop_hits_fresh_open_row(store: Store) -> None:
    store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "显卡驱动修好了吗"))

    loop = store.latest_open_loop(GROUP, CHAT)

    assert loop is not None
    assert loop.content == "显卡驱动修好了吗"
    assert loop.status == "open"


def test_latest_open_loop_skips_row_older_than_window(store: Store) -> None:
    old_id = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "很久之前没下文的事"))
    store._conn.execute(
        "UPDATE open_loops SET created_at = datetime('now', '-8 days') WHERE id = ?",
        (old_id,),
    )
    store._conn.commit()

    assert store.latest_open_loop(GROUP, CHAT) is None


def test_latest_open_loop_skips_closed_row(store: Store) -> None:
    loop_id = store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "已经办完的事"))
    store.close_open_loop(loop_id, status="done")

    assert store.latest_open_loop(GROUP, CHAT) is None


def test_latest_open_loop_is_chat_scoped(store: Store) -> None:
    store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "qq:1 的事"))

    assert store.latest_open_loop(GROUP, "qq:2") is None
    assert store.latest_open_loop(GROUP, CHAT) is not None


def test_latest_open_loop_days_zero_returns_none(store: Store) -> None:
    store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "刚记下的事"))

    assert store.latest_open_loop(GROUP, CHAT, days=0) is None
