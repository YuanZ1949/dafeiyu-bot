from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dafeiyu.brain import factory
from dafeiyu.brain.client import BrainError
from dafeiyu.brain.opencode import OpenCodeBrain
from dafeiyu.brain.opencode_http import OpenCodeHttpBrain
from dafeiyu.config import ConfigError, load_settings
from dafeiyu.memory.store import Store

BASE = """\
persona:
  name: 大肥鱼
  aliases: [肥鱼]
  max_reply_chars: 120
  max_reply_chars_technical: 300
  os_probability: 0.05
brain:
  provider: opencode
opencode:
  agent: deepseek-qq
  server_url: http://127.0.0.1:24310
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


def make_settings(tmp_path: Path, **opencode_overrides):
    config = tmp_path / "config.yaml"
    config.write_text(BASE, encoding="utf-8")
    env_file = tmp_path / "test.env"
    env_file.write_text("BOT_QQ=10001\n", encoding="utf-8")
    settings = load_settings(env_file=env_file, config_file=config)
    if opencode_overrides:
        settings = replace(
            settings, opencode=replace(settings.opencode, **opencode_overrides)
        )
    return settings


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "mem.db")
    s.init_schema()
    yield s
    s.close()


def test_default_transport_is_http(tmp_path: Path, store: Store) -> None:
    settings = make_settings(tmp_path)

    brain = factory.build_brain(settings, store, "opencode")

    assert isinstance(brain, OpenCodeHttpBrain)
    assert brain.base_url == "http://127.0.0.1:24310"


def test_transport_cli_builds_cli_brain(
    tmp_path: Path, store: Store, monkeypatch
) -> None:
    """回退开关必须真的能回退；这条分支以前一行测试都没有。"""
    monkeypatch.setattr(factory, "resolve_opencode_bin", lambda _bin: "opencode")
    settings = make_settings(tmp_path, transport="cli")

    brain = factory.build_brain(settings, store, "opencode")

    assert isinstance(brain, OpenCodeBrain)
    assert not isinstance(brain, OpenCodeHttpBrain)


def test_transport_cli_missing_bin_becomes_config_error(
    tmp_path: Path, store: Store, monkeypatch
) -> None:
    def boom(_bin: str):
        raise BrainError("找不到 opencode.exe")

    monkeypatch.setattr(factory, "resolve_opencode_bin", boom)
    settings = make_settings(tmp_path, transport="cli")

    with pytest.raises(ConfigError, match="opencode.exe"):
        factory.build_brain(settings, store, "opencode")
