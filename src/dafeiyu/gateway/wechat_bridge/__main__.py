"""微信桥进程入口（PART A：服务器 + 命令行；PART B：轮询循环接线）。

用法：`python -m dafeiyu.gateway.wechat_bridge`（见 `run` / `main`）。

启动即 fail-closed：`gateway.wechat.enabled`、非空 `group_allowlist`、
非空 `account_nickname`、`len(group_allowlist) <= max_groups`，缺一项直接
`ConfigError` 拒绝启动（消息里只提配置键名）。

一切触到 pyweixin 的 import 都在函数体内（`_default_driver_factory` /
`_default_poller_factory`），保证 `--help` 在没装 pyweixin 的机器上退出 0；
本模块也不打印/记录 `bridge_token`。

PART B 接线：
-   没注入 `frame_source` 时，服务器监听成功后启动 `poller.PollingLoop`
    的专用 STA/UIA 线程，轮询 allowlist 并把 message/resync/health 帧交给
    `server.pump_frames`；
-   `frame_source=` 仍可注入（PART A 测试/离线回放），此时不启动 UIA 线程；
-   `poller_factory=` 是测试接缝（默认 `PollingLoop(wechat, driver)`）；
-   `command_handler=` 仍是可注入的占位实现，发送器接线留给后续任务。

停机语义（SIGINT 与 `action:"stop"` 等价）：`stop` 指令第一步先
`PollingLoop.abort_sends()`（丢掉排队中的发送、停止发起新的 UI op；已在执行的
单次 pyweixin 调用无法从外部打断），然后走与 SIGINT 相同的优雅收摊：
停轮询 -> 取消灌帧 -> 广播最后一帧 `connected:false` 健康帧 -> 关服务器 ->
join UIA 线程 -> 返回 0。
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

#: 轮询循环工厂：`(wechat, driver) -> PollingLoop`（鸭子类型，测试可注入）。
PollerFactory = Callable[[WeChatConfig, Any], Any]


async def run(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    driver_factory: Callable[[], Any] | None = None,
    server_factory: ServerFactory | None = None,
    command_handler: CommandHandler | None = None,
    frame_source: FrameSource | None = None,
    poller_factory: PollerFactory | None = None,
) -> int:
    """加载配置、fail-closed 检查、起服务器、轮询、等待停机。

    停机信号：SIGINT（进程收到 Ctrl+C）或动作 == "stop" 的指令；两者都会
    先中止发送/轮询，再广播一帧最终健康帧，join UIA 线程、关服务器并返回
    exit code 0。拒绝启动时抛 `ConfigError`（消息含具体配置键名），由 `main`
    兜底打印并退出非 0。

    `frame_source` 显式注入时按 PART A 的帧源通路运行（不启动 UIA 线程）；
    未注入时使用轮询循环（PART B 的默认路径）。
    """
    parser = _build_parser()
    # argv=None 表示「程序内调用」：不偷看宿主进程的 sys.argv（pytest 就是 argv
    # 里带着测试文件路径的典型例子）。命令行入口由 `main` 显式传 sys.argv[1:]。
    args = parser.parse_args(list(argv) if argv is not None else [])
    if settings is None:
        settings = load_settings(Path(args.env_file), Path(args.config))
    wechat = settings.wechat
    _startup_checks(wechat)

    # 驱动门面只在函数体内加载（pyweixin 保持惰性）。
    driver = (driver_factory or _default_driver_factory)()
    logger.info("微信桥驱动门面已注入：%s", type(driver).__name__)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    # poller 在服务器工厂之后才构造：回调要能引用到它（action:"stop" 先 abort）。
    poller_holder: list[Any] = []

    def _signal_stop() -> None:
        # §7 / PART B：stop 的第一件事是中止发送（丢排队 job、停止新 UI op）。
        for poller in poller_holder:
            with contextlib.suppress(Exception):
                poller.abort_sends()
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

    # 初始 health 帧与帧源无关：注入纯 message 帧源时也必须先有健康快照，
    # 否则客户端 `_health_connected` 一直是 False（wechat.py:168/291-293）。
    # 探测不可用时按 fail-closed 降级（logged_in False / desktop no_wechat）。
    startup_health = _health_frame(wechat, connected=True)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(server.broadcast(startup_health), _AWAIT_TIMEOUT)

    poller: Any | None = None
    pump_task: asyncio.Task[None] | None = None
    try:
        if frame_source is None:
            # UI 线程必须在服务器监听之后启动；它只通过 PollingLoop 的队列被使用。
            poller = (poller_factory or _default_poller_factory)(wechat, driver)
            poller_holder.append(poller)
            await asyncio.wait_for(poller.start(), _AWAIT_TIMEOUT)
            source = poller.frames()
        else:
            source = frame_source
        pump_task = asyncio.create_task(
            _pump_until_stopped(
                server,
                source,
                idle_timeout=_pump_idle_timeout(wechat.poll_seconds),
            ),
            name="wechat-bridge-pump",
        )
        await _wait_for_stop(stop, loop)
    finally:
        # 1) 让轮询循环和排队中的发送一起停手（幂等，可在异常路径重复调用）。
        if poller is not None:
            with contextlib.suppress(Exception):
                poller.abort_sends()
        # 2) 取消灌帧；帧源被关闭，不再产生新事件。
        if pump_task is not None:
            pump_task.cancel()
            # 被取消的任务在 await 时抛 CancelledError（BaseException，不是
            # Exception），显式收下：这是我们自己发的取消，不是停机失败。
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(pump_task, _AWAIT_TIMEOUT)
        # 3) join UIA 线程（带超时；即使上面某步炸了也必须走到）。
        if poller is not None:
            with contextlib.suppress(Exception):
                poller.stop(timeout=_AWAIT_TIMEOUT)
        # 4) 最终健康帧：**无条件**发出（协议 §7 / 计划 task 18 都要求停机时
        #    自报断链）。优先用最近快照；一帧 health 都没推过时用启动帧兜底，
        #    绝不因为 frame_source 只产消息帧就跳过停机通知。
        getter = getattr(server, "latest_health", None)
        # latest_health 是 property（取到的是 dict），不是方法；两种形态都兼容。
        snapshot = getter() if callable(getter) else getter
        final = dict(snapshot) if isinstance(snapshot, dict) else dict(startup_health)
        final.setdefault("type", "health")
        final.setdefault("channel", "wechat")
        final["v"] = 1
        final["connected"] = False  # 停机帧必须自报断链，别留 connected:true
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.broadcast(final), _AWAIT_TIMEOUT)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.stop(), _AWAIT_TIMEOUT)
            logger.info("微信桥已关闭")
    return 0


def _health_frame(wechat: WeChatConfig, *, connected: bool) -> dict[str, Any]:
    """一帧 v1 health：优先用 `health.probe` 的真实快照，探测不可用时降级。

    降级帧是 fail-closed 的：`logged_in=False` + `desktop="no_wechat"`，客户端
    据此停止发送（wechat.py:85-90 的 `_health_connected` 与 send 闸门），
    绝不为了「看起来健康」而伪造 interactive/logged_in。
    """
    try:
        from .health import probe

        frame = probe(wechat, connected=connected)
        if isinstance(frame, dict):
            snapshot = dict(frame)
            snapshot["v"] = 1
            snapshot.setdefault("type", "health")
            snapshot.setdefault("channel", "wechat")
            snapshot["connected"] = bool(connected)
            return snapshot
    except Exception:
        logger.warning("微信桥健康探测不可用，按 fail-closed 降级", exc_info=True)
    return {
        "v": 1,
        "type": "health",
        "channel": "wechat",
        "connected": bool(connected),
        "logged_in": False,
        "desktop": "no_wechat",
        "wechat_version": None,
        "language": None,
        "queue_depth": 0,
    }


def _pump_idle_timeout(poll_seconds: float) -> float:
    """灌帧空转上限：必须大于一个完整轮询间隔（默认 8s）才不会误停泵。

    轮询循环每轮至少产出一帧 health，所以最长帧间隔约等于 `poll_seconds`；
    给两倍余量，且不低于 PART A 的 5s 默认值。
    """
    try:
        interval = float(poll_seconds)
    except (TypeError, ValueError):
        interval = 8.0
    return max(_AWAIT_TIMEOUT, interval * 2.0)


async def _pump_until_stopped(
    server: Any, source: FrameSource, *, idle_timeout: float | None = None
) -> None:
    await pump_frames(server, source, idle_timeout=idle_timeout)


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


def _default_poller_factory(wechat: WeChatConfig, driver: Any) -> Any:
    """默认轮询循环：`PollingLoop` 的 import 也留在函数体内，保持入口轻量。"""
    from .poller import PollingLoop

    return PollingLoop(wechat, driver)


def _default_frame_source(wechat: WeChatConfig) -> Iterable[dict[str, Any]]:
    """离线回放/兼容用的单帧源（不再被 `run()` 默认使用）。

    默认路径已经切到轮询循环；保留它给「只验证服务器通路」的调用方显式注入。
    """
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
