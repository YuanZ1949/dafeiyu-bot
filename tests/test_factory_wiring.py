"""Task 13：旁路 worker 的 provider 接线与 build_brain 的 memory_agent 透传。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from dafeiyu.__main__ import build_workers
from dafeiyu.brain import factory
from dafeiyu.brain.client import BrainClient
from dafeiyu.config import load_settings
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

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


def make_settings(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(BASE, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n", encoding="utf-8")
    return load_settings(env_file=env, config_file=config)


def make_store() -> Store:
    store = Store(":memory:")
    store.init_schema()
    return store


class FakeCompleteBrain:
    """只有 complete_detailed 的脑：opencode HTTP 分支就是这个形状。"""

    async def complete_detailed(self, system, messages, max_tokens=400, temperature=0.2):
        return ("", 0, 0)


def test_worker_wiring_includes_opencode_http(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = make_store()
    try:
        workers = build_workers(settings, store, FakeCompleteBrain(), "opencode")
        assert workers.impressions is not None
        assert workers.scheduler is not None
    finally:
        store.close()


def test_worker_wiring_skips_cli_brain(tmp_path: Path) -> None:
    """CLI 传输没有 complete*，建了 worker 也只会在运行时 AttributeError。"""
    settings = make_settings(tmp_path)
    store = make_store()
    try:
        workers = build_workers(settings, store, SimpleNamespace(), "opencode")
        assert workers.impressions is None
        assert workers.scheduler is None
    finally:
        store.close()


def test_worker_wiring_keeps_token_provider(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = make_store()
    try:
        workers = build_workers(settings, store, FakeCompleteBrain(), "token")
        assert workers.impressions is not None
        assert workers.scheduler is not None
    finally:
        store.close()


def test_build_brain_passes_memory_agent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = make_store()
    try:
        default = factory.build_brain(settings, store, "opencode")
        assert default.memory_agent == "dafeiyu-memory"
        custom = factory.build_brain(
            settings, store, "opencode", memory_agent="dafeiyu-memory-v2"
        )
        assert custom.memory_agent == "dafeiyu-memory-v2"
    finally:
        store.close()


class RecordingBrain:
    """记录调用次数，用来验证 worker 是否跟随 dashboard 换脑。"""

    def __init__(self, payload: str = "[]") -> None:
        self.payload = payload
        self.calls = 0

    async def complete_detailed(self, system, messages, max_tokens=400, temperature=0.2):
        self.calls += 1
        return (self.payload, 12, 6)


@pytest.mark.asyncio
async def test_memory_workers_follow_brain_switch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = make_store()
    try:
        store.add_message(
            Message(ChatType.GROUP, 20001, 1, "鱼片", "周末去钓鱼吗", "m1")
        )
        old = RecordingBrain()
        new = RecordingBrain(
            '[{"user_id": 1, "kind": "preference", "content": "喜欢钓鱼", "confidence": 0.9}]'
        )
        current = {"brain": old}
        workers = build_workers(
            settings, store, old, "token", brain_getter=lambda: current["brain"]
        )
        assert workers.impressions is not None
        assert workers.scheduler is not None

        current["brain"] = new
        added = await workers.impressions.extract(ChatType.GROUP, 20001)

        assert added == 1
        assert (old.calls, new.calls) == (0, 1)
        assert workers.scheduler._current_brain() is new
    finally:
        store.close()


def test_build_brain_five_arg_call_still_works(tmp_path: Path) -> None:
    """dashboard 的 switch() 仍按旧签名调用，不能因为新参数断掉。"""
    settings = make_settings(tmp_path)
    store = make_store()
    try:
        brain = factory.build_brain(settings, store, "token", "", "")
        assert isinstance(brain, BrainClient)
    finally:
        store.close()
