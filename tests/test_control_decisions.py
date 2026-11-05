"""bot 控制口 GET /api/decisions。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.behavior import BehaviorSettings
from dafeiyu.config import GroupsConfig, load_settings
from dafeiyu.control import BrainControl, create_app
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType

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
    async def aclose(self) -> None:
        pass


class FakeService:
    def __init__(self) -> None:
        self.brain = FakeBrain()
        self.impressions = None
        self.behavior = BehaviorSettings()
        self.groups = GroupsConfig(frozenset(), frozenset(), frozenset())


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


def make_control(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)
    store = Store(tmp_path / "test.db")
    store.init_schema()
    control = BrainControl(
        settings,
        store,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )
    return control, create_app(control, "", None)


async def test_decisions_returns_items_and_stats(tmp_path: Path) -> None:
    control, app = make_control(tmp_path)
    control.store.record_decision(
        ChatType.GROUP,
        "qq:1001",
        outcome="replied",
        reply_preview="在呢",
        latency_ms=120,
        prompt_tokens=10,
        completion_tokens=5,
    )
    control.store.record_decision(
        ChatType.GROUP, "qq:1001", outcome="silent", reason="热聊"
    )

    async with running(app) as client:
        resp = await client.get("/api/decisions")
        data = await resp.json()

    assert resp.status == 200
    assert len(data["items"]) == 2
    assert data["items"][0]["outcome"] == "silent"
    assert data["stats"]["total"] == 2
    assert data["stats"]["by_outcome"]["replied"] == 1
    assert data["stats"]["by_outcome"]["silent"] == 1
    assert data["generated_at"]


async def test_decisions_filters_by_outcome_and_chat(tmp_path: Path) -> None:
    control, app = make_control(tmp_path)
    control.store.record_decision(ChatType.GROUP, "qq:1001", outcome="replied")
    control.store.record_decision(ChatType.PRIVATE, "qq:2002", outcome="silent")

    async with running(app) as client:
        resp = await client.get(
            "/api/decisions?chat_type=group&chat_id=qq:1001&outcome=replied"
        )
        data = await resp.json()

    assert resp.status == 200
    assert [item["outcome"] for item in data["items"]] == ["replied"]


async def test_decisions_rejects_bad_chat_type(tmp_path: Path) -> None:
    _, app = make_control(tmp_path)

    async with running(app) as client:
        resp = await client.get("/api/decisions?chat_type=unknown")
        data = await resp.json()

    assert resp.status == 400
    assert "error" in data
