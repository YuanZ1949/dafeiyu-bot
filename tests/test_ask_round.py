"""Task 7：_ask 的本轮契约（RoundResult / 排序 / 结束 / token / 提示）。"""

import httpx

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
