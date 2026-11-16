"""面板视觉验收：Edge + CDP 截图与气泡几何度量（只读 QA 脚本）。

用法::

    .venv\\Scripts\\python scripts/qa_dashboard_shots.py --out-dir .omo/evidence/visual/run1

做四件事：

1. **先探活再拉浏览器**：``GET /api/auth/session`` 通不了就直接退出，不启动
   Edge（CI 上没有运行中的面板，也不保证有 Edge，不能把浏览器成本带进 CI）。
2. **登录**：401 时用 ``POST /api/auth/login`` 换 ``dafeiyu_dashboard_session``，
   再把 cookie 写进 headless Edge（``Storage.setCookies``），后续请求都带着它。
3. **两档视口截图**：桌面 1280×900、手机 390×844（``Emulation.setDeviceMetricsOverride``）；
   每档产出整页截图 + 打开会话时间线抽屉后的视口截图，共 4 张 PNG。
4. **几何度量**：量出每个 ``.timeline-msg`` 的 ``getBoundingClientRect()``、
   ``.timeline-body`` 的内容左右边缘、``data-channel`` 与日期分隔条宽度，
   写成 ``metrics.json``；主题切换持久化、6 个分区导航、三个新接口的状态码
   写成 ``interactions.json``。

本机没有任何会话时不报错：只产出整页截图，``metrics.json`` 里写
``"bubbles": []`` 与 ``"reason": "no-session"``。

口令来源（按优先级，脚本不落盘、不写进提交）。**不接受命令行参数**：
命令行口令会留在 shell history 与进程命令行里，只走进程环境或 0600 文件。

1. 环境变量 ``DAFEIYU_DASHBOARD_PASSWORD``
2. ``data/dashboard.password``（只读；本机通常不存在）

退出码::

    0 成功            1 参数错误        2 面板不可达
    3 需要登录但没口令  4 登录失败        5 找不到 Edge/Chrome
    6 浏览器或 CDP 起不来              7 截图 / 度量失败
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 面板会话 cookie 名：与 ``src/dafeiyu/supervisor/auth.py`` 的 COOKIE_NAME 一致。
#: 这里硬编码而不 import 产品代码：本脚本承诺只读、不依赖 aiohttp 环境。
COOKIE_NAME = "dafeiyu_dashboard_session"

#: 二进制探测顺序与 ``src/dafeiyu/links_browser.py`` 一致（本机只有 x86 路径）。
BROWSER_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
)

#: 两档视口：(名字, 宽, 高, 像素比, 是否移动端仿真)。
VIEWPORTS: tuple[tuple[str, int, int, float, bool], ...] = (
    ("desktop", 1280, 900, 1.0, False),
    ("mobile", 390, 844, 2.0, True),
)

#: 打开时间线抽屉后等消息出现的最长时间（秒）：没有消息就按 no-session 收尾。
TIMELINE_WAIT = 5.0


class QaError(Exception):
    """带退出码的可读失败：main 统一转成 stderr 消息 + 非零退出。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SessionProbe:
    """面板探活结果：enabled 表示登录是否开启，authenticated 表示 cookie 是否有效。"""

    status: int
    enabled: bool
    authenticated: bool


# ---- HTTP（stdlib，避免给 QA 脚本加依赖）------------------------------------


def _request(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    cookie: str = "",
    timeout: float = 5.0,
) -> tuple[int, Any, bytes]:
    """发一个 HTTP 请求；4xx/5xx 也当有效响应返回（状态码本身是度量对象）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: dict[str, str] = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = f"{COOKIE_NAME}={cookie}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        # 401 / 400 / 429 都是「面板活着」的证据，必须把状态码带回给调用方。
        return exc.code, exc.headers, exc.read()


def _cookie_value(headers: Any) -> str:
    """从 Set-Cookie 头里取面板会话 cookie 的值。"""
    getter = getattr(headers, "get_all", None)
    raw_values: list[str] = []
    if callable(getter):
        values = getter("Set-Cookie")
        if isinstance(values, (list, tuple)):
            raw_values = [str(item) for item in values]
    for raw in raw_values:
        for part in str(raw).split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value
    return ""


def _probe(base_url: str, timeout: float) -> SessionProbe:
    """探活面板；连不上就是「面板不可达」，直接以退出码 2 结束。"""
    try:
        status, _, payload = _request(
            "GET", f"{base_url}/api/auth/session", timeout=timeout
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise QaError(2, f"面板不可达：{base_url}（{reason}）") from exc
    if status == 401:
        # 登录已开启且当前请求没带 cookie：不是不可达，是需要登录。
        return SessionProbe(status=status, enabled=True, authenticated=False)
    if status != 200:
        raise QaError(2, f"面板探活返回 {status}：{base_url}/api/auth/session")
    try:
        data = json.loads(payload.decode("utf-8") or "{}")
    except ValueError:
        data = {}
    enabled = bool(data.get("enabled")) if isinstance(data, dict) else False
    authenticated = bool(data.get("authenticated")) if isinstance(data, dict) else True
    return SessionProbe(status=status, enabled=enabled, authenticated=authenticated)


def _login(base_url: str, user: str, password: str, timeout: float) -> str:
    """登录换 cookie；登录未启用（400）时返回空串。"""
    try:
        status, headers, payload = _request(
            "POST",
            f"{base_url}/api/auth/login",
            body={"username": user, "password": password},
            timeout=timeout,
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise QaError(2, f"登录请求失败：{base_url}/api/auth/login（{exc}）") from exc
    if status == 400:
        # loopback + auth=off：登录没开，本来就不需要 cookie。
        return ""
    if status == 429:
        raise QaError(4, "登录失败：尝试次数过多（面板限流中，等 5 分钟再试）")
    if status not in (200, 204):
        detail = payload.decode("utf-8", "replace")[:200]
        raise QaError(4, f"登录失败：HTTP {status} {detail}".strip())
    token = _cookie_value(headers)
    if not token:
        raise QaError(4, "登录失败：响应里没有面板会话 cookie")
    return token


def _resolve_password() -> str:
    """两级口令来源：环境变量 → data/dashboard.password（只读）。"""
    env = os.environ.get("DAFEIYU_DASHBOARD_PASSWORD", "").strip()
    if env:
        return env
    path = REPO_ROOT / "data" / "dashboard.password"
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


# ---- 浏览器进程 -------------------------------------------------------------


def _find_browser(explicit: str) -> str:
    """找浏览器可执行文件；找不到必须以非零退出（没截图 ≠ 截图没问题）。"""
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise QaError(5, f"指定的浏览器不存在：{explicit}")
    for candidate in BROWSER_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    raise QaError(
        5,
        "找不到 Edge / Chrome 可执行文件（试过："
        + "；".join(BROWSER_CANDIDATES)
        + "）",
    )


def _free_port() -> int:
    """向内核要一个空闲端口给 --remote-debugging-port（有竞态但可接受）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_version(port: int, timeout: float) -> dict[str, Any]:
    """等 CDP 版本端点就绪并返回它；超时按「浏览器起不来」处理。"""
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            status, _, payload = _request("GET", endpoint, timeout=2.0)
            if status == 200:
                data = json.loads(payload.decode("utf-8") or "{}")
                if isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
                    return data
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(0.2)
    raise QaError(6, f"CDP 端点未就绪：{endpoint}（{last or '超时'}）")


def _create_target(port: int, url: str) -> dict[str, Any]:
    """新建 target；新版浏览器只认 PUT，旧版只认 GET（PUT 回 405 时回退）。"""
    target_url = f"http://127.0.0.1:{port}/json/new?url={url}"
    try:
        status, _, payload = _request("PUT", target_url, timeout=5.0)
        if status == 405:
            status, _, payload = _request("GET", target_url, timeout=5.0)
    except (urllib.error.URLError, OSError) as exc:
        raise QaError(6, f"创建浏览器 target 失败：{exc}") from exc
    if status != 200:
        raise QaError(6, f"创建浏览器 target 失败：HTTP {status}")
    try:
        data = json.loads(payload.decode("utf-8") or "{}")
    except ValueError as exc:
        raise QaError(6, f"创建浏览器 target 返回非法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not data.get("webSocketDebuggerUrl"):
        raise QaError(6, f"创建浏览器 target 没有回传 WebSocket 地址：{data!r}")
    return data


# ---- 最小 CDP 客户端（只用 websockets，与 links_browser 同一套经验）---------


class _Cdp:
    """一条 target 级 CDP 连接：自增 id 匹配响应，事件不入队（本脚本只轮询）。"""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(data, dict) or "id" not in data:
                    continue
                future = self._pending.pop(int(data["id"]), None)
                if future is not None and not future.done():
                    future.set_result(data)
        except Exception:  # noqa: BLE001, S110 - 连接层异常统一折成失败回包
            pass
        finally:
            closed = ConnectionError("cdp websocket closed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(closed)
            self._pending.clear()

    async def send(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[message_id] = future
        try:
            await self._ws.send(
                json.dumps(
                    {"id": message_id, "method": method, "params": params or {}},
                    ensure_ascii=False,
                )
            )
            try:
                reply = await asyncio.wait_for(future, timeout)
            except TimeoutError as exc:
                raise QaError(7, f"CDP {method} 超时（{timeout:.1f}s）") from exc
        finally:
            self._pending.pop(message_id, None)
        if "error" in reply:
            raise QaError(7, f"CDP {method} 失败：{reply['error']}")
        return reply.get("result") or {}

    async def evaluate(self, expression: str, timeout: float = 10.0) -> Any:
        """跑一段 JS 并取回 JSON 值；页面里抛异常按度量失败处理。"""
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise QaError(7, f"页面脚本异常：{json.dumps(detail, ensure_ascii=False)[:300]}")
        return (result.get("result") or {}).get("value")

    async def close(self) -> None:
        if not self._reader.done():
            self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001, S110 - 关连接失败不影响结论
            pass


# ---- 页面操作 ---------------------------------------------------------------

#: 整页 / 视口截图都走 Page.captureScreenshot；整页必须自己算 css 内容尺寸。
_READY_JS = "document.readyState === 'complete'"
_SESSIONS_JS = (
    "(() => {"
    "const hint = document.getElementById('sessions-hint');"
    "return !!hint && hint.textContent.trim() !== '';"
    "})()"
)


async def _wait_true(
    cdp: _Cdp, expression: str, timeout: float, what: str
) -> bool:
    """轮询表达式直到为真；超时不抛（调用方决定是否当错误）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if await cdp.evaluate(expression, timeout=max(2.0, timeout)) is True:
                return True
        except QaError:
            # 页面还在导航时 evaluate 可能失败：继续轮询到 deadline。
            pass
        await asyncio.sleep(0.2)
    return False


async def _wait_or_fail(
    cdp: _Cdp, expression: str, timeout: float, what: str
) -> None:
    if not await _wait_true(cdp, expression, timeout, what):
        raise QaError(7, f"等待超时：{what}（{timeout:.1f}s）")


async def _settle(seconds: float = 0.3) -> None:
    """给布局 / 平滑滚动 / 首屏 fetch 一点落地时间（CDP 没有 networkidle 事件）。"""
    await asyncio.sleep(seconds)


async def _shot_full(cdp: _Cdp, path: Path) -> tuple[float, float]:
    """截整页：用 cssContentSize 当 clip，否则无头只截视口那一段。"""
    metrics = await cdp.send("Page.getLayoutMetrics", {}, timeout=15.0)
    size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
    width = float(size.get("width") or 0) or 1280.0
    height = float(size.get("height") or 0) or 900.0
    shot = await cdp.send(
        "Page.captureScreenshot",
        {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
        },
        timeout=30.0,
    )
    path.write_bytes(base64.b64decode(shot["data"]))
    return width, height


async def _shot_viewport(cdp: _Cdp, path: Path) -> None:
    shot = await cdp.send("Page.captureScreenshot", {"format": "png"}, timeout=30.0)
    path.write_bytes(base64.b64decode(shot["data"]))


#: 顶部页面度量：横向是否溢出（面板排版不变量的一半）。
_PAGE_METRICS_JS = """(() => {
  const doc = document.documentElement;
  const view = window.visualViewport;
  return {
    scrollWidth: doc.scrollWidth,
    clientWidth: doc.clientWidth,
    scrollHeight: doc.scrollHeight,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    overflowX: doc.scrollWidth > doc.clientWidth,
    devicePixelRatio: window.devicePixelRatio,
    screen: {width: screen.width, height: screen.height},
    visualViewport: view ? {width: view.width, height: view.height, scale: view.scale} : null,
    narrowLayout: window.matchMedia('(max-width: 720px)').matches,
    // 横向溢出的元凶（只列前 10 个）：移动端排版不变量要能定位到具体卡片。
    overflow: (() => {
      const limit = doc.clientWidth + 1;
      const items = [];
      document.querySelectorAll('body *').forEach((el) => {
        const box = el.getBoundingClientRect();
        if (box.width > 0 && box.right > limit) {
          items.push({
            tag: el.tagName.toLowerCase(),
            id: el.id || '',
            cls: String(el.className || '').slice(0, 40),
            right: Math.round(box.right),
            width: Math.round(box.width),
          });
        }
      });
      return items.slice(0, 10);
    })(),
  };
})()"""

#: 时间线几何度量：气泡宽度 / 左右边缘 / 容器内容边缘 / 通道 / 日期条宽度。
_TIMELINE_METRICS_JS = """(() => {
  const body = document.getElementById('timeline-messages');
  if (!body) return {drawer: false, bubbles: [], reason: 'no-drawer'};
  const style = getComputedStyle(body);
  const rect = body.getBoundingClientRect();
  const padLeft = parseFloat(style.paddingLeft) || 0;
  const padRight = parseFloat(style.paddingRight) || 0;
  // 纵向滚动条占掉的是 clientWidth 之外的那 15px：右基准必须把它减掉，
  // 否则 bot 气泡右边缘会「差 15px」，看起来像没对齐。
  const scrollbar = body.offsetWidth - body.clientWidth;
  const content = {
    left: rect.left + padLeft,
    right: rect.right - padRight - scrollbar,
    width: body.clientWidth - padLeft - padRight,
    clientWidth: body.clientWidth,
    offsetWidth: body.offsetWidth,
    paddingLeft: padLeft,
    paddingRight: padRight,
    scrollbar: scrollbar,
  };
  const bubbles = Array.from(body.querySelectorAll('.timeline-msg')).map((el) => {
    const box = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const text = el.querySelector('.timeline-msg-text');
    return {
      bot: el.classList.contains('bot'),
      channel: el.dataset.channel || '',
      width: box.width,
      left: box.left,
      right: box.right,
      top: box.top,
      textLength: (text ? text.textContent : '').length,
      // 按码点截断：slice(0, 40) 可能把 emoji 的代理对切成孤立半字符，
      // metrics.json 写 UTF-8 时会直接抛 UnicodeEncodeError。
      preview: Array.from(text ? text.textContent : '').slice(0, 40).join(''),
      justifySelf: cs.justifySelf,
      maxWidth: cs.maxWidth,
      borderTopColor: cs.borderTopColor,
      borderTopWidth: cs.borderTopWidth,
    };
  });
  const dates = Array.from(body.querySelectorAll('.timeline-date')).map((el) => {
    const box = el.getBoundingClientRect();
    return {width: box.width, left: box.left, right: box.right,
            gridColumn: getComputedStyle(el).gridColumn};
  });
  // 现有数据里不一定有 1 字 / 10 字消息：用同一个 .timeline-msg 组件做离屏探针
  // （同步创建 → 量 → 移除，不进截图、不改页面），把「按内容定宽」变成可断言数值。
  const probes = [1, 10].map((chars) => {
    const el = document.createElement('div');
    el.className = 'timeline-msg';
    el.style.visibility = 'hidden';
    el.style.position = 'absolute';
    el.style.left = '-9999px';
    el.style.top = '0';
    el.innerHTML = '<div class="timeline-msg-head"><b>探针</b><span>—</span></div>' +
                   '<div class="timeline-msg-text"></div>';
    el.querySelector('.timeline-msg-text').textContent = '字'.repeat(chars);
    body.appendChild(el);
    const box = el.getBoundingClientRect();
    const result = {chars: chars, width: box.width,
                    maxWidth: getComputedStyle(el).maxWidth};
    el.remove();
    return result;
  });
  const drawer = document.getElementById('timeline-drawer');
  const drawerBox = drawer ? drawer.getBoundingClientRect() : null;
  return {
    drawer: !!drawer && !drawer.hidden,
    content: content,
    bubbles: bubbles,
    dates: dates,
    probes: probes,
    drawerBox: drawerBox ? {width: drawerBox.width, left: drawerBox.left} : null,
    viewport: {width: window.innerWidth, height: window.innerHeight},
    theme: document.documentElement.dataset.theme || '',
  };
})()"""

#: 6 个分区导航：点一下读 scrollY，证明导航可用且能滚到位。
_PANES_JS = """(async () => {
  const items = [];
  const buttons = Array.from(document.querySelectorAll('[data-pane]'));
  for (const button of buttons) {
    button.click();
    await new Promise((resolve) => setTimeout(resolve, 650));
    const pane = document.getElementById(button.dataset.pane);
    items.push({
      pane: button.dataset.pane,
      label: (button.textContent || '').trim(),
      scrollY: Math.round(window.scrollY),
      paneTop: pane ? Math.round(pane.getBoundingClientRect().top) : null,
    });
  }
  return {count: buttons.length, items: items};
})()"""

#: 新接口状态码：旧进程没有这些代理路由时会记 404，如实留证不当作失败。
_API_JS = """(async () => {
  const paths = ['/api/holds', '/api/thread-states', '/api/sessions?limit=1&offset=0'];
  const out = {};
  for (const path of paths) {
    try {
      const response = await fetch(path, {credentials: 'same-origin'});
      out[path] = response.status;
    } catch (err) {
      out[path] = 'error: ' + (err && err.message ? err.message : String(err));
    }
  }
  return out;
})()"""

#: 主题：点按钮 → reload → 读回，证明 localStorage 只有一个键且能记住。
_THEME_CLICK_JS = """(() => {
  const button = document.getElementById('btn-theme');
  if (!button) return false;
  button.click();
  return true;
})()"""

_THEME_READ_JS = """(() => {
  let stored = null;
  try { stored = localStorage.getItem('dafeiyu.theme'); } catch (err) { stored = 'error'; }
  return {theme: document.documentElement.dataset.theme || '', stored: stored};
})()"""

_OPEN_TIMELINE_JS = """(() => {
  const row = document.querySelector('#sessions .session-row');
  if (!row) return 'no-session';
  const button = Array.from(row.querySelectorAll('button'))
    .find((item) => (item.textContent || '').includes('查看时间线'));
  if (!button) return 'no-button';
  button.click();
  return 'clicked';
})()"""

_MSG_COUNT_JS = "document.querySelectorAll('#timeline-messages .timeline-msg').length"


async def _reload_and_wait(cdp: _Cdp, base_url: str, timeout: float) -> None:
    """重载首页并等到文档与首屏会话请求都落地。"""
    await cdp.send("Page.navigate", {"url": f"{base_url}/"}, timeout=timeout)
    await _wait_or_fail(cdp, _READY_JS, timeout, "页面加载完成")
    await _wait_true(cdp, _SESSIONS_JS, min(10.0, timeout), "会话列表首屏")
    await _settle(0.4)


async def _theme_probe(cdp: _Cdp, base_url: str, timeout: float) -> dict[str, Any]:
    """验证主题切换 + reload 后仍记住，并把主题恢复成深色基线。"""
    before = await cdp.evaluate(_THEME_READ_JS)
    clicked = await cdp.evaluate(_THEME_CLICK_JS)
    await _settle(0.2)
    after_click = await cdp.evaluate(_THEME_READ_JS)
    await _reload_and_wait(cdp, base_url, timeout)
    after_reload = await cdp.evaluate(_THEME_READ_JS)
    restored = after_reload.get("theme") == "dark"
    if not restored:
        # 不把临时主题留在浏览器 profile 里：回到深色基线再 reload 一次。
        await cdp.evaluate(_THEME_CLICK_JS)
        await _settle(0.2)
        reverted = await cdp.evaluate(_THEME_READ_JS)
        restored = reverted.get("theme") == "dark"
        if restored:
            await _reload_and_wait(cdp, base_url, timeout)
    return {
        "clicked": clicked,
        "before": before,
        "afterClick": after_click,
        "afterReload": after_reload,
        "persisted": after_click.get("theme") == after_reload.get("theme"),
        "restoredToDark": restored,
    }


async def _collect_viewport(
    cdp: _Cdp, name: str, width: int, height: int, scale: float, mobile: bool,
    base_url: str, cookie: str, out_dir: Path, timeout: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑一档视口：截图、几何度量、交互度量。"""
    await cdp.send(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": width,
            "height": height,
            "deviceScaleFactor": scale,
            "mobile": mobile,
            # 显式声明设备尺寸：让 screen / visualViewport 与视口一致（默认值随
            # 浏览器版本变化，量出来的 dpr / screen 宽度会漂）。
            "screenWidth": width,
            "screenHeight": height,
            "positionX": 0,
            "positionY": 0,
        },
        timeout=timeout,
    )
    await cdp.send(
        "Emulation.setTouchEmulationEnabled",
        {"enabled": mobile, "maxTouchPoints": 5 if mobile else 1},
        timeout=timeout,
    )
    if cookie:
        host = urlparse(base_url).hostname or "127.0.0.1"
        await cdp.send(
            "Storage.setCookies",
            {
                "cookies": [
                    {
                        "name": COOKIE_NAME,
                        "value": cookie,
                        "domain": host,
                        "path": "/",
                        "secure": False,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            },
            timeout=timeout,
        )
    await _reload_and_wait(cdp, base_url, timeout)
    auth_state = await cdp.evaluate(
        "(async () => {"
        "  try { const r = await fetch('/api/auth/session', {credentials: 'same-origin'});"
        "        return {status: r.status, body: await r.json()}; }"
        "  catch (err) { return {status: 0, error: String(err)}; }"
        "})()"
    )
    if isinstance(auth_state, dict) and auth_state.get("status") == 401:
        raise QaError(4, f"登录态没能写进浏览器（{name} 档 /api/auth/session 仍 401）")

    page_metrics = await cdp.evaluate(_PAGE_METRICS_JS)
    full_width, full_height = await _shot_full(
        cdp, out_dir / f"dashboard-{name}.png"
    )

    theme = await _theme_probe(cdp, base_url, timeout)
    panes = await cdp.evaluate(_PANES_JS, timeout=max(20.0, timeout))
    api_status = await cdp.evaluate(_API_JS, timeout=max(20.0, timeout))
    # 分区导航把页面滚到日志区了：回到顶部再开时间线，抽屉截图才不会压在日志上。
    await cdp.evaluate("(() => { window.scrollTo(0, 0); return true; })()")
    await _settle(0.4)

    opened = await cdp.evaluate(_OPEN_TIMELINE_JS)
    reason = ""
    if opened == "no-session":
        reason = "no-session"
    elif opened == "no-button":
        reason = "no-timeline-button"
    else:
        appeared = await _wait_true(
            cdp,
            f"{_MSG_COUNT_JS} > 0",
            TIMELINE_WAIT,
            "时间线消息出现",
        )
        if not appeared:
            reason = "no-messages"
    await _settle(0.4)
    await _shot_viewport(cdp, out_dir / f"timeline-{name}.png")
    timeline = await cdp.evaluate(_TIMELINE_METRICS_JS)
    if not isinstance(timeline, dict):
        raise QaError(7, f"时间线度量结果不是对象：{timeline!r}")
    if reason:
        timeline["reason"] = reason
    else:
        timeline["reason"] = ""
    timeline["opened"] = opened
    timeline["viewportName"] = name
    timeline["page"] = page_metrics
    timeline["screenshot"] = {
        "dashboard": f"dashboard-{name}.png",
        "timeline": f"timeline-{name}.png",
        "dashboardSize": {"width": full_width, "height": full_height},
    }
    timeline["auth"] = auth_state
    interactions = {
        "viewport": name,
        "theme": theme,
        "panes": panes,
        "api": api_status,
        "openedTimeline": opened,
    }
    await cdp.evaluate(
        "(() => { const b = document.getElementById('btn-timeline-close');"
        "  if (b) b.click(); return true; })()"
    )
    await _settle(0.2)
    return timeline, interactions


async def _run(
    *,
    binary: str,
    base_url: str,
    cookie: str,
    out_dir: Path,
    timeout: float,
    debug: bool,
    viewports: tuple[tuple[str, int, int, float, bool], ...] = VIEWPORTS,
) -> dict[str, Any]:
    """拉起 headless 浏览器，跑完两档视口，产出 metrics / interactions。"""
    profile_dir = Path(tempfile.mkdtemp(prefix="dafeiyu-qa-shots-"))
    port = _free_port()
    args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--lang=zh-CN",
        "--window-size=1280,900",
        "about:blank",
    ]
    proc = await asyncio.to_thread(
        subprocess.Popen,
        [binary, *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp: _Cdp | None = None
    target_id = ""
    try:
        _wait_version(port, timeout)
        target = _create_target(port, "about:blank")
        target_id = str(target.get("id") or "")
        ws_url = str(target["webSocketDebuggerUrl"])
        ws = await websockets.connect(ws_url, max_size=None)
        cdp = _Cdp(ws)
        await cdp.send("Page.enable", {}, timeout=timeout)
        await cdp.send("Runtime.enable", {}, timeout=timeout)

        metrics: dict[str, Any] = {}
        interactions: dict[str, Any] = {"baseUrl": base_url, "viewports": []}
        for name, width, height, scale, mobile in viewports:
            view_metrics, view_interactions = await _collect_viewport(
                cdp,
                name,
                width,
                height,
                scale,
                mobile,
                base_url,
                cookie,
                out_dir,
                timeout,
            )
            metrics[name] = view_metrics
            interactions["viewports"].append(view_interactions)

        (out_dir / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_dir / "interactions.json").write_text(
            json.dumps(interactions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"metrics": metrics, "interactions": interactions}
    finally:
        if cdp is not None:
            await cdp.close()
        if target_id:
            try:
                _request("GET", f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=2.0)
            except (urllib.error.URLError, OSError):
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001, S110 - 进程回收失败不应掩盖真实结论
            pass
        # 独立的临时 profile：删除，绝不在仓库里留产物。
        shutil.rmtree(profile_dir, ignore_errors=True)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="面板视觉验收：Edge + CDP 截图与气泡几何度量（只读）"
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8787", help="面板地址"
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="产物目录（默认 .omo/evidence/visual/<时间戳>）",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("DAFEIYU_DASHBOARD_USER", "admin"),
        help="登录用户名（默认取 DAFEIYU_DASHBOARD_USER 或 admin）",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="整体超时（秒，默认 60）"
    )
    parser.add_argument(
        "--browser", default="", help="指定的浏览器可执行文件（默认自动探测）"
    )
    parser.add_argument(
        "--viewports",
        choices=("both", "desktop", "mobile"),
        default="both",
        help="只跑某一档视口（默认两档都跑）",
    )
    parser.add_argument(
        "--debug", action="store_true", help="失败时打印 traceback"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    base_url = args.base_url.rstrip("/")
    if not base_url:
        print("错误：--base-url 不能为空", file=sys.stderr)
        return 1
    if args.timeout <= 0:
        print("错误：--timeout 必须大于 0", file=sys.stderr)
        return 1
    out_dir = (
        Path(args.out_dir).resolve()
        if args.out_dir
        else REPO_ROOT / ".omo" / "evidence" / "visual" / time.strftime("%Y%m%d-%H%M%S")
    )
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"错误：无法创建产物目录 {out_dir}（{exc}）", file=sys.stderr)
        return 1

    try:
        probe = _probe(base_url, min(10.0, args.timeout))
        cookie = ""
        if probe.status == 401:
            password = _resolve_password()
            if not password:
                raise QaError(
                    3,
                    "面板需要登录但没有口令：设置环境变量 "
                    "DAFEIYU_DASHBOARD_PASSWORD 或写入 data/dashboard.password",
                )
            cookie = _login(base_url, args.user, password, min(10.0, args.timeout))
        binary = _find_browser(args.browser)
        selected = (
            VIEWPORTS
            if args.viewports == "both"
            else tuple(item for item in VIEWPORTS if item[0] == args.viewports)
        )
        result = asyncio.run(
            _run(
                binary=binary,
                base_url=base_url,
                cookie=cookie,
                out_dir=out_dir,
                timeout=args.timeout,
                debug=args.debug,
                viewports=selected,
            )
        )
    except QaError as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
        if args.debug:
            import traceback

            traceback.print_exc()
        return exc.code
    except KeyboardInterrupt:
        print("错误：已中断", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 兜底：不裸崩，给人看得懂的失败
        print(f"错误：未预期失败：{exc}", file=sys.stderr)
        if args.debug:
            import traceback

            traceback.print_exc()
        return 7

    summary = {
        "outDir": str(out_dir),
        "files": sorted(
            [path.name for path in out_dir.iterdir() if path.is_file()]
        ),
        "viewports": {
            name: {
                "bubbles": len(view.get("bubbles") or []),
                "reason": view.get("reason") or "",
                "theme": view.get("theme"),
            }
            for name, view in result["metrics"].items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
