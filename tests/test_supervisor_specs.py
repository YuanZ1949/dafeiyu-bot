from __future__ import annotations

from pathlib import Path

from dafeiyu.config import load_settings
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
