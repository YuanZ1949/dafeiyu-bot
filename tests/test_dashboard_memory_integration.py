"""dashboard 记忆面板的端到端闭环：临时库 -> bot 控制口 -> dashboard 代理。

单独一个文件，是为了让这条链路不依赖任何单层的 mock 断言：
播种的数据必须经过真实 HTTP 才能出现在 dashboard 的响应里。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML

from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, create_app as create_bot_app
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.supervisor.dashboard import create_app as create_dashboard_app
from dafeiyu.supervisor.logs import LogHub

YAML = TEST_CONFIG_YAML

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
    store.upsert_impression(ChatType.GROUP, GROUP_ID, 42, "nickname", "外号叫鱼片", 0.9)
    store.upsert_impression(ChatType.GROUP, GROUP_ID, 43, "preference", "喜欢猫", 0.8)


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


async def test_dashboard_extension_end_to_end(tmp_path: Path) -> None:
    async with boot(tmp_path) as (dash, store):
        usage = await (await dash.get("/api/usage?days=7")).json()
        assert len(usage["series"]) == 7
        assert usage["series"][-1]["calls"] == 1
        assert usage["series"][-1]["errors"] == 1
        assert usage["series"][-1]["tokens"] == 15
        assert usage["top"][0]["chat_id"] == GROUP_ID

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
        row = next(item for item in sessions["items"] if item["chat_id"] == GROUP_ID)
        assert row["message_count"] == 2
        assert row["session_id"] == "ses_demo"
        assert row["summary_count"] == 1

        config = await (await dash.get("/api/config")).json()
        assert config["brain"]["provider"] == "token"
        assert config["cooldown"]["brain_error_seconds"] == 30.0

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
