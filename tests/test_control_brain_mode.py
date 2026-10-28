"""控制口三档切换（POST /api/brain 的 mode 分支）。

覆盖的是「面板保存 → 校验 → 探活 → 落盘 → 重生生成物 → 重载 → 清会话 → 重建」
这条链路里的顺序契约：探活失败必须一个字节都不写，重载失败不能回滚已写入的
配置，api_key 只进密钥文件、绝不进响应。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML

from dafeiyu import control as control_mod
from dafeiyu import secrets_store
from dafeiyu.brain.provider_config import agent_path, config_path
from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, SwitchError, create_app
from dafeiyu.memory.store import Store
from dafeiyu.model_probe import ProbeError
from dafeiyu.models import ChatType
from dafeiyu.secrets_store import read_secret, write_secret
from dafeiyu.settings_local import read_overrides


class FakeBrain:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeService:
    def __init__(self) -> None:
        self.brain = FakeBrain()

    def set_brain(self, brain):
        previous = self.brain
        self.brain = brain
        return previous


class FakeAdapter:
    connected = True


class StoreSpy:
    """转发到真实 Store，只记「清空全部 opencode 会话」被调了几次。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.cleared = 0

    def __getattr__(self, name):
        return getattr(self._store, name)

    def clear_opencode_sessions(self) -> int:
        self.cleared += 1
        return self._store.clear_opencode_sessions()


def make_settings(tmp_path: Path):
    # 生成物必须落在 tmp_path：project_dir/config_home 指到仓库外，测试才不会
    # 改写仓库里的 .opencode/agents/deepseek-qq.md。
    config = tmp_path / "config.yaml"
    config.write_text(
        TEST_CONFIG_YAML
        + "opencode:\n"
        + f"  project_dir: {(tmp_path / 'project').as_posix()}\n"
        + f"  config_home: {(tmp_path / 'config-home').as_posix()}\n",
        encoding="utf-8",
    )
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    return load_settings(env_file=env, config_file=config)


@pytest.fixture
def store_spy(tmp_path: Path) -> StoreSpy:
    store = StoreSpy(Store(tmp_path / "test.db"))
    store.init_schema()
    return store


@pytest.fixture
def control(tmp_path: Path, monkeypatch, store_spy: StoreSpy) -> BrainControl:
    settings = make_settings(tmp_path)
    ctrl = BrainControl(
        settings,
        store_spy,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )
    monkeypatch.setattr(control_mod, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    monkeypatch.setattr(control_mod, "KEY_PATH", tmp_path / "provider.key")
    # 测试机不跑真实 icacls：密钥文件内容与清理逻辑才是本文件的关注点。
    monkeypatch.setattr(secrets_store, "harden_token_file", lambda path: None)

    async def fake_probe(base_url, *, api_key="", timeout=8.0):
        # 回环 1 端口在真机上必然拒绝连接；这里保持同样语义，其余端点给成功桩。
        if base_url.rstrip("/").endswith(":1/v1"):
            raise ProbeError()
        return ["m"]

    monkeypatch.setattr(control_mod, "probe_models", fake_probe)
    # 只替换「构造 brain」这一步：本机 cacert 加载病态慢（单次 40s+），而
    # switch_mode 的契约是文件与顺序，重建本身由 tests/test_control_api.py 覆盖。
    monkeypatch.setattr(
        control_mod,
        "build_brain",
        lambda settings, store, provider, model="": FakeBrain(),
    )
    return ctrl


async def test_switch_to_key_writes_everything(
    tmp_path: Path, monkeypatch, control: BrainControl
) -> None:
    monkeypatch.setattr(control_mod, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    out = await control.switch_mode(
        {
            "mode": "key",
            "key": {
                "base_url": "https://api.x/v1",
                "model": "m",
                "api_key": "sk-test-123456",
            },
        }
    )

    assert out["mode"] == "key"
    assert out["model"] == "dafeiyu-key/m"
    assert out["reload"] == "pending"  # 本任务不接 reload 通道
    overrides = read_overrides(tmp_path / "settings.local.yaml")
    assert overrides["brain"]["mode"] == "key"
    assert overrides["brain"]["key"] == {"base_url": "https://api.x/v1", "model": "m"}
    assert read_secret(control_mod.KEY_PATH) == "sk-test-123456"
    assert "sk-test-123456" not in json.dumps(out)

    generated = config_path(Path(control.settings.opencode.config_home))
    assert generated.is_file()
    assert "dafeiyu-key/m" in generated.read_text(encoding="utf-8")
    agent_file = agent_path(Path(control.settings.opencode.project_dir))
    assert agent_file.is_file()
    assert "model: dafeiyu-key/m" in agent_file.read_text(encoding="utf-8")


async def test_invalid_mode_returns_400(control: BrainControl) -> None:
    with pytest.raises(SwitchError) as exc:
        await control.switch_mode({"mode": "local"})
    assert exc.value.status == 400
    assert "local_model" in exc.value.reason


async def test_failed_probe_leaves_no_partial_write(
    tmp_path: Path, monkeypatch, control: BrainControl
) -> None:
    monkeypatch.setattr(control_mod, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    with pytest.raises(SwitchError) as exc:
        await control.switch_mode(
            {
                "mode": "local_model",
                "local": {"base_url": "http://127.0.0.1:1/v1", "model": "x"},
            }
        )
    assert exc.value.status == 502
    assert not (tmp_path / "settings.local.yaml").exists()
    assert read_secret(control_mod.KEY_PATH) == ""
    # 连探测都没过，生成物目录也不该被创建。
    assert not (tmp_path / "config-home").exists()


async def test_switch_clears_session_cache(
    control: BrainControl, store_spy: StoreSpy
) -> None:
    store_spy.set_opencode_session(ChatType.GROUP, 42, "s-old")

    await control.switch_mode({"mode": "free"})

    assert store_spy.cleared == 1
    assert store_spy.get_opencode_session(ChatType.GROUP, 42) is None


async def test_force_skips_probe(
    tmp_path: Path, monkeypatch, control: BrainControl
) -> None:
    async def boom(*args, **kwargs):
        raise AssertionError("force=true 时不允许探测端点")

    monkeypatch.setattr(control_mod, "probe_models", boom)
    out = await control.switch_mode(
        {
            "mode": "key",
            "force": True,
            "key": {"base_url": "https://api.x/v1", "model": "m"},
        }
    )

    assert out["mode"] == "key"
    assert read_overrides(control_mod.OVERRIDES_PATH)["brain"]["mode"] == "key"


async def test_failed_reload_keeps_writes_and_reports_pending(
    control: BrainControl, store_spy: StoreSpy
) -> None:
    async def broken_reload():
        raise RuntimeError("supervisor 挂了")

    control.opencode_reload = broken_reload
    out = await control.switch_mode({"mode": "free"})

    assert out["reload"] == "pending"
    assert any("重载" in warning for warning in out["warnings"])
    assert read_overrides(control_mod.OVERRIDES_PATH)["brain"]["mode"] == "free"
    assert store_spy.cleared == 1


async def test_reload_ok_is_reported(control: BrainControl) -> None:
    calls: list[int] = []

    async def reload():
        calls.append(1)
        return {"ok": True, "pid": 4321}

    control.opencode_reload = reload
    out = await control.switch_mode({"mode": "free"})

    assert out["reload"] == "ok"
    assert calls == [1]
    assert out["warnings"] == []


async def test_empty_api_key_keeps_stored_secret(control: BrainControl) -> None:
    write_secret("sk-keep-me-1234", control_mod.KEY_PATH)

    await control.switch_mode(
        {
            "mode": "key",
            "force": True,
            "key": {"base_url": "https://api.x/v1", "model": "m", "api_key": ""},
        }
    )

    assert read_secret(control_mod.KEY_PATH) == "sk-keep-me-1234"


async def test_repeated_switch_does_not_back_up_generated_config(
    control: BrainControl,
) -> None:
    await control.switch_mode({"mode": "free"})
    out = await control.switch_mode(
        {
            "mode": "key",
            "force": True,
            "key": {"base_url": "https://api.x/v1", "model": "m"},
        }
    )

    for root in (
        Path(control.settings.opencode.config_home),
        Path(control.settings.opencode.project_dir),
    ):
        assert list(root.rglob("*.bak.*")) == []
    assert not any("备份" in warning for warning in out["warnings"])


async def test_probe_timeout_maps_to_504(monkeypatch, control: BrainControl) -> None:
    async def slow(*args, **kwargs):
        raise ProbeError(timed_out=True)

    monkeypatch.setattr(control_mod, "probe_models", slow)
    with pytest.raises(SwitchError) as exc:
        await control.switch_mode(
            {
                "mode": "local_model",
                "local": {"base_url": "http://h:1/v1", "model": "m"},
            }
        )
    assert exc.value.status == 504


async def test_post_api_brain_dispatches_mode_payload(control: BrainControl) -> None:
    app = create_app(control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/brain", json={"mode": "free"})
        data = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert data["mode"] == "free"
