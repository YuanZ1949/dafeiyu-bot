from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML

from dafeiyu import control as control_module
from dafeiyu.brain.client import BrainClient
from dafeiyu.brain.factory import mode_status
from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, MemoryActions, create_app
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType
from dafeiyu.runtime import RuntimeState
from dafeiyu.workers.failure import MemoryPaused

YAML = TEST_CONFIG_YAML


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


def make_control(
    tmp_path: Path,
    token: str = "",
    runtime: str | None = None,
    memory_actions: MemoryActions | None = None,
):
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
    return control, service, create_app(control, token, memory_actions)


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
    assert payload == {
        "summaries": [],
        "impressions": [],
        "summary_total": 0,
        "impression_total": 0,
        "summary_has_more": False,
        "impression_has_more": False,
    }


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


async def test_memory_endpoint_reports_totals_and_has_more(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    for day in ("2026-10-22", "2026-10-23", "2026-10-24"):
        control.store.upsert_summary(ChatType.GROUP, 100, day, day)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "群聊", 0.9)
    control.store.upsert_impression(ChatType.PRIVATE, 200, 43, "fact", "私聊", 0.9)

    async with running(app) as client:
        first = await (await client.get("/api/memory?limit=2")).json()
        second = await (await client.get("/api/memory?limit=2&offset=2")).json()
        filtered = await (
            await client.get("/api/memory?limit=10&chat_type=private")
        ).json()

    assert first["summary_total"] == 3
    assert len(first["summaries"]) == 2
    assert first["summary_has_more"] is True
    assert second["summary_has_more"] is False
    assert len(second["summaries"]) == 1

    assert first["impression_total"] == 2
    assert first["impression_has_more"] is False

    assert filtered["impression_total"] == 1
    assert filtered["summary_total"] == 0
    assert filtered["summary_has_more"] is False


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


async def test_memory_endpoint_filters_by_chat_type_and_chat_id(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "群聊印象", 0.9)
    control.store.upsert_impression(ChatType.PRIVATE, 200, 43, "fact", "私聊印象", 0.8)

    async with running(app) as client:
        resp = await client.get("/api/memory?limit=10&chat_type=private")
        payload = await resp.json()
        only_group = await client.get(
            "/api/memory?limit=10&chat_type=group&chat_id=100"
        )
        group_payload = await only_group.json()

    assert resp.status == 200
    assert [i["content"] for i in payload["impressions"]] == ["私聊印象"]
    assert [i["chat_id"] for i in group_payload["impressions"]] == [100]


async def test_memory_endpoint_pages_with_offset(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    control.store.upsert_summary(ChatType.GROUP, 100, "2026-10-24", "最近一天")
    control.store.upsert_summary(ChatType.GROUP, 100, "2026-10-23", "更早一天")

    async with running(app) as client:
        resp = await client.get("/api/memory?limit=1&offset=1")
        payload = await resp.json()

    assert resp.status == 200
    assert [s["content"] for s in payload["summaries"]] == ["更早一天"]


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=201",
        "offset=-1",
        "offset=abc",
        "chat_type=bad",
        "chat_id=0",
        "chat_id=abc",
    ],
)
async def test_memory_endpoint_rejects_invalid_query(tmp_path: Path, query: str) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get(f"/api/memory?{query}")
    assert resp.status == 400


async def test_memory_actions_disabled_returns_503(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        summary = await client.post(
            "/api/memory/summaries", json={"chat_type": "group", "chat_id": 100}
        )
        impression = await client.post(
            "/api/memory/impressions", json={"chat_type": "group", "chat_id": 100}
        )
        deleted = await client.delete("/api/memory/impressions/1")
    assert summary.status == 503
    assert impression.status == 503
    assert deleted.status == 503


def make_actions(control, *, summarize=None, extract=None) -> MemoryActions:
    async def default_summarize(chat_type, chat_id, date):
        return "默认摘要"

    async def default_extract(chat_type, chat_id):
        return 0

    return MemoryActions(
        summarize=summarize or default_summarize,
        extract=extract or default_extract,
        delete_impression=control.store.delete_impression,
    )


async def test_post_summary_runs_action_with_target(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)
    seen = {}

    async def summarize(chat_type, chat_id, date):
        seen.update(chat_type=chat_type, chat_id=chat_id, date=date)
        return "昨天聊了钓鱼"

    app = create_app(control, "", make_actions(control, summarize=summarize))
    async with running(app) as client:
        resp = await client.post(
            "/api/memory/summaries",
            json={"chat_type": "private", "chat_id": 200, "date": "2026-10-24"},
        )
        payload = await resp.json()

    assert resp.status == 200
    assert payload == {"ok": True, "date": "2026-10-24", "summary": "昨天聊了钓鱼"}
    assert seen == {
        "chat_type": ChatType.PRIVATE,
        "chat_id": 200,
        "date": "2026-10-24",
    }


async def test_post_summary_defaults_to_yesterday(tmp_path: Path) -> None:
    from datetime import datetime, timedelta

    from dafeiyu.timeutil import SHANGHAI

    control, _, _ = make_control(tmp_path)
    seen = {}

    async def summarize(chat_type, chat_id, date):
        seen["date"] = date
        return "ok"

    app = create_app(control, "", make_actions(control, summarize=summarize))
    async with running(app) as client:
        resp = await client.post(
            "/api/memory/summaries", json={"chat_type": "group", "chat_id": 100}
        )
    expected = (datetime.now(SHANGHAI).date() - timedelta(days=1)).isoformat()
    assert resp.status == 200
    assert seen["date"] == expected


async def test_post_summary_reports_generation_failure(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)

    async def summarize(chat_type, chat_id, date):
        return None

    app = create_app(control, "", make_actions(control, summarize=summarize))
    async with running(app) as client:
        resp = await client.post(
            "/api/memory/summaries", json={"chat_type": "group", "chat_id": 100}
        )
        payload = await resp.json()
    assert resp.status == 502
    assert "生成摘要失败" in payload["error"]


async def test_post_summary_reports_quota_pause(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)

    async def summarize(chat_type, chat_id, date):
        raise MemoryPaused("配额暂停中，稍后再试")

    app = create_app(control, "", make_actions(control, summarize=summarize))
    async with running(app) as client:
        resp = await client.post(
            "/api/memory/summaries", json={"chat_type": "group", "chat_id": 100}
        )
    assert resp.status == 409


@pytest.mark.parametrize(
    "body",
    [
        {"chat_type": "channel", "chat_id": 100},
        {"chat_type": "group"},
        {"chat_type": "group", "chat_id": 0},
        {"chat_type": "group", "chat_id": "abc"},
        {"chat_type": "group", "chat_id": 100, "date": "2026/10/24"},
    ],
)
async def test_post_summary_rejects_invalid_body(tmp_path: Path, body: dict) -> None:
    control, _, _ = make_control(tmp_path)
    app = create_app(control, "", make_actions(control))
    async with running(app) as client:
        resp = await client.post("/api/memory/summaries", json=body)
    assert resp.status == 400


async def test_post_impressions_returns_added_count(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)
    seen = {}

    async def extract(chat_type, chat_id):
        seen.update(chat_type=chat_type, chat_id=chat_id)
        return 3

    app = create_app(control, "", make_actions(control, extract=extract))
    async with running(app) as client:
        resp = await client.post(
            "/api/memory/impressions", json={"chat_type": "group", "chat_id": 100}
        )
        payload = await resp.json()

    assert resp.status == 200
    assert payload == {"ok": True, "added": 3}
    assert seen == {"chat_type": ChatType.GROUP, "chat_id": 100}


async def test_post_impressions_reports_failure(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)

    async def extract(chat_type, chat_id):
        return None

    app = create_app(control, "", make_actions(control, extract=extract))
    async with running(app) as client:
        resp = await client.post(
            "/api/memory/impressions", json={"chat_type": "group", "chat_id": 100}
        )
    assert resp.status == 502


async def test_delete_impression_removes_row(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "喜欢猫", 0.9)

    async with running(create_app(control, "", make_actions(control))) as client:
        listing = await (await client.get("/api/memory")).json()
        impression_id = listing["impressions"][0]["id"]
        resp = await client.delete(f"/api/memory/impressions/{impression_id}")
        payload = await resp.json()
        again = await client.delete(f"/api/memory/impressions/{impression_id}")

    assert resp.status == 200
    assert payload == {"ok": True, "deleted": impression_id}
    assert again.status == 404
    assert control.store.count_impressions() == 0


@pytest.mark.parametrize("impression_id", ["abc", "0", "-3"])
async def test_delete_impression_rejects_bad_id(
    tmp_path: Path, impression_id: str
) -> None:
    control, _, _ = make_control(tmp_path)
    async with running(create_app(control, "", make_actions(control))) as client:
        resp = await client.delete(f"/api/memory/impressions/{impression_id}")
    assert resp.status == 400
