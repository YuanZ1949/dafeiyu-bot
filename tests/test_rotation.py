from __future__ import annotations

from datetime import datetime

import pytest

from dafeiyu.memory.rotation import apply_rotation, needs_rotation
from dafeiyu.memory.store import SessionStats, Store
from dafeiyu.models import ChatType, Message
from dafeiyu.timeutil import SHANGHAI
from dafeiyu.workers.summary import summarize_for_rotation

GROUP = ChatType.GROUP


class FakeBrain:
    def __init__(self, reply: str = "最近在聊猫粮和显卡") -> None:
        self.reply = reply
        self.reset_calls = 0
        self.prompts: list[str] = []

    def reset_session(self, chat_type: ChatType, chat_id: int) -> None:
        self.reset_calls += 1

    async def complete_detailed(self, system, messages, max_tokens=400, temperature=0.2):
        self.prompts.append(system + messages[0]["content"])
        return self.reply, 12, 34


class BrokenBrain(FakeBrain):
    async def complete_detailed(self, *args, **kwargs):
        raise RuntimeError("boom")


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "rot.db")
    s.init_schema()
    return s


def make_msg(idx: int, content: str) -> Message:
    return Message(GROUP, 1, 1000 + idx, f"u{idx}", content, f"m{idx}", timestamp=1000 + idx)


def test_needs_rotation_by_each_threshold():
    now = datetime(2026, 10, 24, tzinfo=SHANGHAI)
    kw = {
        "now": now,
        "rotation_messages": 60,
        "rotation_days": 7,
        "rotation_tokens": 40000,
    }
    assert needs_rotation(SessionStats(0, 40000, "2026-10-24"), **kw) is True
    assert needs_rotation(SessionStats(60, 0, "2026-10-24"), **kw) is True
    assert needs_rotation(SessionStats(0, 0, "2026-10-16"), **kw) is True
    assert needs_rotation(SessionStats(59, 39999, "2026-10-24"), **kw) is False


def test_needs_rotation_without_created_at_ignores_days():
    now = datetime(2026, 10, 24, tzinfo=SHANGHAI)
    kw = {
        "now": now,
        "rotation_messages": 60,
        "rotation_days": 7,
        "rotation_tokens": 40000,
    }
    assert needs_rotation(SessionStats(0, 0, None), **kw) is False
    assert needs_rotation(SessionStats(60, 0, None), **kw) is True


def test_rotation_days_reads_created_at_from_store(store):
    """链路：set_brain_session 落库的 created_at → session_stats → 天数判据。"""
    now = datetime(2026, 10, 24, tzinfo=SHANGHAI)
    kw = {
        "now": now,
        "rotation_messages": 60,
        "rotation_days": 7,
        "rotation_tokens": 40000,
    }

    store.set_brain_session("opencode", GROUP, 1, "s-1")
    assert store.session_stats(GROUP, 1).created_at
    assert needs_rotation(store.session_stats(GROUP, 1), **kw) is False

    store._conn.execute(
        "UPDATE brain_sessions SET created_at = '2026-10-16' WHERE chat_id = 1"
    )
    store._conn.commit()

    assert needs_rotation(store.session_stats(GROUP, 1), **kw) is True


async def test_apply_rotation_resets_and_returns_continuation(store):
    brain = FakeBrain()
    store.bump_session_stats(GROUP, 1, messages=61, tokens=40001)
    messages = [make_msg(0, "在聊猫粮"), make_msg(1, "还有显卡")]

    cont = await apply_rotation(brain, store, summarize_for_rotation, GROUP, 1, messages)

    assert cont is not None and "上次聊到" in cont
    assert "猫粮和显卡" in cont
    assert store.latest_session_summary(GROUP, 1) == "最近在聊猫粮和显卡"
    stats = store.session_stats(GROUP, 1)
    assert stats.delivered_count == 0
    assert stats.token_estimate == 0
    assert brain.reset_calls == 1


async def test_apply_rotation_survives_summary_failure(store):
    brain = FakeBrain(reply="")
    store.bump_session_stats(GROUP, 1, messages=61, tokens=0)

    cont = await apply_rotation(brain, store, summarize_for_rotation, GROUP, 1, [make_msg(0, "x")])

    assert cont is None
    assert brain.reset_calls == 0
    assert store.session_stats(GROUP, 1).delivered_count == 61


async def test_summarize_for_rotation_writes_session_summary(store):
    brain = FakeBrain(reply="  最近在聊猫粮和显卡  ")

    text = await summarize_for_rotation(brain, store, GROUP, 1, [make_msg(0, "在聊猫粮")])

    assert text == "最近在聊猫粮和显卡"
    assert store.latest_session_summary(GROUP, 1) == "最近在聊猫粮和显卡"
    assert store.usage_today_by_purpose(GROUP, 1, store.today(), ["summary"]) == (1, 46)


async def test_summarize_for_rotation_failure_returns_empty(store):
    text = await summarize_for_rotation(BrokenBrain(), store, GROUP, 1, [make_msg(0, "x")])

    assert text == ""
    assert store.latest_session_summary(GROUP, 1) is None
