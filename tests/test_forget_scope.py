import pytest

from dafeiyu.memory.store import (
    CHAT_SCOPED_TABLES,
    MESSAGE_SCOPED_TABLES,
    Store,
)
from dafeiyu.models import (
    ChatType,
    Episode,
    Message,
    OpenLoop,
)

GROUP = ChatType.GROUP
CHAT = "qq:1"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "mind.db")
    s.init_schema()
    yield s
    s.close()


def _message(message_id: str = "m1") -> Message:
    return Message(
        chat_type=GROUP,
        chat_id=CHAT,
        user_id="qq:2",
        nickname="阿猫",
        content="大家好呀",
        message_id=message_id,
        timestamp=1000,
    )


def _episode() -> Episode:
    return Episode(
        chat_type=GROUP,
        chat_id=CHAT,
        start_message_id="m1",
        last_message_id="m1",
        topic="鱼缸",
        summary="大家在聊水草和灯",
    )


def _seed_memory(store: Store) -> None:
    """写全五类聚合记忆 + 原文 + 游标 + 运行状态/决策，作为 forget 各入口的共同前置。"""
    store.add_message(_message())
    store.upsert_impression(GROUP, CHAT, "qq:2", "preference", "阿猫喜欢三湖", 0.8)
    store.set_impression_cursor(GROUP, CHAT, 5)
    store.save_episode(_episode())
    store.add_open_loop(OpenLoop(GROUP, CHAT, "question", "阿猫问的灯怎么选，没人接"))
    store.upsert_summary(GROUP, CHAT, "2026-11-03", "那天聊了灯")
    store.upsert_weekly_summary(GROUP, CHAT, "2026-11-02", "这一周都在聊鱼缸")
    # T15：运行状态与决策流水也属于会话派生数据，/forget 后必须消失。
    store.save_chat_state(
        GROUP,
        CHAT,
        {"muted_until": 9999.0, "no_reply_streak": 3, "downshift_until": 8888.0},
    )
    store.set_bot_state_chat(GROUP, CHAT, focus="鱼缸", mood_note="好奇")
    store.record_decision(
        GROUP,
        CHAT,
        outcome="replied",
        read="群里在聊灯",
        think="该接一句",
        reason="话题没结束",
        reply_preview="灯选暖光的",
    )


# ---- 常量防呆 ---------------------------------------------------------------


def test_chat_scoped_tables_have_no_duplicates() -> None:
    assert len(CHAT_SCOPED_TABLES) == len(set(CHAT_SCOPED_TABLES))


def test_message_scoped_tables_are_a_subset() -> None:
    assert set(MESSAGE_SCOPED_TABLES) <= set(CHAT_SCOPED_TABLES)
    # 聚合记忆不进软忘记清单。
    assert "messages" in MESSAGE_SCOPED_TABLES
    assert "impressions" not in MESSAGE_SCOPED_TABLES
    assert "summaries" not in MESSAGE_SCOPED_TABLES
    assert "weekly_summaries" not in MESSAGE_SCOPED_TABLES
    assert "episodes" not in MESSAGE_SCOPED_TABLES
    assert "open_loops" not in MESSAGE_SCOPED_TABLES
    assert "usage" not in MESSAGE_SCOPED_TABLES


def test_fixed_vulnerability_tables_are_in_chat_scoped() -> None:
    """本次修的漏洞：这两个表要留在硬忘记清单里，防止以后被随手删掉。"""
    assert "weekly_summaries" in CHAT_SCOPED_TABLES
    assert "impression_cursors" in CHAT_SCOPED_TABLES


def test_chat_runtime_state_tables_are_in_chat_scoped() -> None:
    """T15：运行状态与决策流水必须进硬忘记清单，软忘清单保持不变。"""
    assert set(CHAT_SCOPED_TABLES) >= {"chat_state", "bot_state_chats", "decisions"}
    # decisions 存 read/think/reason/reply_preview 模型原文，/forget 后必须消失。
    for table in ("chat_state", "bot_state_chats", "decisions"):
        assert table not in MESSAGE_SCOPED_TABLES, table


def test_chat_scoped_tables_all_exist_in_schema(store: Store) -> None:
    existing = {
        row["name"]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = set(CHAT_SCOPED_TABLES) - existing
    assert missing == set()


def test_chat_scoped_tables_all_carry_chat_columns(store: Store) -> None:
    for table in CHAT_SCOPED_TABLES:
        cols = {
            row["name"]
            for row in store._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert {"chat_type", "chat_id"} <= cols, table


# ---- forget_chat / forget_all ----------------------------------------------


@pytest.mark.parametrize("use_forget_all", [False, True])
def test_hard_forget_clears_everything(store: Store, use_forget_all: bool) -> None:
    _seed_memory(store)

    if use_forget_all:
        store.forget_all()
    else:
        store.forget_chat(GROUP, CHAT)

    assert store.count_messages(GROUP, CHAT) == 0
    assert store.list_impressions(GROUP, CHAT, 5) == []
    assert store.get_impression_cursor(GROUP, CHAT) == 0
    assert store.latest_episode(GROUP, CHAT) is None
    assert store.open_loops(GROUP, CHAT) == []
    assert store.get_summary(GROUP, CHAT, "2026-11-03") is None
    assert store.latest_weekly_summary(GROUP, CHAT) is None
    # T15：运行状态/会话级 bot_state/决策流水随硬忘记一起清。
    assert store.load_chat_state(GROUP, CHAT) is None
    assert store.get_bot_state_chat(GROUP, CHAT) is None
    assert store.list_decisions(chat_type=GROUP, chat_id=CHAT) == []


# ---- forget_chat 清三张运行状态/决策表（含显式副作用锁） ---------------------


def test_hard_forget_drops_chat_state_row_unmuting_chat(store: Store) -> None:
    """显式副作用：整行删 chat_state 会一并清掉 muted_until/downshift_until/
    no_reply_streak —— /forget 解除静音与降频，这是有意的「忘干净」。"""
    _seed_memory(store)

    store.forget_chat(GROUP, CHAT)

    state = store.load_chat_state(GROUP, CHAT)
    assert state is None, "chat_state 整行必须随 /forget 删除，不得保留静音"
    assert store.get_bot_state_chat(GROUP, CHAT) is None
    assert store.list_decisions(chat_type=GROUP, chat_id=CHAT) == []


def test_hard_forget_only_touches_target_chat(store: Store) -> None:
    store.save_chat_state(GROUP, CHAT, {"muted_until": 9999.0})
    store.set_bot_state_chat(GROUP, CHAT, focus="本群")
    store.record_decision(GROUP, CHAT, outcome="held", reason="先攒着")
    other = "qq:9"
    store.save_chat_state(GROUP, other, {"muted_until": 1234.0})
    store.set_bot_state_chat(GROUP, other, focus="别的群")
    store.record_decision(GROUP, other, outcome="replied", reply_preview="别群回复")

    store.forget_chat(GROUP, CHAT)

    other_state = store.load_chat_state(GROUP, other)
    assert other_state is not None and other_state["muted_until"] == 1234.0
    other_bot = store.get_bot_state_chat(GROUP, other)
    assert other_bot is not None and other_bot["focus"] == "别的群"
    other_decisions = store.list_decisions(chat_type=GROUP, chat_id=other)
    assert len(other_decisions) == 1


# ---- forget_messages --------------------------------------------------------


def test_soft_forget_keeps_aggregate_memory(store: Store) -> None:
    _seed_memory(store)

    store.forget_messages(GROUP, CHAT)

    # 原文清掉，聚合记忆全部保留。
    assert store.count_messages(GROUP, CHAT) == 0
    assert [x.content for x in store.list_impressions(GROUP, CHAT, 5)] == [
        "阿猫喜欢三湖"
    ]
    assert store.get_impression_cursor(GROUP, CHAT) == 5
    assert store.latest_episode(GROUP, CHAT) is not None
    assert store.open_loops(GROUP, CHAT) != []
    assert store.get_summary(GROUP, CHAT, "2026-11-03") == "那天聊了灯"
    weekly = store.latest_weekly_summary(GROUP, CHAT)
    assert weekly is not None and weekly.content == "这一周都在聊鱼缸"
    # T15：软忘记不动运行状态与决策流水（不在 MESSAGE_SCOPED_TABLES 里）。
    assert store.load_chat_state(GROUP, CHAT) is not None
    assert store.get_bot_state_chat(GROUP, CHAT) is not None
    assert store.list_decisions(chat_type=GROUP, chat_id=CHAT) != []


def test_soft_forget_only_touches_target_chat(store: Store) -> None:
    _seed_memory(store)
    other = Episode(
        chat_type=GROUP,
        chat_id="qq:2",
        start_message_id="m1",
        last_message_id="m1",
        topic="别的群",
        summary="别的群的对话",
    )
    store.save_episode(other)

    store.forget_messages(GROUP, CHAT)

    assert store.latest_episode(GROUP, "qq:2") is not None
    assert store.count_messages(GROUP, CHAT) == 0


# shared_facts 也带会话来源，forget_chat 要一并清掉（回归既有语义）。
def test_forget_chat_purges_shared_facts(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[ChatType, str]] = []
    original = Store.purge_shared_facts_for_chat

    def spy(self: Store, chat_type: ChatType, chat_id: str) -> int:
        calls.append((chat_type, chat_id))
        return original(self, chat_type, chat_id)

    monkeypatch.setattr(Store, "purge_shared_facts_for_chat", spy)
    store.forget_chat(GROUP, CHAT)

    assert calls == [(GROUP, CHAT)]


def test_forget_all_clears_shared_facts(store: Store) -> None:
    _seed_memory(store)
    store._conn.execute(
        "INSERT INTO shared_facts"
        " (question_key, question_text, answer_text, source_chat_type, source_chat_id)"
        " VALUES ('k', 'q', 'a', ?, ?)",
        (GROUP.value, CHAT),
    )
    store._conn.commit()

    store.forget_all()

    row = store._conn.execute("SELECT 1 FROM shared_facts").fetchone()
    assert row is None


# ---- forget_chat 清 mind_state 的 observer/followup 键 -----------------------


def _mind_keys(chat_id: str) -> list[str]:
    """与 workers 里 mind_state 三类键的同构写法（reflector/observer/followup）。"""
    return [
        f"reflector/{GROUP.value}/{chat_id}",
        f"observer/episode/{GROUP.value}/{chat_id}",
        f"followup/{GROUP.value}/{chat_id}/2026-01-01",
    ]


def test_forget_clears_observer_and_followup_keys(store: Store) -> None:
    tricky = "qq:%_1"  # chat_id 里的 SQL 通配符：LIKE 不转义会误删别的会话
    for chat_id in (tricky, "qq:2"):
        for key in _mind_keys(chat_id):
            store.set_mind_state(key, "x")

    store.forget_chat(GROUP, tricky)

    for key in _mind_keys(tricky):
        assert store.get_mind_state(key, "MISSING") == "MISSING", key
    for key in _mind_keys("qq:2"):
        assert store.get_mind_state(key) == "x", key
