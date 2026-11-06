"""微信桥轮询循环（PART B）：专用 STA/UIA 线程 + 轮询 + 杀开关 + 优雅停机。

一切触到 pyweixin / pywinauto / comtypes / pythoncom 的 import 都在函数体内；
没装桌面依赖时本模块照样能 import（`--help` 路径不受影响），UIA 线程退化成
一个普通的串行任务线程（COM 初始化是带日志的 no-op 兜底）。

线程模型
--------
桌面微信的 UIA 自动化**必须**在初始化成 STA 的单一线程里串行执行，
所以本模块只有一个 :class:`UiThread`：

-   asyncio 线程通过 :meth:`UiThread.submit` 投递纯同步 job，拿回
    ``concurrent.futures.Future``；调用方用 ``asyncio.wrap_future`` 桥回事件环；
-   job 分两个队列：普通（轮询）与 ``send`` 优先队列。worker 每处理完一个 job
    都会先看优先队列，因此**一条排队中的 send 会插在下一个轮询 op 之前**执行；
-   轮询循环只通过这个线程调用驱动门面，绝不在 asyncio 线程里内联调用
    driver 函数（驱动是 UIA，跨线程调用会炸/卡死）。

杀开关（`gateway.wechat.kill_switch_file`，相对路径按进程 cwd 解析）
--------------------------------------------------------------------
每一**轮**开始时先看这个文件；存在则立刻停止轮询、推一帧
``connected:false / degraded:true / reason 含 kill_switch`` 的健康帧，然后
结束帧源。语义选择：**这是单向、不可自动恢复的停机**——删掉文件也不会让
轮询偷偷恢复；要重新开始必须重启桥进程。理由：杀开关是人工「立刻别动微信」
的最后一道闸，若删文件就复活，半夜误删一次就会在没有监督的情况下重新操作
桌面。轮询循环结束后桥服务器仍然在线（客户端还能探活），只是不再读屏。

停止语义（`action:"stop"` / SIGINT）
-----------------------------------
``action:"stop"`` 先经 `__main__` 的 ``on_stop_requested`` 调用
:meth:`PollingLoop.abort_sends`：丢掉所有**排队中**的发送 job（in-flight 的
一次 pyweixin 调用无法从外部打断，只能保证之后不再发起新的 UI op），然后进入
与 SIGINT 完全相同的优雅停机：停轮询、取消灌帧、广播最后一帧
``connected:false`` 健康帧、关服务器、join UIA 线程、返回退出码 0。

本模块不 import `sender.py`：发送任务由后续任务接线，PART B 只提供
:meth:`PollingLoop.enqueue_send` / :meth:`PollingLoop.request_send` 这个优先级
接缝。`recall` 事件无法从 8 个驱动函数里推导（快照 diff 只能得到新消息与
resync），所以本轮不伪造 recall，留给有撤回数据的后续任务。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import importlib
import inspect
import json
import logging
import os
import queue
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .align import SessionAligner
from ...hotconfig import WeChatRuntime

logger = logging.getLogger(__name__)

__all__ = [
    "BACKFILL_LIMIT",
    "INBOUND_LOCALDB_REASON",
    "KILL_SWITCH_REASON",
    "MAX_AWAIT",
    "PollingLoop",
    "UiThread",
]


def _alias_of(chat_id: str) -> str | None:
    """``wxg:<alias>`` -> ``alias``；其它通道/空值返回 None。"""
    value = str(chat_id or "").strip()
    if not value.startswith("wxg:"):
        return None
    alias = value[4:].strip()
    return alias or None


def _accepts_kwarg(factory: Callable[..., Any], name: str) -> bool:
    """工厂签名是否接某个关键字参数：旧测试替身没有就不硬塞。"""
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == name
        for parameter in signature.parameters.values()
    )


def _accepts_user_alias(factory: Callable[..., Any]) -> bool:
    """工厂签名是否接 ``user_alias``。"""
    return _accepts_kwarg(factory, "user_alias")


def _snapshot_call(
    source: Any, group: str, limit: int | None
) -> dict[str, Any] | None:
    """调本地库快照；``limit`` 只在补读时给，旧替身没有这个参数就按原样调。"""
    snapshot = getattr(source, "snapshot", None)
    if not callable(snapshot):
        return None
    call = cast(Callable[..., Any], snapshot)
    result = (
        call(group, limit=limit)
        if limit is not None and _accepts_kwarg(call, "limit")
        else call(group)
    )
    return result if isinstance(result, dict) else None

#: 反挂死护栏：本模块的每一个 await 都不超过这个秒数（与 PART A 一致）。
MAX_AWAIT = 5.0

#: 杀开关健康帧的稳定 reason 片段。
KILL_SWITCH_REASON = "kill_switch"

#: hook 模式下入站读不到本地库时的稳定 reason 片段（面板显示「已连接（降级）」）。
INBOUND_LOCALDB_REASON = "inbound_localdb_unavailable"

#: 本地库恢复后一次补读的上限（条）。断档期间的消息都在库里，读宽一点才能和
#: 上一窗口接上；超过这个量说明缺口太大，按 resync 处理，不无限回灌。
BACKFILL_LIMIT = 200

#: 默认的杀开关相对路径（与 `WeChatConfig.kill_switch_file` 默认值一致）。
DEFAULT_KILL_SWITCH = "data/wechat.stop"

#: 对齐窗口的落盘点：桥崩溃重启后靠它把停机期间的新消息 diff 出来。
DEFAULT_ALIGN_STATE = "data/wechat-bridge/align-state.json"

_JOB_POLL = "poll"
_JOB_SEND = "send"


@dataclass
class _Job:
    fn: Callable[[], Any]
    future: concurrent.futures.Future[Any]
    kind: str = _JOB_POLL


def _init_com() -> str:
    """在 UIA 线程内部把 COM 初始化成 STA；返回实际使用的后端名（可为空串）。

    pythoncom 优先（pywin32 的 `CoInitialize` 默认就是 apartment-threaded），
    退到 comtypes；两者都缺席时只记一条 debug 并继续跑普通线程（测试环境就是
    这样，驱动是注入的假门面，不需要 COM）。
    """
    try:
        pythoncom = importlib.import_module("pythoncom")
    except Exception:  # noqa: BLE001 - 没装 pywin32 是正常情况
        pythoncom = None
    if pythoncom is not None:
        try:
            pythoncom.CoInitialize()
            return "pythoncom"
        except Exception as exc:  # noqa: BLE001 - 已初始化/线程模型冲突都降级
            logger.debug("pythoncom.CoInitialize 失败，尝试 comtypes：%s", exc)

    try:
        comtypes = importlib.import_module("comtypes")
    except Exception:  # noqa: BLE001 - 桌面依赖未安装
        logger.debug("pythoncom/comtypes 都不可用：UIA 线程按普通串行线程运行")
        return ""
    try:
        comtypes.CoInitialize()
        return "comtypes"
    except Exception as exc:  # noqa: BLE001
        logger.debug("comtypes.CoInitialize 失败：%s", exc)
        return ""


def _fini_com(backend: str) -> None:
    if not backend:
        return
    try:
        module = importlib.import_module(backend)
        module.CoUninitialize()
    except Exception as exc:  # noqa: BLE001 - 收摊失败不该影响退出
        logger.debug("COM 反初始化失败（%s）：%s", backend, exc)


class UiThread:
    """唯一的 STA/UIA 工作线程：一次只执行一个 job，发送 job 优先。"""

    def __init__(self, *, name: str = "wechat-uia", com: str | None = None) -> None:
        self.name = name
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._priority: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._stopping = threading.Event()
        #: 测试可强制指定 COM 后端；默认走惰性探测。
        self._forced_com = com
        self._backend = ""
        self.thread_ident: int | None = None

    # ------------------------------------------------------------- 状态
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def com_backend(self) -> str:
        return self._backend

    # ----------------------------------------------------------- 生命周期
    def start(self) -> None:
        """启动线程并等它完成 COM 初始化；重复调用幂等。"""
        with self._lock:
            if self.running:
                return
            self._stopping.clear()
            self._ready.clear()
            self._done.clear()
            thread = threading.Thread(
                target=self._run,
                name=self.name,
                daemon=True,
            )
            self._thread = thread
            thread.start()
        # 只等 COM 初始化，正常情况下是微秒级；给 5s 上限防止挂死。
        if not self._ready.wait(MAX_AWAIT):
            raise RuntimeError("微信桥 UIA 线程在 5s 内没有完成启动")

    def stop(self, *, timeout: float = MAX_AWAIT) -> bool:
        """投停止哨兵、join（带超时）、取消剩余 job；幂等。

        返回线程是否已经退出。in-flight 的 pyweixin 调用无法从外部打断，
        join 超时后线程保持 daemon（不会拖住进程退出），但会留一条 warning。
        """
        with self._lock:
            thread = self._thread
            if thread is None:
                self._cancel_remaining()
                return True
            self._stopping.set()
            self._priority.put(None)
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("微信桥 UIA 线程在 %.1fs 内没有退出（可能有 UI 调用卡住）", timeout)
            return False
        self._cancel_remaining()
        with self._lock:
            if self._thread is thread:
                self._thread = None
        return True

    def _run(self) -> None:
        self.thread_ident = threading.get_ident()
        try:
            self._backend = self._forced_com or _init_com()
        except Exception:
            logger.exception("微信桥 UIA 线程 COM 初始化异常")
            self._backend = ""
        self._ready.set()
        try:
            while True:
                job = self._next_job()
                if job is None:
                    break
                # 已被取消（例如 stop 时 abort_sends）就跳过，不执行。
                if not job.future.set_running_or_notify_cancel():
                    continue
                try:
                    result = job.fn()
                except BaseException as exc:  # noqa: BLE001 - 异常送回调用方
                    job.future.set_exception(exc)
                else:
                    job.future.set_result(result)
        finally:
            _fini_com(self._backend)
            self._done.set()

    def _next_job(self) -> _Job | None:
        while True:
            # 每个 job 边界先看发送队列：send 永远插在下一个轮询 op 之前。
            try:
                item = self._priority.get_nowait()
            except queue.Empty:
                pass
            else:
                if item is None:
                    return None
                return item
            try:
                return self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stopping.is_set():
                    return None
                continue

    # --------------------------------------------------------------- 投递
    def submit(self, fn: Callable[[], Any], *, kind: str = _JOB_POLL) -> concurrent.futures.Future[Any]:
        if not self.running:
            raise RuntimeError("微信桥 UIA 线程未启动")
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        job = _Job(fn=fn, future=future, kind=kind)
        (self._priority if kind == _JOB_SEND else self._queue).put(job)
        return future

    def cancel_pending(self, kind: str = _JOB_SEND) -> int:
        """取消某个队列里还没被 worker 取走的 job；返回取消数量。"""
        target = self._priority if kind == _JOB_SEND else self._queue
        keep: list[_Job | None] = []
        cancelled = 0
        while True:
            try:
                item = target.get_nowait()
            except queue.Empty:
                break
            if item is None:
                keep.append(item)
            elif item.kind == kind:
                item.future.cancel()
                cancelled += 1
            else:
                keep.append(item)
        for item in keep:
            target.put(item)
        return cancelled

    def _cancel_remaining(self) -> None:
        for target in (self._priority, self._queue):
            while True:
                try:
                    item = target.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item.future.cancel()


def _default_probe(wechat: Any) -> Callable[[bool], dict[str, Any]]:
    """默认健康帧来源：`health.probe` 只在真正出帧时才 import/调用。"""
    from .health import probe as health_probe

    def _probe(connected: bool) -> dict[str, Any]:
        return health_probe(wechat, connected=connected)

    return _probe


class PollingLoop:
    """按面板白名单轮询微信群并把事件帧交给调用方（sink / async 迭代器）。

    参数都是鸭子类型，测试可以注入：假驱动、假 probe、假 registry、临时
    kill-switch 路径。
    """

    def __init__(
        self,
        wechat: Any,
        driver: Any,
        *,
        ui: UiThread | None = None,
        registry: Any | None = None,
        probe: Callable[[bool], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
        aligner_factory: Callable[..., SessionAligner] = SessionAligner,
        poll_seconds: float | None = None,
        kill_switch_path: str | Path | None = None,
        localdb: Any | None = None,
        panel: Callable[[], frozenset[str] | None] | None = None,
        align_state_path: str | Path | None = None,
        private_enabled: bool | None = None,
        runtime: Callable[[], WeChatRuntime | None] | None = None,
    ) -> None:
        self.wechat = wechat
        self.driver = driver
        # 本地库帧源（可选）：有它且 `available` 时入站直接读解密镜像，
        # `open_chat`/`snapshot` 只作为降级路径；发送永远走 UIA。
        self.localdb = localdb
        # 面板白名单（可选）：返回 `{"wxg:<alias>", ...}`，None = 读不出来。
        # 面板是用户的唯一操作面：非空只碰列出的群，空 = 不限制（与面板的
        # `empty_means:"all"` 一致）。读不出来时 fail-closed：不轮询、不发送。
        self._panel = panel
        self._panel_none_warned = False
        # 微信私聊：默认关闭。关闭时不枚举好友、面板里列了也不轮询，
        # `allows_chat` 也不放行好友出站；开启后才把好友纳入范围。
        # `private_enabled` 参数仍是启动默认值：热开关（`runtime`）读不出来时
        # 的 fallback（P1-6）。
        raw_private = private_enabled
        if raw_private is None:
            raw_private = bool(getattr(wechat, "private_enabled", False))
        self.private_enabled = bool(raw_private)
        # 私聊运行时开关（可选）：返回 WeChatRuntime 或 None。每次现取，
        # 面板改 `wechat.private_enabled/reply/allow/deny` 一个轮询周期内生效。
        self._runtime = runtime
        self.ui = ui if ui is not None else UiThread()
        self._owns_ui = ui is None
        if registry is None:
            from .identity import PeerRegistry  # 只在真跑时加载身份表

            registry = PeerRegistry()
        self.registry = registry
        self._probe = probe if probe is not None else _default_probe(wechat)
        self._clock = clock
        self._aligner_factory = aligner_factory
        # 出站后端决定入站能不能碰桌面：`hook` 完全不依赖 UIA，回落
        # `open_chat`/`snapshot` 只会调 `Navigator.open_weixin()`（restore +
        # 置顶 + 居中）把微信窗口拽到前台，用户明确要的是「不打扰」。
        # 因此 hook 模式下本地库不可用时整轮跳过，绝不回落 UIA；
        # `uia`/`auto`（auto 可能真的回落到 UIA 发送）保持原行为。
        raw_outbound = str(getattr(wechat, "outbound", "uia") or "uia").strip()
        self.outbound = raw_outbound.lower() or "uia"
        self._allow_uia_fallback = self.outbound != "hook"
        self._uia_skip_warned = False
        # 断档期间跳过/读失败的会话：恢复后要用宽窗补读一次，把漏掉的消息补上。
        self._backfill_groups: set[str] = set()

        raw_poll = poll_seconds
        if raw_poll is None:
            raw_poll = getattr(wechat, "poll_seconds", 8.0)
        self.poll_seconds = max(0.0, float(raw_poll))
        # 未读门控：轮询 tick 只读一次侧边栏红点（~20ms），有未读才打开会话。
        # 门控只是省开销，绝不牺牲正确性：免打扰不计红点，所以每
        # `full_sweep_seconds` 仍全量扫一遍白名单。
        self.unread_gate = bool(getattr(wechat, "unread_gate", True))
        try:
            sweep = float(getattr(wechat, "full_sweep_seconds", 120.0))
        except (TypeError, ValueError):
            sweep = 120.0
        self.full_sweep_seconds = max(0.0, sweep)
        self._last_full_sweep = 0.0
        self._skip_scan_until = 0.0
        account_nickname = str(getattr(wechat, "account_nickname", "") or "")
        self._account_nickname = account_nickname
        # 群昵称/别名：@ 与引用回复判定都用这一组，不再只认账号昵称。
        raw_self = getattr(wechat, "self_nicknames", ()) or ()
        self._self_nicknames = tuple(
            dict.fromkeys(
                str(item).strip() for item in raw_self if str(item).strip()
            )
        )

        raw_kill = (
            kill_switch_path
            if kill_switch_path is not None
            else getattr(wechat, "kill_switch_file", DEFAULT_KILL_SWITCH)
        )
        self.kill_switch_path = Path(str(raw_kill or DEFAULT_KILL_SWITCH))

        self._aligners: dict[tuple[str, str], SessionAligner] = {}
        # 对齐窗口落盘：重启后 feed 回 aligner，停机期间的新消息不会丢。
        self.align_state_path = (
            Path(str(align_state_path)) if align_state_path else None
        )
        self._align_state = self._load_align_state()
        self._align_dirty = False
        # 上一次成功的健康帧：探测超时时用它兜底（尤其是 desktop 字段，
        # bot 侧据此决定能不能发送）。
        self._last_health: dict[str, Any] | None = None
        self._stopped = asyncio.Event()
        self._started = False
        self._rounds = 0

    # ------------------------------------------------------------- 状态
    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    @property
    def rounds(self) -> int:
        return self._rounds

    # ----------------------------------------------------------- 生命周期
    async def start(self) -> None:
        """启动 UIA 线程；重复调用幂等。由 `run()` 在服务器监听之后调用。"""
        if self._started:
            return
        self.ui.start()
        self._started = True

    def stop(self, *, timeout: float = MAX_AWAIT) -> bool:
        """信号化停止 + join UIA 线程（带超时）；幂等。"""
        self._stopped.set()
        self._started = False
        return self.ui.stop(timeout=timeout)

    def abort_sends(self) -> int:
        """`action:"stop"` 的第一步：停止轮询并丢掉排队中的发送 job。"""
        self._stopped.set()
        return self.ui.cancel_pending(_JOB_SEND)

    def reset_for_resume(self) -> bool:
        """删除急停文件后恢复轮询；文件仍在时返回 False（不改状态）。"""
        if self.kill_switch_path.exists():
            return False
        self._stopped.clear()
        # 恢复后第一轮必须全量：急停期间可能积压了消息，红点也可能已被清掉。
        self._last_full_sweep = 0.0
        self._skip_scan_until = 0.0
        if not self._allow_uia_fallback:
            # hook 停机期间没有轮询：恢复后按会话宽窗补读，别把积压消息漏掉。
            self._backfill_groups.update(self._scope())
        return True

    # ------------------------------------------------------------- 发送
    def enqueue_send(self, fn: Callable[[], Any]) -> concurrent.futures.Future[Any]:
        """把一次发送 op 投进优先队列；worker 会在下一个轮询 op 之前执行它。"""
        return self.ui.submit(fn, kind=_JOB_SEND)

    async def request_send(self, fn: Callable[[], Any]) -> Any:
        future = self.enqueue_send(fn)
        return await asyncio.wait_for(asyncio.wrap_future(future), MAX_AWAIT)

    # ------------------------------------------------------------- 轮询
    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        """帧源：每轮先查杀开关，再逐群轮询，每群之后一帧 health。"""
        await self.start()
        try:
            while not self._stopped.is_set():
                if self.kill_switch_path.exists():
                    # 急停时立刻丢掉排队中的发送：别让急停前排队的消息继续操作桌面。
                    self.abort_sends()
                    yield await self._kill_switch_frame()
                    logger.warning(
                        "检测到杀开关 %s：已单向停止轮询；删除文件后可用 resume 指令恢复，或重启桥进程",
                        self.kill_switch_path,
                    )
                    # 单向停机：标记为已停止，删掉文件也不会让循环复活。
                    self._stopped.set()
                    return

                if not self.ui.running:
                    logger.error("微信桥 UIA 线程已退出，轮询结束")
                    return

                self._rounds += 1
                async for frame in self._poll_round(await self._targets()):
                    yield frame

                if self._stopped.is_set():
                    return
                yield await self._health_frame(connected=True)
                if not await self._nap(self.poll_seconds):
                    return
        finally:
            self._started = False

    async def run(self, sink: Callable[[dict[str, Any]], Any]) -> None:
        """直接消费帧源的便利入口（sink 可以是同步或异步 callable）。"""
        async for frame in self.frames():
            result = sink(frame)
            if hasattr(result, "__await__"):
                await result

    async def _targets(self) -> tuple[str, ...]:
        """本轮该打开哪些群：未读门控 + 周期性全量兜底。

        空元组 = 没有任何未读，整轮跳过（只保留一帧 health）。门控关闭、驱动
        没有未读探测能力、或探测/扫描抛错时一律退回全量轮询——省开销可以，
        漏消息不行。免打扰会话不计入红点，所以 `full_sweep_seconds` 到点必全量。

        本地库可用时**不走红点门控**：读 SQLite 本来就不需要开窗，红点反而会
        漏掉免打扰群，直接返回本轮范围即可。
        """
        scope = self._scope()
        if self._uses_localdb():
            return scope
        if not self._allow_uia_fallback:
            # hook 模式：宁可这一轮什么都不读，也不去碰微信窗口。
            # 同时记下这些会话：本地库恢复后要宽窗补读，断档期间的消息不能丢。
            self._backfill_groups.update(scope)
            self._warn_uia_skip_once("本地库尚未就绪")
            return ()
        if not self.unread_gate:
            return scope

        now = self._clock()
        if now - self._last_full_sweep >= self.full_sweep_seconds:
            self._last_full_sweep = now
            return scope
        if now < self._skip_scan_until:
            return ()

        probe = getattr(self.driver, "unread_total", None)
        scan = getattr(self.driver, "unread_sessions", None)
        if not callable(probe) or not callable(scan):
            # 替身驱动/旧驱动没有这个能力：保持原来的全量轮询语义。
            return scope

        try:
            total = await self._ui_call(probe)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 探测失败只能退回全量
            logger.warning("未读红点探测失败，退回全量轮询：%s", exc)
            return scope
        if not total:
            return ()

        try:
            pending = await self._ui_call(scan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 扫描失败只能退回全量
            logger.warning("未读会话扫描失败，退回全量轮询：%s", exc)
            return scope

        targets = tuple(group for group in scope if group in pending)
        if not targets:
            # 未读都在范围外：别每个 tick 都去翻会话列表，等下一轮全量兜底。
            self._skip_scan_until = now + self.full_sweep_seconds
        return targets

    def _warn_uia_skip_once(self, reason: str) -> None:
        """hook 模式跳过 UIA 回落时只告警一次，避免每个 tick 刷屏。"""
        if self._uia_skip_warned:
            return
        self._uia_skip_warned = True
        logger.warning(
            "outbound=hook：%s，本轮跳过（不回落 UIA，避免把微信窗口拉到前台）；"
            "本地库就绪后自动恢复",
            reason,
        )

    # ------------------------------------------------------- 轮询/发送范围
    def _panel_whitelist(self) -> frozenset[str] | None:
        """面板白名单；未接面板或读取抛错时返回 None（fail-closed）。"""
        if self._panel is None:
            return None
        try:
            panel = self._panel()
        except Exception as exc:  # noqa: BLE001 - 面板坏了不能拖垮轮询
            logger.warning("读取面板白名单失败，本轮按「无群可轮询」处理：%s", exc)
            return None
        if panel is None and not self._panel_none_warned:
            # 面板返回 None = `data/settings.local.yaml` 存在但从没成功解析过。
            # 这时的「空」不是用户意图，不能当成 empty_means:"all" 放开全部群；
            # 也没有配置 allowlist 可以退，只能 fail-closed：不轮询、不发送。
            logger.warning(
                "面板白名单不可读（settings.local.yaml 从未成功解析）："
                "本轮不轮询任何群，也不放行任何发送"
            )
            self._panel_none_warned = True
        return panel

    def _fallback_runtime(self) -> WeChatRuntime:
        """启动配置值：没有热开关视图或它读不出来时用这份。"""
        wechat = self.wechat
        return WeChatRuntime(
            private_enabled=self.private_enabled,
            private_reply=bool(getattr(wechat, "private_reply", False)),
            private_allow=frozenset(getattr(wechat, "private_allow", None) or ()),
            private_deny=frozenset(getattr(wechat, "private_deny", None) or ()),
        )

    def _runtime_wechat(self) -> WeChatRuntime:
        """当前私聊运行时开关；无注入或读取失败时退回启动默认值。

        调用方每轮/每闸门各取一次（mtime 缓存使单次开销接近零），不得在高频
        路径（每帧）上重复读取。
        """
        reader = self._runtime
        if reader is None:
            return self._fallback_runtime()
        try:
            current = reader()
        except Exception as exc:  # noqa: BLE001 - 热开关坏了不能拖垮轮询
            logger.warning("读取私聊运行时开关失败，退回启动默认值：%s", exc)
            return self._fallback_runtime()
        if current is None:
            return self._fallback_runtime()
        return current

    def _friend_allowed(self, name: str, runtime: WeChatRuntime) -> bool:
        """好友私聊的 allow/deny 筛选；deny 优先，registry 判不行时宁可不发。

        - deny 非空且命中 → False；
        - allow 非空且未命中 → False；
        - 两个名单都空 → 不限制（True）；
        - registry 不可用/抛错 → 无法判定时 fail-closed（False）。
        只对 kind == 'friend' 调用；群聊完全不受 allow/deny 影响。
        """
        if not runtime.private_allow and not runtime.private_deny:
            return True
        try:
            chat_id = str(self.registry.chat_id(name, kind="friend"))
        except Exception as exc:  # noqa: BLE001 - 解析不出来就跳过这个好友
            logger.debug("好友 %r 解析稳定会话 id 失败，跳过：%s", name, exc)
            return False
        chat_id = chat_id.strip().casefold()
        if runtime.private_deny and chat_id in runtime.private_deny:
            return False
        if runtime.private_allow and chat_id not in runtime.private_allow:
            return False
        return True

    def _peer(self, alias: str) -> Any | None:
        lookup = getattr(self.registry, "by_alias", None)
        if not callable(lookup):
            return None
        try:
            return lookup(alias)
        except Exception as exc:  # noqa: BLE001 - 身份表只影响范围解析
            logger.debug("解析会话别名 %r 失败：%s", alias, exc)
            return None

    def _localdb_group_names(self) -> tuple[str, ...]:
        """本地库能枚举到的全部群显示名；不可用/无能力时返回空元组。"""
        return self._localdb_names("group")

    def _localdb_friend_names(self, runtime: WeChatRuntime) -> tuple[str, ...]:
        """本地库能枚举到的好友显示名；私聊关闭/无能力时返回空元组。

        非空 allow 或 deny 名单在场时同样过滤好友（群不受影响）。
        """
        if not runtime.private_enabled:
            return ()
        names = self._localdb_names("friend")
        if not (runtime.private_allow or runtime.private_deny):
            return names
        return tuple(
            sorted(name for name in names if self._friend_allowed(name, runtime))
        )

    def _localdb_names(self, kind: str) -> tuple[str, ...]:
        source = self.localdb
        getter = getattr(source, "group_names" if kind == "group" else "friend_names", None)
        if not callable(getter):
            return ()
        try:
            raw = getter()
        except Exception as exc:  # noqa: BLE001 - 枚举失败按「不知道」处理
            logger.warning("枚举本地库%s名失败：%s", "群" if kind == "group" else "好友", exc)
            return ()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
            return ()
        names = tuple(sorted({str(name).strip() for name in raw if str(name).strip()}))
        # 枚举得到不等于读得到：还没有消息分库的会话（例如刚建/只有系统消息）
        # resolve 会失败，此时 UIA 兜底只会多一次开窗超时，什么也读不到。
        resolver = getattr(source, "resolve_username", None)
        if not callable(resolver):
            return () if kind == "friend" else names
        kind_of = getattr(source, "peer_kind", None)
        kept: list[str] = []
        for name in names:
            try:
                if not resolver(name):
                    continue
                # 好友必须同时确认类型，避免把同名群误当私聊。
                if kind == "friend":
                    if not callable(kind_of) or kind_of(name) != "friend":
                        continue
                kept.append(name)
            except Exception as exc:  # noqa: BLE001 - 解析失败按「读不到」处理
                logger.debug("解析本地库会话 %r 失败：%s", name, exc)
        return tuple(kept)

    def _kind_of_session(self, name: str) -> str | None:
        """会话类型：本地库优先，其次身份表；判断不出来返回 None。"""
        source = self.localdb
        kind_of = getattr(source, "peer_kind", None)
        if callable(kind_of):
            try:
                kind = kind_of(name)
            except Exception as exc:  # noqa: BLE001 - 类型判断失败按未知处理
                logger.debug("本地库判断会话 %r 类型失败：%s", name, exc)
            else:
                if kind in ("group", "friend"):
                    return str(kind)
        all_peers = getattr(self.registry, "all_peers", None)
        if callable(all_peers):
            peers_fn = cast(Callable[[], Iterable[Any]], all_peers)
            try:
                kinds: set[str] = set()
                for peer in peers_fn():
                    live = bool(getattr(peer, "live", False))
                    answers = getattr(peer, "answers_to", lambda _: False)
                    if live and answers(name):
                        kinds.add(str(peer.kind))
            except Exception as exc:  # noqa: BLE001 - 身份表坏了只影响类型
                logger.debug("身份表判断会话 %r 类型失败：%s", name, exc)
            else:
                if len(kinds) == 1:
                    return str(next(iter(kinds)))
        return None

    def _scope(self) -> tuple[str, ...]:
        """本轮允许轮询的会话显示名；面板是唯一来源。

        - 面板白名单非空：只返回其中能解析成存活会话的那些（好友还要求
          运行时 `private_enabled`，且不被 allow/deny 名单拦下）；
        - 面板白名单为空：返回本地库枚举到的全部群；`private_enabled` 时再
          加上全部好友（本地库不能枚举时一个都不轮询——UIA 没有可靠的会话
          清单，宁可少开窗）；
        - 面板读不出来（None）：fail-closed，返回空（不轮询任何会话）。
        群聊（kind == 'group'）完全不受 allow/deny 名单影响。
        """
        runtime = self._runtime_wechat()
        panel = self._panel_whitelist()
        if panel is None:
            return ()
        if not panel:
            groups = self._localdb_group_names()
            friends = self._localdb_friend_names(runtime)
            if not friends:
                return tuple(sorted(set(groups)))
            overlap = set(groups) & set(friends)
            if overlap:
                # 群/好友重名无法判定类型：跳过，绝不让私聊抢走群会话。
                logger.warning(
                    "本地库里有显示名同时属于群和好友，已跳过：%s", sorted(overlap)
                )
            return tuple(sorted((set(groups) | set(friends)) - overlap))
        names: set[str] = set()
        for chat_id in panel:
            alias = _alias_of(chat_id)
            if alias is None:
                continue
            peer = self._peer(alias)
            if peer is None or not getattr(peer, "live", False):
                continue
            kind = str(getattr(peer, "kind", "group"))
            if kind not in ("group", "friend"):
                continue
            name = str(getattr(peer, "display_name", "") or "").strip()
            if not name:
                continue
            if kind == "friend":
                if not runtime.private_enabled:
                    continue
                if not self._friend_allowed(name, runtime):
                    continue
            names.add(name)
        return tuple(sorted(names))

    def allows_chat(self, chat_id: str) -> bool:
        """出站闸门：面板白名单非空时只放行列出的会话，空 = 不限群。

        面板读不出来（None）时一律拒绝：没有可信的白名单就不发送。
        好友私聊还要求运行时 `private_enabled` 打开，且不被 allow/deny
        名单拦下（deny 优先）：出站同样不能给被禁好友发消息。群聊不受影响。
        """
        alias = _alias_of(chat_id)
        if alias is None:
            # 桥只管微信会话：面板放开的只是「哪些会话」，不是别的通道。
            return False
        panel = self._panel_whitelist()
        if panel is None:
            return False
        runtime = self._runtime_wechat()
        peer = self._peer(alias)
        if (
            peer is not None
            and str(getattr(peer, "kind", "group")) == "friend"
        ):
            if not runtime.private_enabled:
                return False
            name = str(getattr(peer, "display_name", "") or "").strip()
            if not self._friend_allowed(name, runtime):
                return False
        if panel:
            return str(chat_id or "").strip() in panel
        return True

    async def _poll_round(
        self, groups: Iterable[str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        scope = self._scope()
        allowed = frozenset(scope)
        try:
            for group in scope if groups is None else groups:
                if self._stopped.is_set():
                    return
                # 双保险：永远不碰本轮范围之外的会话（面板可能刚被改小）。
                if group not in allowed:
                    continue
                try:
                    frames = await self._poll_group(group)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 单群失败不该打断整轮
                    logger.warning("轮询会话 %r 失败，跳过本轮：%s", group, exc)
                else:
                    for frame in frames:
                        yield frame
                if self._stopped.is_set():
                    return
                # 每群之后补一帧心跳：把最大帧间隔钉在「单群耗时」（开窗+快照约 5s）
                # 上。3 个群串行一轮 15s+，若只在轮末发心跳，帧泵的空转看门狗就会
                # 把健康的多群轮询误判成卡死。
                yield await self._health_frame(connected=True)
        finally:
            # 一轮结束（含被取消/提前 return）统一落盘对齐窗口。
            self._save_align_state()

    async def _poll_group(self, group: str) -> list[dict[str, Any]]:
        uses_localdb = self._uses_localdb()
        # 上一轮因为本地库不可用而跳过/读失败：这次读宽一点，把断档期间的消息补上。
        wide = uses_localdb and group in self._backfill_groups
        entries = (
            await self._localdb_entries(
                group, limit=BACKFILL_LIMIT if wide else None
            )
            if uses_localdb
            else None
        )
        if entries is None:
            if not self._allow_uia_fallback:
                # hook：开窗只为读消息是不必要的（还会把微信拉到前台），
                # 读不到就跳过这一轮，下一轮本地库恢复后宽窗补读。
                self._backfill_groups.add(group)
                self._warn_uia_skip_once(f"会话 {group!r} 的本地库快照不可用")
                return []
            handle = await self._ui_call(self.driver.open_chat, group)
            snapshot = await self._ui_call(self.driver.snapshot, handle)
            entries = snapshot.get("messages") if isinstance(snapshot, dict) else []
        kind = self._kind_of_session(group) or "group"
        aligner = self._aligner(group, kind)
        if aligner is None:
            return []
        poll = aligner.poll
        if wide and _accepts_kwarg(poll, "wide"):
            result = poll(entries or [], wide=True)
        else:
            result = poll(entries or [])
        self._backfill_groups.discard(group)
        self._remember_align_window(group, aligner)
        frames = list(result.messages)
        if result.resync is not None:
            frames.append(result.resync)
        return frames

    def _uses_localdb(self) -> bool:
        """本地库是否已就绪；就绪才敢跳过开窗，否则一律走 UIA。"""
        source = self.localdb
        if source is None:
            return False
        try:
            return bool(getattr(source, "available", False))
        except Exception as exc:  # noqa: BLE001 - 帧源坏掉只能当没有
            logger.debug("本地库状态检查失败：%s", exc)
            return False

    async def _localdb_entries(
        self, group: str, *, limit: int | None = None
    ) -> list[Any] | None:
        """后台线程取本地库快照；None = 本轮不可用，调用方决定是否回落 UIA。"""
        source = self.localdb
        if source is None:
            return None
        fallback = "本轮回落 UIA" if self._allow_uia_fallback else "本轮跳过（不回落 UIA）"
        try:
            snapshot = await asyncio.wait_for(
                asyncio.to_thread(_snapshot_call, source, group, limit), MAX_AWAIT
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # 首次建库可能在扫进程内存；线程还在跑，下一轮就绪后自动接管。
            logger.warning("本地库快照超时（%r），%s", group, fallback)
            return None
        except Exception as exc:  # noqa: BLE001 - 任何异常都只降级，不打断轮询
            logger.warning("本地库快照失败（%r），%s：%s", group, fallback, exc)
            return None
        if not isinstance(snapshot, dict):
            return None
        messages = snapshot.get("messages")
        return list(messages) if isinstance(messages, list) else None

    def _aligner(self, group: str, kind: str = "group") -> SessionAligner | None:
        cache_key = (group, kind)
        cached = self._aligners.get(cache_key)
        if cached is not None:
            return cached
        registry_kind = "friend" if kind == "friend" else "group"
        chat_type = "private" if kind == "friend" else "group"
        try:
            chat_id = str(self.registry.chat_id(group, kind=registry_kind))
        except Exception as exc:  # noqa: BLE001 - 身份歧义时 fail-closed，不猜
            logger.warning("会话 %r 无法解析稳定 alias，跳过：%s", group, exc)
            return None
        aligner_kwargs: dict[str, Any] = {
            "account_nickname": self._account_nickname,
            "chat_type": chat_type,
            "clock": self._clock,
        }
        # 好友私聊：user_id 用 registry 解析出的稳定 chat_id；群聊不传回调，
        # 成员仍按昵称生成会话内随机 alias（重启语义不变）。
        if chat_type == "private" and _accepts_user_alias(self._aligner_factory):

            def stable_alias(_name: str) -> str:
                return chat_id

            aligner_kwargs["user_alias"] = stable_alias
        if self._self_nicknames and _accepts_kwarg(
            self._aligner_factory, "self_nicknames"
        ):
            aligner_kwargs["self_nicknames"] = self._self_nicknames
        aligner = self._aligner_factory(chat_id, group, **aligner_kwargs)
        # 旧替身没有 seed：跳过续跑，退回「第一轮只建基线」的语义。
        seed = getattr(aligner, "seed", None)
        restored = self._align_state.get(group)
        if restored and callable(seed):
            try:
                seed(restored)
            except Exception as exc:  # noqa: BLE001 - 坏状态只影响一轮，不阻断轮询
                logger.warning("会话 %r 的对齐窗口还原失败：%s", group, exc)
        self._aligners[cache_key] = aligner
        return aligner

    def _remember_align_window(self, group: str, aligner: Any) -> None:
        """把本轮对齐窗口记到内存，等这一轮结束统一落盘。"""
        export = getattr(aligner, "export", None)
        if not callable(export):
            return
        try:
            entries = export()
        except Exception as exc:  # noqa: BLE001 - 落盘失败不能影响帧源
            logger.debug("导出会话 %r 的对齐窗口失败：%s", group, exc)
            return
        if not isinstance(entries, list):
            return
        if self._align_state.get(group) != entries:
            self._align_state[group] = entries
            self._align_dirty = True

    def _load_align_state(self) -> dict[str, list[dict[str, Any]]]:
        path = self.align_state_path
        if path is None or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 状态坏了按首次启动处理
            logger.warning("读取对齐状态失败，按首次启动处理：%s", exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        state: dict[str, list[dict[str, Any]]] = {}
        for group, entries in raw.items():
            if isinstance(entries, list):
                state[str(group)] = [item for item in entries if isinstance(item, dict)]
        return state

    def _save_align_state(self) -> None:
        path = self.align_state_path
        if path is None or not self._align_dirty:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._align_state, ensure_ascii=False), "utf-8"
            )
            os.replace(tmp, path)
            self._align_dirty = False
        except Exception as exc:  # noqa: BLE001 - 磁盘问题只影响续跑，不影响轮询
            logger.debug("保存对齐状态失败：%s", exc)

    # ------------------------------------------------------------- 健康
    async def _health_frame(self, *, connected: bool) -> dict[str, Any]:
        """健康帧：探测失败只降级，绝不让单次 UIA 超时打死整个帧源。

        发送和探测共用同一个 UIA 线程：一条长发送会让排队的探测超过
        `MAX_AWAIT`。以前这个 `TimeoutError` 会一路冒到 `frames()`，把桥整个
        打死；现在沿用上一次成功的字段（`desktop`/`logged_in` 尤其重要，bot
        据此决定能不能发），只补 `degraded` 和失败原因。
        """
        try:
            frame = await self._ui_call(self._probe, connected)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 探测失败只能降级，不能退出
            logger.warning("健康探测失败，本帧降级：%s", exc)
            out = dict(self._last_health or {})
            # 从没有过成功帧时 fail-closed：宁可这一轮不发，也不在状态不明时操作桌面。
            out.setdefault("desktop", "locked")
            out.setdefault("logged_in", False)
            out["degraded"] = True
            out["reason"] = f"health_probe_failed:{type(exc).__name__}"
        else:
            out = dict(frame) if isinstance(frame, dict) else {}
            self._last_health = dict(out)
        out.setdefault("v", 1)
        out["type"] = "health"
        out["connected"] = bool(connected)
        if not self._allow_uia_fallback and not self._uses_localdb():
            # hook 模式没有 UIA 兜底：本地库不可用就意味着这一轮读不到消息。
            # 标成降级让面板/告警看得见，而不是只在日志里留一句。
            out["degraded"] = True
            reasons = [part for part in str(out.get("reason") or "").split(";") if part]
            if INBOUND_LOCALDB_REASON not in reasons:
                reasons.append(INBOUND_LOCALDB_REASON)
            out["reason"] = ";".join(reasons)
        return out

    async def _kill_switch_frame(self) -> dict[str, Any]:
        """杀开关帧：``connected:false`` + 强制 ``degraded`` + ``kill_switch`` reason。

        probe 也走 UIA 线程（默认实现会读驱动/桌面状态）；probe 失败时退回一个
        最小可用信封，停机路径绝不能被探测失败挡住。
        """
        try:
            out = await self._health_frame(connected=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("杀开关健康探测失败，用最小信封", exc_info=True)
            out = {"v": 1, "type": "health"}
        out["connected"] = False
        out["degraded"] = True
        reasons = [part for part in str(out.get("reason") or "").split(";") if part]
        if KILL_SWITCH_REASON not in reasons:
            reasons.append(KILL_SWITCH_REASON)
        out["reason"] = ";".join(reasons)
        return out

    # ------------------------------------------------------------- 工具
    async def _ui_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        future = self.ui.submit(lambda: fn(*args))
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), MAX_AWAIT)
        except TimeoutError:
            future.cancel()
            logger.warning("微信桥 UIA 调用超时（%s）", getattr(fn, "__name__", fn))
            raise

    async def _nap(self, seconds: float) -> bool:
        """可被 stop 立刻打断的等待；单个 await 仍不超过 MAX_AWAIT。"""
        remaining = float(seconds)
        while remaining > 0:
            if self._stopped.is_set():
                return False
            chunk = min(remaining, MAX_AWAIT)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), chunk)
                return False
            remaining -= chunk
        return not self._stopped.is_set()
