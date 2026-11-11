"""supervisor 启动装配的回归：口令不进日志、bot 就绪探针跟随监听地址。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dafeiyu.config import load_settings
from dafeiyu.supervisor import main as main_mod
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.main import _probe_host, _report_dashboard_password

BASE = """\
persona:
  name: 大肥鱼
  aliases: [肥鱼]
  max_reply_chars: 120
  max_reply_chars_technical: 300
  os_probability: 0.05
triggers:
  cooldown_seconds: 60
  sample_rate: 0.05
  quiet_hours: ["01:00", "07:00"]
  keywords: [怎么]
groups:
  whitelist: []
  blacklist: []
budget:
  daily_calls: 200
  daily_tokens: 300000
memory:
  window_size: 20
  impression_topk: 5
  extract_every: 20
logging:
  level: INFO
  dir: logs
"""

ENV = "BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n"


@pytest.fixture(autouse=True)
def _fake_napcat(fake_napcat):
    """build_specs 需要 vendor/napcat/node.exe；测试不依赖本机安装状态。"""


def _settings(tmp_path: Path, control_host: str):
    config = tmp_path / "config.yaml"
    config.write_text(
        BASE
        + "\nsupervisor:\n  control:\n"
        + f'    host: "{control_host}"\n    port: 8788\n',
        encoding="utf-8",
    )
    env_file = tmp_path / "test.env"
    env_file.write_text(ENV, encoding="utf-8")
    return load_settings(env_file=env_file, config_file=config)


def _assert_secret_absent(logs: LogHub, password: str) -> str:
    text = "\n".join(logs.tail("supervisor"))
    assert password not in text
    return text


def test_password_not_logged_on_first_start(tmp_path: Path) -> None:
    """LogHub 会进环形缓冲、落盘、推 SSE：生成口令时只能记路径。"""
    logs = LogHub(tmp_path / "logs")
    password_path = tmp_path / "dashboard.password"
    password = "s3cr3t-面板口令-9f2c"
    password_path.write_text(password, encoding="utf-8")

    _report_dashboard_password(logs, password, existed=False, path=password_path)

    text = _assert_secret_absent(logs, password)
    assert str(password_path) in text
    assert "已生成" in text


@pytest.mark.parametrize("existed", [True, False])
def test_password_logging_never_contains_secret(
    tmp_path: Path, existed: bool
) -> None:
    logs = LogHub(tmp_path / "logs")
    password_path = tmp_path / "dashboard.password"
    password = "another-secret-42"
    password_path.write_text(password, encoding="utf-8")

    _report_dashboard_password(logs, password, existed=existed, path=password_path)

    text = _assert_secret_absent(logs, password)
    assert str(password_path) in text


def test_password_write_failure_only_logs_path(tmp_path: Path) -> None:
    """写盘失败分支（path 不存在）同样不能把口令带进日志。"""
    logs = LogHub(tmp_path / "logs")
    password_path = tmp_path / "missing" / "dashboard.password"
    password = "ephemeral-secret-7"

    _report_dashboard_password(logs, password, existed=False, path=password_path)

    text = _assert_secret_absent(logs, password)
    assert str(password_path) in text
    assert "写盘失败" in text


def test_probe_host_normalizes_wildcards() -> None:
    assert _probe_host("") == "127.0.0.1"
    assert _probe_host("  ") == "127.0.0.1"
    assert _probe_host("0.0.0.0") == "127.0.0.1"
    assert _probe_host("::") == "127.0.0.1"
    assert _probe_host("*") == "127.0.0.1"
    assert _probe_host(" 192.168.1.10 ") == "192.168.1.10"


@pytest.mark.parametrize(
    ("control_host", "expected_host"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("192.168.1.10", "192.168.1.10"),
        ("::", "127.0.0.1"),
        ("::1", "[::1]"),
    ],
)
def test_ready_url_follows_control_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_host: str,
    expected_host: str,
) -> None:
    """bot 监听 LAN IP 或 IPv6 时，探针不能再硬编码回环。"""
    monkeypatch.setattr(main_mod, "resolve_opencode_bin", lambda _bin: "opencode.exe")
    settings = _settings(tmp_path, control_host)

    specs = main_mod.build_specs(settings, "token-abc")

    bot = specs["bot"]
    assert bot.ready_url == (
        f"http://{expected_host}:{settings.supervisor.control_port}/api/health"
    )
    # env 仍传原始监听地址，不能被探针归一化污染。
    assert (bot.env or {})["DAFEIYU_CONTROL_HOST"] == control_host
