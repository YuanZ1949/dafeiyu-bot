"""Task 13：旁路 worker 的 provider 接线与 build_brain 的 memory_agent 透传。"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dafeiyu.__main__ import (
    build_adapters,
    build_workers,
    log_active_channels,
    wechat_enabled_for,
)
from dafeiyu.brain import factory
from dafeiyu.brain.client import BrainClient
from dafeiyu.config import (
    GatewayConfig,
    load_settings,
    observer_uses_local_model,
)
from dafeiyu.gateway.channels import qq_enabled_for
from dafeiyu.gateway.wechat import WeChatAdapter
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


def test_observer_uses_local_model_requires_local_provider(tmp_path: Path) -> None:
    """启动时要不要拉起/保留 LM Studio 全看这个判断，必须与 build_workers 一致。"""
    settings = make_settings(tmp_path)
    # 默认 mind.enabled=false：本地端点与旁路无关。
    assert observer_uses_local_model(settings) is False

    enabled = replace(
        settings,
        mind=replace(settings.mind, enabled=True, observer_provider="local"),
        local_model=replace(
            settings.local_model, base_url="http://127.0.0.1:1234/v1", model="qwen/x"
        ),
    )
    assert observer_uses_local_model(enabled) is True

    # 旁路复用主脑时不占用本地端点。
    on_main = replace(
        enabled, mind=replace(enabled.mind, observer_provider="main")
    )
    assert observer_uses_local_model(on_main) is False

    # 端点/模型没配置时 build_workers 会跳过旁路，这里也必须返回 False。
    unset = replace(
        enabled, local_model=replace(enabled.local_model, model="")
    )
    assert observer_uses_local_model(unset) is False


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
            '[{"user_id": "qq:1", "kind": "preference", "content": "喜欢钓鱼", "confidence": 0.9}]'
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


def test_worker_wiring_warns_when_skipping(tmp_path: Path, caplog) -> None:
    """跳过旁路必须说出来：用户不该靠面板空白去猜记忆为什么不涨。"""
    settings = make_settings(tmp_path)
    store = make_store()
    try:
        with caplog.at_level(logging.WARNING, logger="dafeiyu.__main__"):
            workers = build_workers(settings, store, SimpleNamespace(), "opencode")
    finally:
        store.close()

    assert workers.scheduler is None
    text = caplog.text
    assert "没有 complete_detailed" in text
    assert "opencode" in text
    assert f"transport={settings.opencode.transport}" in text


# ---- Task 20：gateway.provider 通道接线 -------------------------------------


def _gateway_yaml(provider: str, enabled: bool) -> str:
    return (
        "gateway:\n"
        f"  provider: {provider}\n"
        "  wechat:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    bridge_port: 8791\n"
        "    account_nickname: 大肥鱼\n"
    )


def make_gateway_settings(
    tmp_path: Path, provider: str, *, enabled: bool
):
    config = tmp_path / "config.yaml"
    config.write_text(BASE + _gateway_yaml(provider, enabled), encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text(
        "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n"
        "DAFEIYU_WECHAT_BRIDGE_TOKEN=secret-token\n",
        encoding="utf-8",
    )
    return load_settings(env_file=env, config_file=config)


class ForbiddenWeChat:
    def __init__(self, config) -> None:
        raise AssertionError("不应构造 WeChatAdapter")


def test_provider_qq_constructs_no_wechat_adapter(tmp_path: Path, monkeypatch) -> None:
    settings = make_gateway_settings(tmp_path, "qq", enabled=True)
    built: list[object] = []

    class RecordingWeChat:
        def __init__(self, config) -> None:
            built.append(config)

    monkeypatch.setattr("dafeiyu.__main__.WeChatAdapter", RecordingWeChat)

    adapters = build_adapters(settings)

    assert set(adapters) == {"qq"}
    assert built == []
    assert wechat_enabled_for(settings) is False


def test_provider_qq_with_wechat_enabled_has_no_bridge_spec(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_gateway_settings(tmp_path, "qq", enabled=True)
    monkeypatch.setattr("dafeiyu.__main__.WeChatAdapter", ForbiddenWeChat)

    assert set(build_adapters(settings)) == {"qq"}

    import dafeiyu.supervisor.main as supervisor_main

    node_dir = tmp_path / "napcat"
    node_dir.mkdir()
    (node_dir / "node.exe").write_bytes(b"")
    monkeypatch.setattr(supervisor_main, "NAPCAT_DIR", node_dir)
    monkeypatch.setattr(
        supervisor_main, "resolve_opencode_bin", lambda _bin: "opencode.exe"
    )

    specs = supervisor_main.build_specs(settings, "token-abc")

    assert "wechat_bridge" not in specs


def test_provider_both_constructs_both_adapters(tmp_path: Path) -> None:
    settings = make_gateway_settings(tmp_path, "both", enabled=True)

    assert wechat_enabled_for(settings) is True
    adapters = build_adapters(settings)

    assert set(adapters) == {"qq", "wechat"}
    assert isinstance(adapters["wechat"], WeChatAdapter)


def test_qq_enabled_for_mirrors_adapter_wiring(tmp_path: Path) -> None:
    """supervisor 用它决定是否拉起 NapCat，必须与 build_adapters 逐条对齐。"""
    assert qq_enabled_for(make_gateway_settings(tmp_path, "qq", enabled=True)) is True
    assert qq_enabled_for(make_gateway_settings(tmp_path, "both", enabled=True)) is True
    assert (
        qq_enabled_for(make_gateway_settings(tmp_path, "wechat", enabled=True)) is False
    )
    # 微信没启用时 build_adapters 会回落 QQ，NapCat 必须照常启动。
    assert (
        qq_enabled_for(make_gateway_settings(tmp_path, "wechat", enabled=False)) is True
    )


def test_provider_wechat_without_enabled_degrades_with_warning(
    tmp_path: Path, caplog
) -> None:
    with caplog.at_level(logging.WARNING, logger="dafeiyu.config"):
        settings = make_gateway_settings(tmp_path, "wechat", enabled=False)

    assert settings.gateway.provider == "qq"
    assert any(
        "gateway.provider=wechat" in record.getMessage()
        and "降级为 qq" in record.getMessage()
        for record in caplog.records
    )

    # 配置层已经改写了 provider；接线层对「provider=wechat 且未启用」也必须
    # 自己兜底，不能把微信适配器建出来。
    forced = replace(settings, gateway=GatewayConfig(provider="wechat"))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dafeiyu.__main__"):
        adapters = build_adapters(forced)

    assert set(adapters) == {"qq"}
    assert "降级为 qq" in caplog.text


def test_provider_qq_never_reads_bridge_token(tmp_path: Path) -> None:
    settings = make_gateway_settings(tmp_path, "qq", enabled=True)
    inner = settings.wechat
    assert inner.bridge_token == "secret-token"
    accessed: list[str] = []

    class SpyWeChat:
        def __getattr__(self, name: str):
            accessed.append(name)
            if name == "bridge_token":
                raise AssertionError(
                    "provider=qq 不应读取 settings.wechat.bridge_token"
                )
            return getattr(inner, name)

    spied = replace(settings, wechat=SpyWeChat())
    adapters = build_adapters(spied)

    assert set(adapters) == {"qq"}
    assert "bridge_token" not in accessed


def test_startup_logs_one_line_per_active_channel(tmp_path: Path, caplog) -> None:
    settings = make_gateway_settings(tmp_path, "both", enabled=True)
    adapters = build_adapters(settings)

    with caplog.at_level(logging.INFO, logger="dafeiyu.__main__"):
        log_active_channels(settings, adapters, "token")

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "dafeiyu.__main__"
    ]
    assert any("QQ 通道" in line for line in lines)
    assert any(
        "微信通道" in line and str(settings.wechat.bridge_port) in line
        for line in lines
    )
