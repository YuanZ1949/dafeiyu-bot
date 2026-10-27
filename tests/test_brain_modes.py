"""无状态大脑的 reply 适配：同一个入口，历史由 service 显式传入。"""

from __future__ import annotations

import json

import httpx

from dafeiyu.brain.client import BrainClient
from dafeiyu.models import ChatType


async def test_reply_sends_system_history_and_prompt() -> None:
    body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{"speak": true, "reply": "在"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    client = BrainClient(
        "k", "http://x/v1", "m", transport=httpx.MockTransport(handler)
    )
    try:
        reply = await client.reply(
            ChatType.PRIVATE,
            1,
            "新消息",
            system="人设",
            history=[
                {"role": "user", "content": "旧消息"},
                {"role": "assistant", "content": "旧回复"},
            ],
        )
    finally:
        await client.aclose()

    messages = body["messages"]
    assert messages[0]["role"] == "system"
    assert "人设" in messages[0]["content"]
    assert [m["content"] for m in messages[1:3]] == ["旧消息", "旧回复"]
    assert messages[3] == {"role": "user", "content": "新消息"}
    assert reply.speak is True
    assert reply.reply == "在"
