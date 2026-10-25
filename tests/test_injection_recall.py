from __future__ import annotations

from pathlib import Path

import pytest

from dafeiyu.memory.context import (
    INJECTION_BOUNDARY,
    INJECTION_RECALL_HEADER,
    InjectionConfig,
    build_injection,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

GROUP = ChatType.GROUP
PRIVATE = ChatType.PRIVATE


def make_msg(
    message_id: str,
    content: str,
    *,
    timestamp: int = 0,
    chat_type: ChatType = GROUP,
    chat_id: int = 100,
    user_id: int = 7,
    nickname: str = "鱼片",
    is_bot: bool = False,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=message_id,
        is_bot=is_bot,
        timestamp=timestamp,
    )


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "recall.db")
    s.init_schema()
    yield s
    s.close()


def inject(store: Store, cfg: InjectionConfig, **kwargs) -> str:
    return build_injection(
        store,
        None,
        GROUP,
        100,
        cfg,
        today="2026-10-26",
        **kwargs,
    )


def test_recall_disabled_by_default(store: Store) -> None:
    assert InjectionConfig().recall_enabled is False
    store.add_message(make_msg("m1", "显卡驱动坏了"))
    text = inject(
        store,
        InjectionConfig(),
        recall_queries=["显卡驱动怎么装"],
        exclude_ids={"m2"},
    )
    assert INJECTION_RECALL_HEADER not in text


def test_recall_injects_related_old_message_with_boundary(store: Store) -> None:
    store.add_message(make_msg("m1", "显卡驱动坏了", timestamp=10))
    store.add_message(make_msg("m2", "今天吃什么", timestamp=20))
    store.add_message(make_msg("m3", "显卡驱动怎么装", timestamp=30, nickname="问的人"))

    text = inject(
        store,
        InjectionConfig(recall_enabled=True),
        recall_queries=["显卡驱动怎么装"],
        exclude_ids={"m3"},
    )

    assert INJECTION_RECALL_HEADER in text
    assert INJECTION_BOUNDARY in text
    assert "显卡驱动坏了" in text
    assert "显卡驱动怎么装" not in text
    assert "今天吃什么" not in text


def test_recall_excludes_recent_window_and_bot_messages(store: Store) -> None:
    store.add_message(make_msg("m1", "显卡驱动坏了", timestamp=10))
    store.add_message(make_msg("m2", "显卡驱动怎么装", timestamp=20))
    store.add_message(
        make_msg("m3", "显卡驱动回复", timestamp=30, is_bot=True)
    )

    text = inject(
        store,
        InjectionConfig(recall_enabled=True),
        recall_queries=["显卡驱动怎么装"],
        exclude_ids={"m1", "m2", "m3"},
    )
    assert INJECTION_RECALL_HEADER not in text


def test_recall_finds_keyword_late_in_long_question(store: Store) -> None:
    store.add_message(make_msg("m1", "显卡驱动坏了", timestamp=10))
    text = inject(
        store,
        InjectionConfig(recall_enabled=True),
        recall_queries=["我想问一下关于显卡驱动怎么安装的问题"],
        exclude_ids={"m2"},
    )
    assert "显卡驱动坏了" in text


def test_recall_is_scoped_to_current_conversation(store: Store) -> None:
    store.add_message(make_msg("g1", "显卡驱动坏了", chat_id=100))
    store.add_message(
        make_msg("p1", "显卡驱动坏了", chat_type=PRIVATE, chat_id=100)
    )

    text = inject(
        store,
        InjectionConfig(recall_enabled=True),
        recall_queries=["显卡驱动怎么装"],
        exclude_ids={"m2"},
    )

    assert "显卡驱动坏了" in text
    # 只能命中当前群；私聊同关键词不应被带进来。
    assert text.count("显卡驱动坏了") == 1


def test_recall_respects_topk_and_char_budget(store: Store) -> None:
    for i in range(5):
        store.add_message(
            make_msg(f"m{i}", f"显卡驱动旧消息{i}", timestamp=10 + i)
        )

    text = inject(
        store,
        InjectionConfig(recall_enabled=True, recall_topk=2, recall_chars=500),
        recall_queries=["显卡驱动怎么装"],
        exclude_ids={"current"},
    )
    body = text.split(f"{INJECTION_RECALL_HEADER}\n{INJECTION_BOUNDARY}\n", 1)[1]
    assert len(body.splitlines()) == 2

    clipped = inject(
        store,
        InjectionConfig(recall_enabled=True, recall_topk=2, recall_chars=12),
        recall_queries=["显卡驱动怎么装"],
        exclude_ids={"current"},
    )
    clipped_body = clipped.split(
        f"{INJECTION_RECALL_HEADER}\n{INJECTION_BOUNDARY}\n", 1
    )[1]
    assert len(clipped_body) <= 12
    assert clipped_body.endswith("…")


def test_recall_ignores_empty_and_short_queries(store: Store) -> None:
    store.add_message(make_msg("m1", "显卡驱动坏了"))
    assert inject(
        store,
        InjectionConfig(recall_enabled=True),
        recall_queries=["   "],
        exclude_ids={"current"},
    ) == ""
    assert inject(
        store,
        InjectionConfig(recall_enabled=True),
        recall_queries=[],
        exclude_ids={"current"},
    ) == ""
