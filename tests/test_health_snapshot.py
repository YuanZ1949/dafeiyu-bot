from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML

from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, create_app
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message


class FakeBrain:
    async def aclose(self) -> None:
        pass


class FakeService:
    def __init__(self) -> None:
        self.brain = FakeBrain()
        self.scheduler = None

    def set_brain(self, brain):
        previous = self.brain
        self.brain = brain
        return previous


class FakeAdapter:
    def __init__(self) -> None:
        self.connected = True
        self.sent: list[tuple[ChatType, int, str]] = []

    async def send(self, chat_type: ChatType, chat_id: int, text: str) -> None:
        self.sent.append((chat_type, chat_id, text))


@asynccontextmanager
async def running(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def make_settings(tmp_path: Path, admins: frozenset[int] = frozenset()):
    config = tmp_path / "config.yaml"
    config.write_text(TEST_CONFIG_YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)
    if admins:
        settings = replace(
            settings, groups=replace(settings.groups, admins=admins)
        )
    return settings


def make_control(tmp_path: Path, admins: frozenset[int] = frozenset(), token: str = ""):
    settings = make_settings(tmp_path, admins)
    store = Store(tmp_path / "test.db")
    store.init_schema()
    service = FakeService()
    adapter = FakeAdapter()
    control = BrainControl(
        settings,
        store,
        service,
        adapter,
        runtime_path=str(tmp_path / "runtime.json"),
    )
    return control, adapter, store, create_app(control, token)


def test_health_snapshot_shape_and_no_secrets(tmp_path: Path) -> None:
    control, adapter, store, _app = make_control(tmp_path)
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=100,
            user_id=1,
            nickname="secret-nick",
            content="hello-secret",
            message_id="m1",
            timestamp=1,
        )
    )
    adapter.last_event_at = 1_700_000_000
    adapter.last_send_at = 1_700_000_100

    snapshot = control.health_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["version"]
    assert snapshot["gateway"]["connected"] is True
    assert snapshot["gateway"]["last_event_at"].startswith("2023-")
    assert snapshot["traffic"]["last_reply_at"].startswith("2023-")
    assert snapshot["memory"]["message_count"] == 1
    assert snapshot["quota"]["reason"] == ""
    assert set(snapshot["budget"]) >= {"per_chat", "global_daily", "monthly", "warnings"}

    text = json.dumps(snapshot, ensure_ascii=False)
    assert "hello-secret" not in text
    assert "secret-nick" not in text
    assert "sk-test" not in text


def test_health_snapshot_reports_per_channel_state(tmp_path: Path) -> None:
    control, adapter, _store, _app = make_control(tmp_path)
    adapter.last_event_at = 1_700_000_000
    adapter.last_disconnect_at = 1_700_000_050
    adapter.last_send_at = 1_700_000_100
    control.service.channel_status = lambda: {
        "qq": {
            "connected": True,
            "degraded": False,
            "last_event_at": 1_700_000_000,
            "last_disconnect_at": None,
            "last_send_at": 1_700_000_100,
        },
        "wechat": {
            "connected": False,
            "degraded": True,
            "last_event_at": 1_700_000_200,
            "last_disconnect_at": 1_700_000_300,
            "last_send_at": 1_700_000_400,
            "logged_in": False,
            "desktop": "locked",
        },
    }

    snapshot = control.health_snapshot()

    # 顶层 gateway 继续来自 self.adapter，旧面板与旧探针不受影响。
    assert snapshot["gateway"]["connected"] is True
    assert snapshot["gateway"]["last_event_at"].startswith("2023-")
    assert snapshot["gateway"]["last_disconnect_at"].startswith("2023-")

    channels = snapshot["channels"]
    assert set(channels) == {"qq", "wechat"}
    required = {
        "connected",
        "degraded",
        "last_event_at",
        "last_disconnect_at",
        "last_send_at",
    }
    for name in ("qq", "wechat"):
        assert required <= set(channels[name])
        assert isinstance(channels[name]["connected"], bool)
        assert isinstance(channels[name]["degraded"], bool)
        assert channels[name]["last_event_at"].startswith("2023-")
        assert channels[name]["last_send_at"].startswith("2023-")

    assert channels["qq"]["last_disconnect_at"] is None
    assert "logged_in" not in channels["qq"]
    assert "desktop" not in channels["qq"]
    assert channels["wechat"]["last_disconnect_at"].startswith("2023-")
    assert channels["wechat"]["logged_in"] is False
    assert channels["wechat"]["desktop"] == "locked"


def test_health_snapshot_channel_status_failure_keeps_snapshot(
    tmp_path: Path,
) -> None:
    control, adapter, _store, _app = make_control(tmp_path)
    adapter.last_event_at = 1_700_000_000

    def boom():
        raise RuntimeError("channel status broken")

    control.service.channel_status = boom
    snapshot = control.health_snapshot()

    assert snapshot["channels"] == {}
    assert snapshot["ok"] is True
    assert snapshot["gateway"]["connected"] is True
    assert snapshot["gateway"]["last_event_at"].startswith("2023-")


def test_health_snapshot_budget_report(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = replace(
        settings,
        budget=replace(
            settings.budget,
            global_daily_calls=100,
            monthly_calls=200,
            monthly_cost_limit=10.0,
            price_input_per_million=1.0,
            price_output_per_million=2.0,
        ),
    )
    store = Store(tmp_path / "test.db")
    store.init_schema()
    store.record_usage(ChatType.GROUP, 100, "reply", 1_000_000, 500_000)
    control = BrainControl(
        settings,
        store,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )

    snapshot = control.health_snapshot()
    budget = snapshot["budget"]

    assert budget["per_chat"]["calls"] == 1
    assert budget["per_chat"]["tokens"] == 1_500_000
    assert budget["global_daily"] == {
        "calls": 1,
        "tokens": 1_500_000,
        "limit_calls": 100,
        "limit_tokens": 0,
    }
    assert budget["monthly"]["calls"] == 1
    assert budget["monthly"]["limit_calls"] == 200
    assert budget["cost"]["cost"] == 2.0
    assert budget["cost"]["cost_limit"] == 10.0
    assert budget["cost"]["currency"] == "CNY"
    assert isinstance(budget["warnings"], list)
    assert all(isinstance(item, dict) for item in budget["warnings"])


def test_health_snapshot_budget_storage_error_keeps_shape(
    tmp_path: Path, monkeypatch
) -> None:
    control, _adapter, store, _app = make_control(tmp_path)

    def boom(_date):
        raise RuntimeError("db broken")

    monkeypatch.setattr(store, "usage_global_today", boom)
    snapshot = control.health_snapshot()

    budget = snapshot["budget"]
    assert set(budget) == {"per_chat", "global_daily", "monthly", "cost", "warnings"}
    assert budget["global_daily"]["calls"] == 0
    assert budget["monthly"]["calls"] == 0
    assert budget["cost"]["cost"] == 0.0
    assert budget["warnings"] == [
        {"scope": "storage_error", "ratio": 1.0, "calls": 0, "limit": 0}
    ]


def test_health_snapshot_db_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    control, _adapter, store, _app = make_control(tmp_path)

    def boom():
        raise RuntimeError("db broken")

    monkeypatch.setattr(store, "db_stats", boom)
    snapshot = control.health_snapshot()

    assert snapshot["ok"] is False
    assert snapshot["errors"]["last_error_at"]


def test_health_snapshot_counts_scheduler(tmp_path: Path) -> None:
    control, _adapter, _store, _app = make_control(tmp_path)

    class Scheduler:
        def pending_count(self):
            return 3

        def inflight_count(self):
            return 1

    control.service.scheduler = Scheduler()
    snapshot = control.health_snapshot()
    assert snapshot["scheduler"] == {"pending_chats": 3, "inflight_chats": 1}


async def test_health_detail_requires_token(tmp_path: Path) -> None:
    _control, _adapter, _store, app = make_control(tmp_path, token="t")
    async with running(app) as client:
        resp = await client.get("/api/health")
        assert resp.status == 200
        assert await resp.json() == {"ok": True}

        resp = await client.get("/api/health/detail")
        assert resp.status == 401

        resp = await client.get(
            "/api/health/detail", headers={"X-Control-Token": "t"}
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert "gateway" in data


async def test_notify_admin_sends_to_first_admin(tmp_path: Path) -> None:
    _control, adapter, _store, app = make_control(
        tmp_path, admins=frozenset({12345}), token="t"
    )
    headers = {"X-Control-Token": "t"}
    async with running(app) as client:
        resp = await client.post(
            "/api/notify/admin", json={"text": "告警", "dedupe_key": "bot_down"}, headers=headers
        )
        assert resp.status == 200
        assert adapter.sent == [(ChatType.PRIVATE, 12345, "告警")]


async def test_notify_admin_validates_payload_and_limits(tmp_path: Path) -> None:
    _control, adapter, _store, app = make_control(
        tmp_path, admins=frozenset({12345}), token="t"
    )
    headers = {"X-Control-Token": "t"}
    async with running(app) as client:
        assert (
            await client.post("/api/notify/admin", json={}, headers=headers)
        ).status == 400
        assert (
            await client.post(
                "/api/notify/admin", json={"text": "x" * 1001}, headers=headers
            )
        ).status == 400

        assert (
            await client.post(
                "/api/notify/admin", json={"text": "a"}, headers=headers
            )
        ).status == 200
        assert (
            await client.post(
                "/api/notify/admin", json={"text": "b"}, headers=headers
            )
        ).status == 429
        assert len(adapter.sent) == 1


async def test_notify_admin_unavailable_without_admin_or_gateway(
    tmp_path: Path,
) -> None:
    _control, adapter, _store, app = make_control(tmp_path, token="t")
    headers = {"X-Control-Token": "t"}
    async with running(app) as client:
        assert (
            await client.post("/api/notify/admin", json={"text": "a"}, headers=headers)
        ).status == 503

    (tmp_path / "second").mkdir()
    _control, adapter, _store, app = make_control(
        tmp_path / "second", admins=frozenset({1}), token="t"
    )
    adapter.connected = False
    async with running(app) as client:
        assert (
            await client.post("/api/notify/admin", json={"text": "a"}, headers=headers)
        ).status == 503
