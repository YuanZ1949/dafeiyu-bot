import json

import httpx
import pytest
import respx

from dafeiyu.brain.client import BrainClient, BrainError

URL = "https://api.deepseek.com/chat/completions"

OK_PAYLOAD = {
    "id": "x",
    "choices": [{"message": {"content": '{"speak": true, "reply": "本鱼在。", "reason": "被点名"}'}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 30},
}


@pytest.fixture(scope="module")
def brain() -> BrainClient:
    # trust_env=False 只是测试提速：避免 httpx 为系统代理额外构建 transport。
    return BrainClient(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        trust_env=False,
    )


@pytest.fixture
def client(brain: BrainClient):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    brain._sleep = fake_sleep
    return brain, slept


@respx.mock
async def test_normal_json_reply(client) -> None:
    client, _ = client
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))

    reply = await client.decide("system prompt", [{"role": "user", "content": "在吗"}])

    assert reply.speak is True
    assert reply.reply == "本鱼在。"
    assert reply.prompt_tokens == 120
    assert reply.completion_tokens == 30
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "deepseek-chat"
    assert body["temperature"] == 1.0
    assert body["messages"][0]["role"] == "system"


@respx.mock
async def test_json_fence_is_stripped(client) -> None:
    client, _ = client
    payload = {
        "choices": [{"message": {"content": '```json\n{"speak": false, "reply": ""}\n```'}}],
        "usage": {},
    }
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))

    reply = await client.decide("s", [])

    assert reply.speak is False
    assert reply.reply == ""


@respx.mock
async def test_invalid_json_falls_back_to_raw_text(client) -> None:
    client, _ = client
    payload = {"choices": [{"message": {"content": "我不知道该说什么"}}], "usage": {}}
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))

    reply = await client.decide("s", [])

    assert reply.speak is True
    assert reply.reply == "我不知道该说什么"


@respx.mock
async def test_empty_reply_uses_fallback_sentence(client) -> None:
    client, _ = client
    payload = {"choices": [{"message": {"content": '{"speak": true, "reply": "  "}'}}], "usage": {}}
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))

    reply = await client.decide("s", [])

    assert reply.speak is True
    assert reply.reply
    assert "鱼" in reply.reply


@respx.mock
async def test_retries_once_after_500(client) -> None:
    client, slept = client
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=OK_PAYLOAD)]
    )

    reply = await client.decide("s", [])

    assert reply.reply == "本鱼在。"
    assert route.call_count == 2
    assert slept == [2]


@respx.mock
async def test_two_500s_raise_brain_error(client) -> None:
    client, _ = client
    respx.post(URL).mock(return_value=httpx.Response(500))

    with pytest.raises(BrainError):
        await client.decide("s", [])


@respx.mock
async def test_429_raises_without_retry(client) -> None:
    client, slept = client
    route = respx.post(URL).mock(return_value=httpx.Response(429))

    with pytest.raises(BrainError):
        await client.decide("s", [])

    assert route.call_count == 1
    assert slept == []


@respx.mock
async def test_bad_json_body_raises_brain_error(client) -> None:
    client, _ = client
    respx.post(URL).mock(return_value=httpx.Response(200, text="not json"))

    with pytest.raises(BrainError):
        await client.decide("s", [])
