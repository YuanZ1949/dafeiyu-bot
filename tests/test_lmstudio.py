"""LM Studio 自启：只测编排，不真的拉起进程。"""

from __future__ import annotations

import pytest

from dafeiyu.brain import lmstudio
from dafeiyu.brain.lmstudio import EnsureResult, ensure_server
from dafeiyu.config import LocalModelConfig


def cfg(**overrides) -> LocalModelConfig:
    base = dict(base_url="http://127.0.0.1:1234/v1", autostart_timeout_seconds=1.0)
    base.update(overrides)
    return LocalModelConfig(**base)


async def test_already_running_without_model_does_not_spawn() -> None:
    calls: list[list[str]] = []

    async def probe(_url: str) -> bool:
        return True

    result = await ensure_server(cfg(), "", probe_fn=probe, spawn_fn=calls.append)

    assert result == EnsureResult(ready=True, started=False, detail="已在运行")
    assert calls == []


async def test_already_running_warms_requested_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """server 在跑不代表模型已加载：切过去时顺手发一个后台 load。"""
    monkeypatch.setattr(lmstudio, "find_lms", lambda _explicit="": "lms.exe")
    calls: list[list[str]] = []

    async def probe(_url: str) -> bool:
        return True

    result = await ensure_server(cfg(), "qwen/x", probe_fn=probe, spawn_fn=calls.append)

    assert result.ready is True
    assert result.started is False
    assert calls == [["lms.exe", "load", "qwen/x"]]


async def test_starts_server_then_loads_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lmstudio, "find_lms", lambda _explicit="": "lms.exe")
    probes = iter([False, False, True])
    calls: list[list[str]] = []

    async def probe(_url: str) -> bool:
        return next(probes)

    async def sleep(_seconds: float) -> None:
        return None

    result = await ensure_server(
        cfg(), "qwen/x", probe_fn=probe, spawn_fn=calls.append, sleep_fn=sleep
    )

    assert result.ready is True
    assert result.started is True
    assert calls == [["lms.exe", "server", "start"], ["lms.exe", "load", "qwen/x"]]


async def test_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lmstudio, "find_lms", lambda _explicit="": "lms.exe")
    calls: list[list[str]] = []

    async def probe(_url: str) -> bool:
        return False

    async def sleep(_seconds: float) -> None:
        return None

    result = await ensure_server(
        cfg(autostart_timeout_seconds=0.01),
        "qwen/x",
        probe_fn=probe,
        spawn_fn=calls.append,
        sleep_fn=sleep,
    )

    assert result.ready is False
    assert result.started is True
    assert result.detail == "启动超时"
    # 超时后不应再尝试 load 模型。
    assert calls == [["lms.exe", "server", "start"]]


async def test_missing_lms_cli_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lmstudio, "find_lms", lambda _explicit="": None)
    calls: list[list[str]] = []

    async def probe(_url: str) -> bool:
        return False

    result = await ensure_server(cfg(), "qwen/x", probe_fn=probe, spawn_fn=calls.append)

    assert result.ready is False
    assert result.started is False
    assert result.detail == "找不到 lms CLI"
    assert calls == []


async def test_autostart_can_be_disabled() -> None:
    result = await ensure_server(cfg(autostart=False))
    assert result.ready is False
    assert result.detail == "autostart 已关闭"
