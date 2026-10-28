from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
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
from dafeiyu.memory.store import MAX_MEMORY_PAGE, Store
from dafeiyu.models import ChatType, Message
from dafeiyu.runtime import RuntimeState
from dafeiyu.workers.failure import QUOTA_KEY, MemoryPaused

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


async def test_models_endpoint_rejects_malformed_base_url(tmp_path: Path) -> None:
    # 回归：http://h:abc/v1 曾让 httpx.InvalidURL 漏出 _get → 500 + traceback。
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/brain/models?base_url=http://h:abc/v1")
        payload = await resp.json()

    assert resp.status == 502
    assert payload["error"] == "地址不可达或不是 OpenAI 兼容接口"


async def test_models_endpoint_probes_given_base_url(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple] = []

    async def fake_probe(base_url, *, api_key="", timeout=8.0):
        calls.append((base_url, api_key, timeout))
        return ["m1", "m2"]

    monkeypatch.setattr(control_module, "probe_models", fake_probe)
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get(
            "/api/brain/models",
            params={"base_url": "http://h:1234/v1/v1/", "api_key": "k-secret"},
        )
        payload = await resp.json()

    assert resp.status == 200
    assert payload == {"models": ["m1", "m2"]}
    # 粘贴多出来的 /v1/ 被规整掉；key 只进探测层，超时仍共用 3s。
    assert calls == [("http://h:1234/v1", "k-secret", 3.0)]


async def test_models_endpoint_maps_timeout_to_504(tmp_path: Path, monkeypatch) -> None:
    async def fake_probe(base_url, *, api_key="", timeout=8.0):
        raise control_module.ProbeError(timed_out=True)

    monkeypatch.setattr(control_module, "probe_models", fake_probe)
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get(
            "/api/brain/models", params={"base_url": "http://h:1234/v1"}
        )
        payload = await resp.json()

    assert resp.status == 504
    assert payload["error"] == "地址不可达或不是 OpenAI 兼容接口"


async def test_models_endpoint_never_echoes_api_key(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_probe(base_url, *, api_key="", timeout=8.0):
        return ["m1"]

    monkeypatch.setattr(control_module, "probe_models", fake_probe)
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        ok = await client.get(
            "/api/brain/models",
            params={"base_url": "http://h:1234/v1", "api_key": "k-secret"},
        )
        ok_text = await ok.text()

        async def boom(base_url, *, api_key="", timeout=8.0):
            raise control_module.ProbeError()

        monkeypatch.setattr(control_module, "probe_models", boom)
        failed = await client.get(
            "/api/brain/models",
            params={"base_url": "http://h:1234/v1", "api_key": "k-secret"},
        )
        failed_text = await failed.text()

    assert ok.status == 200
    assert failed.status == 502
    assert "k-secret" not in ok_text
    assert "k-secret" not in failed_text


async def test_models_endpoint_default_path_uses_local_settings(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple] = []

    async def fake_probe(base_url, *, api_key="", timeout=8.0):
        calls.append((base_url, api_key, timeout))
        return ["local-model"]

    monkeypatch.setattr(control_module, "probe_models", fake_probe)
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/brain/models")
        payload = await resp.json()

    assert resp.status == 200
    assert payload == {"models": ["local-model"]}
    # 不带参数时仍走本机 local_model.base_url，且不带 api_key。
    assert calls == [("http://127.0.0.1:1/v1", "", 3.0)]


async def test_models_endpoint_keeps_upstream_http_status(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_probe(base_url, *, api_key="", timeout=8.0):
        raise control_module.ProbeError(status=503)

    monkeypatch.setattr(control_module, "probe_models", fake_probe)
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/brain/models")
        payload = await resp.json()

    # 状态码不变（502），但文案保留上游状态，便于诊断。
    assert resp.status == 502
    assert payload["error"] == "LM Studio 返回 HTTP 503"


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
        "limit_max": MAX_MEMORY_PAGE,
        "quota": {"paused": False, "until": None},
    }


async def test_memory_endpoint_reports_quota_pause(tmp_path: Path) -> None:
    """熔断中面板要能说明原因：从库里读，跨重启也准。"""
    control, _, app = make_control(tmp_path)
    until = time.time() + 600
    control.store.save_backoff(QUOTA_KEY, until)

    async with running(app) as client:
        payload = await (await client.get("/api/memory")).json()

    assert payload["quota"]["paused"] is True
    assert abs(payload["quota"]["until"] - until) < 1


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
    assert first["limit_max"] == MAX_MEMORY_PAGE
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


async def test_patch_impression_updates_fields_and_fts(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)
    control.store.upsert_impression(
        ChatType.GROUP, 100, 42, "preference", "喜欢猫", 0.5,
        source_message_id="m1", source_excerpt="我喜欢猫",
    )
    impression = control.store.list_impressions(ChatType.GROUP, 100, topk=10)[0]

    async with running(create_app(control, "", make_actions(control))) as client:
        resp = await client.patch(
            f"/api/memory/impressions/{impression.id}",
            json={"content": "喜欢狗", "confidence": 0.92, "kind": "preference"},
        )
        payload = await resp.json()

    assert resp.status == 200
    assert payload["ok"] is True
    assert payload["impression"]["id"] == impression.id
    assert payload["impression"]["content"] == "喜欢狗"
    assert payload["impression"]["confidence"] == 0.92
    assert payload["impression"]["source_message_id"] == "m1"
    assert [i.content for i in control.store.search_impressions(
        ChatType.GROUP, 100, "喜欢狗", topk=10
    )] == ["喜欢狗"]
    assert control.store.search_impressions(ChatType.GROUP, 100, "喜欢猫", topk=10) == []


async def test_patch_impression_validates_payload(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "住在杭州", 0.7)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "养了一只猫", 0.7)
    first = control.store.list_impressions(ChatType.GROUP, 100, topk=10)[0]
    second = control.store.list_impressions(ChatType.GROUP, 100, topk=10)[1]

    app = create_app(control, "", make_actions(control))
    async with running(app) as client:
        missing = await client.patch("/api/memory/impressions/999", json={"content": "x"})
        bad_id = await client.patch("/api/memory/impressions/abc", json={"content": "x"})
        empty = await client.patch(
            f"/api/memory/impressions/{first.id}", json={"content": "  "}
        )
        too_long = await client.patch(
            f"/api/memory/impressions/{first.id}", json={"content": "猫" * 201}
        )
        bad_confidence = await client.patch(
            f"/api/memory/impressions/{first.id}", json={"confidence": 1.1}
        )
        bool_confidence = await client.patch(
            f"/api/memory/impressions/{first.id}", json={"confidence": True}
        )
        bad_kind = await client.patch(
            f"/api/memory/impressions/{first.id}", json={"kind": "topic"}
        )
        no_fields = await client.patch(
            f"/api/memory/impressions/{first.id}", json={}
        )
        conflict = await client.patch(
            f"/api/memory/impressions/{first.id}", json={"content": second.content}
        )

    assert missing.status == 404
    assert bad_id.status == 400
    assert empty.status == 400
    assert too_long.status == 400
    assert bad_confidence.status == 400
    assert bool_confidence.status == 400
    assert bad_kind.status == 400
    assert no_fields.status == 400
    assert conflict.status == 409
    assert control.store.get_impression(first.id).content == first.content


async def test_merge_impressions_keeps_earliest_and_max_confidence(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "住在杭州", 0.6)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "养了一只猫", 0.9)
    rows = control.store.list_impressions(ChatType.GROUP, 100, topk=10)
    target_id = min(row.id for row in rows)

    async with running(create_app(control, "", make_actions(control))) as client:
        resp = await client.post(
            "/api/memory/impressions/merge",
            json={"ids": [row.id for row in rows], "content": "住在杭州，养了一只猫"},
        )
        payload = await resp.json()

    assert resp.status == 200
    assert payload["ok"] is True
    assert payload["impression"]["id"] == target_id
    assert payload["impression"]["content"] == "住在杭州，养了一只猫"
    assert payload["impression"]["confidence"] == 0.9
    assert control.store.count_impressions() == 1
    assert control.store.search_impressions(
        ChatType.GROUP, 100, "养了一只猫", topk=10
    )[0].content == "住在杭州，养了一只猫"


async def test_merge_impressions_rejects_bad_scope_and_conflict(tmp_path: Path) -> None:
    control, _, _ = make_control(tmp_path)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "A", 0.6)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "B", 0.7)
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "fact", "D", 0.8)
    control.store.upsert_impression(ChatType.GROUP, 100, 43, "fact", "C", 0.8)
    rows = control.store.list_impressions(ChatType.GROUP, 100, topk=10)
    by_content = {row.content: row.id for row in rows}

    app = create_app(control, "", make_actions(control))
    async with running(app) as client:
        cross_subject = await client.post(
            "/api/memory/impressions/merge",
            json={"ids": [by_content["A"], by_content["C"]]},
        )
        missing = await client.post(
            "/api/memory/impressions/merge",
            json={"ids": [by_content["A"], 999]},
        )
        one = await client.post(
            "/api/memory/impressions/merge",
            json={"ids": [by_content["A"]]},
        )
        duplicate = await client.post(
            "/api/memory/impressions/merge",
            json={"ids": [by_content["A"], by_content["A"]]},
        )
        bad_id = await client.post(
            "/api/memory/impressions/merge",
            json={"ids": [by_content["A"], True]},
        )
        empty = await client.post(
            "/api/memory/impressions/merge",
            json={"ids": [by_content["A"], by_content["B"]], "content": ""},
        )
        conflict = await client.post(
            "/api/memory/impressions/merge",
            json={"ids": [by_content["A"], by_content["B"]], "content": "D"},
        )

    assert cross_subject.status == 400
    assert missing.status == 404
    assert one.status == 400
    assert duplicate.status == 400
    assert bad_id.status == 400
    assert empty.status == 400
    assert conflict.status == 409
    assert control.store.count_impressions() == 4


async def test_search_endpoint_returns_snippet(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    long_text = "闲聊" * 20 + "显卡驱动" + "收尾" * 20
    _seed_message(control.store, "m1", long_text, timestamp=1_700_000_100)

    async with running(app) as client:
        resp = await client.get(
            "/api/memory/search",
            params={"q": "显卡驱动", "chat_type": "group", "chat_id": "100"},
        )
        payload = await resp.json()

    assert resp.status == 200
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["content"] == long_text
    assert "显卡驱动" in item["snippet"]
    assert item["snippet"].startswith("…")
    assert item["snippet"].endswith("…")
    assert len(item["snippet"]) < len(long_text)


def test_dashboard_exposes_impression_edit_and_merge() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "src/dafeiyu/supervisor/dashboard/index.html"
    ).read_text(encoding="utf-8")
    for anchor in (
        'id="btn-merge-impressions"',
        "selectedImpressionIds",
        "editImpression(i)",
        "mergeSelectedImpressions",
        "source_message_id",
        "source_excerpt",
        "不可撤销",
        'method: "PATCH"',
    ):
        assert anchor in html, f"面板缺少 {anchor}"


def test_dashboard_page_max_fallback_matches_backend() -> None:
    """面板的兜底常量必须等于后端上限；正常路径以 limit_max 为准。"""
    html = (
        Path(__file__).resolve().parents[1]
        / "src/dafeiyu/supervisor/dashboard/index.html"
    ).read_text(encoding="utf-8")
    match = re.search(r"MEMORY_PAGE_MAX_FALLBACK = (\d+)", html)
    assert match, "面板缺少分页上限兜底常量"
    assert int(match.group(1)) == MAX_MEMORY_PAGE
    assert "data.limit_max" in html
    assert "if (memoryLimit > memoryPageMax) memoryLimit = memoryPageMax;" in html
    assert "memoryPageMax" in html


def test_dashboard_surfaces_quota_pause() -> None:
    """记忆面板要显示熔断状态，否则「没有新摘要」会被当成故障。"""
    html = (
        Path(__file__).resolve().parents[1]
        / "src/dafeiyu/supervisor/dashboard/index.html"
    ).read_text(encoding="utf-8")
    assert "data.quota" in html
    assert "quota.paused" in html
    assert "配额暂停中" in html


def _seed_message(
    store: Store,
    message_id: str,
    content: str,
    *,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: int = 100,
    user_id: int = 42,
    nickname: str = "鱼片",
    timestamp: int = 1_700_000_000,
    is_bot: bool = False,
) -> None:
    store.add_message(
        Message(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=user_id,
            nickname=nickname,
            content=content,
            message_id=message_id,
            is_bot=is_bot,
            timestamp=timestamp,
        )
    )


async def test_usage_endpoint_fills_empty_days(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    control.store.record_usage(ChatType.GROUP, 100, "reply", 10, 5)
    control.store.record_usage(ChatType.GROUP, 100, "ambient", 2, 1)
    control.store.record_usage(ChatType.GROUP, 100, "error", 0, 0)
    control.store.record_usage(ChatType.GROUP, 100, "summary", 4, 2)

    async with running(app) as client:
        resp = await client.get("/api/usage?days=7")
        payload = await resp.json()

    assert resp.status == 200
    assert payload["days"] == 7
    assert len(payload["series"]) == 7
    assert payload["series"][-1] == {
        "date": control.store.today(),
        "calls": 2,
        "errors": 1,
        "tokens": 24,
    }
    assert payload["totals"] == {"calls": 2, "errors": 1, "tokens": 24}
    assert payload["top"] == [
        {"chat_type": "group", "chat_id": 100, "calls": 2, "errors": 1, "tokens": 24}
    ]
    assert len(payload["recent_errors"]) == 1
    assert re.fullmatch(r"\d\d-\d\d \d\d:\d\d", payload["recent_errors"][0]["at"])


async def test_usage_endpoint_rejects_bad_days(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        assert (await client.get("/api/usage?days=0")).status == 400
        assert (await client.get("/api/usage?days=abc")).status == 400
        assert (await client.get("/api/usage?days=91")).status == 400


async def test_search_endpoint_matches_and_escapes(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    _seed_message(control.store, "m1", "100% 装机", timestamp=1_700_000_100)
    _seed_message(control.store, "m2", "1000 装机")
    _seed_message(control.store, "m3", "装机私聊", chat_type=ChatType.PRIVATE, chat_id=9)

    async with running(app) as client:
        resp = await client.get("/api/memory/search", params={"q": "100%"})
        payload = await resp.json()
        assert resp.status == 200
        assert payload["total"] == 1
        assert [item["message_id"] for item in payload["items"]] == ["m1"]
        assert payload["items"][0]["at"]

        scoped = await (
            await client.get(
                "/api/memory/search",
                params={"q": "装机", "chat_type": "group", "chat_id": "100"},
            )
        ).json()
        assert scoped["total"] == 2
        assert [item["message_id"] for item in scoped["items"]] == ["m2", "m1"]

        paged = await (
            await client.get(
                "/api/memory/search", params={"q": "装机", "limit": "1", "offset": "1"}
            )
        ).json()
        assert paged["total"] == 3
        assert [item["message_id"] for item in paged["items"]] == ["m2"]


async def test_search_endpoint_validates_query(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        assert (await client.get("/api/memory/search")).status == 400
        assert (await client.get("/api/memory/search?q=x")).status == 200
        assert (await client.get("/api/memory/search?q=x&offset=-1")).status == 400
        assert (
            await client.get("/api/memory/search?q=x&chat_id=1")
        ).status == 400  # 只给 chat_id 不够


async def test_export_markdown_and_json(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    _seed_message(control.store, "m1", "第一句", timestamp=1_700_000_100)
    _seed_message(control.store, "m2", "第二句", timestamp=1_700_000_200)
    control.store.upsert_summary(ChatType.GROUP, 100, "2026-10-23", "聊了装机")
    control.store.upsert_impression(ChatType.GROUP, 100, 42, "preference", "喜欢猫", 0.9)

    async with running(app) as client:
        md = await client.get(
            "/api/memory/export", params={"chat_type": "group", "chat_id": "100"}
        )
        text = await md.text()
        assert md.status == 200
        assert md.headers["Content-Type"].startswith("text/markdown")
        assert "attachment" in md.headers["Content-Disposition"]
        assert "大肥鱼记忆导出 · group/100" in text
        assert text.index("第一句") < text.index("第二句")
        assert "聊了装机" in text
        assert "喜欢猫" in text

        js = await client.get(
            "/api/memory/export",
            params={"chat_type": "group", "chat_id": "100", "format": "json"},
        )
        bundle = await js.json()
        assert js.status == 200
        assert bundle["truncated"] is False
        assert [m["message_id"] for m in bundle["messages"]] == ["m1", "m2"]
        assert bundle["message_total"] == 2
        assert bundle["summaries"][0]["date"] == "2026-10-23"

        truncated = await (
            await client.get(
                "/api/memory/export",
                params={"chat_type": "group", "chat_id": "100", "format": "json", "limit": "1"},
            )
        ).json()
        assert truncated["truncated"] is True
        assert len(truncated["messages"]) == 1


async def test_export_endpoint_validates_scope_and_format(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        assert (await client.get("/api/memory/export")).status == 400
        assert (
            await client.get("/api/memory/export?chat_type=group&chat_id=0")
        ).status == 400
        assert (
            await client.get(
                "/api/memory/export?chat_type=group&chat_id=1&format=pdf"
            )
        ).status == 400


async def test_backup_endpoint_returns_openable_snapshot(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    _seed_message(control.store, "m1", "备份里的消息")
    control.store.upsert_summary(ChatType.GROUP, 100, "2026-10-23", "备份里的摘要")

    async with running(app) as client:
        resp = await client.get("/api/backup")
        body = await resp.read()
        assert resp.status == 200
        disposition = resp.headers["Content-Disposition"]
        assert "attachment" in disposition
        assert re.search(r'dafeiyu-backup-\d{8}-\d{6}\.db', disposition)

    target = tmp_path / "snapshot.db"
    target.write_bytes(body)
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert (
            conn.execute("SELECT content FROM summaries").fetchone()[0]
            == "备份里的摘要"
        )
    finally:
        conn.close()


async def test_sessions_endpoint_and_actions(tmp_path: Path) -> None:
    control, service, app = make_control(tmp_path)
    _seed_message(control.store, "m1", "群消息")
    control.store.upsert_summary(ChatType.GROUP, 200, "2026-10-23", "只有摘要")
    control.store.set_brain_session("opencode", ChatType.GROUP, 100, "ses_abc")

    async with running(app) as client:
        payload = await (await client.get("/api/sessions")).json()
        assert [item["chat_id"] for item in payload["items"]] == [100, 200]
        assert payload["items"][0]["session_id"] == "ses_abc"
        assert payload["total"] == 2

        page = await (
            await client.get("/api/sessions", params={"limit": "1", "offset": "1"})
        ).json()
        assert page["total"] == 2
        assert page["limit"] == 1
        assert page["offset"] == 1
        assert [item["chat_id"] for item in page["items"]] == [200]

        new = await client.post("/api/sessions/group/100/new")
        assert new.status == 200
        assert control.store.count_messages(ChatType.GROUP, 100) == 1
        assert control.store.get_brain_session("opencode", ChatType.GROUP, 100) is None

        forget = await client.post("/api/sessions/group/100/forget")
        assert forget.status == 200
        assert control.store.count_messages(ChatType.GROUP, 100) == 0
        assert control.store.get_summary(ChatType.GROUP, 200, "2026-10-23") is not None

        calls: list[tuple[ChatType, int]] = []

        async def forget_chat(chat_type: ChatType, chat_id: int) -> None:
            calls.append((chat_type, chat_id))
            control.store.forget_chat(chat_type, chat_id)

        service.forget_chat = forget_chat
        deleted = await client.delete("/api/sessions/group/100")
        assert deleted.status == 200
        assert calls == [(ChatType.GROUP, 100)]
        assert control.store.count_summaries(ChatType.GROUP, 100) == 0


async def test_session_action_rejects_unknown(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        assert (await client.post("/api/sessions/group/1/rotate")).status == 400
        assert (await client.post("/api/sessions/team/1/new")).status == 400
        assert (await client.delete("/api/sessions/group/abc")).status == 400
        assert (await client.get("/api/sessions?limit=0")).status == 400
        assert (await client.get("/api/sessions?limit=201")).status == 400
        assert (await client.get("/api/sessions?offset=-1")).status == 400


async def test_config_endpoint_has_thresholds_and_no_secrets(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/config")
        raw = await resp.text()
        payload = json.loads(raw)

    assert resp.status == 200
    assert "sk-test" not in raw
    assert "api_key" not in raw
    assert payload["brain"]["provider"] == "token"
    assert payload["persona"]["os_probability"] == 0.05
    assert payload["cooldown"]["brain_error_seconds"] == 30.0
    assert payload["rate_limit"] == {
        "threshold": 3,
        "window_seconds": 600.0,
        "backoff_seconds": 300.0,
        "quota_pause_seconds": 3600.0,
    }
    assert payload["budget"]["daily_calls"] == 200
    assert payload["memory"] == {"window_size": 20, "retention_days": 0}


def test_dashboard_exposes_new_panels() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "src/dafeiyu/supervisor/dashboard/index.html"
    ).read_text(encoding="utf-8")
    for anchor in (
        'id="usage-bars"',
        'id="usage-days"',
        'id="search-q"',
        'id="btn-search-more"',
        'id="sessions"',
        'id="btn-sessions-more"',
        'id="config-body"',
        'id="btn-restart-bot"',
        'id="btn-backup"',
    ):
        assert anchor in html, f"面板缺少 {anchor}"
    assert "/api/usage?days=" in html
    assert "/api/memory/search?" in html
    assert "/api/memory/export?" in html
    assert "/api/sessions?limit=" in html
    assert "/api/config" in html
    assert "/api/backup" in html
    assert "loadSessions(true)" in html
    assert "c.memory.retention_days" in html
    assert "URL.createObjectURL" in html
    assert "confirm(" in html


def test_dashboard_usage_bars_scale_by_max() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "src/dafeiyu/supervisor/dashboard/index.html"
    ).read_text(encoding="utf-8")
    assert "Math.max(1, ...series.map" in html
    assert "bar-seg err" in html
    assert "usage-totals" in html


async def test_budget_endpoint_returns_report(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    control.store.record_usage(ChatType.GROUP, 100, "reply", 1200, 800)

    async with running(app) as client:
        resp = await client.get("/api/budget?chat_type=group&chat_id=100")
        data = await resp.json()

    assert resp.status == 200
    assert data["chat_type"] == "group"
    assert data["chat_id"] == 100
    assert data["per_chat"]["calls"] == 1
    assert data["per_chat"]["tokens"] == 2000
    assert {
        "per_chat",
        "global_daily",
        "monthly",
        "cost",
        "warnings",
    } <= set(data)
    assert data["global_daily"]["calls"] == 1
    assert data["monthly"]["tokens"] == 2000
    assert data["cost"]["currency"]
    assert isinstance(data["warnings"], list)


@pytest.mark.parametrize(
    "query",
    [
        "chat_type=channel&chat_id=1",
        "chat_type=group&chat_id=abc",
        "chat_type=group&chat_id=0",
        "chat_type=group",
        "chat_id=1",
    ],
)
async def test_budget_endpoint_rejects_invalid_scope(
    tmp_path: Path, query: str
) -> None:
    _control, _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get(f"/api/budget?{query}")
        data = await resp.json()
    assert resp.status == 400
    assert data["error"]

async def test_delete_user_messages_requires_delete_user_confirm(
    tmp_path: Path,
) -> None:
    control, _, app = make_control(tmp_path)
    _seed_message(control.store, "m1", "保留", user_id=7)

    async with running(app) as client:
        missing = await client.delete("/api/memory/group/100/user/7")
        wrong = await client.delete(
            "/api/memory/group/100/user/7", json={"confirm": "DELETE"}
        )

    assert missing.status == 400
    assert wrong.status == 400
    assert control.store.count_messages(ChatType.GROUP, 100) == 1


async def test_delete_user_messages_returns_count_and_is_idempotent(
    tmp_path: Path,
) -> None:
    control, _, app = make_control(tmp_path)
    _seed_message(control.store, "m1", "用户7一", user_id=7)
    _seed_message(control.store, "m2", "用户7二", user_id=7)
    _seed_message(control.store, "m3", "用户8", user_id=8)

    async with running(app) as client:
        first = await client.delete(
            "/api/memory/group/100/user/7", json={"confirm": "DELETE_USER"}
        )
        first_data = await first.json()
        second = await client.delete(
            "/api/memory/group/100/user/7", json={"confirm": "DELETE_USER"}
        )
        second_data = await second.json()

    assert first.status == 200
    assert first_data == {"ok": True, "deleted_messages": 2}
    assert second.status == 200
    assert second_data == {"ok": True, "deleted_messages": 0}
    assert control.store.count_messages(ChatType.GROUP, 100) == 1
    remaining = control.store.recent_messages(ChatType.GROUP, 100, 10)
    assert [message.user_id for message in remaining] == [8]


async def test_delete_user_messages_rejects_invalid_target(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    _seed_message(control.store, "m1", "保留", user_id=7)

    async with running(app) as client:
        results = [
            await client.delete(
                "/api/memory/channel/100/user/7", json={"confirm": "DELETE_USER"}
            ),
            await client.delete(
                "/api/memory/group/0/user/7", json={"confirm": "DELETE_USER"}
            ),
            await client.delete(
                "/api/memory/group/100/user/0", json={"confirm": "DELETE_USER"}
            ),
        ]

    assert [resp.status for resp in results] == [400, 400, 400]
    assert control.store.count_messages(ChatType.GROUP, 100) == 1


async def test_delete_user_messages_hides_store_errors(
    tmp_path: Path, monkeypatch
) -> None:
    control, _, app = make_control(tmp_path)

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such column: secret_sql")

    monkeypatch.setattr(control.store, "delete_user_messages", boom)

    async with running(app) as client:
        resp = await client.delete(
            "/api/memory/group/100/user/7", json={"confirm": "DELETE_USER"}
        )
        data = await resp.json()

    assert resp.status == 500
    assert "secret_sql" not in data["error"]
    assert "sqlite" not in data["error"].lower()
    assert "删除" in data["error"]
