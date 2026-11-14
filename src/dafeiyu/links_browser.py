"""本机浏览器渲染层（chat-driven-behaviour-fixes T2）：Edge/Chrome 有头最小化 CDP。

为什么必须有头：实测知乎对无头一律回 40362 风控（先有头预热一次再换无头
也一样被挡，见 .omo/evidence/chat-audit-2026-11-14/_probe_warm.py.txt），
只有真窗口指纹能读到知乎正文——所以本模块绝不加 ``--headless``，用
``--start-minimized`` 代替。

为什么独立 user-data-dir：绝不读用户自己的浏览器 profile（那既侵入隐私、
又会带出登录态），一律用 ``link.browser.profile_dir`` 的独立目录，不要求
用户登录知乎。

为什么 ``--start-minimized``：微信桥接对前台焦点敏感（
``gateway/wechat_bridge/sender.py`` 的 ``prepare`` 断言前台窗口必须是
微信会话窗口，抢了焦点会导致发送失败并触发 ``_recover_foreground``），
最小化启动的浏览器窗口不抢前台。

自动化只走 CDP（venv 里已有的 ``websockets``），不引入 Playwright/Selenium，
不下载浏览器：本机装了 Edge 就用 Edge，否则探测 Chrome，都没有就
``browser_launch_failed``。
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import websockets

from dafeiyu.config import LinkBrowserConfig
from dafeiyu.links import extract_title, html_to_text
from dafeiyu.netssl import default_ssl_context

__all__ = ["BrowserResult", "BrowserRenderer"]

#: 等待 CDP 端点 / 命令回包时，注入时钟（测试）与真实时钟共用的轮询切片（秒）。
#: 注入时钟不会随真实时间前进，切片必须短到测试能在几步内看到时钟被推进。
_POLL_SLICE = 0.25
#: 版本端点就绪重试的保底次数上限：即便注入时钟被冻结也不会无限循环。
_MAX_VERSION_RETRIES = 500

#: 二进制探测顺序：config.binary → Edge 两个常见路径 → Chrome 三个常见路径。
#: 都找不到时返回 browser_launch_failed，绝不下载浏览器。
_BROWSER_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
)

#: 知乎等站点把正文塞在 <script id="js-initialData"> 里，优先从中解析，
#: 不依赖整页 innerText（导航壳文本会混进正文）。
_INITIAL_DATA_RE = re.compile(
    r"<script[^>]*\bid=[\"']js-initialData[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
#: initialData 里值得收集的键：title 是标题，content/excerpt 是正文（HTML 串）。
_TITLE_KEY = "title"
_CONTENT_KEYS = ("content", "excerpt")


@dataclass(frozen=True)
class BrowserResult:
    """一次浏览器渲染的结果；失败时 ok=False 且 reason 说明原因。"""

    ok: bool
    url: str
    title: str = ""
    text: str = ""
    reason: str = ""


class CdpSession(Protocol):
    """一条 target 级 CDP 连接：纯协程接口，超时由渲染层用注入时钟兜。"""

    async def command(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def wait_event(self, name: str) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class CdpConnector(Protocol):
    """CDP 端点面（HTTP 发现端点 + WebSocket 连接工厂）；测试注入点。"""

    async def version(self, port: int) -> dict[str, Any]: ...

    async def create_target(self, port: int, url: str) -> dict[str, Any]: ...

    async def close_target(self, port: int, target_id: str) -> None: ...

    async def connect(self, ws_url: str) -> CdpSession: ...


#: 进程工厂：返回带 terminate()（可选 poll()/wait()）的进程对象；测试注入点。
Launcher = Callable[[str, Sequence[str]], Any]
#: 时钟：生产用 time.time，测试注入可手动推进的假时钟。
Clock = Callable[[], float]


class _NoTarget(Exception):
    """create_target 没给回可用的 target id / WebSocket 地址。"""


def _free_port() -> int:
    """向内核要一个空闲 TCP 端口给 --remote-debugging-port（存在竞态但可接受）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _squash_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _walk_initial_data(
    node: Any, titles: list[str], chunks: list[str]
) -> None:
    """深度优先收集 initialData 里的标题与正文串（content/excerpt 常是 HTML）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                if key == _TITLE_KEY and value.strip() and not titles:
                    titles.append(value.strip())
                elif key in _CONTENT_KEYS and value.strip():
                    chunks.append(value)
            else:
                _walk_initial_data(value, titles, chunks)
    elif isinstance(node, list):
        for item in node:
            _walk_initial_data(item, titles, chunks)


def _initial_data_text(html: str) -> tuple[str, str]:
    """从整页 HTML 里解析 js-initialData，返回 (标题, 正文)；解析不动就都为空。"""
    match = _INITIAL_DATA_RE.search(html)
    if match is None:
        return "", ""
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return "", ""
    titles: list[str] = []
    chunks: list[str] = []
    _walk_initial_data(data, titles, chunks)
    title = titles[0] if titles else ""
    text = html_to_text("\n".join(chunks)) if chunks else ""
    return title, text


# ---- 默认 CDP 连接实现 -------------------------------------------------------


class _WsCdpSession:
    """websockets 实现的 target 级会话：自增 id 匹配响应，事件与响应分开。

    命令/事件接口是纯协程：渲染层用注入时钟做超时（外层 cancel 本协程），
    所以这里不做任何内部超时。
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # 事件可能在 wait_event 注册前就到（navigate 返回与 loadEventFired
        # 之间没有先后保证）：留一份最近事件，wait_event 先查缓存再挂等待。
        self._last_event: dict[str, dict[str, Any]] = {}
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                self._dispatch(data)
        except Exception:  # noqa: BLE001 - 连接层异常统一折成失败回包
            pass
        finally:
            closed = ConnectionError("cdp websocket closed")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(closed)
            for fut in self._waiters.values():
                if not fut.done():
                    fut.set_exception(closed)
            self._pending.clear()
            self._waiters.clear()

    def _dispatch(self, data: dict[str, Any]) -> None:
        if "id" in data:
            fut = self._pending.pop(int(data["id"]), None)
            if fut is not None and not fut.done():
                fut.set_result(data)
        elif "method" in data:
            name = str(data["method"])
            self._last_event[name] = data
            fut = self._waiters.pop(name, None)
            if fut is not None and not fut.done():
                fut.set_result(data)

    async def command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        msg_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send(
                json.dumps(
                    {"id": msg_id, "method": method, "params": params},
                    ensure_ascii=False,
                )
            )
        except Exception:
            self._pending.pop(msg_id, None)
            raise
        try:
            reply = await fut
        finally:
            self._pending.pop(msg_id, None)
        if "error" in reply:
            raise RuntimeError(f"cdp error: {reply['error']}")
        return reply.get("result") or {}

    async def wait_event(self, name: str) -> dict[str, Any]:
        cached = self._last_event.pop(name, None)
        if cached is not None:
            return cached
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[name] = fut
        try:
            return await fut
        finally:
            self._waiters.pop(name, None)

    async def close(self) -> None:
        if not self._reader.done():
            self._reader.cancel()
        with suppress(Exception):
            await self._ws.close()


class _DefaultCdpConnector:
    """真实端点实现：HTTP 发现端点用 httpx，target 连接用 websockets。"""

    async def version(self, port: int) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=5.0, verify=default_ssl_context()
        ) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/json/version")
            resp.raise_for_status()
            return resp.json()

    async def create_target(self, port: int, url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=5.0, verify=default_ssl_context()
        ) as client:
            try:
                # 新版 Chrome/Edge 只认 PUT；旧版只认 GET（PUT 会回 405）。
                resp = await client.put(
                    f"http://127.0.0.1:{port}/json/new", params={"url": url}
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 405:
                    raise
                resp = await client.get(
                    f"http://127.0.0.1:{port}/json/new", params={"url": url}
                )
                resp.raise_for_status()
            return resp.json()

    async def close_target(self, port: int, target_id: str) -> None:
        async with httpx.AsyncClient(
            timeout=5.0, verify=default_ssl_context()
        ) as client:
            # best-effort：关不掉也不抛，进程回收会兜底。
            with suppress(httpx.HTTPError):
                await client.get(f"http://127.0.0.1:{port}/json/close/{target_id}")

    async def connect(self, ws_url: str) -> CdpSession:
        # max_size=None：知乎整页 HTML 远超 websockets 默认 1MiB 上限，
        # 不放开会直接断连。
        ws = await websockets.connect(ws_url, max_size=None)
        return _WsCdpSession(ws)


def _default_launcher(binary: str, args: Sequence[str]) -> Any:
    return subprocess.Popen([binary, *args])


# ---- 渲染层 ---------------------------------------------------------------


class BrowserRenderer:
    """有头最小化浏览器渲染器：进程内单例 + 每次渲染一个新 target。

    ``clock`` / ``connector`` / ``launcher`` 都是测试注入点，生产恒传 None。
    ``config.mode != "minimized"`` 直接 ValueError：配置层之外的第二道防线，
    绝不静默改用无头（实测无头必被知乎风控挡掉）。
    """

    def __init__(
        self,
        config: LinkBrowserConfig,
        *,
        clock: Clock | None = None,
        connector: CdpConnector | None = None,
        launcher: Launcher | None = None,
    ) -> None:
        if config.mode != "minimized":
            raise ValueError(
                f"link.browser.mode 只支持 minimized，收到 {config.mode!r}"
                "（无头必被知乎风控挡掉，不支持）"
            )
        self._config = config
        self._clock: Clock = clock or time.time
        self._connector: CdpConnector = connector or _DefaultCdpConnector()
        self._launcher: Launcher = launcher or _default_launcher
        # 并发渲染受 max_targets 限制（每个 render 占一个 target）。
        self._semaphore = asyncio.Semaphore(config.max_targets)
        # 同一实例只允许一个浏览器进程；launch 串行化防双开。
        self._launch_lock = asyncio.Lock()
        self._process: Any = None
        self._port = 0
        self._active_sessions: dict[str, CdpSession] = {}
        self._idle_task: asyncio.Task[None] | None = None
        self._last_used = 0.0

    # ---- 公开 API ---------------------------------------------------------

    async def __aenter__(self) -> "BrowserRenderer":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def render(self, url: str) -> BrowserResult:
        """渲染一个 URL；失败不抛异常，返回 ok=False 的 BrowserResult。"""
        async with self._semaphore:
            session: CdpSession | None = None
            target_id = ""
            kill = False
            try:
                if not await self._ensure_running():
                    return self._failure(url, "browser_launch_failed")
                target_id, ws_url = await self._new_target()
                session = await self._connector.connect(ws_url)
                self._active_sessions[target_id] = session
                deadline = self._clock() + self._config.timeout_seconds
                await self._call(session.command("Page.enable", {}), deadline)
                await self._call(session.command("Runtime.enable", {}), deadline)
                await self._call(
                    session.command("Page.navigate", {"url": url}), deadline
                )
                await self._call(
                    session.wait_event("Page.loadEventFired"), deadline
                )
                snapshot = await self._snapshot(session, deadline)
                title, text = self._extract(snapshot)
                if not text:
                    return self._failure(url, "empty")
                return BrowserResult(
                    ok=True, url=url, title=title, text=text, reason="ok"
                )
            except TimeoutError:
                kill = True
                return self._failure(url, "browser_timeout")
            except _NoTarget:
                return self._failure(url, "browser_no_target")
            except Exception:  # noqa: BLE001 - 失败路径一律折成结果不抛
                return self._failure(url, "browser_error")
            finally:
                await self._teardown(session, target_id, kill=kill)

    async def aclose(self) -> None:
        """停掉空闲看门狗并回收浏览器进程；进程退出前的兜底清理。"""
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._shutdown()

    # ---- 启动与进程 -------------------------------------------------------

    def _resolve_binary(self) -> str:
        if self._config.binary:
            # 显式指定的二进制必须存在：配错就明确报 launch_failed，
            # 不静默回退到别的浏览器（那是用户的配置决策）。
            return self._config.binary if Path(self._config.binary).is_file() else ""
        for candidate in _BROWSER_CANDIDATES:
            if candidate and Path(candidate).is_file():
                return candidate
        return ""

    def _profile_dir(self) -> str:
        # 独立 user-data-dir 的绝对路径：绝不指向用户自己的浏览器 profile。
        return str(Path(self._config.profile_dir).resolve())

    def _launch_args(self, port: int) -> list[str]:
        # 三条为什么见模块 docstring：有头（无头 40362）、独立 profile、
        # 最小化不抢微信桥的前台焦点。
        return [
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self._profile_dir()}",
            "--start-minimized",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=zh-CN",
        ]

    def _process_alive(self) -> bool:
        proc = self._process
        if proc is None:
            return False
        poll = getattr(proc, "poll", None)
        return poll is None or poll() is None

    async def _ensure_running(self) -> bool:
        if self._process_alive():
            return True
        async with self._launch_lock:
            if self._process_alive():
                return True
            binary = self._resolve_binary()
            if not binary:
                return False
            port = _free_port()
            try:
                self._process = self._launcher(binary, self._launch_args(port))
            except Exception:
                # 二进制缺失 / 起不来：归入 launch_failed，不外抛。
                self._process = None
                return False
            self._port = port
            # 版本端点就绪前 target/命令都不可用：在锁内等到就绪再放行，
            # 防止并发 render 走快路径跳过就绪等待、直接打到冷端点上。
            await self._wait_version_ready()
        return True

    async def _wait_version_ready(self) -> None:
        deadline = self._clock() + self._config.timeout_seconds
        delay = 0.1
        for _ in range(_MAX_VERSION_RETRIES):
            info: Any = None
            with suppress(Exception):
                info = await self._connector.version(self._port)
            if isinstance(info, dict) and info.get("webSocketDebuggerUrl"):
                return
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            await asyncio.sleep(min(delay, remaining, _POLL_SLICE))
            delay = min(delay * 2, 0.5)
        raise TimeoutError(f"cdp endpoint not ready on port {self._port}")

    def _terminate(self) -> None:
        proc, self._process = self._process, None
        if proc is None:
            return
        with suppress(Exception):
            proc.terminate()
        wait = getattr(proc, "wait", None)
        if callable(wait):
            with suppress(Exception):
                wait(3)

    async def _shutdown(self) -> None:
        for target_id, session in list(self._active_sessions.items()):
            with suppress(Exception):
                await session.close()
            with suppress(Exception):
                await self._connector.close_target(self._port, target_id)
        self._active_sessions.clear()
        self._terminate()

    # ---- 空闲回收 ---------------------------------------------------------

    def _touch(self) -> None:
        self._last_used = self._clock()
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_watchdog())

    async def _idle_watchdog(self) -> None:
        deadline = self._last_used + self._config.idle_close_seconds
        try:
            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                if self._process is None:
                    return
                await asyncio.sleep(min(remaining, _POLL_SLICE))
            if self._process is not None and not self._active_sessions:
                await self._shutdown()
        except asyncio.CancelledError:
            raise

    # ---- 渲染步骤 ---------------------------------------------------------

    async def _new_target(self) -> tuple[str, str]:
        info = await self._connector.create_target(self._port, "about:blank")
        target_id = str(info.get("id") or "")
        ws_url = str(info.get("webSocketDebuggerUrl") or "")
        if not target_id or not ws_url:
            raise _NoTarget(f"create_target returned {info!r}")
        return target_id, ws_url

    async def _call(self, coro: Any, deadline: float) -> dict[str, Any]:
        """await 一个 CDP 协程，超时判定走注入时钟（分片轮询 deadline）。"""
        task = asyncio.ensure_future(coro)
        try:
            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError("cdp call deadline exceeded")
                done, _ = await asyncio.wait(
                    {task}, timeout=min(remaining, _POLL_SLICE)
                )
                if done:
                    if task.cancelled():
                        raise TimeoutError("cdp call cancelled")
                    return task.result()
        except BaseException:
            if not task.done():
                task.cancel()
            raise

    async def _snapshot(self, session: CdpSession, deadline: float) -> dict[str, str]:
        # 一次 evaluate 拿齐标题/整页 HTML/innerText，少三个来回。
        expression = (
            "JSON.stringify({"
            "title: document.title,"
            "html: document.documentElement.outerHTML,"
            "text: document.body.innerText"
            "})"
        )
        reply = await self._call(
            session.command(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            ),
            deadline,
        )
        value = (reply.get("result") or {}).get("value")
        if not isinstance(value, str):
            raise ValueError(f"unexpected evaluate result: {reply!r}")
        data = json.loads(value)
        if not isinstance(data, dict):
            raise ValueError("evaluate payload is not an object")
        return {
            "title": str(data.get("title") or ""),
            "html": str(data.get("html") or ""),
            "text": str(data.get("text") or ""),
        }

    def _extract(self, snapshot: dict[str, str]) -> tuple[str, str]:
        title = snapshot["title"].strip()
        text = snapshot["text"].strip()
        html = snapshot["html"]
        if "js-initialData" in html:
            # 知乎等站点把正文塞在 js-initialData：优先用它，整页 innerText
            # 只做兜底（导航壳文本会混进来）。
            jd_title, jd_text = _initial_data_text(html)
            if jd_text:
                text = jd_text
            if jd_title:
                title = jd_title
        if not title:
            title = extract_title(html)
        return _squash_whitespace(title), text

    def _failure(self, url: str, reason: str) -> BrowserResult:
        return BrowserResult(ok=False, url=url, reason=reason)

    async def _teardown(
        self, session: CdpSession | None, target_id: str, *, kill: bool
    ) -> None:
        if session is not None:
            with suppress(Exception):
                await session.close()
        if target_id:
            self._active_sessions.pop(target_id, None)
            with suppress(Exception):
                await self._connector.close_target(self._port, target_id)
        if kill:
            self._terminate()
        self._touch()
