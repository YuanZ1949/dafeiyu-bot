"""R17：端口兜底要同时认 IPv4/IPv6，并区分「端口被别的进程占着」。"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from dafeiyu.supervisor import manager as manager_mod
from dafeiyu.supervisor import stop as stop_mod
from dafeiyu.supervisor.stop import StopResult

windows_only = pytest.mark.skipif(
    os.name != "nt", reason="netstat 端口兜底只在 Windows 上启用"
)

TCP_IPV4 = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       4321
  TCP    127.0.0.1:8080         127.0.0.1:55000        ESTABLISHED     4321
"""

TCP_IPV6 = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    [::1]:8080             [::]:0                 LISTENING       1234
"""


def _fake_run(outputs: dict[str, str], calls: list[str] | None = None):
    """按 `netstat -ano -p <TCP|TCPv6>` 的 family 返回对应文本。"""

    def fake(args, **kwargs):  # noqa: ANN001, ANN003 - 与 subprocess.run 同形
        family = args[args.index("-p") + 1]
        if calls is not None:
            calls.append(family)
        return SimpleNamespace(stdout=outputs.get(family, ""), returncode=0)

    return fake


@windows_only
def test_find_listener_pid_parses_tcp_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subprocess.run", _fake_run({"TCP": TCP_IPV4, "TCPv6": ""})
    )

    assert manager_mod._find_listener_pid(8080) == 4321


@windows_only
def test_find_listener_pid_parses_tcpv6_table(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "subprocess.run", _fake_run({"TCP": "", "TCPv6": TCP_IPV6}, calls)
    )

    assert manager_mod._find_listener_pid(8080) == 1234
    # IPv4 表里没有，必须真的去查了 TCPv6 表。
    assert calls == ["TCP", "TCPv6"]


def test_listener_pid_from_netstat_matches_last_segment_as_port() -> None:
    line = "  TCP    [::1]:8080    [::]:0    LISTENING    1234"

    # 用最后一段端口匹配：IPv6 地址里的冒号不能被当成分隔符。
    assert manager_mod._listener_pid_from_netstat(line, 8080) == 1234
    assert manager_mod._listener_pid_from_netstat(line, 80) is None
    assert manager_mod._listener_pid_from_netstat(TCP_IPV4, 8080) == 4321
    # 只有 LISTENING 才算占用（ESTABLISHED 的 8080→55000 行不能算）。
    established = "  TCP    127.0.0.1:8080    127.0.0.1:55000    ESTABLISHED    4321"
    assert manager_mod._listener_pid_from_netstat(established, 8080) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not a netstat line\n",
        "  TCP    [::1]:8080    [::]:0    LISTENING    not-a-pid\n",
        "  TCP    [::1]:8080\n",
    ],
)
def test_listener_pid_from_netstat_tolerates_malformed_output(text: str) -> None:
    # 格式异常只能表示「查不到」，绝不抛异常。
    assert manager_mod._listener_pid_from_netstat(text, 8080) is None


@windows_only
def test_find_listener_pid_returns_none_on_empty_or_broken_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "subprocess.run", _fake_run({"TCP": "", "TCPv6": "garbage output"})
    )
    assert manager_mod._find_listener_pid(8080) is None

    def boom(args, **kwargs):  # noqa: ANN001, ANN003
        raise OSError("netstat missing")

    monkeypatch.setattr("subprocess.run", boom)
    assert manager_mod._find_listener_pid(8080) is None


def test_port_owner_falls_back_to_ipv6_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[tuple[str, int]] = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):  # noqa: ANN002
            return False

    def fake_create_connection(address, timeout=0.0):  # noqa: ANN001
        attempts.append(address)
        if address[0] == "127.0.0.1":
            raise OSError("no ipv4 listener")
        return _Conn()

    monkeypatch.setattr(
        manager_mod.socket, "create_connection", fake_create_connection
    )
    monkeypatch.setattr(manager_mod, "_find_listener_pid", lambda port: 4321)

    assert manager_mod.port_owner(8080) == 4321
    assert attempts == [("127.0.0.1", 8080), ("::1", 8080)]


def test_port_owner_returns_none_when_neither_family_listens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(address, timeout=0.0):  # noqa: ANN001
        raise OSError("closed")

    monkeypatch.setattr(manager_mod.socket, "create_connection", refuse)

    assert manager_mod.port_owner(8080) is None


@windows_only
async def test_stop_reports_port_busy_when_supervisor_gone(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pid 已死、端口仍被无关进程占着：failed + port_busy，且绝不去杀它。"""
    path = tmp_path / "supervisor.pid"
    path.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(stop_mod, "pid_alive", lambda pid: False)
    calls = {"shutdown": 0}

    async def fake_shutdown(host, port, *, username, password):  # noqa: ANN001
        calls["shutdown"] += 1
        return True

    result = await stop_mod.stop_supervisor(
        port=8080,
        pid_path=path,
        shutdown_fn=fake_shutdown,
        port_finder=lambda port: 4321,
        cmdline_fn=lambda pid: r"C:\Windows\system32\notepad.exe",
    )

    assert isinstance(result, StopResult)
    assert result.status == "failed"
    assert result.port_busy is True
    assert result.pid == 999999
    assert calls["shutdown"] == 0
    assert not path.exists()


async def test_stop_reports_not_running_when_port_is_free(tmp_path) -> None:
    path = tmp_path / "supervisor.pid"
    path.write_text("999999", encoding="utf-8")

    result = await stop_mod.stop_supervisor(
        port=8080,
        pid_path=path,
        port_finder=lambda port: None,
    )

    assert result.status == "not_running"
    assert result.port_busy is False


def test_cli_exit_code_and_text_distinguish_port_busy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dashboard = SimpleNamespace(port=8080, username="admin")

    def fake_load_settings():
        return SimpleNamespace(supervisor=SimpleNamespace(dashboard=dashboard))

    async def fake_stop_supervisor(**kwargs):  # noqa: ANN003
        return StopResult(status="failed", pid=999999, stale=True, port_busy=True)

    monkeypatch.setattr(stop_mod, "load_settings", fake_load_settings)
    monkeypatch.setattr(
        stop_mod, "ensure_dashboard_password", lambda dashboard: ""
    )
    monkeypatch.setattr(stop_mod, "stop_supervisor", fake_stop_supervisor)

    code = stop_mod.main()
    err = capsys.readouterr().err

    assert code == stop_mod.EXIT_PORT_BUSY
    assert code != stop_mod.EXIT_FAILED
    assert "8080" in err
    assert "占用" in err
