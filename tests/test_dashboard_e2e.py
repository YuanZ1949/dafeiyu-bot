"""真实浏览器（headless Edge + CDP）dashboard E2E。

默认跑法之外的一条窄道：pytest.ini 用 ``addopts = "-m 'not e2e'"`` 把本模块
排除在默认运行外，CI 的 Dashboard E2E 步骤用 ``-m e2e`` 单独执行。机器上
没有 Edge 时整模块干净 skip（跳过 ≠ 通过，输出里必须能看到 skipped）。

覆盖四个真实 DOM 行为（不 mock 前端 JS，走 create_app 的真路由）：

1. 主题持久化：点 #btn-theme 翻转 documentElement.dataset.theme，
   localStorage 写入，重载页面后主题仍在；
2. 六个分区页签：逐个点 #pane-nav [data-pane]，页面上始终恰好一个
   .pane.active 且 id 与点击目标一致；
3. 会话时间线抽屉：点会话行的「查看时间线」让抽屉可见，点关闭按钮再隐藏；
4. 用量趋势：点 #btn-refresh-usage 后 #usage-bars 渲染出柱子，且 dashboard
   的 GET /api/usage（经 TestClient）透传 200 + series 数组。

实现说明：dashboard 的 /api/usage、/api/sessions、timeline 都是透传给 bot
进程的代理（见 supervisor/dashboard.py 的 bot_call），所以旁边起一个最小
aiohttp 桩 bot 提供这三个端点，避免依赖真实 bot。浏览器只经
``tests/_dashboard_cdp.browser_page``（headless Edge + CDP），不引入
playwright / selenium；每次等待都有界（轮询 + 超时断言），无裸 sleep。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from _dashboard_cdp import QaError, browser_page, browser_path
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dafeiyu.supervisor.dashboard import create_app
from dafeiyu.supervisor.logs import LogHub

pytestmark = pytest.mark.e2e

#: E2E 产出的截图证据目录（.omo/ 已在 .gitignore 里，不会弄脏工作树）。
EVIDENCE_DIR = Path(__file__).resolve().parents[1] / ".omo" / "evidence"

# 单个 DOM 状态等待上限：页面 5s 轮询刷新一次，5s 足够且远小于 10s 红线。
WAIT_SECONDS = 5.0

# 桩 bot 提供的会话与时间线数据：只要 dashboard 的代理契约要求的字段。
SESSION_ITEM = {
    "chat_type": "group",
    "chat_id": "10086",
    "message_count": 2,
    "summary_count": 0,
    "impression_count": 0,
}
USAGE_PAYLOAD = {
    "series": [{"date": "2026-11-16", "calls": 3, "errors": 1, "tokens": 480}],
    "totals": {"calls": 3, "errors": 1, "tokens": 480},
}
TIMELINE_PAYLOAD = {
    "messages": [],
    "chat": {"message_total": 2, "summary_total": 0, "impression_total": 0},
    "usage": {"calls": 0, "tokens": 0},
    "session": {"delivered_count": 0, "token_estimate": 0},
    "next_before": None,
    "has_more": False,
    "session_summary": None,
    "summaries": [],
    "impressions": [],
}


class FakeSupervisor:
    """同 test_dashboard_auth_app.py：dashboard 只读 specs/status，不真启进程。"""

    def __init__(self) -> None:
        self.specs = {"opencode": object(), "bot": object(), "napcat": object()}

    def status(self):
        return []

    async def start(self, name):
        raise AssertionError(f"E2E 不应真的启动进程：{name}")

    async def stop(self, name):
        raise AssertionError(f"E2E 不应真的停止进程：{name}")

    async def restart(self, name):
        raise AssertionError(f"E2E 不应真的重启进程：{name}")


def make_bot_app() -> web.Application:
    """dashboard 代理目标的最小桩：只有页面四个用例真正消费的三个端点。

    其它代理端点返回 404，页面 JS 只是 toast 报错，不影响被测的 DOM 行为。
    """

    async def usage(_: web.Request) -> web.Response:
        return web.json_response(USAGE_PAYLOAD)

    async def sessions(_: web.Request) -> web.Response:
        return web.json_response({"items": [SESSION_ITEM], "total": 1})

    async def timeline(_: web.Request) -> web.Response:
        return web.json_response(TIMELINE_PAYLOAD)

    app = web.Application()
    app.router.add_get("/api/usage", usage)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get("/api/sessions/{chat_type}/{chat_id}/timeline", timeline)
    return app


async def make_clients(tmp_path: Path) -> tuple[TestClient, TestClient]:
    """起桩 bot + dashboard（127.0.0.1 TestServer），返回两个已启动的 client。

    settings=None ⇒ auth 关闭（见 _auth_from_settings），浏览器直开首页即登录态。
    """
    bot_client = TestClient(TestServer(make_bot_app()))
    await bot_client.start_server()
    bot_url = str(bot_client.server.make_url("/")).rstrip("/")
    app = create_app(
        FakeSupervisor(),
        settings=None,
        logs=LogHub(tmp_path / "logs"),
        qr_path=tmp_path / "qrcode.png",
        bot_url=bot_url,
    )
    dash_client = TestClient(TestServer(app))
    await dash_client.start_server()
    return dash_client, bot_client


@asynccontextmanager
async def dashboard_page(
    tmp_path: Path,
) -> AsyncIterator[tuple[TestClient, "object"]]:
    """dashboard TestClient + headless 浏览器页面；无 Edge 时整用例 skip。"""
    try:
        browser_path()
    except QaError as exc:
        import pytest

        pytest.skip(f"本机没有可用的浏览器（Edge）：{exc.message}")
    dash_client, bot_client = await make_clients(tmp_path)
    try:
        url = str(dash_client.server.make_url("/"))
        async with browser_page(url) as page:
            yield dash_client, page
    finally:
        await bot_client.close()
        await dash_client.close()


async def wait_until(page, js: str, *, description: str) -> None:
    """轮询等一段 JS 变真值；有界等待（WAIT_SECONDS），超时直接断言失败。"""
    deadline = WAIT_SECONDS
    while True:
        if await page.eval_js(js):
            return
        if deadline <= 0:
            raise AssertionError(f"等待超时（{WAIT_SECONDS}s）：{description}")
        await asyncio.sleep(0.1)
        deadline -= 0.1


# ---------------------------------------------------------------- 用例 1：主题


async def test_e2e_theme_persists_across_reload(tmp_path: Path) -> None:
    async with dashboard_page(tmp_path) as (client, page):
        before = await page.eval_js("document.documentElement.dataset.theme")
        assert before in ("dark", "light")
        expected = "light" if before == "dark" else "dark"

        await page.click("#btn-theme")
        await wait_until(
            page,
            f"document.documentElement.dataset.theme === {expected!r}",
            description=f"点击 #btn-theme 后主题应翻转为 {expected}",
        )
        saved = await page.eval_js('localStorage.getItem("dafeiyu.theme")')
        assert saved == expected

        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        await page.screenshot(EVIDENCE_DIR / "qg-e2e-theme.png")

        # 重载后 initTheme() 从 localStorage 恢复，主题必须还在。
        await page.goto(str(client.server.make_url("/")))
        await wait_until(
            page,
            f"document.documentElement.dataset.theme === {expected!r}",
            description=f"重载后主题应保持为 {expected}",
        )
        assert (
            await page.eval_js("document.documentElement.dataset.theme") == expected
        )


# ---------------------------------------------------------------- 用例 2：分区


async def test_e2e_six_panes_switch_exclusively(tmp_path: Path) -> None:
    async with dashboard_page(tmp_path) as (_client, page):
        pane_ids = await page.eval_js(
            "[...document.querySelectorAll('#pane-nav [data-pane]')]"
            ".map((b) => b.dataset.pane)"
        )
        assert len(pane_ids) == 6, f"应恰好 6 个分区页签，实际 {pane_ids!r}"

        for pane_id in pane_ids:
            await page.click(f'#pane-nav button[data-pane="{pane_id}"]')
            await wait_until(
                page,
                "(() => { const active = document.querySelectorAll('.pane.active');"
                f"return active.length === 1 && active[0].id === {pane_id!r} }})()",
                description=f"点击 {pane_id} 后应只有 id 匹配的 .pane.active",
            )

        # 任意时刻也必须恰好一个激活分区（最后一个页签落下后再兜底验一遍）。
        state = await page.eval_js(
            "(() => { const active = document.querySelectorAll('.pane.active');"
            "return { count: active.length, id: active.length ? active[0].id : null }; })()"
        )
        assert state == {"count": 1, "id": pane_ids[-1]}

        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        await page.screenshot(EVIDENCE_DIR / "qg-e2e-panes.png")


# ------------------------------------------------------------ 用例 3：时间线


async def test_e2e_timeline_drawer_opens_and_closes(tmp_path: Path) -> None:
    async with dashboard_page(tmp_path) as (_client, page):
        # loadSessions() 首屏自动拉 /api/sessions 并渲染会话行；等行出现再点。
        await page.wait_for(".session-row")
        opened = await page.eval_js(
            "(() => { const btn = [...document.querySelectorAll("
            "'.session-row .acts button')].find((b) => b.textContent === '查看时间线');"
            "if (!btn) return false; btn.click(); return true; })()"
        )
        assert opened, "会话行里应存在「查看时间线」按钮"

        await wait_until(
            page,
            "document.getElementById('timeline-drawer') !== null &&"
            "!document.getElementById('timeline-drawer').hidden",
            description="时间线抽屉应变为可见",
        )

        await page.click("#btn-timeline-close")
        await wait_until(
            page,
            "document.getElementById('timeline-drawer').hidden",
            description="点关闭后时间线抽屉应重新隐藏",
        )


# ---------------------------------------------------------------- 用例 4：用量


async def test_e2e_usage_bars_render_and_api_returns_series(tmp_path: Path) -> None:
    async with dashboard_page(tmp_path) as (client, page):
        # 桩 bot 的 /api/usage 有一条 series 数据 ⇒ 走「有数据」分支：
        # #usage-bars 应渲染出 >0 个 .bar-col 子节点（空态分支由
        # tests/test_dashboard_html.py 的静态用例覆盖）。
        await page.click("#btn-refresh-usage")
        await page.wait_for("#usage-bars .bar-col")
        bars = await page.eval_js(
            "document.getElementById('usage-bars').childElementCount"
        )
        assert bars > 0

        # 同一个 dashboard 实例走 TestClient 再验一次代理契约。
        resp = await client.get("/api/usage")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data.get("series"), list)
        assert len(data["series"]) > 0
