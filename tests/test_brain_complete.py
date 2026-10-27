"""Task 8：旁路 complete、临时 session、真删 session、外来消息审计。"""

from dataclasses import dataclass

import pytest

from dafeiyu.brain.opencode_http import OpenCodeHttpBrain
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType
from test_opencode_http import FakeServer, make_brain


@dataclass
class Fake:
    store: Store
    server: FakeServer
    brain: OpenCodeHttpBrain


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.init_schema()
    yield s
    s.close()


@pytest.fixture
async def fake(store: Store):
    server = FakeServer()
    brain = make_brain(store, server)
    yield Fake(store, server, brain)
    await brain.aclose()


@pytest.fixture
async def fake_404_delete(store: Store):
    server = FakeServer()
    server.delete_missing.add("ses_new")
    brain = make_brain(store, server)
    yield Fake(store, server, brain)
    await brain.aclose()


async def test_complete_uses_temp_session_and_deletes_it(fake: Fake) -> None:
    text = await fake.brain.complete("sys", [{"role": "user", "content": "hi"}])

    assert text == '{"speak": true, "reply": "在群聊卖萌", "reason": "ok"}'
    assert ("POST", "/api/session/ses_new/prompt") in fake.server.calls
    assert ("DELETE", "/api/session/ses_new") in fake.server.calls
    assert fake.server.created_with["agent"] == "dafeiyu-memory"
    assert fake.server.created_with["title"].startswith("mem:")
    # 旁路不碰任何聊天的 session 映射
    assert fake.store.get_opencode_session(ChatType.GROUP, 1) is None


async def test_complete_detailed_returns_triple(fake: Fake) -> None:
    out = await fake.brain.complete_detailed(
        "s", [{"role": "user", "content": "x"}], max_tokens=600
    )

    assert isinstance(out, tuple) and len(out) == 3
    assert isinstance(out[0], str) and out[0]
    assert out[1:] == (100, 35)


async def test_delete_failure_does_not_break_complete(fake_404_delete: Fake) -> None:
    text = await fake_404_delete.brain.complete(
        "s", [{"role": "user", "content": "x"}]
    )

    assert text
    assert ("DELETE", "/api/session/ses_new") in fake_404_delete.server.calls


async def test_forget_deletes_remote_session(fake: Fake) -> None:
    fake.store.set_opencode_session(ChatType.GROUP, 1, "ses_old")

    await fake.brain.forget(ChatType.GROUP, 1)
    assert ("DELETE", "/api/session/ses_old") in fake.server.calls
    assert fake.store.get_opencode_session(ChatType.GROUP, 1) is None

    # 没有映射时不该发 DELETE
    before = sum(1 for call in fake.server.calls if call[0] == "DELETE")
    await fake.brain.forget(ChatType.GROUP, 2)
    after = sum(1 for call in fake.server.calls if call[0] == "DELETE")
    assert before == after == 1


async def test_audit_flags_synthetic_and_ignores_normal_round(fake: Fake) -> None:
    fake.server.inject_synthetic(
        "ses_old", "契约还没闭合，继续做完", created=fake.server.last_created + 1
    )
    since = fake.server.last_created - 1000
    until = fake.server.last_created + 2000
    assert await fake.brain._audit_round("ses_old", since, until) == ["msg_syn"]

    # 白名单内且落在窗口内 → 0 命中（含 idle，漏掉它会每轮误判）
    fake.server.inject_normal_round("ses_clean")
    assert await fake.brain._audit_round("ses_clean", 0, 10**12) == []

    # 上一轮的 user 消息落在本轮窗口之前：是历史，不是外来注入。
    # 曾经把它当注入，导致每两轮就把 session 清一次，上下文全丢。
    fake.server.inject_user("ses_history", "上一轮说的话", created=5)
    assert await fake.brain._audit_round("ses_history", 1000, 2000) == []

    # 本轮窗口里多出来的 user 才是外部写入
    fake.server.inject_normal_round("ses_extra")
    fake.server.inject_user("ses_extra", "外部注入", created=fake.server.last_created)
    assert await fake.brain._audit_round("ses_extra", 0, 10**12) == ["msg_out"]


async def test_reply_rotates_on_foreign_message(
    fake: Fake, caplog: pytest.LogCaptureFixture
) -> None:
    fake.store.set_opencode_session(ChatType.GROUP, 1, "ses_old")
    fake.server.inject_synthetic(
        "ses_old", "外部注入", created=fake.server.last_created + 1
    )

    with caplog.at_level("WARNING"):
        reply = await fake.brain.reply(ChatType.GROUP, 1, "hi")

    assert reply.reply == "在群聊卖萌"
    assert fake.store.get_opencode_session(ChatType.GROUP, 1) is None
    assert any("外来消息" in rec.getMessage() for rec in caplog.records)


async def test_two_rounds_keep_session(fake: Fake) -> None:
    """连续两轮不该把会话判成污染：第三轮还得接得上第一轮。"""
    await fake.brain.reply(ChatType.GROUP, 1, "第一句")
    await fake.brain.reply(ChatType.GROUP, 1, "第二句")

    assert fake.store.get_opencode_session(ChatType.GROUP, 1) == "ses_new"
