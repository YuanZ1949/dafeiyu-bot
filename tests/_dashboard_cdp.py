"""无头浏览器（Edge + CDP）测试辅助：复用 scripts/qa_dashboard_shots.py 的实现。

用法::

    from tests._dashboard_cdp import browser_page

    async with browser_page("http://127.0.0.1:8787/") as page:
        title = await page.eval_js("document.title")
        await page.screenshot(Path("shot.png"))

实现不是复制：浏览器探测 / 空闲端口 / CDP 版本等待 / target 创建 / CDP 客户端
全部 import 自 ``scripts/qa_dashboard_shots.py``（按路径加载），本模块只负责
把它们拼成「单页面」的 async with 入口。不引入 playwright / selenium。
"""

from __future__ import annotations

import asyncio
import importlib.abc
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import websockets

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "qa_dashboard_shots.py"
_SCRIPT_MODULE = "_qa_dashboard_shots"

_spec = importlib.util.spec_from_file_location(_SCRIPT_MODULE, _SCRIPT_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"无法按路径加载 QA 脚本：{_SCRIPT_PATH}")
assert isinstance(_spec.loader, importlib.abc.Loader)
_qa = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_SCRIPT_MODULE, _qa)
_spec.loader.exec_module(_qa)

#: 复用 QA 脚本里的实现（不复制）。
QaError = _qa.QaError
_Cdp = _qa._Cdp
_find_browser = _qa._find_browser
_free_port = _qa._free_port
_wait_version = _qa._wait_version
_create_target = _qa._create_target
_request = _qa._request
_wait_or_fail = _qa._wait_or_fail
_shot_viewport = _qa._shot_viewport
_READY_JS = _qa._READY_JS

#: 拉浏览器 / 等 CDP / 页面操作的统一超时（秒）：浏览器死了不能无限等。
_LAUNCH_TIMEOUT = 30.0
_PAGE_TIMEOUT = 15.0


def browser_path() -> Path:
    """浏览器可执行文件路径；找不到时抛 QaError（消息含「找不到」）。

    环境变量 ``DAFEIYU_BROWSER`` 优先：指向不存在的路径也如实报错，不静默回退。
    """
    explicit = os.environ.get("DAFEIYU_BROWSER", "").strip()
    try:
        found = _find_browser(explicit)
    except QaError as exc:
        raise QaError(exc.code, f"找不到浏览器可执行文件：{exc.message}") from exc
    return Path(found)


def _click_js(selector: str) -> str:
    """按选择器点一下的 JS：元素不存在回 false，由调用方转成 QaError。"""
    return (
        "(() => {"
        f"const el = document.querySelector({json.dumps(selector)});"
        "if (!el) return false;"
        "el.click();"
        "return true;"
        "})()"
    )


def _exists_js(selector: str) -> str:
    return f"!!document.querySelector({json.dumps(selector)})"


class _Page:
    """单个 target 的页面句柄：把 CDP 调用包装成直白的小方法。"""

    def __init__(self, cdp: _Cdp, *, port: int, proc: subprocess.Popen[Any]) -> None:
        self._cdp = cdp
        self.port = port
        self.proc = proc

    @property
    def pid(self) -> int:
        """浏览器进程号（供清理验证用）。"""
        return self.proc.pid

    async def eval_js(self, expression: str) -> Any:
        """跑一段 JS 并取回 JSON 值（字符串 / 数字 / 对象）。"""
        return await self._cdp.evaluate(expression, timeout=_PAGE_TIMEOUT)

    async def screenshot(self, path: Path | str) -> None:
        """截当前视口 PNG（复用 QA 脚本的 _shot_viewport）。"""
        await _shot_viewport(self._cdp, Path(path))

    async def goto(self, url: str) -> None:
        """导航并等到文档加载完成（有界等待，超时抛 QaError）。"""
        await self._cdp.send("Page.navigate", {"url": url}, timeout=_PAGE_TIMEOUT)
        await _wait_or_fail(self._cdp, _READY_JS, _PAGE_TIMEOUT, f"页面加载：{url}")

    async def click(self, selector: str) -> None:
        """点一个元素；选择器命不中直接报错，不静默。"""
        clicked = await self._cdp.evaluate(_click_js(selector), timeout=_PAGE_TIMEOUT)
        if clicked is not True:
            raise QaError(7, f"点击失败：找不到元素 {selector}")

    async def wait_for(self, selector: str, timeout_ms: int = 5000) -> None:
        """轮询等选择器出现（毫秒超时，复用 QA 脚本的 _wait_or_fail）。"""
        await _wait_or_fail(
            self._cdp,
            _exists_js(selector),
            max(0.2, timeout_ms / 1000.0),
            f"等待元素 {selector}",
        )

    async def set_viewport(self, width: int, height: int) -> None:
        """改视口（Emulation.setDeviceMetricsOverride，桌面档）。"""
        await self._cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1.0,
                "mobile": False,
                "screenWidth": width,
                "screenHeight": height,
                "positionX": 0,
                "positionY": 0,
            },
            timeout=_PAGE_TIMEOUT,
        )


@asynccontextmanager
async def browser_page(url: str, *, width: int = 1280, height: int = 900):
    """拉起 headless Edge，开一个 target 导航到 ``url``，yield 一个 _Page。

    退出时（包括 body 抛异常）关 CDP、关 target、terminate 浏览器进程、
    删临时 profile —— 端口随进程退出释放。CDP 就绪等待有界：浏览器起不来
    或半路死掉时按超时报 QaError，不会挂死。
    """
    binary = str(browser_path())
    profile_dir = Path(tempfile.mkdtemp(prefix="dafeiyu-cdp-page-"))
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
        f"--window-size={width},{height}",
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
        _wait_version(port, _LAUNCH_TIMEOUT)
        # /json/new?url= 在 Edge 上不会真的导航（target 停在 about:blank），
        # 与 QA 脚本一致：target 建在 about:blank，再用 Page.navigate 过去。
        target = _create_target(port, "about:blank")
        target_id = str(target.get("id") or "")
        ws = await websockets.connect(str(target["webSocketDebuggerUrl"]), max_size=None)
        page_cdp = _Cdp(ws)
        cdp = page_cdp
        await page_cdp.send("Page.enable", {}, timeout=_PAGE_TIMEOUT)
        await page_cdp.send("Runtime.enable", {}, timeout=_PAGE_TIMEOUT)
        await page_cdp.send("Page.navigate", {"url": url}, timeout=_PAGE_TIMEOUT)
        await _wait_or_fail(page_cdp, _READY_JS, _PAGE_TIMEOUT, f"首屏加载：{url}")
        yield _Page(page_cdp, port=port, proc=proc)
    finally:
        if cdp is not None:
            await cdp.close()
        if target_id:
            try:
                _request(
                    "GET", f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=2.0
                )
            except (urllib.error.URLError, OSError):
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # terminate 失败补一刀 kill，不掩盖原异常
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:  # 进程已消失时忽略
                pass
        shutil.rmtree(profile_dir, ignore_errors=True)


__all__ = ["QaError", "_Cdp", "browser_page", "browser_path"]
