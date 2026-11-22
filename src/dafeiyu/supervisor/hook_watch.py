"""微信 hook 进程看门狗。

``gateway.wechat.outbound: hook`` 时发送全靠 ``scripts/wechat_hook_server.py``，
但 supervisor 只负责 bot / bridge，hook 是 ``start-all.ps1`` 在起 supervisor
之前单独拉起的。hook 自己退出后（微信换进程、helper 注入失败、账号切换），
端口就空着，bridge 每分钟只告警「脚本探测未通过」，不会自愈。

这个看门狗把告警补成自愈：轮询 ``/health``，连续失败且冷却已过就调用
``scripts/start-wechat-hook.ps1`` 重新拉起 hook。冷却是为了不打断正在等待
微信登录的 hook（脚本 60s 内没就绪会返回 1，但进程仍可能随后就绪）。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
START_SCRIPT = REPO_ROOT / "scripts" / "start-wechat-hook.ps1"

PROBE_TIMEOUT = 5.0
WATCH_INTERVAL = 60.0
RESTART_COOLDOWN = 300.0
# 连续两次探测失败（默认约 2 分钟）才动手，避免 supervisor 与 hook 同时启动时
# 把还在等微信登录的 hook 杀掉重来。
MIN_FAILURES = 2
STOP_TIMEOUT = 30.0
RESTART_TIMEOUT = 150.0
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def health_url(hook_url: str) -> str:
    """把 ``.../send_text`` 换成 ``.../health``；推导不出来返回空串。"""
    url = (hook_url or "").strip().rstrip("/")
    if not url:
        return ""
    head, sep, _ = url.rpartition("/")
    if not sep or "://" not in head:
        return ""
    return f"{head}/health"


def should_restart(
    ready: bool,
    failures: int,
    now: float,
    last_attempt: float | None,
    *,
    min_failures: int = MIN_FAILURES,
    cooldown: float = RESTART_COOLDOWN,
) -> bool:
    """是否该重启 hook：已就绪不重启；失败次数够且冷却已过才重启。"""
    if ready or failures < min_failures:
        return False
    if last_attempt is None:
        return True
    return now - last_attempt >= cooldown


def probe_ready(url: str, *, timeout: float = PROBE_TIMEOUT) -> bool:
    """GET /health，只有明确 ``ready: true`` 才算健康。"""
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status >= 400:
                return False
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - 连不上/超时/非 JSON 一律当未就绪
        return False
    return isinstance(data, dict) and data.get("ready") is True


def _run_script(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("执行微信 hook 脚本失败：%s", exc)
        return None


def restart_hook(
    port: int,
    account: str = "",
    *,
    script: Path = START_SCRIPT,
    timeout: float = RESTART_TIMEOUT,
) -> bool:
    """先停再起，真正替换掉端口上的 hook。

    启动脚本对「已在运行」的实例只打印一行就跳过，僵尸 hook 会一直占着端口，
    所以必须先用 ``-Stop`` 结束旧进程，再重新拉起。
    """
    if not script.is_file():
        log.warning("找不到微信 hook 启动脚本：%s", script)
        return False
    base = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Port",
        str(port),
    ]
    stop_result = _run_script(base + ["-Stop"], timeout=STOP_TIMEOUT)
    if stop_result is not None and stop_result.returncode != 0:
        detail = (stop_result.stderr or stop_result.stdout or b"").decode(
            "utf-8", "replace"
        ).strip()
        log.warning("停止旧微信 hook 返回 %s：%s", stop_result.returncode, detail[-500:])
    command = list(base)
    if account:
        command += ["-Account", account]
    result = _run_script(command, timeout=timeout)
    if result is None:
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        log.warning("重启微信 hook 返回 %s：%s", result.returncode, detail[-500:])
        return False
    return True


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
    except TimeoutError:
        pass


async def hook_loop(
    hook_url: str,
    account: str,
    stop: asyncio.Event,
    *,
    interval: float = WATCH_INTERVAL,
    cooldown: float = RESTART_COOLDOWN,
    min_failures: int = MIN_FAILURES,
    script: Path = START_SCRIPT,
    probe: Callable[[str], bool] = probe_ready,
    starter: Callable[[int, str], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """轮询 hook 健康并在连续失败后重启；``stop`` 置位即退出。"""
    url = health_url(hook_url)
    if not url:
        log.info("未配置微信 hook 地址，hook 看门狗不启动")
        return
    port = urlsplit(url).port
    if port is None:
        log.warning("微信 hook 地址缺少端口，hook 看门狗不启动：%s", url)
        return
    if starter is None:
        starter = functools.partial(restart_hook, script=script)

    failures = 0
    last_attempt: float | None = None
    while not stop.is_set():
        try:
            ready = await asyncio.to_thread(probe, url)
            now = clock()
            if ready:
                if failures:
                    log.info("微信 hook 已恢复就绪：%s", url)
                failures = 0
            else:
                failures += 1
                if should_restart(
                    False,
                    failures,
                    now,
                    last_attempt,
                    min_failures=min_failures,
                    cooldown=cooldown,
                ):
                    last_attempt = now
                    log.warning(
                        "微信 hook 探测未通过（连续 %s 次），尝试重启：%s", failures, url
                    )
                    if await asyncio.to_thread(starter, port, account):
                        log.info("微信 hook 重启命令已执行：port=%s", port)
                    else:
                        log.warning("微信 hook 重启失败，%.0fs 后再试", cooldown)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 看门狗不能把 supervisor 带崩
            log.warning("hook 看门狗巡检失败：%s", exc)
        await _sleep_or_stop(stop, interval)
