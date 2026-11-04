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
        "mind_chars": 600,
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


def test_budget_drops_impressions_then_loops(store: Store) -> None:
    seed_all(store)

    text = render(store, mind_chars=25)

    assert INJECTION_IMPRESSION_HEADER not in text
    assert INJECTION_OPEN_LOOP_HEADER not in text
    # 近况与正在聊的事最后才砍。
    assert INJECTION_STATE_HEADER in text
    assert INJECTION_EPISODE_HEADER in text


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
