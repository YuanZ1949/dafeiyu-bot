import json
from pathlib import Path

import pytest

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.workers.impressions import ImpressionWorker

CHAT = ChatType.GROUP
CHAT_ID = 90001


class FakeBrain:
    def __init__(self, response: str = "[]", exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.calls = 0
        self.last_messages: list[dict] = []

    async def complete(self, system: str, messages: list[dict], max_tokens: int = 400) -> str:
        self.calls += 1
        self.last_messages = messages
        if self.exc is not None:
            raise self.exc
        return self.response


def make_message(i: int, chat_id: int = CHAT_ID, user_id: int = 123) -> Message:
    return Message(
        chat_type=CHAT,
        chat_id=chat_id,
        user_id=user_id,
        nickname="鱼片",
        content=f"消息 {i}",
        message_id=f"m{i}",
    )


@pytest.fixture
def store(tmp_path: Path):
    s = Store(str(tmp_path / "test.db"))
    s.init_schema()
    yield s
    s.close()


async def test_no_extraction_before_every(store: Store) -> None:
    brain = FakeBrain()
    worker = ImpressionWorker(store, brain, every=20)
    for i in range(19):
        store.add_message(make_message(i))
        assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert brain.calls == 0


async def test_twentieth_message_triggers_extraction(store: Store) -> None:
    brain = FakeBrain(json.dumps([{"user_id": 123, "kind": "nickname", "content": "外号叫鱼片", "confidence": 0.8}]))
    worker = ImpressionWorker(store, brain, every=20)
    for i in range(20):
        store.add_message(make_message(i))
        await worker.maybe_extract(CHAT, CHAT_ID)
    assert brain.calls == 1
    impressions = store.list_impressions(CHAT, CHAT_ID, topk=10)
    assert [im.content for im in impressions] == ["外号叫鱼片"]
    assert "消息 19" in brain.last_messages[0]["content"]


async def test_invalid_json_returns_zero(store: Store) -> None:
    brain = FakeBrain("这不是 JSON")
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert store.list_impressions(CHAT, CHAT_ID, topk=10) == []


async def test_invalid_items_are_skipped(store: Store) -> None:
    brain = FakeBrain(json.dumps([
        {"kind": "nickname", "content": "缺 user_id"},
        {"user_id": 123, "content": "缺 kind"},
        {"user_id": 123, "kind": "nickname", "content": ""},
        {"user_id": 123, "kind": "fact", "content": ""},
    ]))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert store.list_impressions(CHAT, CHAT_ID, topk=10) == []


async def test_duplicate_extraction_does_not_duplicate_rows(store: Store) -> None:
    brain = FakeBrain(json.dumps([{"user_id": 123, "kind": "nickname", "content": "外号叫鱼片", "confidence": 0.8}]))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))

    assert await worker.maybe_extract(CHAT, CHAT_ID) == 1
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0
    assert len(store.list_impressions(CHAT, CHAT_ID, topk=10)) == 1


async def test_content_is_truncated_to_60_chars(store: Store) -> None:
    long_text = "鱼" * 100
    brain = FakeBrain(json.dumps([{"user_id": 123, "kind": "fact", "content": long_text, "confidence": 0.5}]))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    await worker.maybe_extract(CHAT, CHAT_ID)
    impressions = store.list_impressions(CHAT, CHAT_ID, topk=10)
    assert len(impressions[0].content) == 60


async def test_brain_error_returns_zero(store: Store) -> None:
    brain = FakeBrain(exc=RuntimeError("boom"))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0


async def test_missing_content_is_treated_as_empty(store: Store) -> None:
    brain = FakeBrain(json.dumps([{"user_id": 123, "kind": "nickname", "content": None, "confidence": 0.8}]))
    worker = ImpressionWorker(store, brain, every=1)
    store.add_message(make_message(1))
    assert await worker.maybe_extract(CHAT, CHAT_ID) == 0

