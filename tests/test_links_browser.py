"""chat-driven-behaviour-fixes T2：Edge CDP 有头最小化渲染层。

全部用可注入的假 CDP connector（HTTP 发现端点 + target 会话）与假 launcher，
**不起真浏览器**：焦点/最小化的实机断言属于 F3 的冒烟项（单测里不做
IsWindowVisible——最小化窗口仍带 WS_VISIBLE，该断言必失败）。

覆盖：启动参数（--start-minimized / 独立 --user-data-dir / --remote-debugging-port，
且无 --headless）、mode 非 minimized 显式 ValueError、js-initialData 优先解析、
navigate 装死 → browser_timeout 且 terminate 恰好一次 + target 被关、
空闲关闭与再次拉起、并发两 URL 开两个 target 且 ≤ max_targets、
launcher 抛错 → browser_launch_failed（不外抛）、默认 connector 的
PUT /json/new 与 405 → GET 回退。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

import dafeiyu.links_browser as links_browser
from dafeiyu.config import LinkBrowserConfig
from dafeiyu.links_browser import BrowserRenderer, _DefaultCdpConnector

# ---- 可注入的假件 -------------------------------------------------------------


class FakeClock:
    """可手动推进的时钟；step>0 时每次调用自动前进（驱动注入时钟超时）。"""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 1_700_000_000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class FakeProcess:
    """进程句柄假件（Popen 同形：poll()=None 表示仍在运行）。"""

    def __init__(self) -> None:
        self.terminate_calls = 0
        self.alive = True

    def poll(self) -> int | None:
        return None if self.alive else 1

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakeLauncher:
    """进程工厂假件：记录 (binary, args)，绝不真的起进程；fail 模拟起不来。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.processes: list[FakeProcess] = []
        self.fail = fail

    def __call__(self, binary: str, args: Sequence[str]) -> FakeProcess:
        self.calls.append((binary, list(args)))
        if self.fail:
            raise FileNotFoundError(binary)
        process = FakeProcess()
        self.processes.append(process)
        return process


class FakeSession:
    """target 级 CDP 会话假件：命令按脚本回包，事件走共享标志，可装死。"""

    def __init__(self, url: str, behavior: FakeBehavior, connector: FakeConnector) -> None:
        self.url = url
        self.behavior = behavior
        self.connector = connector
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._next_id = 0

    async def command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        mid = self._next_id
        self.sent.append({"id": mid, "method": method, "params": params})
        reply = self.behavior.reply(method, params, mid)
        if reply is None:
            # 装死：永不返回，等渲染层超时取消本协程。
            await _hang_forever()
            raise AssertionError("unreachable")  # pragma: no cover - 仅供类型收窄
        if "error" in reply:
            raise RuntimeError(f"cdp error: {reply['error']}")
        return reply.get("result") or {}

    async def wait_event(self, name: str) -> dict[str, Any]:
        # 事件是否已到看共享 behavior（navigate 应答与事件之间无先后保证）。
        if name == "Page.loadEventFired" and self.behavior.load_fired:
            return {"method": name}
        await asyncio.Future()
        raise AssertionError("unreachable")  # pragma: no cover - 仅供类型收窄

    async def close(self) -> None:
        self.closed = True
        self.connector.release()


async def _hang_forever() -> None:
    """永不完成的协程：模拟 CDP 端点装死（渲染层超时后 cancel）。"""
    await asyncio.Event().wait()  # pragma: no cover - 测试里不会真的完成


class FakeBehavior:
    """脚本化 CDP 行为：navigate 是否装死、navigate 后是否有 load 事件、快照内容。"""

    def __init__(self) -> None:
        self.navigate_silent = False
        self.load_after_navigate = True
        self.load_fired = False
        self.snapshot_payload = ""

    def reply(self, method: str, params: dict[str, Any], mid: int) -> dict[str, Any] | None:
        if method == "Page.navigate":
            if self.navigate_silent:
                return None
            if self.load_after_navigate:
                self.load_fired = True
            return {"id": mid, "result": {}}
        if method == "Runtime.evaluate":
            expression = str(params.get("expression", ""))
            if "readyState" in expression:
                return {"id": mid, "result": {"result": {"type": "string", "value": "complete"}}}
            return {
                "id": mid,
                "result": {"result": {"type": "string", "value": self.snapshot_payload}},
            }
        return {"id": mid, "result": {}}


class FakeConnector:
    """CDP 端点面假件：version/create_target/close_target/connect 全记录。"""

    def __init__(self, behavior: FakeBehavior) -> None:
        self.behavior = behavior
        self.version_calls = 0
        self.create_calls = 0
        self.close_calls: list[str] = []
        self.sessions: list[FakeSession] = []
        self.page_sessions: list[FakeSession] = []
        self.active = 0
        self.max_active = 0
        self._next_target = 0

    async def version(self, port: int) -> dict[str, Any]:
        self.version_calls += 1
        return {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/fake"}

    async def create_target(self, port: int, url: str) -> dict[str, Any]:
        self.create_calls += 1
        self._next_target += 1
        target_id = f"target-{self._next_target}"
        return {
            "id": target_id,
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/{target_id}",
        }

    async def close_target(self, port: int, target_id: str) -> None:
        self.close_calls.append(target_id)

    async def connect(self, ws_url: str) -> FakeSession:
        session = FakeSession(ws_url, self.behavior, self)
        if "/devtools/page/" in ws_url:
            self.page_sessions.append(session)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.sessions.append(session)
        return session

    def release(self) -> None:
        self.active = max(self.active - 1, 0)


_INITIAL_DATA = json.dumps(
    {
        "state": {
            "entities": {
                "answer-1": {"content": "<p>腾讯回应全文要点：本轮组织调整不涉及裁员。</p>"}
            }
        }
    },
    ensure_ascii=False,
)
_PAGE_HTML = (
    "<html><head><title>知乎标题</title></head><body>"
    "<div id='side'>侧栏导航噪音文本</div>"
    f"<script type='application/json' id='js-initialData'>{_INITIAL_DATA}</script>"
    "</body></html>"
)


class Fixture:
    """一套假件 + 一个 renderer；测试不碰真浏览器。"""

    def __init__(self, config: LinkBrowserConfig, *, clock_step: float = 0.0) -> None:
        self.behavior = FakeBehavior()
        self.behavior.snapshot_payload = json.dumps(
            {"title": "知乎标题", "html": _PAGE_HTML, "text": "整页innerText噪音：侧栏导航噪音文本"},
            ensure_ascii=False,
        )
        self.clock = FakeClock(step=clock_step)
        self.launcher = FakeLauncher()
        self.connector = FakeConnector(self.behavior)
        self.renderer = BrowserRenderer(
            config,
            clock=self.clock,
            launcher=self.launcher,
            connector=self.connector,
        )


def make_config(tmp_path: Path, **overrides: Any) -> LinkBrowserConfig:
    binary = tmp_path / "fake-browser.exe"
    binary.write_text("", encoding="utf-8")  # 占位：显式 binary 必须存在
    defaults: dict[str, Any] = {
        "binary": str(binary),
        "timeout_seconds": 2.0,
        "idle_close_seconds": 300,
        "max_targets": 2,
        "profile_dir": str(tmp_path / "prof"),
    }
    defaults.update(overrides)
    return LinkBrowserConfig(**defaults)  # type: ignore[arg-type]


# ---- (a) 启动参数 ---------------------------------------------------------------


async def test_launch_args_minimized_own_profile_port_no_headless(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fixture = Fixture(config)
    result = await fixture.renderer.render("http://example.test/page")

    assert result.ok is True
    binary, args = fixture.launcher.calls[0]
    assert binary == config.binary
    assert "--start-minimized" in args
    assert "--no-first-run" in args
    assert "--no-default-browser-check" in args
    assert "--lang=zh-CN" in args
    port_args = [a for a in args if a.startswith("--remote-debugging-port=")]
    assert len(port_args) == 1 and int(port_args[0].split("=", 1)[1]) > 0
    # user-data-dir 必须是 config.profile_dir 的绝对路径（绝不指向用户默认 profile）
    assert f"--user-data-dir={str(Path(config.profile_dir).resolve())}" in args
    # 无头被实测（知乎 40362）证明不可用：参数里绝不允许出现
    assert not any(a == "--headless" or a.startswith("--headless") for a in args)


# ---- (b) mode 非 minimized 显式报错 ----------------------------------------------


def test_headless_mode_raises_at_construction(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BrowserRenderer(make_config(tmp_path, mode="headless"))


# ---- (c) js-initialData 优先 -----------------------------------------------------


async def test_initial_data_preferred_over_page_inner_text(tmp_path: Path) -> None:
    fixture = Fixture(make_config(tmp_path))
    result = await fixture.renderer.render("https://www.zhihu.com/question/123")

    assert result.ok is True
    assert result.reason == "ok"
    assert result.title == "知乎标题"
    assert "腾讯回应全文要点" in result.text
    # 优先取 js-initialData：整页 innerText 的导航噪音不得混进正文
    assert "侧栏导航噪音文本" not in result.text


# ---- (d) navigate 装死 → browser_timeout + terminate 一次 + target 关闭 -----------


async def test_navigate_timeout_recycles_process_and_closes_target(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, timeout_seconds=0.3)
    # step 时钟：每次读时钟前进 0.05，让注入时钟的 deadline 在几步内越过
    fixture = Fixture(config, clock_step=0.05)
    fixture.behavior.navigate_silent = True

    result = await asyncio.wait_for(
        fixture.renderer.render("http://example.test/hang"), timeout=10.0
    )

    assert result.ok is False
    assert result.reason == "browser_timeout"
    assert fixture.launcher.processes[0].terminate_calls == 1
    # target 被关闭（connector.close_target 记录），target 的 ws 会话也已关
    assert fixture.connector.close_calls == ["target-1"]
    assert len(fixture.connector.page_sessions) == 1
    assert fixture.connector.page_sessions[0].closed


# ---- (e) 空闲关闭 + 再次拉起 -----------------------------------------------------


async def test_idle_close_then_relaunch(tmp_path: Path) -> None:
    config = make_config(tmp_path, idle_close_seconds=1)
    fixture = Fixture(config)

    first = await fixture.renderer.render("http://example.test/one")
    assert first.ok is True
    assert fixture.launcher.processes[0].terminate_calls == 0

    fixture.clock.now += 2.0  # 越过 idle_close_seconds
    await asyncio.sleep(0.6)  # 空闲看门狗真实轮询到
    assert fixture.launcher.processes[0].terminate_calls == 1

    second = await fixture.renderer.render("http://example.test/two")
    assert second.ok is True
    assert len(fixture.launcher.calls) == 2  # 重新拉起
    assert fixture.launcher.processes[1].terminate_calls == 0


# ---- (f) 并发两个 URL 开两个 target，不超过 max_targets ---------------------------


async def test_concurrent_renders_open_two_targets(tmp_path: Path) -> None:
    fixture = Fixture(make_config(tmp_path, max_targets=2))

    results = await asyncio.gather(
        fixture.renderer.render("http://example.test/a"),
        fixture.renderer.render("http://example.test/b"),
    )

    assert all(r.ok for r in results)
    assert fixture.connector.create_calls == 2  # 两个 target
    assert len(fixture.connector.page_sessions) == 2
    target_ids = {s.url.rsplit("/", 1)[-1] for s in fixture.connector.page_sessions}
    assert target_ids == {"target-1", "target-2"}
    assert fixture.connector.max_active <= 2


# ---- (g) 进程起不来 → browser_launch_failed，不抛异常 -----------------------------


async def test_launcher_failure_returns_launch_failed(tmp_path: Path) -> None:
    fixture = Fixture(make_config(tmp_path))
    fixture.launcher.fail = True  # Popen 抛 FileNotFoundError

    result = await fixture.renderer.render("http://example.test/x")

    assert result.ok is False
    assert result.reason == "browser_launch_failed"


# ---- 默认 connector：PUT /json/new，405 → GET 回退 --------------------------------


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", factory)


async def test_default_connector_uses_put_first(monkeypatch: pytest.MonkeyPatch) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200, json={"id": "t1", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/t1"}
        )

    _patch_httpx(monkeypatch, handler)
    info = await _DefaultCdpConnector().create_target(1, "about:blank")

    assert info["id"] == "t1"
    assert methods == ["PUT"]  # 新版只认 PUT：一次到位


async def test_default_connector_falls_back_to_get_on_405(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "PUT":
            return httpx.Response(405, text="")  # 旧版只认 GET
        return httpx.Response(
            200, json={"id": "t2", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/t2"}
        )

    _patch_httpx(monkeypatch, handler)
    info = await _DefaultCdpConnector().create_target(1, "about:blank")

    assert info["id"] == "t2"
    assert methods == ["PUT", "GET"]
