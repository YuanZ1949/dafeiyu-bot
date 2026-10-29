"""key 档模型名的自动对齐。

官方模型名会换版本号（`deepseek-chat` 已不在 `/v1/models` 清单里），所以切档
落盘前按端点清单判一次：清单里有原样保留，没有就退到 flash，清单拉不到就
保持原值——判定失败绝不能挡住保存。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import TEST_CONFIG_YAML

from dafeiyu import control as control_mod
from dafeiyu import secrets_store
from dafeiyu.brain.provider_config import config_path
from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl
from dafeiyu.memory.store import Store
from dafeiyu.model_probe import ProbeError, pick_flash_model
from dafeiyu.settings_local import read_overrides


class FakeBrain:
    async def aclose(self) -> None:
        return None


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
    def __init__(self, store: Store) -> None:
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def clear_opencode_sessions(self) -> int:
        return self._store.clear_opencode_sessions()


def make_settings(tmp_path: Path):
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


@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        (["deepseek-v4-pro", "deepseek-flash"], "deepseek-flash"),
        (["deepseek-v4-pro", "deepseek-v4.1-flash"], "deepseek-v4.1-flash"),
        (["DeepSeek-Flash"], "DeepSeek-Flash"),
        (["deepseek-v4-pro", "text-embedding-3-small"], None),
    ],
)
def test_pick_flash_model(ids: list[str], expected: str | None) -> None:
    assert pick_flash_model(ids) == expected


@pytest.fixture
def listing(monkeypatch) -> dict[str, object]:
    """模型清单桩：`ids` 是清单内容，`fail` 为真时抛 ProbeError。"""
    state: dict[str, object] = {"ids": [], "fail": False}

    async def fake_probe_models(base_url, *, api_key="", timeout=8.0):
        if state["fail"]:
            raise ProbeError()
        return list(state["ids"])  # type: ignore[arg-type]

    monkeypatch.setattr(control_mod, "probe_models", fake_probe_models)
    return state


@pytest.fixture
def control(tmp_path: Path, monkeypatch, listing) -> BrainControl:
    settings = make_settings(tmp_path)
    store = StoreSpy(Store(tmp_path / "test.db"))
    store.init_schema()
    ctrl = BrainControl(
        settings,
        store,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )
    monkeypatch.setattr(control_mod, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    monkeypatch.setattr(control_mod, "KEY_PATH", tmp_path / "provider.key")
    monkeypatch.setattr(control_mod, "LOCAL_KEY_PATH", tmp_path / "provider-local.key")
    monkeypatch.setattr(secrets_store, "harden_token_file", lambda path: None)
    # 重建 brain 会构造真实 httpx 客户端；本文件只关心落盘的模型名。
    monkeypatch.setattr(control_mod, "build_brain", lambda *a, **k: FakeBrain())
    return ctrl


def key_payload(model: str, *, force: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "mode": "key",
        "key": {
            "base_url": "https://api.x/v1",
            "model": model,
            "api_key": "sk-test-123456",
        },
    }
    if force:
        payload["force"] = True
    return payload


async def test_stale_key_model_falls_back_to_flash(
    tmp_path: Path, control: BrainControl, listing: dict[str, object]
) -> None:
    listing["ids"] = ["deepseek-v4-pro", "deepseek-flash"]

    out = await control.switch_mode(key_payload("deepseek-chat"))

    assert out["model"] == "dafeiyu-key/deepseek-flash"
    assert any("deepseek-flash" in w for w in out["warnings"])
    overrides = read_overrides(tmp_path / "settings.local.yaml")
    assert overrides["brain"]["key"]["model"] == "deepseek-flash"
    generated = config_path(Path(control.settings.opencode.config_home))
    assert "dafeiyu-key/deepseek-flash" in generated.read_text(encoding="utf-8")


async def test_listed_key_model_is_kept(
    tmp_path: Path, control: BrainControl, listing: dict[str, object]
) -> None:
    listing["ids"] = ["deepseek-v4-pro", "deepseek-flash"]

    out = await control.switch_mode(key_payload("deepseek-v4-pro"))

    assert out["model"] == "dafeiyu-key/deepseek-v4-pro"
    assert not any("自动切换" in w for w in out["warnings"])
    overrides = read_overrides(tmp_path / "settings.local.yaml")
    assert overrides["brain"]["key"]["model"] == "deepseek-v4-pro"


async def test_unreachable_listing_keeps_model_and_saves(
    tmp_path: Path, control: BrainControl, listing: dict[str, object]
) -> None:
    listing["fail"] = True

    out = await control.switch_mode(key_payload("deepseek-chat", force=True))

    assert out["model"] == "dafeiyu-key/deepseek-chat"
    assert not any("自动切换" in w for w in out["warnings"])
    overrides = read_overrides(tmp_path / "settings.local.yaml")
    assert overrides["brain"]["key"]["model"] == "deepseek-chat"


async def test_blank_key_model_uses_listing_flash(
    tmp_path: Path, control: BrainControl, listing: dict[str, object]
) -> None:
    """面板文案「留空 = 自动选 flash」必须真的能存下去。"""
    listing["ids"] = ["deepseek-v4-pro", "deepseek-v4.1-flash"]

    out = await control.switch_mode(key_payload("", force=True))

    assert out["model"] == "dafeiyu-key/deepseek-v4.1-flash"
    assert any("deepseek-v4.1-flash" in w for w in out["warnings"])
    overrides = read_overrides(tmp_path / "settings.local.yaml")
    assert overrides["brain"]["key"]["model"] == "deepseek-v4.1-flash"


async def test_blank_key_model_without_listing_is_rejected(
    tmp_path: Path, control: BrainControl, listing: dict[str, object]
) -> None:
    """选不出模型时宁可报错，也不落盘一个跑不起来的 `dafeiyu-key/`。"""
    listing["fail"] = True

    with pytest.raises(control_mod.SwitchError) as excinfo:
        await control.switch_mode(key_payload("", force=True))

    assert excinfo.value.status == 502
    assert not (tmp_path / "settings.local.yaml").exists()
