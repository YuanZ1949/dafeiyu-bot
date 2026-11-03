"""微信桥进程入口（PART A：服务器 + 命令行；PART B：轮询循环接线）。

用法：`python -m dafeiyu.gateway.wechat_bridge`（见 `run` / `main`）。

启动即 fail-closed：`gateway.wechat.enabled`、非空 `account_nickname`，以及
出站后端（hook/auto 需要 `hook_url`，hook 需要本地库）缺一项直接
`ConfigError` 拒绝启动（消息里只提配置键名）。轮询/发送范围只看面板白名单
（`data/settings.local.yaml` 的 `groups.whitelist`）。

一切触到 pyweixin 的 import 都在函数体内（`_default_driver_factory` /
`_default_poller_factory`），保证 `--help` 在没装 pyweixin 的机器上退出 0；
本模块也不打印/记录 `bridge_token`。

PART B 接线：
-   没注入 `frame_source` 时，服务器监听成功后启动 `poller.PollingLoop`
    的专用 STA/UIA 线程，按面板白名单轮询并把 message/resync/health 帧交给
    `server.pump_frames`；
-   `frame_source=` 仍可注入（PART A 测试/离线回放），此时不启动 UIA 线程；
-   `poller_factory=` 是测试接缝（默认 `PollingLoop(wechat, driver)`）；
-   `command_handler=` 注入时由调用方全权接管所有动作；默认走内置发送路径：
    `wxg:<alias>` -> 注册表显示名 -> `Sender` -> `_UiThreadDriver`（投递到
    轮询循环的 STA/UIA 线程），事件循环线程永不直接执行 UIA/pyweixin。

停机语义（SIGINT 与 `action:"stop"` 等价）：`stop` 指令第一步先
`PollingLoop.abort_sends()`（丢掉排队中的发送、停止发起新的 UI op；已在执行的
单次 pyweixin 调用无法从外部打断），然后走与 SIGINT 相同的优雅收摊：
停轮询 -> 取消灌帧 -> 广播最后一帧 `connected:false` 健康帧 -> 关服务器 ->
join UIA 线程 -> 返回 0。
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import logging
import signal
import sys
import threading
import time
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from dafeiyu.config import ConfigError, Settings, WeChatConfig, load_settings

from .server import BridgeServer, pump_frames

__all__ = ["main", "run"]

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_ENV_FILE = ".env"

# 反挂死护栏：正常指令的每一个 await 都有显式超时（<= 5s）。
_AWAIT_TIMEOUT = 5.0
# 发送指令单独放宽：Sender 默认 20s 节奏，5s 会在消息仍要发出时先回 timeout，
# 客户端按 §7.6 换新 id 重试就会重复发送。客户端 _SEND_TIMEOUT 必须更大。
_SEND_ACTIONS = ("send_text", "send_at")
#: 帧泵连续空转多少次才停泵。1 次 = 单次慢轮询就自杀，正是之前「帧源超时即
#: return」导致假活的根源；3 次（≈ 6 × poll_seconds）给慢轮询留足余量，真卡死
#: 时仍会退出，由 run() 的完成回调请求停机、交给 supervisor 重启。
_PUMP_STALL_LIMIT = 3

#: 本地库预热任务：只用来阻止被 GC，退出时不强等（扫描线程是 daemon 语义）。
_WARMUP_TASKS: set[asyncio.Future[Any]] = set()

logger = logging.getLogger(__name__)

#: 可注入的指令处理器：见 `BridgeServer.CommandHandler`。
CommandHandler = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any] | None]]

#: 服务器工厂：参数与 `BridgeServer.__init__` 一致，便于测试与 PART B 复用。
ServerFactory = Callable[..., Any]

FrameSource = Iterable[dict[str, Any]] | AsyncIterable[dict[str, Any]]

#: 轮询循环工厂：`(wechat, driver) -> PollingLoop`（鸭子类型，测试可注入）。
PollerFactory = Callable[[WeChatConfig, Any], Any]

#: 发送器工厂：`(wechat, poller, driver) -> Sender`（测试与停机缝可注入）。
SenderFactory = Callable[[WeChatConfig, Any, Any], Any]


class _CommandError(Exception):
    """指令参数/会话解析失败；``code`` 直接作为回执的 error_code。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _UiThreadDriver:
    """把 Sender 的驱动调用路由到轮询循环的单 STA/UIA 线程。

    Sender 的 worker 是普通线程；桌面微信的 UIA 调用必须在 `PollingLoop.ui` 的
    COM/STA 线程上执行，否则会抛 COM 初始化错误。本代理只做一件事：把
    `open_chat/send_text/snapshot` 投进 UI 线程的发送优先队列并等结果。

    `Sender.__init__` 会探测 ``force_global_config``（其次回退到
    ``_force_global_config`` + ``_pyweixin_or_none``）；两条路径都投到 UI 线程，
    Sender 不会再在自己的构造线程上 import pyweixin。
    """

    #: 单次 UI 调用的本地等待上限；必须小于服务器/客户端对发送的整体超时。
    _TIMEOUT = 60.0

    def __init__(self, poller: Any, raw: Any) -> None:
        self._poller = poller
        self._raw = raw

    def open_chat(self, name: str) -> Any:
        return self._call("open_chat", lambda: self._raw.open_chat(name))

    def send_text(self, name: str, text: str, delay: float) -> Any:
        return self._call(
            "send_text", lambda: self._raw.send_text(name, text, delay)
        )

    def snapshot(self, handle: Any) -> Any:
        return self._call("snapshot", lambda: self._raw.snapshot(handle))

    def _call(self, label: str, fn: Callable[[], Any]) -> Any:
        from .driver import DriverError

        # 桥已在停机：不要再往 UI 线程投递新 op。
        if getattr(self._poller, "stopped", False):
            raise DriverError("aborted", "bridge is stopping")
        ui = getattr(self._poller, "ui", None)
        if ui is None or not getattr(ui, "running", False):
            raise DriverError("uia_unavailable", f"{label}: UIA 线程未运行")
        # 死锁护栏：`request_send(lambda: sender.submit(...))` 会在 UI 线程里再等
        # UI 线程，必须立刻失败而不是挂住。
        if threading.get_ident() == getattr(ui, "thread_ident", None):
            raise DriverError("deadlock_guard", f"{label}: 不能从 UIA 线程再投递")
        try:
            enqueue = getattr(self._poller, "enqueue_send", None)
            if callable(enqueue):
                future = enqueue(fn)
            else:
                future = ui.submit(fn, kind="send")
        except RuntimeError as exc:
            raise DriverError("uia_unavailable", f"{label}: {exc}") from exc
        try:
            return future.result(timeout=self._TIMEOUT)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise DriverError("timeout", label) from exc
        except concurrent.futures.CancelledError as exc:
            raise DriverError("aborted", label) from exc

    def force_global_config(self) -> None:
        force = getattr(self._raw, "force_global_config", None)
        if not callable(force):
            return
        self._call("force_global_config", force)

    def _pyweixin_or_none(self) -> Any:
        getter = getattr(self._raw, "_pyweixin_or_none", None)
        if not callable(getter):
            return None
        return self._call("pyweixin", getter)

    def _force_global_config(self, wx: Any) -> None:
        force = getattr(self._raw, "_force_global_config", None)
        if callable(force):
            force(wx)

    def last_synthetic_input_age(self) -> float | None:
        """桥自己注入键鼠是多久以前；纯读模块里的时间戳，不碰 UIA。

        `sender` 的前台保护会在 worker 线程里比对「最后一次系统输入」和这个值，
        用来区分「人刚按的」和「我们自己刚按的」。这里不能走 `_call`（那会把
        worker 线程的查询再投回 UIA 线程，纯读没有这个必要）。
        """
        probe = getattr(self._raw, "last_synthetic_input_age", None)
        if not callable(probe):
            return None
        try:
            value = probe()
        except Exception:  # noqa: BLE001 - 读不到就按「不是自己按的」处理
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)


def _default_sender_factory(wechat: WeChatConfig, poller: Any, driver: Any) -> Any:
    """生产发送器：惰性 import Sender，驱动换成 STA 代理。

    不传 `health=`：HealthProbe 会直接调 pyweixin，必须在 UI 线程，P0 先走
    `desktop_state()`/前台检查的实时路径（见计划 P1 项）。

    `outbound` 决定传输层：`uia`（默认）保持历史行为；`hook` / `auto` 由
    `outbound.build_transport()` 构造（auto 的 UIA 回落复用同一 STA 代理）。
    """
    from .outbound import build_transport
    from .sender import Sender, UiaTransport

    uia_driver = _UiThreadDriver(poller, raw=driver)

    def uia_factory() -> Any:
        return UiaTransport(wechat, driver=uia_driver)

    transport = build_transport(
        wechat,
        uia_driver=uia_driver,
        poller=poller,
        uia_factory=uia_factory,
    )
    return Sender(wechat, driver=uia_driver, transport=transport)


def _default_receipt_verifier(wechat: WeChatConfig, poller: Any) -> Any | None:
    """发送后回读本地库确认送达；本地库没接或关闭校验时返回 None。"""
    if not bool(getattr(wechat, "send_verify", True)):
        return None
    source = getattr(poller, "localdb", None)
    if source is None:
        return None
    from .receipt import ReceiptVerifier

    try:
        return ReceiptVerifier(
            source,
            timeout=float(getattr(wechat, "send_verify_timeout_seconds", 8.0)),
            interval=float(getattr(wechat, "send_verify_interval_seconds", 0.5)),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("回执校验参数无效，跳过本地库确认：%s", exc)
        return None


def _failed(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "delivered": "failed",
        "message_id": None,
    }


def _validate_send(
    action: str, params: dict[str, Any]
) -> tuple[str, str, tuple[str, ...]]:
    """校验 send_text / send_at 的入参，返回 ``(chat_id, text, at)``。"""
    chat_id = params.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id.strip().startswith("wxg:"):
        raise _CommandError("bad_params")
    text = params.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _CommandError("bad_params")
    at: tuple[str, ...] = ()
    if action == "send_at":
        raw = params.get("at")
        if not isinstance(raw, (list, tuple)):
            raise _CommandError("bad_params")
        if any(not isinstance(item, str) or not item.strip() for item in raw):
            raise _CommandError("bad_params")
        at = tuple(item.strip() for item in raw)
    return chat_id.strip(), text, at


def _resolve_chat_name(poller: Any, chat_id: str) -> str:
    """`wxg:<alias>` -> 微信显示名；只读注册表，绝不 resolve 出新别名。"""
    alias = chat_id[4:].strip()
    if not alias or alias == "self":
        raise _CommandError("bad_params")
    registry = getattr(poller, "registry", None)
    if registry is None:
        raise _CommandError("chat_name_unresolved")
    peer = registry.by_alias(alias)
    name = str(getattr(peer, "display_name", "") or "").strip()
    if peer is None or not getattr(peer, "live", False) or not name:
        raise _CommandError("chat_name_unresolved")
    # 白名单只有面板一个来源：poller 有 `allows_chat` 就交给它判定
    # （空白名单 = 不限群）。没有这个接缝的旧测试替身，注册表已经确认是
    # 存活群会话，直接放行。
    gate = getattr(poller, "allows_chat", None)
    if callable(gate):
        allowed = bool(gate(chat_id))
    else:
        allowed = getattr(peer, "kind", "group") == "group"
    if not allowed:
        raise _CommandError("chat_not_allowed")
    return name


def _receipt_from_result(result: Any) -> dict[str, Any]:
    """`SendResult` -> 协议回执（§4 的四个字段）。"""
    ok = bool(getattr(result, "ok", False))
    code = getattr(result, "error_code", None)
    delivered = str(getattr(result, "delivered", "failed") or "failed")
    message_id = getattr(result, "message_id", None)
    if ok:
        return {
            "ok": True,
            "error_code": None,
            "delivered": delivered,
            "message_id": message_id,
        }
    # 已打进输入框但回轮询没看到气泡：send_text 已经成功，按 typed 回执，
    # 映射成失败会诱发客户端换 id 重发（协议 §7.6）。
    if code == "not_observed":
        return {
            "ok": True,
            "error_code": "not_observed",
            "delivered": "typed",
            "message_id": None,
        }
    if code in ("not_friend", "chat_not_found"):
        code = "chat_name_unresolved"
    return {
        "ok": False,
        "error_code": code or "send_failed",
        "delivered": "failed",
        "message_id": None,
    }


async def run(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    driver_factory: Callable[[], Any] | None = None,
    server_factory: ServerFactory | None = None,
    command_handler: CommandHandler | None = None,
    frame_source: FrameSource | None = None,
    poller_factory: PollerFactory | None = None,
    sender_factory: SenderFactory | None = None,
) -> int:
    """加载配置、fail-closed 检查、起服务器、轮询、等待停机。

    停机信号：SIGINT（进程收到 Ctrl+C）或动作 == "stop" 的指令；两者都会
    先中止发送/轮询，再广播一帧最终健康帧，join UIA 线程、关服务器并返回
    exit code 0。若帧泵在轮询仍应继续时意外结束（帧源耗尽/异常），请求停机
    并返回 1，让 supervisor 重启进程；拒绝启动时抛 `ConfigError`（消息含具体
    配置键名），由 `main` 兜底打印并退出非 0。

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
    sender_holder: list[Any] = []
    #: 回执校验器：本地库可用时把 typed 升级成 observed（run 里按配置构造）。
    receipt_verifier: Any | None = None
    # 必须早于 server / resume 闭包初始化，否则 resume 可能撞上 UnboundLocalError。
    poller: Any | None = None
    pump_task: asyncio.Task[None] | None = None
    # 帧泵意外死亡 = 入站通道静默失效（进程还活着但永不推帧）。这种状态必须在
    # 本进程内可见：置标志 -> 请求停机 -> 非 0 退出，让 supervisor 自动重启。
    pump_fatal = False

    def _signal_stop() -> None:
        # §7 / PART B：stop 的第一件事是中止发送（丢排队 job、停止新 UI op）。
        for sender in sender_holder:
            with contextlib.suppress(Exception):
                sender.abort()
        for poller in poller_holder:
            with contextlib.suppress(Exception):
                poller.abort_sends()
        if not stop.done():
            stop.set_result(None)

    def _on_pump_done(task: asyncio.Task[None]) -> None:
        nonlocal pump_fatal
        if task.cancelled() or stop.done():
            return
        # 注入 frame_source 的测试/替身路径没有 poller：帧源耗尽属于正常结束。
        if poller is None:
            return
        # 急停（kill switch）与主动停机已把 poller 置为 stopped，属于预期退出。
        if getattr(poller, "stopped", False):
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "微信桥帧泵异常退出，请求停机等待 supervisor 重启", exc_info=exc
            )
        else:
            logger.error("微信桥帧泵意外结束（帧源不再产出），请求停机等待 supervisor 重启")
        pump_fatal = True
        _signal_stop()

    def _start_pump(source: FrameSource) -> asyncio.Task[None]:
        pump = _spawn_pump(server, source, wechat)
        pump.add_done_callback(_on_pump_done)
        return pump

    async def _resume_bridge() -> dict[str, Any]:
        nonlocal pump_task
        raw_kill = str(getattr(wechat, "kill_switch_file", "") or "")
        if raw_kill and Path(raw_kill).exists():
            return {"ok": False, "error_code": "kill_switch_active"}
        if poller is None:
            return {"ok": False, "error_code": "not_resumable"}
        reset = getattr(poller, "reset_for_resume", None)
        if not callable(reset) or not reset():
            return {"ok": False, "error_code": "not_resumable"}
        if pump_task is None or pump_task.done():
            pump_task = _start_pump(poller.frames())
        logger.info("微信桥收到 resume 指令，轮询已恢复")
        return {"ok": True}

    async def _builtin_send(
        action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """生产发送路径：解析 alias -> 提交 Sender -> 映射回执 -> 本地库确认。"""
        try:
            chat_id, text, at = _validate_send(action, params)
            if at:
                # P0 不支持定向 @：明确失败，绝不悄悄丢掉 @ 当普通消息发。
                return _failed("unsupported_at")
            name = _resolve_chat_name(poller, chat_id)
            if not sender_holder:
                raise _CommandError("not_ready")
            sender = sender_holder[0]
            started_at = time.time()
            # 只等 future：驱动调用在 Sender worker -> UiThread 上，事件循环
            # 线程绝不直接碰 UIA。
            result = await asyncio.wrap_future(sender.submit_async(name, text))
        except asyncio.CancelledError:
            raise
        except _CommandError as exc:
            return _failed(exc.code)
        except Exception:
            logger.exception("微信桥发送异常 action=%s", action)
            return _failed("internal_error")
        receipt = _receipt_from_result(result)
        verifier = receipt_verifier
        if verifier is not None:
            # 未确认不回失败：客户端换 id 重发会造成重复消息（协议 §7.6）。
            receipt = await verifier.apply(name, text, started_at, receipt)
        return receipt

    async def _handle_command(
        action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any] | None:
        if action == "resume":
            return await _resume_bridge()
        if command_handler is not None:
            return await command_handler(action, params, command_id)
        if action not in _SEND_ACTIONS:
            return None
        return await _builtin_send(action, params)

    server = (server_factory or BridgeServer)(
        wechat.bridge_host,
        wechat.bridge_port,
        bridge_token=wechat.bridge_token,
        command_handler=_handle_command,
        on_stop_requested=_signal_stop,
    )
    bound_port = await asyncio.wait_for(server.start(), _AWAIT_TIMEOUT)
    logger.info("微信桥已监听 %s:%s", wechat.bridge_host, bound_port)

    # 初始 health 帧与帧源无关：注入纯 message 帧源时也必须先有健康快照，
    # 否则客户端 `_health_connected` 一直是 False（wechat.py:168/291-293）。
    # 探测不可用时按 fail-closed 降级（logged_in False / desktop no_wechat）。
    # 生产路径的探测在 poller 启动后投到 UIA 线程执行；只有注入 frame_source
    # 的测试/替身路径（无 poller）用 fail-closed 帧，不做任何驱动调用。
    startup_health: dict[str, Any] | None = None

    try:
        if frame_source is None:
            # UI 线程必须在服务器监听之后启动；它只通过 PollingLoop 的队列被使用。
            poller = (poller_factory or _default_poller_factory)(wechat, driver)
            poller_holder.append(poller)
            await asyncio.wait_for(poller.start(), _AWAIT_TIMEOUT)
            _warmup_localdb(poller)
            # 默认指令处理器 = 内置发送路径；注入 handler 时由调用方全权接管，
            # 注入 frame_source 时没有轮询循环，也就没有可解析的 alias。
            if command_handler is None:
                sender = (sender_factory or _default_sender_factory)(
                    wechat, poller, driver
                )
                sender_holder.append(sender)
                receipt_verifier = _default_receipt_verifier(wechat, poller)
            startup_health = await _startup_health_frame(poller, wechat, driver)
            source = poller.frames()
        else:
            startup_health = _fallback_health_frame(connected=True)
            source = frame_source

        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.broadcast(startup_health), _AWAIT_TIMEOUT)

        pump_task = _start_pump(source)
        await _wait_for_stop(stop, loop)
    finally:
        # 1) 让轮询循环和排队中的发送一起停手（幂等，可在异常路径重复调用）。
        #    先 abort 发送器：丢弃 Sender 队列里还没开始的 job，再停 UIA 侧的发送队列。
        for sender in sender_holder:
            with contextlib.suppress(Exception):
                sender.abort()
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
        # 3.5) join 发送器 worker：abort() 之后 close() 仍会 join（幂等）。
        for sender in sender_holder:
            with contextlib.suppress(Exception):
                sender.close(timeout=_AWAIT_TIMEOUT)
        # 4) 最终健康帧：**无条件**发出（协议 §7 / 计划 task 18 都要求停机时
        #    自报断链）。优先用最近快照；一帧 health 都没推过时用启动帧兜底，
        #    绝不因为 frame_source 只产消息帧就跳过停机通知。
        getter = getattr(server, "latest_health", None)
        # latest_health 是 property（取到的是 dict），不是方法；两种形态都兼容。
        snapshot = getter() if callable(getter) else getter
        final = dict(snapshot) if isinstance(snapshot, dict) else dict(
            startup_health or _fallback_health_frame(connected=True)
        )
        final.setdefault("type", "health")
        final.setdefault("channel", "wechat")
        final["v"] = 1
        final["connected"] = False  # 停机帧必须自报断链，别留 connected:true
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.broadcast(final), _AWAIT_TIMEOUT)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.stop(), _AWAIT_TIMEOUT)
            logger.info("微信桥已关闭")
    return 1 if pump_fatal else 0


def _health_frame(
    wechat: WeChatConfig, *, connected: bool, driver_impl: Any | None = None
) -> dict[str, Any]:
    """一帧 v1 health：优先用 `health.probe` 的真实快照，探测不可用时降级。

    降级帧是 fail-closed 的：`logged_in=False` + `desktop="no_wechat"`，客户端
    据此停止发送（wechat.py:85-90 的 `_health_connected` 与 send 闸门），
    绝不为了「看起来健康」而伪造 interactive/logged_in。

    ``driver_impl`` 传生产驱动的真实实现；调用方负责把它跑在 UI 线程上。
    """
    try:
        from .health import probe

        frame = probe(wechat, connected=connected, driver_impl=driver_impl)
        if isinstance(frame, dict):
            snapshot = dict(frame)
            snapshot["v"] = 1
            snapshot.setdefault("type", "health")
            snapshot.setdefault("channel", "wechat")
            snapshot["connected"] = bool(connected)
            return snapshot
    except Exception:
        logger.warning("微信桥健康探测不可用，按 fail-closed 降级", exc_info=True)
    return _fallback_health_frame(connected=connected)


def _fallback_health_frame(*, connected: bool) -> dict[str, Any]:
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


async def _startup_health_frame(
    poller: Any, wechat: WeChatConfig, driver_impl: Any
) -> dict[str, Any]:
    """在 UI 线程上跑启动探测：UIA/ctypes 调用绝不留事件循环线程。"""
    def _probe() -> dict[str, Any]:
        return _health_frame(wechat, connected=True, driver_impl=driver_impl)

    try:
        return await asyncio.wait_for(poller.request_send(_probe), _AWAIT_TIMEOUT)
    except Exception:
        logger.warning("启动健康探测失败，按 fail-closed 降级", exc_info=True)
        return _fallback_health_frame(connected=True)


def _pump_idle_timeout(poll_seconds: float) -> float:
    """灌帧空转上限：必须大于一个完整轮询间隔（默认 8s）才不会误停泵。

    轮询循环每群之后都产出一帧 health，所以最长帧间隔是「单群耗时 + 轮询
    间隔」。单群最坏情况 = 开窗 5s + 快照 5s（两个 UIA 调用各自的超时），
    因此下限取 3 倍 await 超时；同时给两倍 `poll_seconds` 余量。
    """
    try:
        interval = float(poll_seconds)
    except (TypeError, ValueError):
        interval = 8.0
    return max(_AWAIT_TIMEOUT * 3.0, interval * 2.0)


async def _pump_until_stopped(
    server: Any, source: FrameSource, *, idle_timeout: float | None = None
) -> None:
    await pump_frames(
        server,
        source,
        idle_timeout=idle_timeout,
        stall_limit=_PUMP_STALL_LIMIT,
    )


def _spawn_pump(
    server: Any, source: FrameSource, wechat: WeChatConfig
) -> asyncio.Task[None]:
    """创建灌帧任务；首轮与 resume 重建共用同一入口。"""
    return asyncio.create_task(
        _pump_until_stopped(
            server,
            source,
            idle_timeout=_pump_idle_timeout(wechat.poll_seconds),
        ),
        name="wechat-bridge-pump",
    )


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
    if not wechat.account_nickname:
        raise ConfigError(
            "gateway.wechat.account_nickname 为空：无法判定是否被 @"
        )
    mode = str(getattr(wechat, "outbound", "uia") or "uia").strip().lower()
    if mode in ("hook", "auto") and not str(
        getattr(wechat, "hook_url", "") or ""
    ).strip():
        raise ConfigError(
            "gateway.wechat.outbound 是 hook/auto 但 hook_url 为空："
            "先填 hook 服务地址，或把 outbound 改回 uia"
        )
    # hook 没有 UIA 兜底：本地库不可用就永远解析不出群 username，所有发送都会
    # 失败。这种配置应该在启动时拒绝，而不是每条消息都回 chat_name_unresolved。
    if mode == "hook" and not bool(getattr(wechat, "local_db_enabled", False)):
        raise ConfigError(
            "gateway.wechat.outbound 是 hook 但 local_db_enabled 是 false："
            "hook 需要本地库把群显示名解析成 xxx@chatroom"
        )


def _default_driver_factory() -> Any:
    """默认驱动门面：pyweixin 的 import 只在函数体内（help 路径保持轻）。"""
    from . import driver

    return driver


def _default_poller_factory(wechat: WeChatConfig, driver: Any) -> Any:
    """默认轮询循环：`PollingLoop` 的 import 也留在函数体内，保持入口轻量。"""
    from .panel import PanelGroups
    from .poller import DEFAULT_ALIGN_STATE, PollingLoop

    localdb = _build_localdb_source(wechat)
    # 范围以面板白名单为准（热加载，面板改完一个轮询间隔内生效）。
    return PollingLoop(
        wechat,
        driver,
        localdb=localdb,
        panel=PanelGroups().whitelist,
        # 对齐窗口落盘：桥崩溃重启后把停机期间的新消息补出来。
        align_state_path=DEFAULT_ALIGN_STATE,
    )


def _build_localdb_source(wechat: WeChatConfig) -> Any | None:
    """按 `gateway.wechat.local_db_enabled` 建本地库帧源；失败只降级不拒绝启动。"""
    if not bool(getattr(wechat, "local_db_enabled", False)):
        return None
    try:
        from .localdb.source import LocalDbSource
    except Exception as exc:  # noqa: BLE001 - 包缺失时退回 UIA 路线
        logger.warning("本地消息库模块不可用，回落 UIA 入站：%s", exc)
        return None
    try:
        return LocalDbSource(
            db_root=str(getattr(wechat, "local_db_root", "") or "") or None,
            self_display_name=str(getattr(wechat, "account_nickname", "") or ""),
            refresh_seconds=float(getattr(wechat, "local_db_refresh_seconds", 1.0)),
        )
    except Exception as exc:  # noqa: BLE001 - 构造失败同样只降级
        logger.warning("本地消息库初始化参数无效，回落 UIA 入站：%s", exc)
        return None


def _warmup_localdb(poller: Any) -> None:
    """后台预热：扫进程内存提密钥可能要几十秒，绝不阻塞轮询启动。"""
    source = getattr(poller, "localdb", None)
    prepare = getattr(source, "prepare_async", None)
    if not callable(prepare):
        return
    task = asyncio.ensure_future(prepare())
    _WARMUP_TASKS.add(task)
    task.add_done_callback(_WARMUP_TASKS.discard)


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
