"""切到本地模型时按需拉起 LM Studio，切走时再关掉。

LM Studio 的 OpenAI 端点是 1234；server 没起时 bot 只会在第一次请求失败，
表现成「她突然不理人」。这里先探活，再调 lms CLI 起服务，最后后台加载模型。
不用本地模型时端点会一直占着显存/内存，所以切走（或启动时发现当前档位不是
本地模型）再调 lms CLI 停服务；``stop_when_unused`` 打开时还会把 LM Studio
主程序一起结束——``lms server stop`` 只停服务，App 壳仍会驻留几百 MB。

所有外部动作都可注入，测试不需要真的启动/停止 LM Studio。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import LocalModelConfig
from ..netssl import default_ssl_context

log = logging.getLogger(__name__)

PROBE_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class EnsureResult:
    ready: bool
    started: bool
    detail: str


def find_lms(explicit: str = "") -> str | None:
    """优先用配置路径，其次 PATH，最后 LM Studio 的默认安装位置。"""
    if explicit:
        return explicit if Path(explicit).exists() else None
    found = shutil.which("lms")
    if found:
        return found
    name = "lms.exe" if os.name == "nt" else "lms"
    candidate = Path.home() / ".lmstudio" / "bin" / name
    return str(candidate) if candidate.exists() else None


async def probe(base_url: str, *, timeout: float = 1.5) -> bool:
    """端点能回 /models 就算就绪（不关心模型是否已加载）。"""
    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=default_ssl_context()) as client:
            resp = await client.get(url)
            return resp.status_code < 400
    except Exception:  # noqa: BLE001 - 连不上就是没就绪
        return False


def _spawn_detached(args: list[str]) -> None:
    if os.name == "nt":
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _run_lms_stop(args: list[str], *, timeout: float = 15.0) -> None:
    """同步执行 ``lms server stop``；调用点用 to_thread 包住，别卡事件循环。"""
    if os.name == "nt":
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )


LMSTUDIO_IMAGE = "LM Studio.exe"


def kill_app_processes(*, timeout: float = 15.0) -> bool:
    """结束 LM Studio 主程序（含子进程）。

    ``lms server stop`` 只停 HTTP 服务，Electron 壳仍驻留（实测约 300MB）。
    只在 Windows 用 taskkill；未找到进程时它返回 128，按「本来没开」处理。
    退出码 0 = 确实结束了进程，供调用点区分「关了」和「本来就没有」。
    """
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/IM", LMSTUDIO_IMAGE, "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("结束 LM Studio 进程失败：%s", exc)
        return False
    return result.returncode == 0


async def stop_server(
    cfg: LocalModelConfig,
    *,
    probe_fn: Callable[[str], Awaitable[bool]] | None = None,
    stop_fn: Callable[[list[str]], None] | None = None,
    kill_fn: Callable[[], bool] | None = None,
) -> EnsureResult:
    """端点在线时停掉 LM Studio；``stop_when_unused`` 时连主程序一起结束。

    先探活：端点不可用就不必再调 ``lms server stop``（但仍要清掉可能驻留的
    App 壳，见下）。停止/结束进程都放在线程里跑，不会阻塞事件循环。
    """
    check = probe_fn or probe
    running = await check(cfg.base_url)
    stopped = False
    detail = "未在运行"

    if running:
        exe = find_lms(cfg.lms_path)
        if not exe:
            log.warning("LM Studio 仍在运行，但找不到 lms CLI，无法自动停止")
            return EnsureResult(ready=True, started=False, detail="找不到 lms CLI")

        stop = stop_fn or _run_lms_stop
        try:
            await asyncio.to_thread(stop, [exe, "server", "stop"])
        except Exception as exc:  # noqa: BLE001 - 停止失败只降级
            log.warning("停止 LM Studio server 失败：%s", exc)
            return EnsureResult(ready=True, started=False, detail=f"停止失败：{exc}")

        log.info("已停止 LM Studio server：%s", cfg.base_url)
        stopped = True
        detail = "已停止"

    if not cfg.stop_when_unused:
        return EnsureResult(ready=not stopped and running, started=stopped, detail=detail)

    # `lms server stop` 只停 HTTP 服务，GUI 壳还占着几百 MB：一并结束。
    kill = kill_fn or kill_app_processes
    try:
        killed = await asyncio.to_thread(kill)
    except Exception as exc:  # noqa: BLE001 - 结束进程失败只降级
        log.warning("结束 LM Studio 进程失败：%s", exc)
        killed = False

    if not killed:
        return EnsureResult(ready=False, started=stopped, detail=detail)

    log.info("已结束 LM Studio 进程")
    return EnsureResult(
        ready=False,
        started=True,
        detail="已停止并结束进程" if stopped else "已结束残留进程",
    )


async def ensure_server(
    cfg: LocalModelConfig,
    model: str = "",
    *,
    probe_fn: Callable[[str], Awaitable[bool]] | None = None,
    spawn_fn: Callable[[list[str]], None] | None = None,
    sleep_fn: Callable[[float], Awaitable[None]] | None = None,
) -> EnsureResult:
    """确保本地端点可用；已在运行时不重复启动。

    启动失败只记日志并返回结果，不抛异常：切换大脑不应该被 LM 拖住。
    """
    if not cfg.autostart:
        return EnsureResult(ready=False, started=False, detail="autostart 已关闭")

    check = probe_fn or probe
    spawn = spawn_fn or _spawn_detached
    sleep = sleep_fn or asyncio.sleep

    if await check(cfg.base_url):
        log.info("LM Studio 已在运行：%s", cfg.base_url)
        # server 起来但模型可能没加载：顺手发一个后台 load，避免第一条消息
        # 依赖 LM Studio 的 JIT 开关（关掉 JIT 时首条会直接失败）。
        exe = find_lms(cfg.lms_path)
        if exe and model:
            try:
                spawn([exe, "load", model])
            except Exception as exc:  # noqa: BLE001 - 加载失败不阻断切换
                log.warning("请求加载本地模型 %s 失败：%s", model, exc)
        return EnsureResult(ready=True, started=False, detail="已在运行")

    exe = find_lms(cfg.lms_path)
    if not exe:
        log.warning(
            "本地模型端点未就绪，且找不到 lms CLI（可用 local_model.lms_path 指定）"
        )
        return EnsureResult(ready=False, started=False, detail="找不到 lms CLI")

    try:
        spawn([exe, "server", "start"])
    except Exception as exc:  # noqa: BLE001 - 启动失败只降级
        log.warning("启动 LM Studio server 失败：%s", exc)
        return EnsureResult(ready=False, started=False, detail=f"启动失败：{exc}")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.autostart_timeout_seconds
    while loop.time() < deadline:
        await sleep(PROBE_INTERVAL_SECONDS)
        if await check(cfg.base_url):
            if model:
                try:
                    spawn([exe, "load", model])
                except Exception as exc:  # noqa: BLE001 - 交给 LM Studio 首次请求自动加载
                    log.warning("请求加载本地模型 %s 失败：%s", model, exc)
                else:
                    log.info("已请求 LM Studio 后台加载模型 %s", model)
            log.info("LM Studio 已就绪：%s", cfg.base_url)
            return EnsureResult(ready=True, started=True, detail="已启动")

    log.warning(
        "LM Studio 在 %.0fs 内没有就绪：%s",
        cfg.autostart_timeout_seconds,
        cfg.base_url,
    )
    return EnsureResult(ready=False, started=True, detail="启动超时")
