
import time

import pytest

from dafeiyu.config import InjectionConfig
from dafeiyu.memory.context import (
    CONTEXT_BUDGET,
    INJECTION_BOUNDARY,
    INJECTION_EPISODE_HEADER,
    INJECTION_OPEN_LOOP_HEADER,
    OUTPUT_RESERVE,
    SYSTEM_MAX_TOKENS,
    ContextBundle,
    build_context,
    build_injection,
    estimate_tokens,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Episode, Message, OpenLoop

GROUP = ChatType.GROUP
CHAT = 10001


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "ctx.db")
    s.init_schema()
    yield s
    s.close()


def add(store: Store, i: int, text: str, *, is_bot: bool = False) -> None:
    store.add_message(
        Message(
            chat_type=GROUP,
            chat_id=CHAT,
            user_id=999 if is_bot else 1,
            nickname="溟月" if is_bot else "阿伟",
            content=text,
            message_id=f"m{i}",
            segments=(),
            is_bot=is_bot,
            at_bot=False,
            reply_to_bot=False,
            timestamp=1700000000 + i,
        )
    )


def test_estimate_tokens_cjk() -> None:
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1


def test_window_keeps_latest_messages(store: Store) -> None:
    for i in range(25):
        add(store, i, f"消息{i}")
    bundle = build_context(store, GROUP, CHAT, 20, 5, "系统", "2026-10-24")
    assert isinstance(bundle, ContextBundle)
    assert len(bundle.messages) == 20
    assert bundle.messages[0]["content"] == "阿伟: 消息5"
    assert bundle.messages[-1]["content"] == "阿伟: 消息24"


def test_bot_messages_are_assistant_role(store: Store) -> None:
    add(store, 0, "你好", is_bot=False)
    add(store, 1, "本鱼在", is_bot=True)
    bundle = build_context(store, GROUP, CHAT, 20, 5, "系统", "2026-10-24")
    assert [m["role"] for m in bundle.messages] == ["user", "assistant"]


def test_current_message_survives_budget(store: Store) -> None:
    for i in range(19):
        add(store, i, "长" * 3500)
    add(store, 19, "当前问题")
    bundle = build_context(store, GROUP, CHAT, 20, 5, "系统", "2026-10-24")
    assert bundle.messages[-1]["content"] == "阿伟: 当前问题"
    assert len(bundle.messages) == 1


def test_impressions_dropped_after_window_shrinks(store: Store) -> None:
    for i in range(3):
        add(store, i, f"消息{i}")
    # 摘要现在最多注入 500 字，改用一条超长消息把预算撑爆，保持本用例原意。
    add(store, 3, "长" * 3000)
    for j in range(30):
        store.upsert_impression(GROUP, CHAT, j + 100, "nickname", "肥" * 200, 0.9)
    store.upsert_summary(GROUP, CHAT, "2026-10-23", "摘" * 4000)
    bundle = build_context(store, GROUP, CHAT, 20, 5, "系统", "2026-10-24")
    assert len(bundle.messages) == 1
    assert "群友印象" not in bundle.system
    assert "近期摘要" not in bundle.system


def test_summary_used_when_budget_allows(store: Store) -> None:
    add(store, 0, "早")
    store.upsert_impression(GROUP, CHAT, 42, "nickname", "大家叫他鱼片", 0.8)
    store.upsert_summary(GROUP, CHAT, "2026-10-23", "昨天聊了显卡")
    bundle = build_context(store, GROUP, CHAT, 20, 5, "系统", "2026-10-24")
    assert "鱼片" in bundle.system
    assert "昨天聊了显卡" in bundle.system


def test_system_truncated_when_alone_too_big(store: Store) -> None:
    add(store, 0, "在吗")
    huge = "设定" * 8000
    bundle = build_context(store, GROUP, CHAT, 20, 5, huge, "2026-10-24")
    assert estimate_tokens(bundle.system) <= SYSTEM_MAX_TOKENS
    assert bundle.messages[-1]["content"] == "阿伟: 在吗"


def test_scene_survives_persona_truncation(store: Store) -> None:
    """人设超预算时先砍正文，保住「当前场景」（群名/日期/字数上限）。"""
    add(store, 0, "在吗")
    persona = "设定" * 8000 + "\n\n## 当前场景\n\n- 群名：测试群\n- 今天：2026-10-24\n"
    bundle = build_context(store, GROUP, CHAT, 20, 5, persona, "2026-10-24")
    assert "## 当前场景" in bundle.system
    assert "群名：测试群" in bundle.system
    assert bundle.system.endswith("今天：2026-10-24\n")
    assert estimate_tokens(bundle.system) <= SYSTEM_MAX_TOKENS


def test_estimated_tokens_never_reports_under_budget(store: Store) -> None:
    for i in range(20):
        add(store, i, "字" * 100)
    bundle = build_context(store, GROUP, CHAT, 20, 5, "系统", "2026-10-24")
    assert bundle.estimated_tokens == estimate_tokens(bundle.system) + sum(
        estimate_tokens(m["content"]) for m in bundle.messages
    )
    assert bundle.estimated_tokens <= CONTEXT_BUDGET - OUTPUT_RESERVE


# ---- episodes 注入：importance / phase / mood ---------------------------

CHAT_ID = str(CHAT)


def _seed_episode(
    store: Store,
    *,
    start: str,
    topic: str,
    summary: str,
    importance: float = 0.5,
    mood: str = "",
    phase: str = "discussion",
) -> int:
    return store.save_episode(
        Episode(
            chat_type=GROUP,
            chat_id=CHAT_ID,
            start_message_id=start,
            last_message_id=start,
            topic=topic,
            summary=summary,
            importance=importance,
            mood=mood,
            phase=phase,
            # 用「刚开不久」的时间：超过 1 小时的 episode 会带年龄标签、多占预算，
            # 预算相关用例不需要这层噪音（年龄标签另有专门用例）。
            started_at=int(time.time()),
        )
    )


def _episode_body(text: str) -> str:
    prefix = f"{INJECTION_EPISODE_HEADER}\n{INJECTION_BOUNDARY}\n"
    return text.split(prefix, 1)[1]


def test_episodes_order_by_importance_and_truncate_low_first(store: Store) -> None:
    """重要度高的先占预算；同预算下低分的先被截掉。"""
    _seed_episode(store, start="m1", topic="低分", summary="低分摘要", importance=0.1)
    _seed_episode(store, start="m3", topic="高分", summary="高分摘要", importance=0.9)
    _seed_episode(store, start="m2", topic="中分", summary="中分摘要", importance=0.5)
    cfg = InjectionConfig(inject_episodes=True, episode_chars=16)

    text = build_injection(store, None, GROUP, CHAT_ID, cfg, today="2026-10-24")

    assert text.index("高分：高分摘要") < text.index("中分：中分摘要")
    assert "低分" not in text
    assert len(_episode_body(text)) <= 16


def test_episodes_skip_closed_and_invalid_phases(store: Store) -> None:
    """closed 阶段和旧库里的脏 phase 都不进注入。"""
    _seed_episode(
        store, start="m1", topic="已收尾", summary="不看了", importance=0.99, phase="closed"
    )
    _seed_episode(store, start="m2", topic="进行中", summary="还在聊", importance=0.1)
    dirty = _seed_episode(store, start="m3", topic="脏阶段", summary="旧数据", importance=0.8)
    store._conn.execute("UPDATE episodes SET phase = 'mystery' WHERE id = ?", (dirty,))
    store._conn.commit()
    cfg = InjectionConfig(inject_episodes=True)

    text = build_injection(store, None, GROUP, CHAT_ID, cfg, today="2026-10-24")

    assert "进行中：还在聊" in text
    assert "已收尾" not in text
    assert "脏阶段" not in text


def test_episode_mood_is_a_separate_observation_line(store: Store) -> None:
    _seed_episode(store, start="m1", topic="鱼缸", summary="水草", mood="轻松愉快")
    cfg = InjectionConfig(inject_episodes=True)

    text = build_injection(store, None, GROUP, CHAT_ID, cfg, today="2026-10-24")

    assert "群气氛：轻松愉快" in text
    assert text.index(INJECTION_EPISODE_HEADER) < text.index("群气氛：")


def test_episode_without_mood_has_no_empty_mood_line(store: Store) -> None:
    """没有 mood 时输出保持原样，不写「群气氛：」。"""
    _seed_episode(store, start="m1", topic="鱼缸", summary="水草", mood="  ")
    cfg = InjectionConfig(inject_episodes=True)

    text = build_injection(store, None, GROUP, CHAT_ID, cfg, today="2026-10-24")

    assert "鱼缸：水草" in text
    assert "群气氛" not in text


def test_episode_mood_fits_episode_chars_budget(store: Store) -> None:
    """mood 行也从 episode_chars 里出，不能把预算撑破。"""
    _seed_episode(
        store,
        start="m1",
        topic="鱼缸",
        summary="很长的讨论" * 20,
        mood="紧张",
    )
    cfg = InjectionConfig(inject_episodes=True, episode_chars=20)

    text = build_injection(store, None, GROUP, CHAT_ID, cfg, today="2026-10-24")

    body = _episode_body(text)
    assert len(body) <= 20
    assert "群气氛：紧张" in body


# ---- open_loops 注入：kind 话术分流 -------------------------------------


def test_open_loop_kind_uses_specific_phrasing(store: Store) -> None:
    store.add_open_loop(OpenLoop(GROUP, CHAT_ID, "question", "显卡怎么装"))
    store.add_open_loop(OpenLoop(GROUP, CHAT_ID, "promise", "周末发照片"))
    store.add_open_loop(OpenLoop(GROUP, CHAT_ID, "running_joke", "鱼片梗"))
    store.add_open_loop(OpenLoop(GROUP, CHAT_ID, "plan", "周末去钓鱼"))
    cfg = InjectionConfig(inject_open_loops=True, open_loop_topk=4)

    text = build_injection(store, None, GROUP, CHAT_ID, cfg, today="2026-10-24")

    assert INJECTION_OPEN_LOOP_HEADER in text
    assert "- 显卡怎么装（等答复）" in text
    assert "- 周末发照片（等兑现）" in text
    assert "- 鱼片梗（延续梗）" in text
    # plan 不在本轮话术表里，保持原来的纯内容文案。
    assert "- 周末去钓鱼" in text
    assert "- 周末去钓鱼（" not in text


def test_open_loop_unknown_kind_falls_back_to_plain_line(store: Store) -> None:
    """旧库里的未知 kind 不猜口吻，按原样输出。"""
    loop_id = store.add_open_loop(OpenLoop(GROUP, CHAT_ID, "question", "旧库脏数据"))
    store._conn.execute(
        "UPDATE open_loops SET kind = ? WHERE id = ?", ("mystery", loop_id)
    )
    store._conn.commit()
    cfg = InjectionConfig(inject_open_loops=True)

    text = build_injection(store, None, GROUP, CHAT_ID, cfg, today="2026-10-24")

    assert "- 旧库脏数据" in text
    assert "- 旧库脏数据（" not in text


def test_mind_injection_empty_store_stays_empty(store: Store) -> None:
    cfg = InjectionConfig(inject_episodes=True, inject_open_loops=True)

    text = build_injection(store, None, GROUP, CHAT_ID, cfg, today="2026-10-24")

    assert text == ""
