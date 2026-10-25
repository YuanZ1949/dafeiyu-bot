"""Task 7：_ask 的本轮契约（RoundResult / 排序 / 结束 / token / 提示）。"""

import asyncio
import time

import httpx

from dafeiyu.brain import opencode_http
from dafeiyu.brain.opencode_http import OpenCodeHttpBrain
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType
from test_opencode_http import FakeServer

# 默认两步：第一步也有文本，只有按 created 取最后一条才会拿到「结论是 X」。
DEFAULT_STEPS = [("我先查一下", "done"), ("结论是 X", "done")]


def make_brain(
    *,
    order: str = "desc",
    unit: float = 1.0,
    missing_tokens: bool = False,
    steps: list[tuple[str, str]] | None = None,
) -> OpenCodeHttpBrain:
    server = FakeServer(
        order=order,
        unit=unit,
        missing_tokens=missing_tokens,
        steps=DEFAULT_STEPS if steps is None else steps,
    )
    server.known.add("s")
    server.messages["s"] = []
    store = Store(":memory:")
    store.init_schema()
    return OpenCodeHttpBrain(
        base_url="http://opencode.test",
        store=store,
        password="",
        timeout=2.0,
        transport=httpx.MockTransport(server.handler),
    )


async def test_multi_step_waits_for_final_answer() -> None:
    brain = make_brain(steps=[("我先查一下", "running"), ("结论是 X", "done")])
    try:
        result = await brain._ask("s", "p")
    finally:
        await brain.aclose()

    assert result.text == "结论是 X"


async def test_same_result_regardless_of_server_order() -> None:
    asc = make_brain(order="asc")
    desc = make_brain(order="desc")
    try:
        asc_result = await asc._ask("s", "p")
        desc_result = await desc._ask("s", "p")
    finally:
        await asc.aclose()
        await desc.aclose()

    assert asc_result.text == desc_result.text == "结论是 X"


async def test_millisecond_timestamps_still_match_window() -> None:
    brain = make_brain(unit=1000.0)
    try:
        result = await brain._ask("s", "p")
    finally:
        await brain.aclose()

    assert result.text == "结论是 X"
    assert result.until >= result.since


async def test_missing_tokens_are_zero() -> None:
    brain = make_brain(missing_tokens=True)
    try:
        result = await brain._ask("s", "p")
    finally:
        await brain.aclose()

    assert (result.prompt_tokens, result.completion_tokens) == (0, 0)


async def test_tokens_accumulate_across_steps() -> None:
    brain = make_brain(steps=[("我先查一下", "done"), ("结论是 X", "done")])
    try:
        result = await brain._ask("s", "p")
    finally:
        await brain.aclose()

    assert (result.prompt_tokens, result.completion_tokens) == (200, 70)


async def test_on_tool_fires_once() -> None:
    seen: list[str] = []

    async def on_tool(name: str) -> None:
        seen.append(name)

    brain = make_brain(steps=[("a", "done"), ("b", "done")])
    try:
        await brain._ask("s", "p", on_tool=on_tool)
    finally:
        await brain.aclose()

    assert seen == ["webfetch"]


async def test_reply_uses_round_tokens() -> None:
    brain = make_brain(steps=[("查完了", "done"), ("结论是 X", "done")])
    try:
        reply = await brain.reply(ChatType.PRIVATE, 42, "在吗")
    finally:
        await brain.aclose()

    assert reply.reply == "结论是 X"
    assert (reply.prompt_tokens, reply.completion_tokens) == (200, 70)


class BlockingWaitServer:
    """真实 opencode 的 /wait 会一直挂到会话空闲才回 204。

    ``FakeServer`` 的 wait 每次只补一步就回 200，等于抹掉了「阻塞期间客户端
    拿不到新消息」这个事实，on_tool 用例因此假绿。这里按真实语义阻塞：
    工具 part 在阻塞期间才出现，204 只在本轮跑完时返回。
    """

    def __init__(self, *, total: float = 0.3) -> None:
        self.total = float(total)
        self.messages: list[dict] = []
        self.created = 1000.0
        self.idle_at: float | None = None
        self.tool_shown = False
        self.finished = False
        self.wait_calls = 0

    def _stamp(self) -> float:
        self.created += 10
        return self.created

    def _step(self, *, status: str, text: str) -> dict:
        return {
            "id": f"msg_a{len(self.messages)}",
            "type": "assistant",
            "time": {"created": self._stamp()},
            "content": [
                {"type": "tool", "name": "webfetch", "state": {"status": status}},
                {"type": "text", "text": text},
            ],
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/prompt"):
            self.messages.append(
                {"id": "msg_u", "type": "user", "time": {"created": self._stamp()}}
            )
            return httpx.Response(
                200, json={"data": {"id": "msg_u", "time": {"created": self.created}}}
            )
        if request.method == "GET" and path.endswith("/message"):
            return httpx.Response(200, json={"data": list(reversed(self.messages))})
        if request.method == "POST" and path.endswith("/wait"):
            self.wait_calls += 1
            if not self.tool_shown:
                self.tool_shown = True
                self.messages.append(self._step(status="running", text="我先查一下"))
            if self.idle_at is None:
                self.idle_at = time.monotonic() + self.total
            await asyncio.sleep(max(0.0, self.idle_at - time.monotonic()))
            if not self.finished:
                self.finished = True
                self.messages.append(self._step(status="completed", text="结论是 X"))
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "not found"})


async def test_on_tool_fires_while_wait_blocks(monkeypatch) -> None:
    """真实 /wait 会挂到本轮空闲；提示语必须在阻塞期间就触发。"""
    server = BlockingWaitServer(total=0.3)
    # 生产切片是 2s；测试里缩到 50ms，让同一段逻辑在毫秒级跑完。
    monkeypatch.setattr(opencode_http, "WAIT_SLICE_SECONDS", 0.05, raising=False)
    marks: list[tuple[str, bool]] = []

    async def on_tool(name: str) -> None:
        marks.append((name, server.finished))

    store = Store(":memory:")
    store.init_schema()
    brain = OpenCodeHttpBrain(
        base_url="http://opencode.test",
        store=store,
        password="",
        timeout=2.0,
        transport=httpx.MockTransport(server.handler),
    )
    try:
        result = await brain._ask("s", "p", on_tool=on_tool)
    finally:
        await brain.aclose()

    assert result.text == "结论是 X"
    # 触发时本轮还没结束，且 wait 真的被切成了多次。
    assert marks == [("webfetch", False)]
    assert server.wait_calls > 1
