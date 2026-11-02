from __future__ import annotations

import re
from pathlib import Path

import pytest

from dafeiyu.brain.opencode_auth import PASSWORD_ENV
from dafeiyu.config import ConfigError, load_settings
from dafeiyu.supervisor.main import build_specs

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


def test_find_napcat_pids_returns_every_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """主进程崩溃后 worker 会留在同一目录：必须全部列出，不能只取一个。"""
    import dafeiyu.supervisor.main as main_mod

    class Result:
        stdout = "4321\r\n8765\r\n\r\n"
        returncode = 0

    monkeypatch.setattr(main_mod.subprocess, "run", lambda *a, **k: Result())

    assert main_mod.find_napcat_pids(Path("C:/napcat")) == [4321, 8765]
    assert main_mod.find_napcat_pid(Path("C:/napcat")) == 4321
