"""T3：MirrorRouter 的渲染、队列、指纹、错误分类与落库隔离。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

import dafeiyu.mirror as mirror_module
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    MirrorConfig,
    MirrorPairConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.gateway.base import GatewayError
from dafeiyu.memory.store import Store
from dafeiyu.mirror import MirrorItem, MirrorRouter
from dafeiyu.models import Attachment, ChatType, Message, Segment
from dafeiyu.service import Service

QQ = "qq:1001"
WXG = "wxg:group"


class Recorder:
    """最小内层 adapter：记录发送、可注入延迟/异常。"""

    def __init__(
        self,
        bot_id: str,
        *,
        delay: float = 0.0,
        fail: Any = None,
    ) -> None:
        self.bot_id = bot_id
        self.connected = True
        self.sent: list[tuple[str, str]] = []
        self.calls = 0
        self.delay = delay
        self.fail = fail

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            exc = self.fail()
            if exc is not None:
                raise exc
        self.sent.append((chat_id, text))
        return f"{self.bot_id}:{self.calls}"


def make_settings() -> Settings:
    return Settings(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        bot_qq=999,
        ws_host="127.0.0.1",
        ws_port=8080,
        ws_token="",
        db_path=":memory:",
        persona=PersonaConfig("溟月", ("溟月",), 120, 300, 0.05),
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        mirror=MirrorConfig(
            enabled=True,
            pairs=(MirrorPairConfig(qq=QQ, wechat=WXG),),
        ),
    )


def make_router(
    tmp_path: Path,
    *,
    qq: Recorder | None = None,
    wechat: Recorder | None = None,
) -> tuple[MirrorRouter, Store, dict[str, Recorder]]:
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    settings = make_settings()
    adapters = {
        "qq": qq or Recorder("qq:999"),
        "wechat": wechat or Recorder("wxg:self"),
    }
    return MirrorRouter(settings, store, adapters), store, adapters


def make_msg(
    chat_id: str,
    content: str,
    *,
    user_id: str = "qq:2002",
    message_id: str = "m1",
    nickname: str = "阿鱼",
    is_bot: bool = False,
    segments: list[Segment] | None = None,
    attachments: list[Attachment] | None = None,
    kind: str = "text",
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=message_id,
        segments=segments or [],
        attachments=attachments or [],
        is_bot=is_bot,
        kind=kind,
    )


def render(router: MirrorRouter, message: Message) -> list[str]:
    direction = router._direction(message.chat_id)
    assert direction is not None
    source_channel, target = direction
    return router._render(router._snapshot(message, target, source_channel))


async def wait_until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


# ---------------------------------------------------------------- 渲染
def test_render_human_has_nickname_prefix(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    assert render(router, make_msg(QQ, "你好")) == ["阿鱼：你好"]


def test_render_bot_has_no_prefix(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    assert render(router, make_msg(QQ, "你好", is_bot=True)) == ["你好"]


def test_render_qq_pure_image_uses_attachments_not_content(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        QQ,
        "[图片]",
        segments=[Segment("image", {"url": "http://example/a.png"})],
        attachments=[Attachment(kind="image", source="http://example/a.png")],
    )
    assert render(router, message) == ["阿鱼：[图片]"]


def test_render_wechat_voice_from_kind(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        WXG,
        "[语音]",
        user_id="wxg:human",
        kind="voice",
        attachments=[Attachment(kind="voice")],
    )
    assert render(router, message) == ["阿鱼：[语音]"]


def test_render_file_keeps_name_and_forward_count(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    file_msg = make_msg(
        QQ,
        "[文件] a.txt",
        attachments=[Attachment(kind="file", name="a.txt")],
    )
    assert render(router, file_msg) == ["阿鱼：[文件] a.txt"]

    forward_msg = make_msg(
        QQ,
        "[合并转发]",
        segments=[Segment("node", {"content": [{"name": "a"}, {"name": "b"}]})],
        attachments=[Attachment(kind="forward")],
    )
    assert render(router, forward_msg) == ["阿鱼：[合并转发 2 条]"]


def test_render_long_text_uses_sender_split(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    chunks = render(router, make_msg(QQ, "字" * 1200))
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks).replace("阿鱼：", "") == "字" * 1200


# ---------------------------------------------------------------- 队列
async def test_two_directions_do_not_block_each_other(tmp_path: Path) -> None:
    slow = Recorder("qq:999", delay=0.3)
    fast = Recorder("wxg:self")
    router, _store, adapters = make_router(tmp_path, qq=slow, wechat=fast)
    router.start()
    try:
        router.observe_inbound(
            make_msg(WXG, "慢方向", user_id="wxg:human", message_id="w1")
        )
        router.observe_inbound(
            make_msg(QQ, "快方向", user_id="qq:human", message_id="q1")
        )
        await wait_until(lambda: len(fast.sent) == 1)
        assert slow.sent == []
        await wait_until(lambda: len(slow.sent) == 1)
    finally:
        await router.stop()


# ---------------------------------------------------------------- 落库
async def test_text_success_stores_mirror_row_and_excludes_it_from_context(
    tmp_path: Path,
) -> None:
    router, store, _adapters = make_router(tmp_path)
    router.start()
    try:
        router.observe_inbound(
            make_msg(QQ, "大家好", user_id="qq:2002", message_id="q1")
        )
        await wait_until(lambda: router.stats()["sent"] == 1)
    finally:
        await router.stop()

    rows = store.recent_bot_messages(since_ts=0)
    assert (WXG, "阿鱼：大家好", rows[0][2]) in rows

    row = store._conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND mirror = 1", (WXG,)
    ).fetchone()
    assert row is not None
    assert row["mirror"] == 1
    assert row["is_bot"] == 1
    assert row["timestamp"] > 0

    human = make_msg(WXG, "接一句", user_id="wxg:human", message_id="w1")
    store.add_message(human)
    service = object.__new__(Service)
    service.store = store
    service.settings = router.settings
    assert service._previous_message(human) is None
    assert service._history_before(ChatType.GROUP, WXG, [human]) == []


async def test_pure_media_stores_only_explanation_line(tmp_path: Path) -> None:
    router, store, _adapters = make_router(tmp_path)
    router.start()
    try:
        router.observe_inbound(
            make_msg(
                QQ,
                "[图片]",
                user_id="qq:2002",
                message_id="q1",
                segments=[Segment("image", {"url": "http://example/a.png"})],
                attachments=[Attachment(kind="image", source="http://example/a.png")],
            )
        )
        await wait_until(lambda: router.stats()["sent"] == 1)
    finally:
        await router.stop()
    stored = store._conn.execute(
        "SELECT content FROM messages WHERE chat_id = ? AND mirror = 1", (WXG,)
    ).fetchall()
    assert [row["content"] for row in stored] == ["阿鱼：[图片]"]


# ---------------------------------------------------------------- 错误
async def test_not_connected_retries_three_times_then_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mirror_module, "RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    failing = Recorder(
        "wxg:self",
        fail=lambda: GatewayError("down", error_code="not_connected"),
    )
    router, _store, _adapters = make_router(tmp_path, wechat=failing)
    router.start()
    try:
        router.observe_inbound(make_msg(QQ, "重试", user_id="qq:2002", message_id="q1"))
        await wait_until(lambda: router.stats()["degraded"] == 1)
    finally:
        await router.stop()
    assert failing.calls == 4
    assert router.stats()["retries"] == 3
    assert router.stats()["sent"] == 0


async def test_timeout_sends_once_and_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mirror_module, "RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    failing = Recorder("wxg:self", fail=lambda: TimeoutError("late"))
    router, _store, _adapters = make_router(tmp_path, wechat=failing)
    router.start()
    try:
        router.observe_inbound(make_msg(QQ, "超时", user_id="qq:2002", message_id="q1"))
        await wait_until(lambda: router.stats()["degraded"] == 1)
    finally:
        await router.stop()
    assert failing.calls == 1
    assert router.stats()["last_error"] == "timeout"


def test_overflow_drops_newest_and_counts(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    for index in range(mirror_module.QUEUE_MAXSIZE + 1):
        router.observe_inbound(
            make_msg(QQ, f"m{index}", user_id="qq:2002", message_id=f"q{index}")
        )
    assert router._queues["qq"].qsize() == mirror_module.QUEUE_MAXSIZE
    assert router.stats()["dropped"] == 1


async def test_worker_exception_continues_with_next_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, _store, adapters = make_router(tmp_path)
    original = router._render
    calls = {"count": 0}

    def flaky(item: MirrorItem) -> list[str]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return original(item)

    monkeypatch.setattr(router, "_render", flaky)
    router.start()
    try:
        router.observe_inbound(make_msg(QQ, "第一", user_id="qq:2002", message_id="q1"))
        router.observe_inbound(make_msg(QQ, "第二", user_id="qq:2002", message_id="q2"))
        await wait_until(lambda: len(adapters["wechat"].sent) == 1)
    finally:
        await router.stop()
    assert adapters["wechat"].sent == [(WXG, "阿鱼：第二")]
    assert "RuntimeError" in str(router.stats()["last_error"])


# ---------------------------------------------------------------- 指纹
def test_human_duplicate_not_swallowed_but_own_echo_consumed(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    router._register_fingerprints(WXG, "hello", ())

    human = make_msg(WXG, "hello", user_id="wxg:human", message_id="w1")
    router.observe_inbound(human)
    assert router._queues["wechat"].qsize() == 1
    assert router.stats()["deduped"] == 0

    # 人类消息没有消费指纹；本账号回显在 10s 内仍然被吞掉。
    router._register_fingerprints(WXG, "hello", ())
    echo = make_msg(WXG, "hello", user_id="wxg:self", message_id="w2")
    router.observe_inbound(echo)
    assert router._queues["wechat"].qsize() == 1
    assert router.stats()["deduped"] == 1


async def test_expired_item_counts_and_is_not_sent(tmp_path: Path) -> None:
    router, _store, adapters = make_router(tmp_path)
    router.start()
    try:
        router._enqueue(
            "qq",
            MirrorItem(
                source_chat_id=QQ,
                target_chat_id=WXG,
                source_message_id="old",
                nickname="阿鱼",
                is_bot=False,
                text_lines=("过期",),
                attachments=(),
                segments=(),
                source_channel="qq",
                created_at=time.time() - 400,
            ),
        )
        await wait_until(lambda: router.stats()["expired"] == 1)
    finally:
        await router.stop()
    assert adapters["wechat"].sent == []
    assert router.stats()["sent"] == 0


def test_message_exists_blocks_redelivered_frame(tmp_path: Path) -> None:
    router, store, _adapters = make_router(tmp_path)
    message = make_msg(QQ, "已经入库", user_id="qq:2002", message_id="q1")
    store.add_message(message)
    router.observe_inbound(message)
    assert router.stats()["deduped"] == 1
    assert router._queues["qq"].qsize() == 0
