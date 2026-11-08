"""控制口三档切换（POST /api/brain 的 mode 分支）。

覆盖的是「面板保存 → 校验 → 探活 → 落盘 → 重生生成物 → 重载 → 清会话 → 重建」
这条链路里的顺序契约：探活失败必须一个字节都不写，重载失败不能回滚已写入的
配置，api_key 只进密钥文件、绝不进响应。
"""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML

from dafeiyu import config as config_mod
from dafeiyu import control as control_mod
from dafeiyu import secrets_store
from dafeiyu.brain.lmstudio import EnsureResult
from dafeiyu.brain.provider_config import agent_path, config_path, memory_agent_path
from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, SwitchError, create_app
from dafeiyu.memory.store import Store
from dafeiyu.model_probe import ProbeError
from dafeiyu.models import ChatType
from dafeiyu.runtime import load_runtime
from dafeiyu.secrets_store import read_secret, write_secret
from dafeiyu.settings_local import read_overrides, write_overrides


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
    monkeypatch.setattr(control_mod, "LOCAL_KEY_PATH", tmp_path / "provider-local.key")
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
    # 桩必须记录参数：Important 1 要求重建用的是「刚切换到的模式」。
    built: list[tuple[str, str]] = []

    def fake_build_brain(settings, store, provider, model=""):
        built.append((provider, model))
        return FakeBrain()

    monkeypatch.setattr(control_mod, "build_brain", fake_build_brain)
    ctrl.brain_builds = built  # type: ignore[attr-defined]
    return ctrl


@pytest.fixture(autouse=True)
def lm_stop_spy(monkeypatch) -> list[str]:
    """拦截 control_mod.stop_server：测试里绝不真的停本机 LM Studio。

    记录被停止的端点，专门用来断言「切走时停、切到本地档不停」。
    """
    calls: list[str] = []

    async def fake_stop(cfg) -> EnsureResult:
        calls.append(cfg.base_url)
        return EnsureResult(ready=False, started=True, detail="已停止")

    monkeypatch.setattr(control_mod, "stop_server", fake_stop)
    return calls


def key_file_target(config_file: Path) -> Path:
    """从 `{file:...}` 引用反解出绝对路径（断言它真的指向 KEY_PATH）。"""
    text = config_file.read_text(encoding="utf-8")
    start = text.index("{file:") + len("{file:")
    end = text.index("}", start)
    return (config_file.parent / text[start:end]).resolve()


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
    generated_text = generated.read_text(encoding="utf-8")
    assert "dafeiyu-key/m" in generated_text
    # 密钥只能以文件引用出现，生成物与响应里都不能有明文。
    assert "sk-test-123456" not in generated_text
    assert key_file_target(generated) == Path(control_mod.KEY_PATH).resolve()
    agent_file = agent_path(Path(control.settings.opencode.project_dir))
    agent_text = agent_file.read_text(encoding="utf-8")
    assert "model: dafeiyu-key/m" in agent_text
    assert "sk-test-123456" not in agent_text

    # 重建必须按新模式走 opencode，snapshot 也要和响应一致。
    assert control.brain_builds == [("opencode", "")]
    assert control.snapshot()["provider"] == "opencode"
    assert control.snapshot()["model"] == out["model"]
    assert load_runtime(control.runtime_path).provider == "opencode"


async def test_invalid_mode_returns_400(control: BrainControl) -> None:
    with pytest.raises(SwitchError) as exc:
        await control.switch_mode({"mode": "local"})
    assert exc.value.status == 400
    assert "local_model" in exc.value.reason

def test_snapshot_reports_model_before_any_switch(
    tmp_path: Path, store_spy: StoreSpy
) -> None:
    """重启后 GET /api/brain 的 model 不能是空串：以前只在切档时赋值。"""
    settings = make_settings(tmp_path)
    settings = dc_replace(
        settings,
        brain=dc_replace(
            settings.brain, mode="free", provider="opencode", mode_from_overrides=True
        ),
    )
    ctrl = BrainControl(
        settings,
        store_spy,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime-free.json"),
    )

    snap = ctrl.snapshot()

    assert snap["mode"] == "free"
    assert snap["model"] == "opencode/deepseek-v4.1-flash-free"


def test_snapshot_reports_key_model_without_switch(
    tmp_path: Path, store_spy: StoreSpy
) -> None:
    """进程按 key 档启动、还没人点过「应用」时，也要报出当前模型名。"""
    settings = make_settings(tmp_path)
    settings = dc_replace(
        settings,
        brain=dc_replace(
            settings.brain,
            mode="key",
            provider="opencode",
            mode_from_overrides=True,
            key_model="deepseek-flash",
        ),
    )
    ctrl = BrainControl(
        settings,
        store_spy,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime2.json"),
    )

    snap = ctrl.snapshot()

    assert snap["mode"] == "key"
    assert snap["model"] == "dafeiyu-key/deepseek-flash"


def test_invalid_mode_in_settings_keeps_model_empty(
    tmp_path: Path, store_spy: StoreSpy
) -> None:
    """坏配置只该让展示字段为空，不能让构造直接抛。"""
    settings = make_settings(tmp_path)
    settings = dc_replace(settings, brain=dc_replace(settings.brain, mode="nope"))
    ctrl = BrainControl(
        settings,
        store_spy,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime3.json"),
    )

    assert ctrl._mode_model == ""


async def test_switch_keeps_memory_agent_on_same_model(
    tmp_path: Path, monkeypatch, control: BrainControl
) -> None:
    """摘要/印象走的是 memory agent，它的模型必须跟着档位走。"""
    monkeypatch.setattr(control_mod, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    await control.switch_mode(
        {
            "mode": "key",
            "key": {
                "base_url": "https://api.x/v1",
                "model": "m",
                "api_key": "sk-test-123456",
            },
        }
    )
    memory_file = memory_agent_path(Path(control.settings.opencode.project_dir))
    text = memory_file.read_text(encoding="utf-8")
    assert "model: dafeiyu-key/m" in text
    assert "steps: 1" in text
    assert "flash-free" not in text

    await control.switch_mode({"mode": "free"})
    free_text = memory_file.read_text(encoding="utf-8")
    assert "model: opencode/deepseek-v4.1-flash-free" in free_text


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


async def test_failed_reload_surfaces_supervisor_reason(control: BrainControl) -> None:
    async def reload():
        return {"ok": False, "error": "未知进程：opencode"}

    control.opencode_reload = reload
    out = await control.switch_mode({"mode": "free"})

    assert out["reload"] == "pending"
    assert any("未知进程：opencode" in warning for warning in out["warnings"])


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
    assert read_secret(control_mod.LOCAL_KEY_PATH) == ""
    generated = config_path(Path(control.settings.opencode.config_home))
    assert key_file_target(generated) == Path(control_mod.KEY_PATH).resolve()


async def test_snapshot_has_key_flags_without_exposing_values(
    control: BrainControl,
) -> None:
    snap = control.snapshot()
    assert snap["key"]["has_key"] is False
    assert snap["local"]["has_key"] is False
    assert "api_key" not in json.dumps(snap)

    write_secret("sk-key-snapshot", control_mod.KEY_PATH)
    write_secret("sk-local-snapshot", control_mod.LOCAL_KEY_PATH)
    snap = control.snapshot()

    assert snap["key"]["has_key"] is True
    assert snap["local"]["has_key"] is True
    assert "sk-key-snapshot" not in json.dumps(snap)
    assert "sk-local-snapshot" not in json.dumps(snap)


async def test_snapshot_mode_survives_restart(
    tmp_path: Path, monkeypatch, control: BrainControl, store_spy: StoreSpy
) -> None:
    overrides_path = tmp_path / "settings.local.yaml"
    monkeypatch.setattr(control_mod, "OVERRIDES_PATH", overrides_path)
    monkeypatch.setattr(config_mod, "OVERRIDES_PATH", overrides_path)
    await control.switch_mode(
        {
            "mode": "key",
            "force": True,
            "key": {
                "base_url": "https://api.x/v1",
                "model": "m",
                "api_key": "sk-restart",
            },
        }
    )

    settings = load_settings(
        env_file=tmp_path / "test.env", config_file=tmp_path / "config.yaml"
    )
    restarted = BrainControl(
        settings,
        store_spy,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )
    snap = restarted.snapshot()

    assert snap["mode"] == "key"
    assert snap["key"]["base_url"] == "https://api.x/v1"
    assert snap["key"]["model"] == "m"
    assert snap["key"]["has_key"] is True
    assert snap["local"]["has_key"] is False


async def test_restart_mode_comes_from_override_layer(
    tmp_path: Path, monkeypatch, control: BrainControl, store_spy: StoreSpy
) -> None:
    """重启后档位只能读覆盖层：按 provider 反推会给出另一个档位。"""
    overrides_path = tmp_path / "settings.local.yaml"
    monkeypatch.setattr(control_mod, "OVERRIDES_PATH", overrides_path)
    monkeypatch.setattr(config_mod, "OVERRIDES_PATH", overrides_path)
    await control.switch_mode({"mode": "free"})

    settings = load_settings(
        env_file=tmp_path / "test.env", config_file=tmp_path / "config.yaml"
    )
    # 启动配置的 brain.provider 是 token（按 provider 映射会得到 key），
    # 覆盖层写的是 free：两个来源不一致，只有覆盖层优先才是对的。
    assert settings.brain.provider == "token"
    assert settings.brain.mode_from_overrides is True

    restarted = BrainControl(
        settings,
        store_spy,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )
    snap = restarted.snapshot()

    assert snap["provider"] == "token"
    assert snap["mode"] == "free"


async def test_successful_post_never_echoes_local_secret(control: BrainControl) -> None:
    app = create_app(control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/brain",
            json={
                "mode": "local_model",
                "force": True,
                "local": {
                    "base_url": "http://h:1/v1",
                    "model": "lm",
                    "api_key": "sk-local-response-secret",
                },
            },
        )
        text = await resp.text()
    finally:
        await client.close()

    assert resp.status == 200
    assert "sk-local-response-secret" not in text
    assert read_secret(control_mod.LOCAL_KEY_PATH) == "sk-local-response-secret"


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


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "free"},
        {
            "mode": "key",
            "force": True,
            "key": {"base_url": "https://api.x/v1", "model": "m"},
        },
        {
            "mode": "local_model",
            "force": True,
            "local": {"base_url": "http://h:1/v1", "model": "lm"},
        },
    ],
)
async def test_each_mode_rebuilds_opencode_and_snapshot_agrees(
    control: BrainControl, payload: dict
) -> None:
    out = await control.switch_mode(payload)

    # 三档都由 opencode 承载：重建参数不能沿用启动时的 token/local_model。
    assert control.brain_builds == [("opencode", "")]
    assert control.snapshot()["provider"] == "opencode"
    assert control.snapshot()["model"] == out["model"]
    # 档位不能靠 provider 反推：三档的 provider 都是 opencode，反推只会得到 free。
    # 这里钉住 switch_mode 成功后的生效视图（key/local 没有它就会显示成 free）。
    assert control.snapshot()["mode"] == payload["mode"]
    assert load_runtime(control.runtime_path).provider == "opencode"


async def test_omitted_local_fields_use_persisted_overrides(
    control: BrainControl,
) -> None:
    await control.switch_mode(
        {
            "mode": "local_model",
            "force": True,
            "local": {
                "base_url": "http://h:1/v1",
                "model": "lm",
                "context": 65536,
                "lean": True,
            },
        }
    )
    await control.switch_mode({"mode": "free"})
    # 第二轮故意不再带 context/lean：生成物必须以覆盖层里保留的值为准，
    # 不能回落到启动快照的 32768 / false。
    await control.switch_mode(
        {
            "mode": "local_model",
            "force": True,
            "local": {"base_url": "http://h:1/v1", "model": "lm"},
        }
    )

    overrides = read_overrides(control_mod.OVERRIDES_PATH)
    assert overrides["brain"]["local_model"]["context"] == 65536
    assert overrides["brain"]["local_model"]["lean"] is True

    generated = config_path(Path(control.settings.opencode.config_home))
    generated_text = generated.read_text(encoding="utf-8")
    assert '"context": 65536' in generated_text
    assert '"lean": true' in generated_text
    agent_text = agent_path(Path(control.settings.opencode.project_dir)).read_text(
        encoding="utf-8"
    )
    assert "- action: webfetch" not in agent_text  # lean 档不开联网


async def test_generation_failure_rolls_back_overrides(
    control: BrainControl, monkeypatch
) -> None:
    previous = {"brain": {"mode": "free", "local_model": {"context": 111}}, "keep": 1}
    write_overrides(previous, control_mod.OVERRIDES_PATH)

    def boom(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(control_mod, "write_generated", boom)
    with pytest.raises(SwitchError) as exc:
        await control.switch_mode(
            {
                "mode": "local_model",
                "force": True,
                "local": {
                    "base_url": "http://h:1/v1",
                    "model": "lm",
                    "context": 4096,
                },
            }
        )

    assert exc.value.status == 500
    assert "OSError" in exc.value.reason
    assert "回滚" in exc.value.reason
    assert read_overrides(control_mod.OVERRIDES_PATH) == previous
    agent_file = agent_path(Path(control.settings.opencode.project_dir))
    assert not agent_file.exists()


async def test_generation_failure_restores_previous_secret(
    control: BrainControl, monkeypatch
) -> None:
    write_secret("sk-old-secret", control_mod.KEY_PATH)

    def boom(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(control_mod, "write_generated", boom)
    with pytest.raises(SwitchError) as exc:
        await control.switch_mode(
            {
                "mode": "key",
                "force": True,
                "key": {
                    "base_url": "https://api.x/v1",
                    "model": "m",
                    "api_key": "sk-new-secret",
                },
            }
        )

    assert exc.value.status == 500
    assert "回滚" in exc.value.reason
    assert read_secret(control_mod.KEY_PATH) == "sk-old-secret"
    assert not Path(control_mod.OVERRIDES_PATH).exists()


async def test_session_clear_failure_keeps_writes(
    control: BrainControl, store_spy: StoreSpy, monkeypatch
) -> None:
    def boom() -> int:
        raise RuntimeError("db locked")

    monkeypatch.setattr(store_spy, "clear_opencode_sessions", boom)
    out = await control.switch_mode({"mode": "free"})

    assert out["mode"] == "free"
    assert any("会话缓存" in warning for warning in out["warnings"])
    assert read_overrides(control_mod.OVERRIDES_PATH)["brain"]["mode"] == "free"
    assert config_path(Path(control.settings.opencode.config_home)).is_file()


async def test_brain_rebuild_failure_keeps_writes_and_warns(
    control: BrainControl, monkeypatch
) -> None:
    def boom(settings, store, provider, model=""):
        raise RuntimeError("no brain")

    monkeypatch.setattr(control_mod, "build_brain", boom)
    out = await control.switch_mode({"mode": "free"})

    assert out["mode"] == "free"
    assert any(
        "大脑重建失败" in warning and "RuntimeError" in warning
        for warning in out["warnings"]
    )
    assert read_overrides(control_mod.OVERRIDES_PATH)["brain"]["mode"] == "free"
    assert config_path(Path(control.settings.opencode.config_home)).is_file()


async def test_reload_requires_explicit_ack(control: BrainControl) -> None:
    async def reload():
        return None

    control.opencode_reload = reload
    out = await control.switch_mode({"mode": "free"})

    assert out["reload"] == "pending"
    assert any("重载" in warning for warning in out["warnings"])


@pytest.mark.parametrize("bad", [[], "", 0])
async def test_key_must_be_object(control: BrainControl, bad) -> None:
    with pytest.raises(SwitchError) as exc:
        await control.switch_mode({"mode": "key", "key": bad})
    assert exc.value.status == 400


async def test_local_key_feeds_probe_and_generated_reference(
    control: BrainControl, monkeypatch
) -> None:
    seen: list[str] = []

    async def fake_probe(base_url, *, api_key="", timeout=8.0):
        seen.append(api_key)
        return ["lm"]

    monkeypatch.setattr(control_mod, "probe_models", fake_probe)
    write_secret("sk-key-must-stay", control_mod.KEY_PATH)
    out = await control.switch_mode(
        {
            "mode": "local_model",
            "local": {
                "base_url": "http://h:1/v1",
                "model": "lm",
                "api_key": "sk-local-777",
            },
        }
    )

    assert seen == ["sk-local-777"]
    # 本地令牌只进 provider-local.key，自备 Key 文件一个字节都不能动。
    assert read_secret(control_mod.LOCAL_KEY_PATH) == "sk-local-777"
    assert read_secret(control_mod.KEY_PATH) == "sk-key-must-stay"
    generated = config_path(Path(control.settings.opencode.config_home))
    generated_text = generated.read_text(encoding="utf-8")
    assert "sk-local-777" not in generated_text
    assert key_file_target(generated) == Path(control_mod.LOCAL_KEY_PATH).resolve()
    assert "sk-local-777" not in json.dumps(out)
    assert control.snapshot()["local"]["has_key"] is True
    assert control.snapshot()["key"]["has_key"] is True


async def test_local_empty_token_reuses_local_secret_not_provider_key(
    control: BrainControl, monkeypatch
) -> None:
    write_secret("sk-key-only", control_mod.KEY_PATH)
    write_secret("sk-local-keep", control_mod.LOCAL_KEY_PATH)
    seen: list[str] = []

    async def fake_probe(base_url, *, api_key="", timeout=8.0):
        seen.append(api_key)
        return ["lm"]

    monkeypatch.setattr(control_mod, "probe_models", fake_probe)
    await control.switch_mode(
        {
            "mode": "local_model",
            "local": {"base_url": "http://h:1/v1", "model": "lm"},
        }
    )

    assert seen == ["sk-local-keep"]
    assert read_secret(control_mod.LOCAL_KEY_PATH) == "sk-local-keep"
    assert read_secret(control_mod.KEY_PATH) == "sk-key-only"
    generated = config_path(Path(control.settings.opencode.config_home))
    assert key_file_target(generated) == Path(control_mod.LOCAL_KEY_PATH).resolve()


async def test_local_probe_falls_back_to_configured_token(
    control: BrainControl, monkeypatch
) -> None:
    control.settings = dc_replace(
        control.settings,
        local_model=dc_replace(control.settings.local_model, api_key="sk-stored-1"),
    )
    seen: list[str] = []

    async def fake_probe(base_url, *, api_key="", timeout=8.0):
        seen.append(api_key)
        return ["lm"]

    monkeypatch.setattr(control_mod, "probe_models", fake_probe)
    await control.switch_mode(
        {
            "mode": "local_model",
            "local": {"base_url": "http://h:1/v1", "model": "lm"},
        }
    )

    assert seen == ["sk-stored-1"]
    assert read_secret(control_mod.LOCAL_KEY_PATH) == "sk-stored-1"
    generated = config_path(Path(control.settings.opencode.config_home))
    assert key_file_target(generated) == Path(control_mod.LOCAL_KEY_PATH).resolve()


async def test_switch_mode_away_from_local_stops_lmstudio(
    control: BrainControl, lm_stop_spy: list[str]
) -> None:
    await control.switch_mode({"mode": "free"})

    assert lm_stop_spy == [control.settings.local_model.base_url]


async def test_switch_mode_to_local_does_not_stop_lmstudio(
    control: BrainControl, lm_stop_spy: list[str]
) -> None:
    await control.switch_mode(
        {
            "mode": "local_model",
            "force": True,
            "local": {"base_url": "http://h:1/v1", "model": "lm"},
        }
    )

    assert lm_stop_spy == []


async def test_stop_when_unused_can_be_disabled(
    control: BrainControl, lm_stop_spy: list[str]
) -> None:
    control.settings = dc_replace(
        control.settings,
        local_model=dc_replace(
            control.settings.local_model, stop_when_unused=False
        ),
    )

    await control.switch_mode({"mode": "free"})

    assert lm_stop_spy == []


async def test_switch_away_keeps_lmstudio_when_observer_needs_local(
    control: BrainControl, lm_stop_spy: list[str]
) -> None:
    """旁路直连本地端点时切走主脑不能停服务，否则每轮观察都是 ConnectError。"""
    control.settings = dc_replace(
        control.settings,
        mind=dc_replace(
            control.settings.mind, enabled=True, observer_provider="local"
        ),
        local_model=dc_replace(control.settings.local_model, model="qwen/x"),
    )

    await control.switch_mode({"mode": "free"})

    assert lm_stop_spy == []


async def test_switch_provider_away_from_local_stops_lmstudio(
    control: BrainControl, lm_stop_spy: list[str]
) -> None:
    """老协议入口 switch() 切离 local_model 时同样要停端点。"""
    out = await control.switch("opencode")

    assert out["provider"] == "opencode"
    assert lm_stop_spy == [control.settings.local_model.base_url]
