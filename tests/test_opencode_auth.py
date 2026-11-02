from __future__ import annotations

import re
from pathlib import Path

from dafeiyu.brain.opencode_auth import PASSWORD_ENV, password_env, resolve_password

HEX32 = re.compile(r"[0-9a-f]{32}")

# 最简可用配置：这些用例只关心 opencode 段，其余段给能过校验的最小值。
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


def load_with_opencode(tmp_path: Path, opencode_yaml: str):
    from dafeiyu.config import load_settings

    config = tmp_path / "config.yaml"
    config.write_text(BASE + "\nopencode:\n" + opencode_yaml, encoding="utf-8")
    env_file = tmp_path / "test.env"
    env_file.write_text(ENV, encoding="utf-8")
    return load_settings(env_file=env_file, config_file=config)


def test_configured_password_returned_as_is() -> None:
    assert resolve_password("P7-fixed-pass") == "P7-fixed-pass"


def test_configured_password_is_stripped() -> None:
    """值里的首尾空白不该带进环境变量，否则 serve 与 bot 的口令对不上。"""
    assert resolve_password("  P7-fixed-pass  ") == "P7-fixed-pass"


def test_blank_password_generates_32_hex() -> None:
    for blank in ("", "   "):
        assert HEX32.fullmatch(resolve_password(blank))


def test_blank_password_differs_each_call() -> None:
    """每次启动生成新口令；同一次启动内由调用方复用同一个值。"""
    assert resolve_password("") != resolve_password("")


def test_password_env_shape() -> None:
    assert PASSWORD_ENV == "OPENCODE_SERVER_PASSWORD"
    assert password_env("secret") == {PASSWORD_ENV: "secret"}


def test_manage_server_password_generated_and_written_back(
    tmp_path: Path, monkeypatch
) -> None:
    """bot 自己拉 serve：空配置时生成口令并写回 settings，供 build_brain 使用。"""
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    from dafeiyu.__main__ import ensure_manage_server_password

    settings = load_with_opencode(tmp_path, "  manage_server: true\n")
    assert settings.opencode.server_password == ""

    updated, password = ensure_manage_server_password(settings)

    assert HEX32.fullmatch(password)
    assert updated.opencode.server_password == password
    # frozen dataclass：原对象不能被就地改写。
    assert settings.opencode.server_password == ""


def test_manage_server_configured_password_kept(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    from dafeiyu.__main__ import ensure_manage_server_password

    settings = load_with_opencode(
        tmp_path, '  manage_server: true\n  server_password: "fixed-pass"\n'
    )

    updated, password = ensure_manage_server_password(settings)

    assert password == "fixed-pass"
    assert updated.opencode.server_password == "fixed-pass"


def test_main_module_importable_without_starting_serve() -> None:
    """导入入口模块只注册函数，不会启动 serve（真正的启动在 asyncio.run 里）。"""
    import dafeiyu.__main__ as entry

    assert callable(entry.main)
    assert callable(entry.ensure_manage_server_password)
