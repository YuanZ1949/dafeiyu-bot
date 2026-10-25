from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from dafeiyu import control as control_module
from dafeiyu.brain.client import BrainClient
from dafeiyu.brain.factory import mode_status
from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, create_app
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType
from dafeiyu.runtime import RuntimeState

YAML = """\
persona:
  name: 溟月
  aliases: [大肥鱼]
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
brain:
  provider: token
local_model:
  base_url: http://127.0.0.1:1/v1
  model: ""
  api_key: local
"""


class FakeBrain:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeService:
    def __init__(self) -> None:
        self.brain = FakeBrain()
        self.impressions = None

    def set_brain(self, brain):
        previous = self.brain
        self.brain = brain
        return previous


class FakeAdapter:
    connected = True


@asynccontextmanager
async def running(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def make_settings(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    return load_settings(env_file=env, config_file=config)


def make_control(tmp_path: Path, token: str = "", runtime: str | None = None):
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "test.db")
    store.init_schema()
    service = FakeService()
    control = BrainControl(
        settings,
        store,
        service,
        FakeAdapter(),
        runtime_path=runtime or str(tmp_path / "runtime.json"),
    )
    return control, service, create_app(control, token)


async def test_snapshot_lists_three_modes(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/brain")
        data = await resp.json()

    assert resp.status == 200
    assert data["provider"] == "token"
    assert data["gateway_connected"] is True
    assert [m["id"] for m in data["modes"]] == ["local_model", "token", "opencode"]
    by_id = {m["id"]: m for m in data["modes"]}
    assert by_id["token"]["ready"] is True
    assert by_id["opencode"]["ready"] is False
    assert "server_url" in by_id["opencode"]["reason"]
    assert by_id["local_model"]["ready"] is False  # 还没选模型


def test_local_model_mode_stays_ready_after_switching_away() -> None:
    settings = SimpleNamespace(
        local_model=SimpleNamespace(base_url="http://127.0.0.1:1234/v1", model=""),
        opencode=SimpleNamespace(server_url="http://127.0.0.1:24310"),
        api_key="sk-test",
    )
    runtime = RuntimeState(provider="opencode", local_model_model="qwen/qwen3.5-9b")

    by_id = {m["id"]: m for m in mode_status(settings, runtime)}

    assert by_id["local_model"]["ready"] is True
    assert by_id["local_model"]["reason"] == ""


async def test_token_guard(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path, token="s3cret")
    async with running(app) as client:
        assert (await client.get("/api/brain")).status == 401
        assert (await client.get("/api/health")).status == 200
        ok = await client.get("/api/brain", headers={"X-Control-Token": "s3cret"})
        assert ok.status == 200


async def test_switch_to_local_model_persists_and_closes_old(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(control_module, "CLOSE_DELAY_SECONDS", 0.01)
    _, service, app = make_control(tmp_path)
    original = service.brain

    async with running(app) as client:
        resp = await client.post(
            "/api/brain", json={"provider": "local_model", "model": "qwen2.5-7b"}
        )
        data = await resp.json()

    assert resp.status == 200
    assert data["provider"] == "local_model"
    assert data["model"] == "qwen2.5-7b"
    assert isinstance(service.brain, BrainClient)
    assert service.brain is not original

    await asyncio.sleep(0.05)
    assert original.closed is True

    saved = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))
    assert saved["provider"] == "local_model"
    assert saved["local_model_model"] == "qwen2.5-7b"


async def test_switch_is_idempotent(tmp_path: Path) -> None:
    _, service, app = make_control(tmp_path)
    async with running(app) as client:
        first = await client.post(
            "/api/brain", json={"provider": "local_model", "model": "qwen"}
        )
        assert first.status == 200
        current = service.brain
        second = await client.post(
            "/api/brain", json={"provider": "local_model", "model": "qwen"}
        )
        assert second.status == 200

    assert service.brain is current


async def test_switch_without_model_is_rejected(tmp_path: Path) -> None:
    _, service, app = make_control(tmp_path)
    before = service.brain
    async with running(app) as client:
        resp = await client.post("/api/brain", json={"provider": "local_model"})
        payload = await resp.json()

    assert resp.status == 409
    assert "未选择" in payload["error"]
    assert service.brain is before


async def test_unknown_provider_rejected(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.post("/api/brain", json={"provider": "gpt"})
    assert resp.status == 400


async def test_invalid_json_rejected(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.post("/api/brain", data="not-json")
    assert resp.status == 400


async def test_models_endpoint_reports_unreachable(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/brain/models")
        payload = await resp.json()

    assert resp.status == 502
    assert "LM Studio" in payload["error"]


async def test_runtime_file_overrides_config_provider(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps({"provider": "local_model", "local_model_model": "saved-model"}),
        encoding="utf-8",
    )
    control, _, _ = make_control(tmp_path, runtime=str(runtime))
    snapshot = control.snapshot()
    assert snapshot["provider"] == "local_model"
    assert snapshot["model"] == "saved-model"

async def test_memory_endpoint_empty(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/memory")
        payload = await resp.json()
    assert resp.status == 200
    assert payload == {"summaries": [], "impressions": []}


async def test_memory_endpoint_returns_store_rows(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    control.store.upsert_summary(ChatType.GROUP, 100, "2026-10-24", "昨天聊了钓鱼")
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "preference", "喜欢猫", 0.9)

    async with running(app) as client:
        resp = await client.get("/api/memory?limit=5")
        payload = await resp.json()

    assert resp.status == 200
    summary = payload["summaries"][0]
    assert summary["chat_type"] == "group"
    assert summary["chat_id"] == 100
    assert summary["date"] == "2026-10-24"
    assert summary["content"] == "昨天聊了钓鱼"
    impression = payload["impressions"][0]
    assert impression["chat_type"] == "group"
    assert impression["chat_id"] == 100
    assert impression["subject_user_id"] == 42
    assert impression["kind"] == "preference"
    assert impression["content"] == "喜欢猫"
    assert impression["confidence"] == 0.9


async def test_memory_endpoint_reports_read_failure(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    control.store.close()

    async with running(app) as client:
        resp = await client.get("/api/memory")
        payload = await resp.json()

    assert resp.status == 500
    assert "读取记忆失败" in payload["error"]


async def test_memory_endpoint_rejects_bad_limit(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/memory?limit=abc")
    assert resp.status == 400
