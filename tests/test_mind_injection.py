"""心智注入段：本鱼近况 / 你和他，以及 mind_chars 的丢弃顺序。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from dafeiyu.config import InjectionConfig
from dafeiyu.memory.context import (
    INJECTION_EPISODE_HEADER,
    INJECTION_IMPRESSION_HEADER,
    INJECTION_OPEN_LOOP_HEADER,
    INJECTION_RELATION_HEADER,
    INJECTION_STATE_HEADER,
    build_injection,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Episode, Message, OpenLoop

GROUP = ChatType.GROUP
PRIVATE = ChatType.PRIVATE
CHAT = "qq:1"
USER = "qq:2"


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "inject.db")
    s.init_schema()
    yield s
    s.close()


def add_message(store: Store, i: int, *, user_id: str = USER, nickname: str = "阿猫") -> None:
    store.add_message(
        Message(
            chat_type=GROUP,
            chat_id=CHAT,
            user_id=user_id,
            nickname=nickname,
            content=f"消息 {i}",
            message_id=f"m{i}",
            timestamp=int(time.time()) - 60 + i,
        )
    )


def seed_all(store: Store) -> None:
    add_message(store, 1)
    store.upsert_impression(
        GROUP, CHAT, USER, "preference", "喜欢猫", 0.8, subject_nickname="阿猫"
    )
    store.save_episode(
        Episode(
            chat_type=GROUP,
            chat_id=CHAT,
            start_message_id="m1",
            last_message_id="m1",
            topic="话题",
            summary="进展",
            started_at=int(time.time()) - 60,
        )
    )
    store.add_open_loop(
        OpenLoop(
            chat_type=GROUP,
            chat_id=CHAT,
            kind="question",
            content="等待回复",
        )
    )
    store.set_bot_state(focus="显卡", mood_note="有点急")


def cfg(**overrides) -> InjectionConfig:
    base: dict[str, Any] = {
        "inject_episodes": True,
        "inject_open_loops": True,
        "inject_impressions": True,
        "inject_state": True,
        "inject_relations": True,
        "mind_chars": 1000,
    }
    base.update(overrides)
    return InjectionConfig(**base)


def render(store: Store, *, chat_type: ChatType = GROUP, **overrides) -> str:
    return build_injection(
        store,
        None,
        chat_type,
        CHAT,
        cfg(**overrides),
        today="2026-11-04",
    )


def test_state_block_renders_focus_and_mood(store: Store) -> None:
    store.set_bot_state(focus="显卡驱动那个坑", mood_note="被夸了一句")

    text = render(store)

    assert INJECTION_STATE_HEADER in text
    assert "现在在意：显卡驱动那个坑" in text
    assert "心情：被夸了一句" in text


def test_empty_state_is_omitted(store: Store) -> None:
    add_message(store, 1)
    assert INJECTION_STATE_HEADER not in render(store)


def test_state_can_be_switched_off(store: Store) -> None:
    store.set_bot_state(focus="显卡")
    assert INJECTION_STATE_HEADER not in render(store, inject_state=False)


def test_relation_block_only_for_group_speakers(store: Store) -> None:
    add_message(store, 1)
    store.upsert_impression(
        GROUP, CHAT, USER, "preference", "喜欢猫", 0.9, subject_nickname="阿猫"
    )

    text = render(store)

    assert INJECTION_RELATION_HEADER in text
    assert "阿猫：喜欢猫" in text
    assert "最近说过 1 句" in text


def test_relation_block_covers_more_than_three_speakers(store: Store) -> None:
    for i in range(1, 6):
        uid = f"qq:{10 + i}"
        add_message(store, i, user_id=uid, nickname=f"人{i}")
        store.upsert_impression(
            GROUP, CHAT, uid, "preference", f"事实{i}", 0.9, subject_nickname=f"人{i}"
        )

    # 关掉【群友印象】段：那里也会渲染「人N：事实N」，留着会让断言被它误判通过。
    # 这样「人4：事实4」只能由关系卡段产生，才真正锁住发言人上限。
    text = render(store, inject_impressions=False)

    # 旧上限 3 时只保留前三个发言人（人4/人5 被切掉）；提到 12 后五个全部入场。
    assert INJECTION_RELATION_HEADER in text
    for i in range(1, 6):
        assert f"人{i}：事实{i}" in text


def test_relation_block_skips_strangers(store: Store) -> None:
    add_message(store, 1, user_id="qq:7", nickname="路人")

    text = render(store)

    # 生人没有事实/历史互动，关系卡整段省略，不凭空写「你和他」。
    assert INJECTION_RELATION_HEADER not in text


def test_relation_block_is_group_only(store: Store) -> None:
    add_message(store, 1)
    store.upsert_impression(
        GROUP, CHAT, USER, "preference", "喜欢猫", 0.9, subject_nickname="阿猫"
    )

    text = render(store, chat_type=PRIVATE)

    # 群里的卡不会因为切到私聊就冒出来：私聊只读自己这个会话。
    assert INJECTION_RELATION_HEADER not in text


def _add_private_message(
    store: Store, chat_id: str, user_id: str, nickname: str, message_id: str
) -> None:
    store.add_message(
        Message(
            chat_type=PRIVATE,
            chat_id=chat_id,
            user_id=user_id,
            nickname=nickname,
            content="在吗",
            message_id=message_id,
            timestamp=int(time.time()) - 30,
        )
    )


def render_private(store: Store, chat_id: str, **overrides) -> str:
    return build_injection(
        store,
        None,
        PRIVATE,
        chat_id,
        cfg(**overrides),
        today="2026-11-04",
    )


def test_relation_block_renders_for_private_chat(store: Store) -> None:
    """D-4：私聊也建卡，"你和他"不再是群聊专属。"""
    peer = "qq:2"
    _add_private_message(store, peer, peer, "阿花", "pm1")
    store.upsert_impression(
        PRIVATE, peer, peer, "preference", "喜欢猫", 0.9, subject_nickname="阿花"
    )

    text = render_private(store, peer)

    assert INJECTION_RELATION_HEADER in text
    assert "阿花：喜欢猫" in text
    assert "最近说过 1 句" in text


def test_relation_block_does_not_mix_private_chats(store: Store) -> None:
    """私聊建卡必须按 chat_id 收口：A 会话的事实不能出现在 B 会话。"""
    for peer, name, fact in (
        ("qq:2", "阿花", "喜欢猫"),
        ("qq:3", "阿蓝", "讨厌猫"),
    ):
        _add_private_message(store, peer, peer, name, f"pm-{peer}")
        store.upsert_impression(
            PRIVATE, peer, peer, "preference", fact, 0.9, subject_nickname=name
        )

    text = render_private(store, "qq:2")

    assert "阿花：喜欢猫" in text
    assert "讨厌猫" not in text
    assert "阿蓝" not in text


def test_relation_block_can_be_switched_off_for_private(store: Store) -> None:
    peer = "qq:2"
    _add_private_message(store, peer, peer, "阿花", "pm1")
    store.upsert_impression(
        PRIVATE, peer, peer, "preference", "喜欢猫", 0.9, subject_nickname="阿花"
    )

    text = render_private(store, peer, inject_relations=False)

    assert INJECTION_RELATION_HEADER not in text


def test_attention_order_follows_spec(store: Store) -> None:
    seed_all(store)

    text = render(store)

    positions = [
        text.index(INJECTION_EPISODE_HEADER),
        text.index(INJECTION_OPEN_LOOP_HEADER),
        text.index(INJECTION_RELATION_HEADER),
        text.index(INJECTION_STATE_HEADER),
        text.index(INJECTION_IMPRESSION_HEADER),
    ]
    assert positions == sorted(positions)


def test_budget_drops_impressions_then_state(store: Store) -> None:
    """预算 25：印象与近况先整段丢，关系卡先于未了事消失，episode 必保留。"""
    seed_all(store)

    text = render(store, mind_chars=25)

    assert INJECTION_IMPRESSION_HEADER not in text
    assert INJECTION_STATE_HEADER not in text
    # 新顺序：关系卡先让位，未了事排最后才丢。
    assert INJECTION_RELATION_HEADER not in text
    assert INJECTION_OPEN_LOOP_HEADER in text
    assert INJECTION_EPISODE_HEADER in text


def test_budget_drops_loops_last(store: Store) -> None:
    """预算 12：未了事先降级为无 kind 仍保留；收到 10 才是最后被丢的段。"""
    seed_all(store)

    kept = render(store, mind_chars=12)
    assert INJECTION_OPEN_LOOP_HEADER in kept
    assert "（等答复）" not in kept
    assert INJECTION_RELATION_HEADER not in kept
    assert INJECTION_STATE_HEADER not in kept
    assert INJECTION_EPISODE_HEADER in kept

    dropped = render(store, mind_chars=10)
    assert INJECTION_OPEN_LOOP_HEADER not in dropped
    assert INJECTION_EPISODE_HEADER in dropped


def test_tiny_budget_only_truncates_episode(store: Store) -> None:
    seed_all(store)

    text = render(store, mind_chars=5)

    for header in (
        INJECTION_IMPRESSION_HEADER,
        INJECTION_OPEN_LOOP_HEADER,
        INJECTION_RELATION_HEADER,
        INJECTION_STATE_HEADER,
    ):
        assert header not in text
    assert INJECTION_EPISODE_HEADER in text
    body = text.split(INJECTION_EPISODE_HEADER, 1)[1].rsplit("\n", 1)[-1]
    assert len(body) <= 5


def test_relation_chars_truncates(store: Store) -> None:
    add_message(store, 1)
    store.upsert_impression(
        GROUP,
        CHAT,
        USER,
        "preference",
        "喜欢猫也喜欢狗还喜欢鱼",
        0.9,
        subject_nickname="阿猫",
    )

    text = render(store, relation_chars=6, inject_impressions=False)

    assert INJECTION_RELATION_HEADER in text
    assert "喜欢猫也喜欢狗还喜欢鱼" not in text


def test_no_mind_data_means_no_blocks(store: Store) -> None:
    text = render(store)

    assert text == ""
