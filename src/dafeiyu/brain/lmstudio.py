"""切到本地模型时按需拉起 LM Studio。

LM Studio 的 OpenAI 端点是 1234；server 没起时 bot 只会在第一次请求失败，
表现成「她突然不理人」。这里先探活，再调 lms CLI 起服务，最后后台加载模型。
所有外部动作都可注入，测试不需要真的启动 LM Studio。
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
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return resp.status_code < 400
    except Exception:  # noqa: BLE001 - 连不上就是没就绪
        return False


def _spawn_detached(args: list[str]) -> None:
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(  # noqa: S603 - 参数由本模块拼装，不经过 shell
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **kwargs,
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
