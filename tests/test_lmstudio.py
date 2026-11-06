"""LM Studio 自启/自动停止：只测编排，不真的拉起/停掉进程。"""

from __future__ import annotations

import logging

import pytest

from dafeiyu.brain import lmstudio
from dafeiyu.brain.lmstudio import EnsureResult, ensure_server, stop_server
from dafeiyu.config import LocalModelConfig


def cfg(**overrides) -> LocalModelConfig:
    base = {"base_url": "http://127.0.0.1:1234/v1", "autostart_timeout_seconds": 1.0}
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


async def test_stop_skips_when_endpoint_is_down() -> None:
    """端点本来就不可用 = 没有遗留进程，不该再调 lms。"""
    calls: list[list[str]] = []

    async def probe(_url: str) -> bool:
        return False

    result = await stop_server(cfg(), probe_fn=probe, stop_fn=calls.append)

    assert result == EnsureResult(ready=False, started=False, detail="未在运行")
    assert calls == []


async def test_stop_calls_lms_when_endpoint_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lmstudio, "find_lms", lambda _explicit="": "lms.exe")
    calls: list[list[str]] = []

    async def probe(_url: str) -> bool:
        return True

    result = await stop_server(cfg(), probe_fn=probe, stop_fn=calls.append)

    assert result == EnsureResult(ready=False, started=True, detail="已停止")
    assert calls == [["lms.exe", "server", "stop"]]


async def test_stop_without_lms_cli_only_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(lmstudio, "find_lms", lambda _explicit="": None)
    calls: list[list[str]] = []

    async def probe(_url: str) -> bool:
        return True

    with caplog.at_level(logging.WARNING, logger="dafeiyu.brain.lmstudio"):
        result = await stop_server(cfg(), probe_fn=probe, stop_fn=calls.append)

    assert result == EnsureResult(ready=True, started=False, detail="找不到 lms CLI")
    assert calls == []
    assert any("找不到 lms CLI" in rec.getMessage() for rec in caplog.records)


async def test_stop_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(lmstudio, "find_lms", lambda _explicit="": "lms.exe")

    async def probe(_url: str) -> bool:
        return True

    def boom(_args: list[str]) -> None:
        raise OSError("denied")

    with caplog.at_level(logging.WARNING, logger="dafeiyu.brain.lmstudio"):
        result = await stop_server(cfg(), probe_fn=probe, stop_fn=boom)

    assert result.ready is True
    assert result.started is False
    assert "停止失败" in result.detail
    assert any(
        "停止 LM Studio server 失败" in rec.getMessage() for rec in caplog.records
    )
