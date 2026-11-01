"""微信桥进程入口（PART A：服务器 + 命令行 + 生命周期）。

用法：`python -m dafeiyu.gateway.wechat_bridge`（见 `run` / `main`）。

启动即 fail-closed：`gateway.wechat.enabled`、非空 `group_allowlist`、
非空 `account_nickname`、`len(group_allowlist) <= max_groups`，缺一项直接
`ConfigError` 拒绝启动（消息里只提配置键名）。

一切触到 pyweixin 的 import 都在函数体内（`_default_driver_factory` /
`_default_frame_source`），保证 `--help` 在没装 pyweixin 的机器上退出 0；
本模块也不打印/记录 `bridge_token`。

PART B（轮询/UIA 线程循环）的接缝：
-   `run(..., driver_factory=...)`：注入假驱动 / 真驱动门面；
-   `run(..., frame_source=...)`：注入轮询循环的帧源（同步或异步迭代器），
    每帧都会经 `server.pump_frames` -> `BridgeServer.broadcast` 下发；
-   `run(..., command_handler=...)`：注入指令处理器（PART B 接发送器）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from dafeiyu.config import ConfigError, Settings, WeChatConfig, load_settings

from .server import BridgeServer, pump_frames

__all__ = ["main", "run"]

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_ENV_FILE = ".env"

# 反挂死护栏：每一个 await 都有显式超时（<= 5s）。
_AWAIT_TIMEOUT = 5.0

logger = logging.getLogger(__name__)

#: 可注入的指令处理器：见 `BridgeServer.CommandHandler`。
CommandHandler = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any] | None]]

#: 服务器工厂：参数与 `BridgeServer.__init__` 一致，便于测试与 PART B 复用。
ServerFactory = Callable[..., Any]

FrameSource = Iterable[dict[str, Any]] | AsyncIterable[dict[str, Any]]


async def run(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    driver_factory: Callable[[], Any] | None = None,
    server_factory: ServerFactory | None = None,
    command_handler: CommandHandler | None = None,
    frame_source: FrameSource | None = None,
) -> int:
    """加载配置、fail-closed 检查、起服务器、发初始健康帧，等待停机。

    停机信号：SIGINT（进程收到 Ctrl+C）或动作 == "stop" 的指令；两者都会
    先广播一帧最终健康帧，再关服务器并返回 exit code 0。拒绝启动时抛
    `ConfigError`（消息含具体配置键名），由 `main` 兜底打印并退出非 0。
    """
    parser = _build_parser()
    # argv=None 表示「程序内调用」：不偷看宿主进程的 sys.argv（pytest 就是 argv
    # 里带着测试文件路径的典型例子）。命令行入口由 `main` 显式传 sys.argv[1:]。
    args = parser.parse_args(list(argv) if argv is not None else [])
    if settings is None:
        settings = load_settings(Path(args.env_file), Path(args.config))
    wechat = settings.wechat
    _startup_checks(wechat)

    # 驱动门面只在函数体内加载（pyweixin 保持惰性）。PART A 只验证接缝，
    # 真正的轮询/发送接线由 PART B 完成。
    driver = (driver_factory or _default_driver_factory)()
    logger.info("微信桥驱动门面已注入：%s", type(driver).__name__)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _signal_stop() -> None:
        if not stop.done():
            stop.set_result(None)

    server = (server_factory or BridgeServer)(
        wechat.bridge_host,
        wechat.bridge_port,
        bridge_token=wechat.bridge_token,
        command_handler=command_handler,
        on_stop_requested=_signal_stop,
    )
    bound_port = await asyncio.wait_for(server.start(), _AWAIT_TIMEOUT)
    logger.info("微信桥已监听 %s:%s", wechat.bridge_host, bound_port)

    source = frame_source if frame_source is not None else _default_frame_source(wechat)
    pump_task = asyncio.create_task(
        _pump_until_stopped(server, source),
        name="wechat-bridge-pump",
    )
    try:
        await _wait_for_stop(stop, loop)
        # 最终健康帧：用最近一帧快照标记优雅停机（没推过健康帧就跳过）。
        getter = getattr(server, "latest_health", None)
        # latest_health 是 property（取到的是 dict），不是方法；两种形态都兼容。
        snapshot = getter() if callable(getter) else getter
        final = dict(snapshot) if isinstance(snapshot, dict) else None
        if final is not None:
            final["connected"] = False  # 停机帧必须自报断链，别留 connected:true
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.broadcast(final), _AWAIT_TIMEOUT)
    finally:
        pump_task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(pump_task, _AWAIT_TIMEOUT)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.stop(), _AWAIT_TIMEOUT)
            logger.info("微信桥已关闭")
    return 0


async def _pump_until_stopped(server: Any, source: FrameSource) -> None:
    await pump_frames(server, source)


async def _wait_for_stop(stop: asyncio.Future[None], loop: asyncio.AbstractEventLoop) -> None:
    """等停止信号。注册 SIGINT hook；拿不到 hook 时靠 KeyboardInterrupt 兜底。"""
    previous = None

    def _on_sigint(signum: int, frame: Any) -> None:
        loop.call_soon_threadsafe(_wake)

    def _wake() -> None:
        if not stop.done():
            stop.set_result(None)

    try:
        # 非主线程 / 非 Windows 语义差异都按「没装 hook」降级。
        previous = signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        previous = None
    try:
        await stop
    except KeyboardInterrupt:
        pass  # hook 失败时的兜底路径
    finally:
        if previous is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGINT, previous)


def _startup_checks(wechat: WeChatConfig) -> None:
    """就绪度 fail-closed：缺一项就拒绝，消息只提配置键名，不回显值。"""
    if not wechat.enabled:
        raise ConfigError(
            "gateway.wechat.enabled 是 false：微信桥已停用，把 enabled 改为 true 再启动"
        )
    if not wechat.group_allowlist:
        raise ConfigError(
            "gateway.wechat.group_allowlist 为空：微信桥没有可轮询的群"
        )
    if not wechat.account_nickname:
        raise ConfigError(
            "gateway.wechat.account_nickname 为空：无法判定是否被 @"
        )
    if len(wechat.group_allowlist) > wechat.max_groups:
        raise ConfigError(
            f"gateway.wechat.group_allowlist 有 {len(wechat.group_allowlist)} 个群，"
            f"超过 gateway.wechat.max_groups={wechat.max_groups}："
            "先调大 max_groups 或精简 group_allowlist"
        )


def _default_driver_factory() -> Any:
    """默认驱动门面：pyweixin 的 import 只在函数体内（help 路径保持轻）。"""
    from . import driver

    return driver


def _default_frame_source(wechat: WeChatConfig) -> Iterable[dict[str, Any]]:
    """默认帧源：启动时用 health.probe 产一帧健康（不碰网络、不持连接）。"""
    from .health import probe

    def source() -> Iterable[dict[str, Any]]:
        return iter([probe(wechat, connected=True)])

    return source()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dafeiyu.gateway.wechat_bridge",
        description="dafeiyu 桌面微信桥（WebSocket 服务器进程）",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="config.yaml 路径（默认 %(default)s）",
    )
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help=".env 路径（默认 %(default)s）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """进程入口：asyncio.run(run(argv))，SIGINT = 正常退出 0，配置缺失退出 2。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(run(sys.argv[1:] if argv is None else list(argv)))
    except KeyboardInterrupt:
        # SIGINT hook 没接住时 asyncio.run 的兜底：按用户主动停机处理。
        return 0
    except ConfigError as exc:
        print(f"微信桥拒绝启动：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
