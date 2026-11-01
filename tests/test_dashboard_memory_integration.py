"""dashboard 记忆面板的端到端闭环：临时库 -> bot 控制口 -> dashboard 代理。

单独一个文件，是为了让这条链路不依赖任何单层的 mock 断言：
播种的数据必须经过真实 HTTP 才能出现在 dashboard 的响应里。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML, TEST_DASHBOARD_LOOPBACK_YAML

from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, create_app as create_bot_app
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.supervisor.dashboard import create_app as create_dashboard_app
from dafeiyu.supervisor.logs import LogHub

YAML = TEST_CONFIG_YAML + TEST_DASHBOARD_LOOPBACK_YAML

GROUP_ID = 20001
PRIVATE_ID = 10001


class FakeBrain:
    async def aclose(self) -> None:
        return None


class FakeService:
    def __init__(self) -> None:
        self.brain = FakeBrain()
        self.impressions = None

    def set_brain(self, brain):
        previous = self.brain
        self.brain = brain
        return previous

    def explain_trigger(self, msg, *, seed=None):
        return {
            "verdict": "model",
            "mode": "rules",
            "reason": "hard",
            "message": {"content": msg.content},
            "checks": [],
            "metrics": {},
        }


class FakeAdapter:
    connected = True


class FakeSupervisor:
    def __init__(self) -> None:
        self.specs = {"opencode": object(), "bot": object(), "napcat": object()}

    def status(self):
        return []

    async def start(self, name):
        return None

    async def stop(self, name):
        return None

    async def restart(self, name):
        return None


def seed(store: Store) -> None:
    store.upsert_summary(ChatType.GROUP, GROUP_ID, "2026-10-24", "群里聊了显卡和钓鱼")
    store.upsert_summary(ChatType.PRIVATE, PRIVATE_ID, "2026-10-23", "私聊聊了晚饭")
    store.upsert_impression(ChatType.GROUP, GROUP_ID, "qq:42", "nickname", "外号叫鱼片", 0.9)
    store.upsert_impression(ChatType.GROUP, GROUP_ID, "qq:43", "preference", "喜欢猫", 0.8)


async def test_dashboard_memory_reads_seeded_store_end_to_end(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)

    store = Store(tmp_path / "demo.db")
    store.init_schema()
    seed(store)
    control = BrainControl(
        settings,
        store,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )

    bot_client = TestClient(TestServer(create_bot_app(control)))
    await bot_client.start_server()
    dash_client = None
    try:
        bot_url = str(bot_client.server.make_url("/")).rstrip("/")
        dash_app = create_dashboard_app(
            FakeSupervisor(),
            settings,
            LogHub(tmp_path / "logs"),
            tmp_path / "qrcode.png",
            bot_url,
        )
        dash_client = TestClient(TestServer(dash_app))
        await dash_client.start_server()

        # 全量：两条摘要、两条印象都从播种库穿过两层 HTTP 到达 dashboard。
        resp = await dash_client.get("/api/memory?limit=10")
        data = await resp.json()
        assert resp.status == 200
        assert {s["content"] for s in data["summaries"]} == {
            "群里聊了显卡和钓鱼",
            "私聊聊了晚饭",
        }
        assert {i["content"] for i in data["impressions"]} == {"外号叫鱼片", "喜欢猫"}

        # 过滤 + 分页：只取群聊第 2 条印象（最新一条是「喜欢猫」）。
        resp = await dash_client.get(
            f"/api/memory?limit=1&offset=1&chat_type=group&chat_id={GROUP_ID}"
        )
        page = await resp.json()
        assert resp.status == 200
        assert [i["content"] for i in page["impressions"]] == ["外号叫鱼片"]

        # 私聊过滤只应看到私聊摘要，群聊印象全部被滤掉。
        resp = await dash_client.get("/api/memory?limit=10&chat_type=private")
        private = await resp.json()
        assert [s["content"] for s in private["summaries"]] == ["私聊聊了晚饭"]
        assert private["impressions"] == []

        # 记忆体检也走同一条代理链路：只有摘要、没有消息，因此没有缺口。
        resp = await dash_client.get("/api/memory/health")
        health = await resp.json()
        assert resp.status == 200
        assert health["ok"] is True
        assert health["pending_count"] == 0
        assert health["last_summary"]["date"] == "2026-10-24"
    finally:
        if dash_client is not None:
            await dash_client.close()
        await bot_client.close()
        store.close()


@asynccontextmanager
async def boot(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)

    store = Store(tmp_path / "demo.db")
    store.init_schema()
    seed(store)
    for index, content in enumerate(("第一条：显卡", "第二条：钓鱼")):
        store.add_message(
            Message(
                chat_type=ChatType.GROUP,
                chat_id=GROUP_ID,
                user_id=42,
                nickname="鱼片",
                content=content,
                message_id=f"m{index}",
                timestamp=1_700_000_000 + index,
            )
        )
    store.set_brain_session("opencode", ChatType.GROUP, GROUP_ID, "ses_demo")
    store.record_usage(ChatType.GROUP, GROUP_ID, "reply", 10, 5)
    store.record_usage(ChatType.GROUP, GROUP_ID, "error", 0, 0)

    control = BrainControl(
        settings,
        store,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )
    bot_client = TestClient(TestServer(create_bot_app(control)))
    await bot_client.start_server()
    dash_client = None
    try:
        bot_url = str(bot_client.server.make_url("/")).rstrip("/")
        dash_app = create_dashboard_app(
            FakeSupervisor(),
            settings,
            LogHub(tmp_path / "logs"),
            tmp_path / "qrcode.png",
            bot_url,
        )
        dash_client = TestClient(TestServer(dash_app))
        await dash_client.start_server()
        yield dash_client, store
    finally:
        if dash_client is not None:
            await dash_client.close()
        await bot_client.close()
        store.close()


async def test_dashboard_timeline_proxy_end_to_end(tmp_path: Path) -> None:
    async with boot(tmp_path) as (dash, store):
        resp = await dash.get(
            f"/api/sessions/group/{GROUP_ID}/timeline?limit=1"
        )
        data = await resp.json()
        assert resp.status == 200
        assert [m["content"] for m in data["messages"]] == ["第二条：钓鱼"]
        assert data["has_more"] is True
        assert data["chat"]["message_total"] == 2

        anchored = await (
            await dash.get(
                f"/api/sessions/group/{GROUP_ID}/timeline?limit=1&anchor=m0"
            )
        ).json()
        assert [m["content"] for m in anchored["messages"]] == ["第一条：显卡"]
        assert anchored["anchor_found"] is True

        bad = await dash.get(f"/api/sessions/group/{GROUP_ID}/timeline?limit=0")
        assert bad.status == 400


async def test_dashboard_trigger_debug_proxy_end_to_end(tmp_path: Path) -> None:
    async with boot(tmp_path) as (dash, store):
        resp = await dash.post(
            "/api/debug/trigger",
            json={
                "chat_type": "group",
                "chat_id": GROUP_ID,
                "user_id": 42,
                "nickname": "鱼片",
                "content": "大肥鱼在吗",
                "at_bot": True,
            },
        )
        data = await resp.json()

        assert resp.status == 200
        assert data["verdict"] == "model"
        assert data["message"]["content"] == "大肥鱼在吗"
        assert store.count_messages(ChatType.GROUP, GROUP_ID) == 2

        bad = await dash.post(
            "/api/debug/trigger",
            json={
                "chat_type": "group",
                "chat_id": GROUP_ID,
                "user_id": 42,
                "content": "",
            },
        )
        assert bad.status == 400


async def test_dashboard_extension_end_to_end(tmp_path: Path) -> None:
    async with boot(tmp_path) as (dash, store):
        usage = await (await dash.get("/api/usage?days=7")).json()
        assert len(usage["series"]) == 7
        assert usage["series"][-1]["calls"] == 1
        assert usage["series"][-1]["errors"] == 1
        assert usage["series"][-1]["tokens"] == 15
        assert usage["top"][0]["chat_id"] == str(GROUP_ID)

        found = await (
            await dash.get("/api/memory/search", params={"q": "显卡"})
        ).json()
        assert found["total"] == 1
        assert found["items"][0]["content"] == "第一条：显卡"

        md = await dash.get(
            "/api/memory/export",
            params={"chat_type": "group", "chat_id": str(GROUP_ID)},
        )
        text = await md.text()
        assert md.status == 200
        assert "text/markdown" in md.headers["Content-Type"]
        assert "第一条：显卡" in text and "群里聊了显卡和钓鱼" in text

        bundle = await (
            await dash.get(
                "/api/memory/export",
                params={
                    "chat_type": "group",
                    "chat_id": str(GROUP_ID),
                    "format": "json",
                },
            )
        ).json()
        assert bundle["message_total"] == 2
        assert len(bundle["summaries"]) == 1

        sessions = await (await dash.get("/api/sessions")).json()
        row = next(
            item for item in sessions["items"] if item["chat_id"] == str(GROUP_ID)
        )
        assert row["message_count"] == 2
        assert row["session_id"] == "ses_demo"
        assert row["summary_count"] == 1

        config = await (await dash.get("/api/config")).json()
        assert config["brain"]["provider"] == "token"
        assert config["cooldown"] == {"tool_hint_seconds": 60.0}
        assert config["behavior"]["chat_intensity"] == "normal"
        assert config["memory"]["retention_days"] == 180

        backup = await dash.get("/api/backup")
        body = await backup.read()
        assert backup.status == 200
        assert "attachment" in backup.headers["Content-Disposition"]
        snapshot = tmp_path / "dashboard-backup.db"
        snapshot.write_bytes(body)
        conn = sqlite3.connect(snapshot)
        try:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        finally:
            conn.close()

        # 新建 session：消息还在，映射没了。
        assert (await dash.post(f"/api/sessions/group/{GROUP_ID}/new")).status == 200
        assert store.count_messages(ChatType.GROUP, GROUP_ID) == 2
        assert store.get_brain_session("opencode", ChatType.GROUP, GROUP_ID) is None

        # 软忘记：消息清掉，摘要保留。
        assert (
            await dash.post(f"/api/sessions/group/{GROUP_ID}/forget")
        ).status == 200
        assert store.count_messages(ChatType.GROUP, GROUP_ID) == 0
        assert store.count_summaries(ChatType.GROUP, GROUP_ID) == 1

        # 彻底删除：FakeService 无 forget_chat 时走 brain/store 兜底。
        assert (await dash.delete(f"/api/sessions/group/{GROUP_ID}")).status == 200
        assert store.count_summaries(ChatType.GROUP, GROUP_ID) == 0


async def test_dashboard_impression_edit_and_merge_proxy(tmp_path: Path) -> None:
    group = ChatType.GROUP
    async with boot(tmp_path) as (dash, store):
        store.upsert_impression(group, GROUP_ID, "qq:42", "preference", "喜欢狗", 0.5)
        rows = [
            row
            for row in store.list_impressions(group, GROUP_ID, topk=100)
            if row.subject_user_id == "qq:42"
        ]
        assert len(rows) == 2
        edit_id = rows[0].id

        edited = await dash.patch(
            f"/api/memory/impressions/{edit_id}",
            json={"content": "外号叫鱼片本人", "confidence": 0.95},
        )
        edited_payload = await edited.json()
        assert edited.status == 200
        assert edited_payload["impression"]["content"] == "外号叫鱼片本人"
        assert store.get_impression(edit_id).content == "外号叫鱼片本人"

        merged = await dash.post(
            "/api/memory/impressions/merge",
            json={"ids": [row.id for row in rows], "content": "鱼片喜欢狗"},
        )
        merged_payload = await merged.json()
        assert merged.status == 200
        assert merged_payload["impression"]["content"] == "鱼片喜欢狗"
        assert merged_payload["impression"]["confidence"] == 0.95
        assert merged_payload["impression"]["id"] == min(row.id for row in rows)
        assert len(
            [
                row
                for row in store.list_impressions(group, GROUP_ID, topk=100)
                if row.subject_user_id == "qq:42"
            ]
        ) == 1


async def test_dashboard_proxy_waits_for_slow_delete_and_backup(tmp_path: Path) -> None:
    """2 秒总超时不能套在删 session / 整库备份上：假 bot 慢 3 秒仍应成功。"""
    config = tmp_path / "config.yaml"
    config.write_text(YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)

    async def slow_delete(_: web.Request) -> web.Response:
        await asyncio.sleep(3.0)
        return web.json_response({"ok": True})

    async def slow_backup(_: web.Request) -> web.Response:
        await asyncio.sleep(3.0)
        return web.Response(
            body=b"snapshot-bytes",
            headers={"Content-Disposition": 'attachment; filename="backup.db"'},
        )

    bot_app = web.Application()
    bot_app.router.add_delete("/api/sessions/{chat_type}/{chat_id}", slow_delete)
    bot_app.router.add_get("/api/backup", slow_backup)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()
    dash_client = None
    try:
        dash_app = create_dashboard_app(
            FakeSupervisor(),
            settings,
            LogHub(tmp_path / "logs"),
            tmp_path / "qrcode.png",
            str(bot_client.server.make_url("/")).rstrip("/"),
        )
        dash_client = TestClient(TestServer(dash_app))
        await dash_client.start_server()

        started = time.monotonic()
        deleted = await dash_client.delete("/api/sessions/group/1")
        delete_elapsed = time.monotonic() - started
        assert deleted.status == 200
        assert delete_elapsed >= 2.8

        started = time.monotonic()
        backup = await dash_client.get("/api/backup")
        backup_elapsed = time.monotonic() - started
        assert backup.status == 200
        assert await backup.read() == b"snapshot-bytes"
        assert "attachment" in backup.headers["Content-Disposition"]
        assert backup_elapsed >= 2.8
    finally:
        if dash_client is not None:
            await dash_client.close()
        await bot_client.close()
