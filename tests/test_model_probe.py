"""模型探测：只走 HTTP、与生成路径共用端点规整、上下文分级边界。"""

from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

from dafeiyu import model_probe
from dafeiyu.brain import provider_config
from dafeiyu.model_probe import (
    ProbeError,
    context_grade,
    normalize_base_url,
    probe_models,
)


class FakeGet:
    """替换 model_probe._get：记录调用参数，返回预设 payload。"""

    def __init__(self, payload=None, *, error=None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple] = []

    async def __call__(self, url, api_key, timeout):
        self.calls.append((url, api_key, timeout))
        if self.error is not None:
            raise self.error
        return self.payload


class FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self.payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "http://h:1234/v1/models"),
                response=httpx.Response(
                    self.status_code,
                    request=httpx.Request("GET", "http://h:1234/v1/models"),
                ),
            )

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, *, payload=None, error=None, status: int = 200) -> None:
        self.payload = payload
        self.error = error
        self.status = status
        self.last: tuple | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None):
        self.last = (url, headers)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload, self.status)


def fake_httpx(monkeypatch, *, payload=None, error=None, status: int = 200):
    """把 httpx.AsyncClient 换成假的，并返回被创建的客户端列表。"""
    created: list[FakeClient] = []

    def factory(*_args, **_kwargs):
        client = FakeClient(payload=payload, error=error, status=status)
        created.append(client)
        return client

    monkeypatch.setattr(model_probe.httpx, "AsyncClient", factory)
    return created


def test_normalize_base_url_is_the_shared_implementation():
    # 两条配置生成路径与探测必须共用同一个函数对象，端点规整规则不允许漂移。
    assert model_probe.normalize_base_url is provider_config.normalize_base_url
    assert normalize_base_url(" http://h:1234/v1/ ") == "http://h:1234/v1"
    assert normalize_base_url("http://h:1234/v1/v1/") == "http://h:1234/v1"


def test_probe_never_shells_out(monkeypatch):
    # 探测只能走 HTTP：调 opencode CLI 会连到用户已运行的共享服务。
    def boom(*args, **kwargs):
        raise AssertionError("不允许调用 subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(model_probe, "_get", FakeGet([{"id": "m"}]))

    assert asyncio.run(probe_models("http://h:1234/v1")) == ["m"]


def test_context_grade_boundaries():
    assert context_grade(8192) == "red"
    assert context_grade(20000) == "yellow"
    assert context_grade(32768) == "green"


async def test_probe_models_reads_openai_ids(monkeypatch):
    fake = FakeGet([{"id": "qwen3"}, {"id": "llama"}])
    monkeypatch.setattr(model_probe, "_get", fake)

    assert await probe_models("http://h:1234/v1/") == ["qwen3", "llama"]
    # 探测地址 = 规整后的 base + /models；没给 key 就不带 Authorization。
    assert fake.calls == [("http://h:1234/v1/models", "", 8.0)]


async def test_probe_models_reads_data_envelope(monkeypatch):
    monkeypatch.setattr(model_probe, "_get", FakeGet({"data": [{"id": "qwen3"}]}))

    assert await probe_models("http://h:1234/v1") == ["qwen3"]


async def test_probe_models_rejects_payload_without_data(monkeypatch):
    monkeypatch.setattr(model_probe, "_get", FakeGet({"object": "list"}))

    with pytest.raises(ProbeError) as info:
        await probe_models("http://h:1234/v1")

    assert str(info.value) == "地址不可达或不是 OpenAI 兼容接口"
    assert info.value.timed_out is False


async def test_get_maps_timeout_to_probe_error(monkeypatch):
    fake_httpx(monkeypatch, error=httpx.ReadTimeout("slow"))

    with pytest.raises(ProbeError) as info:
        await probe_models("http://h:1234/v1", timeout=3.0)

    assert info.value.timed_out is True


async def test_get_maps_http_error_to_probe_error(monkeypatch):
    fake_httpx(monkeypatch, status=401, payload={"error": "no"})

    with pytest.raises(ProbeError) as info:
        await probe_models("http://h:1234/v1")

    assert info.value.timed_out is False


async def test_get_sends_bearer_only_when_key_given(monkeypatch):
    created = fake_httpx(monkeypatch, payload={"data": [{"id": "m"}]})
    await probe_models("http://h:1234/v1")
    assert created[0].last == ("http://h:1234/v1/models", {})

    created = fake_httpx(monkeypatch, payload={"data": [{"id": "m"}]})
    await probe_models("http://h:1234/v1", api_key="test-key")
    assert created[0].last[1] == {"Authorization": "Bearer test-key"}
