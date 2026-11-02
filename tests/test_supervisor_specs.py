from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self

import pytest

from dafeiyu.brain.opencode_auth import PASSWORD_ENV
from dafeiyu.config import ConfigError, RestartConfig, load_settings
from dafeiyu.supervisor.logs import LogHub
from dafeiyu.supervisor.main import WECHAT_BRIDGE_TOKEN_ENV, build_specs
from dafeiyu.supervisor.manager import Supervisor

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


def build(tmp_path: Path, env: str = ENV):
    config = tmp_path / "config.yaml"
    config.write_text(BASE, encoding="utf-8")
    env_file = tmp_path / "test.env"
    env_file.write_text(env, encoding="utf-8")
    settings = load_settings(env_file=env_file, config_file=config)
    return settings, build_specs(settings, "token-abc")


def test_napcat_quick_login_uses_bot_qq(tmp_path: Path) -> None:
    """-q 必须带机器人号：本机快速登录列表里还有用户的个人号，不给 uin 会登错号。"""
    settings, specs = build(tmp_path)

    command = specs["napcat"].command
    assert command[-2:] == ["-q", str(settings.bot_qq)]
    assert str(settings.bot_qq) == "10001"


def test_napcat_quick_login_env_var_is_gone(tmp_path: Path) -> None:
    """NAPCAT_QUICK_LOGIN 在这版 NapCat 里没有任何代码读取，留着只会假装配置生效了。"""
    _settings, specs = build(tmp_path)

    assert "NAPCAT_QUICK_LOGIN" not in (specs["napcat"].env or {})


def test_napcat_password_fallback_goes_to_env(tmp_path: Path) -> None:
    """快登失败后 NapCat 会用这个环境变量再试一次密码登录。"""
    _settings, specs = build(tmp_path, ENV + "NAPCAT_QUICK_PASSWORD=hunter2\n")

    assert (specs["napcat"].env or {})["NAPCAT_QUICK_PASSWORD"] == "hunter2"


def test_napcat_password_md5_goes_to_env(tmp_path: Path) -> None:
    md5 = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    _settings, specs = build(tmp_path, ENV + f"NAPCAT_QUICK_PASSWORD_MD5={md5}\n")

    assert (specs["napcat"].env or {})["NAPCAT_QUICK_PASSWORD_MD5"] == md5


def test_napcat_without_password_keeps_inherited_env(tmp_path: Path) -> None:
    """没配密码就别塞 env，让 NapCat 直接继承父进程环境。"""
    _settings, specs = build(tmp_path)

    assert specs["napcat"].env is None


def test_invalid_quick_password_md5_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="quick_password_md5"):
        build(tmp_path, ENV + "NAPCAT_QUICK_PASSWORD_MD5=not-a-md5\n")


def test_opencode_spec_carries_isolated_config_home(tmp_path: Path) -> None:
    """真正拉起 serve 的是 supervisor（bot 侧 manage_server=false），
    隔离的 XDG_CONFIG_HOME 必须在这里注入，否则 Task 13.1 形同虚设。"""
    cfg = tmp_path / "cfg"
    config = tmp_path / "config.yaml"
    config.write_text(
        BASE + f"\nopencode:\n  config_home: {cfg.as_posix()}\n", encoding="utf-8"
    )
    env_file = tmp_path / "test.env"
    env_file.write_text(ENV, encoding="utf-8")
    settings = load_settings(env_file=env_file, config_file=config)

    specs = build_specs(settings, "token-abc")

    assert (specs["opencode"].env or {})["XDG_CONFIG_HOME"] == str(cfg.resolve())
    assert (specs["opencode"].env or {})["OPENCODE_CONFIG"] == str(
        cfg.resolve() / "opencode" / "opencode.jsonc"
    )
    assert (cfg / "opencode" / "opencode.jsonc").exists()


def test_opencode_spec_without_config_home_keeps_inherited_env(tmp_path: Path) -> None:
    """没配 config_home 就不要塞，保持软回滚（沿用启动环境的配置）。"""
    _settings, specs = build(tmp_path)

    env = specs["opencode"].env or {}
    assert "XDG_CONFIG_HOME" not in env
    assert "OPENCODE_CONFIG" not in env


def build_with_opencode(tmp_path: Path, opencode_yaml: str):
    config = tmp_path / "config.yaml"
    config.write_text(BASE + "\nopencode:\n" + opencode_yaml, encoding="utf-8")
    env_file = tmp_path / "test.env"
    env_file.write_text(ENV, encoding="utf-8")
    settings = load_settings(env_file=env_file, config_file=config)
    return settings, build_specs(settings, "token-abc")


WECHAT_TOKEN = "wx-bridge-secret-6f1c"


def build_wechat(
    tmp_path: Path,
    *,
    provider: str = "wechat",
    enabled: bool = True,
    token: str = WECHAT_TOKEN,
):
    config = tmp_path / "config.yaml"
    config.write_text(
        BASE
        + "\ngateway:\n"
        + f"  provider: {provider}\n"
        + "  wechat:\n"
        + f"    enabled: {'true' if enabled else 'false'}\n"
        + f"    bridge_token: {token}\n"
        + '    group_allowlist: ["wxg:filehelper"]\n'
        + "    account_nickname: 大肥鱼\n",
        encoding="utf-8",
    )
    env_file = tmp_path / "test.env"
    env_file.write_text(ENV, encoding="utf-8")
    settings = load_settings(env_file=env_file, config_file=config)
    return settings, build_specs(settings, "token-abc")


def test_opencode_server_password_injected_into_serve_and_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7：真正拉起 serve 的是 supervisor，配置里的口令必须同时给两个子进程。"""
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    _settings, specs = build_with_opencode(
        tmp_path, '  server_password: "fixed-pass"\n'
    )

    assert (specs["opencode"].env or {})[PASSWORD_ENV] == "fixed-pass"
    assert (specs["bot"].env or {})[PASSWORD_ENV] == "fixed-pass"


def test_opencode_server_password_generated_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没配口令时 supervisor 生成一次，opencode 与 bot 必须拿到同一个 32 位口令。"""
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    _settings, specs = build(tmp_path)

    password = (specs["opencode"].env or {})[PASSWORD_ENV]
    assert re.fullmatch(r"[0-9a-f]{32}", password)
    assert (specs["bot"].env or {})[PASSWORD_ENV] == password
def test_wechat_bridge_registered_when_provider_selects_wechat(tmp_path: Path) -> None:
    settings, specs = build_wechat(tmp_path)

    assert settings.gateway.provider == "wechat"
    assert set(specs) == {"opencode", "bot", "wechat_bridge", "napcat"}
    assert list(specs).index("wechat_bridge") > list(specs).index("bot")


def test_wechat_bridge_registered_for_provider_both(tmp_path: Path) -> None:
    _settings, specs = build_wechat(tmp_path, provider="both")

    assert "wechat_bridge" in specs


def test_wechat_bridge_absent_when_disabled(tmp_path: Path) -> None:
    """enabled=false：配置层把 provider 归一成 qq，桥不该注册。"""
    settings, specs = build_wechat(tmp_path, enabled=False)

    assert set(specs) == {"opencode", "bot", "napcat"}
    assert settings.gateway.provider == "qq"


def test_wechat_bridge_absent_when_provider_is_qq(tmp_path: Path) -> None:
    """反自锁：provider=qq + enabled=true 没有 WeChatAdapter，桥必须不注册。"""
    settings, specs = build_wechat(tmp_path, provider="qq", enabled=True)

    assert settings.wechat.enabled is True
    assert settings.gateway.provider == "qq"
    assert set(specs) == {"opencode", "bot", "napcat"}


def test_wechat_bridge_command_env_and_token_hygiene(tmp_path: Path) -> None:
    _settings, specs = build_wechat(tmp_path)
    spec = specs["wechat_bridge"]

    assert spec.command == [
        sys.executable,
        "-m",
        "dafeiyu.gateway.wechat_bridge",
    ]
    assert spec.command[-1] == "dafeiyu.gateway.wechat_bridge"
    assert (spec.env or {})[WECHAT_BRIDGE_TOKEN_ENV] == WECHAT_TOKEN
    # token 只走 env：argv 与 repr 都不能带出去。
    assert WECHAT_TOKEN not in " ".join(spec.command)
    assert WECHAT_TOKEN not in repr(spec)
    assert WECHAT_TOKEN not in repr(spec.command)


def test_wechat_bridge_has_no_ready_url_but_a_probe(tmp_path: Path) -> None:
    _settings, specs = build_wechat(tmp_path)
    spec = specs["wechat_bridge"]

    assert spec.ready_url == ""
    assert callable(spec.ready_probe)
    assert spec.ready_timeout == 90.0
    # 其他 spec 不受覆盖影响，仍走 Supervisor.ready_timeout。
    assert specs["bot"].ready_timeout is None
    assert specs["opencode"].ready_timeout is None


async def test_disabled_wechat_never_spawns_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, specs = build_wechat(tmp_path, enabled=False)
    spawned: list[str] = []

    async def fake_start(self: Supervisor, name: str) -> None:
        spawned.append(name)

    monkeypatch.setattr(Supervisor, "start", fake_start)
    supervisor = Supervisor(
        specs, LogHub(tmp_path / "logs"), RestartConfig(backoff_seconds=(0.01,))
    )
    await supervisor.start_all()

    assert spawned == ["opencode", "bot", "napcat"]
    assert "wechat_bridge" not in spawned


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch, body: bytes = b"{}", status: int = 200
):
    import dafeiyu.supervisor.main as main_mod

    seen: list[tuple[str, dict[str, str], float | None]] = []

    def fake_urlopen(request, timeout=None):
        seen.append(
            (
                request.full_url,
                dict(request.header_items()),
                timeout,
            )
        )
        return _FakeResponse(body, status=status)

    monkeypatch.setattr(main_mod.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_wechat_bridge_probe_hits_bot_control_port_health_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, specs = build_wechat(tmp_path)
    seen = _patch_urlopen(
        monkeypatch, json.dumps({"channels": {"wechat": {"connected": True}}}).encode()
    )

    assert specs["wechat_bridge"].ready_probe() is True

    assert len(seen) == 1
    url, headers, timeout = seen[0]
    assert url == (
        f"http://127.0.0.1:{settings.supervisor.control_port}/api/health/detail"
    )
    assert f":{settings.supervisor.control_port}/" in url
    assert f":{settings.wechat.bridge_port}/" not in url
    assert settings.supervisor.control_port != settings.wechat.bridge_port
    assert {k.casefold(): v for k, v in headers.items()}["x-control-token"] == (
        "token-abc"
    )
    assert timeout is not None and timeout <= 3.0


def test_wechat_bridge_probe_false_for_ok_only_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/api/health` 那种 {"ok": true} 没有 channels，必须判否。"""
    _settings, specs = build_wechat(tmp_path)
    _patch_urlopen(monkeypatch, json.dumps({"ok": True}).encode())

    assert specs["wechat_bridge"].ready_probe() is False


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        json.dumps({"channels": {}}).encode(),
        json.dumps({"channels": {"wechat": {}}}).encode(),
        json.dumps({"channels": {"wechat": {"connected": "yes"}}}).encode(),
        json.dumps({"channels": {"qq": {"connected": True}}}).encode(),
        json.dumps([]).encode(),
    ],
)
def test_wechat_bridge_probe_false_for_missing_or_invalid_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    _settings, specs = build_wechat(tmp_path)
    _patch_urlopen(monkeypatch, body)

    assert specs["wechat_bridge"].ready_probe() is False


def test_wechat_bridge_probe_false_on_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dafeiyu.supervisor.main as main_mod

    _settings, specs = build_wechat(tmp_path)

    def unauthorized(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", None, None)

    monkeypatch.setattr(main_mod.urllib.request, "urlopen", unauthorized)

    assert specs["wechat_bridge"].ready_probe() is False


def test_wechat_bridge_probe_false_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dafeiyu.supervisor.main as main_mod

    _settings, specs = build_wechat(tmp_path)

    def timed_out(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(main_mod.urllib.request, "urlopen", timed_out)

    assert specs["wechat_bridge"].ready_probe() is False


def test_wechat_bridge_probe_false_on_non_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, specs = build_wechat(tmp_path)
    _patch_urlopen(
        monkeypatch,
        json.dumps({"channels": {"wechat": {"connected": True}}}).encode(),
        status=500,
    )

    assert specs["wechat_bridge"].ready_probe() is False


def test_find_napcat_pids_returns_every_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """主进程崩溃后 worker 会留在同一目录：必须全部列出，不能只取一个。"""
    import dafeiyu.supervisor.main as main_mod

    class Result:
        stdout = "4321\r\n8765\r\n\r\n"
        returncode = 0

    monkeypatch.setattr(main_mod.subprocess, "run", lambda *a, **k: Result())

    assert main_mod.find_napcat_pids(Path("C:/napcat")) == [4321, 8765]
    assert main_mod.find_napcat_pid(Path("C:/napcat")) == 4321
