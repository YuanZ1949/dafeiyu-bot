"""``dafeiyu.supervisor.hook_watch`` 的纯逻辑测试（不真起 hook / powershell）。"""

from __future__ import annotations

import asyncio

import pytest

from dafeiyu.supervisor.hook_watch import (
    health_url,
    hook_loop,
    probe_ready,
    should_restart,
)


def test_health_url_derives_health_from_send_endpoint() -> None:
    assert health_url("http://127.0.0.1:30012/send_text") == "http://127.0.0.1:30012/health"
    assert health_url(" http://127.0.0.1:30012/send_text/ ") == "http://127.0.0.1:30012/health"
    assert health_url("") == ""
    assert health_url("http://127.0.0.1:30012") == ""


def test_probe_ready_rejects_blank_url() -> None:
    assert probe_ready("") is False


def test_should_restart_requires_failures_and_cooldown() -> None:
    assert should_restart(True, 5, 1000.0, None) is False
    assert should_restart(False, 1, 1000.0, None) is False
    assert should_restart(False, 2, 1000.0, None) is True
    assert should_restart(False, 2, 1100.0, 1000.0, cooldown=300.0) is False
    assert should_restart(False, 2, 1300.0, 1000.0, cooldown=300.0) is True


async def test_hook_loop_restarts_after_repeated_failures() -> None:
    stop = asyncio.Event()
    calls: list[tuple[int, str]] = []
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])

    def clock() -> float:
        value = next(ticks)
        if value >= 3.0:
            stop.set()
        return value

    def starter(port: int, account: str) -> bool:
        calls.append((port, account))
        return True

    await asyncio.wait_for(
        hook_loop(
            "http://127.0.0.1:30012/send_text",
            "wxid_test",
            stop,
            interval=0.01,
            probe=lambda _url: False,
            starter=starter,
            clock=clock,
        ),
        timeout=1,
    )

    assert calls == [(30012, "wxid_test")]


async def test_hook_loop_does_not_restart_a_ready_hook() -> None:
    stop = asyncio.Event()
    calls: list[tuple[int, str]] = []

    def clock() -> float:
        stop.set()
        return 0.0

    await asyncio.wait_for(
        hook_loop(
            "http://127.0.0.1:30012/send_text",
            "wxid_test",
            stop,
            interval=0.01,
            probe=lambda _url: True,
            starter=lambda port, account: calls.append((port, account)) or True,
            clock=clock,
        ),
        timeout=1,
    )

    assert calls == []


async def test_hook_loop_ignores_blank_or_portless_url() -> None:
    stop = asyncio.Event()
    stop.set()

    def no_probe(_url: str) -> bool:
        pytest.fail("没有可推导的 health 地址时不该探测")

    await hook_loop("", "wxid_test", stop, probe=no_probe)
    await hook_loop("http://127.0.0.1/send_text", "wxid_test", stop, probe=no_probe)


def test_restart_hook_stops_the_old_instance_first(monkeypatch, tmp_path) -> None:
    from dafeiyu.supervisor import hook_watch

    script = tmp_path / "start-wechat-hook.ps1"
    script.write_text("# fake", encoding="utf-8")
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return Result()

    monkeypatch.setattr(hook_watch.subprocess, "run", fake_run)

    assert hook_watch.restart_hook(30012, "wxid_test", script=script) is True
    assert calls[0][-1] == "-Stop"
    assert calls[1][-2:] == ["-Account", "wxid_test"]


def test_restart_hook_reports_missing_script(tmp_path) -> None:
    from dafeiyu.supervisor import hook_watch

    assert not hook_watch.restart_hook(30012, "wxid_test", script=tmp_path / "nope.ps1")
