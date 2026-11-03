
import pytest

from dafeiyu.memory.context import (
    CONTEXT_BUDGET,
    OUTPUT_RESERVE,
    SYSTEM_MAX_TOKENS,
    ContextBundle,
    build_context,
    estimate_tokens,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

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
    assert "昨日摘要" not in bundle.system


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
