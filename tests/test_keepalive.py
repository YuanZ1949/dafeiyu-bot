from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from dafeiyu.keepalive import KEEPALIVE_TEXT, Keepalive
from dafeiyu.models import ChatType


class FakeAdapter:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[ChatType, int, str]] = []
        self.error = error
        self.connected = True

    async def send(self, chat_type: ChatType, chat_id: int, text: str) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((chat_type, chat_id, text))


async def test_probe_sends_private_message_to_self() -> None:
    adapter = FakeAdapter()
    keepalive = Keepalive(adapter, 10001, 300, clock=lambda: 1000.0)

    assert await keepalive.probe() is True

    assert adapter.sent == [(ChatType.PRIVATE, 10001, KEEPALIVE_TEXT)]
    status = keepalive.status()
    assert status["enabled"] is True
    assert status["interval_seconds"] == 300
    assert status["ok"] is True
    assert status["last_error"] == ""
    assert status["last_at"].endswith("+08:00")


async def test_probe_failure_is_recorded_not_raised(
    caplog,
) -> None:
    adapter = FakeAdapter(error=RuntimeError("boom"))
    keepalive = Keepalive(adapter, 1, 300)

    with caplog.at_level(logging.WARNING):
        assert await keepalive.probe() is False

    status = keepalive.status()
    assert status["ok"] is False
    assert "boom" in status["last_error"]
    assert "保活消息发送失败" in caplog.text


async def test_run_waits_for_gateway_before_first_probe() -> None:
    adapter = FakeAdapter()
    adapter.connected = False
    keepalive = Keepalive(adapter, 10001, 300)

    task = asyncio.create_task(keepalive.run())
    await asyncio.sleep(0.05)
    assert adapter.sent == []

    adapter.connected = True
    for _ in range(40):
        await asyncio.sleep(0.05)
        if adapter.sent:
            break
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert adapter.sent == [(ChatType.PRIVATE, 10001, KEEPALIVE_TEXT)]
