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
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .align import SessionAligner

logger = logging.getLogger(__name__)

__all__ = [
    "KILL_SWITCH_REASON",
    "MAX_AWAIT",
    "PollingLoop",
    "UiThread",
]

#: 反挂死护栏：本模块的每一个 await 都不超过这个秒数（与 PART A 一致）。
MAX_AWAIT = 5.0

#: 杀开关健康帧的稳定 reason 片段。
KILL_SWITCH_REASON = "kill_switch"

#: 默认的杀开关相对路径（与 `WeChatConfig.kill_switch_file` 默认值一致）。
DEFAULT_KILL_SWITCH = "data/wechat.stop"

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
    """按 allowlist 轮询微信群并把事件帧交给调用方（sink / async 迭代器）。

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
    ) -> None:
        self.wechat = wechat
        self.driver = driver
        self.ui = ui if ui is not None else UiThread()
        self._owns_ui = ui is None
        if registry is None:
            from .identity import PeerRegistry  # 只在真跑时加载身份表

            registry = PeerRegistry()
        self.registry = registry
        self._probe = probe if probe is not None else _default_probe(wechat)
        self._clock = clock
        self._aligner_factory = aligner_factory

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
        # 稳定顺序 + 绝不截断：allowlist 是什么就轮询什么，不做「只取前 N 个」。
        self.allowlist: tuple[str, ...] = tuple(
            sorted(str(group) for group in (getattr(wechat, "group_allowlist", ()) or ()))
        )
        self._allowset = frozenset(self.allowlist)
        account_nickname = str(getattr(wechat, "account_nickname", "") or "")
        self._account_nickname = account_nickname

        raw_kill = (
            kill_switch_path
            if kill_switch_path is not None
            else getattr(wechat, "kill_switch_file", DEFAULT_KILL_SWITCH)
        )
        self.kill_switch_path = Path(str(raw_kill or DEFAULT_KILL_SWITCH))

        self._aligners: dict[str, SessionAligner] = {}
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
        """
        if not self.unread_gate:
            return self.allowlist

        now = self._clock()
        if now - self._last_full_sweep >= self.full_sweep_seconds:
            self._last_full_sweep = now
            return self.allowlist
        if now < self._skip_scan_until:
            return ()

        probe = getattr(self.driver, "unread_total", None)
        scan = getattr(self.driver, "unread_sessions", None)
        if not callable(probe) or not callable(scan):
            # 替身驱动/旧驱动没有这个能力：保持原来的全量轮询语义。
            return self.allowlist

        try:
            total = await self._ui_call(probe)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 探测失败只能退回全量
            logger.warning("未读红点探测失败，退回全量轮询：%s", exc)
            return self.allowlist
        if not total:
            return ()

        try:
            pending = await self._ui_call(scan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 扫描失败只能退回全量
            logger.warning("未读会话扫描失败，退回全量轮询：%s", exc)
            return self.allowlist

        targets = tuple(group for group in self.allowlist if group in pending)
        if not targets:
            # 未读都在白名单外：别每个 tick 都去翻会话列表，等下一轮全量兜底。
            self._skip_scan_until = now + self.full_sweep_seconds
        return targets

    async def _poll_round(
        self, groups: Iterable[str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        for group in self.allowlist if groups is None else groups:
            if self._stopped.is_set():
                return
            # 双保险：永远不碰 allowlist 之外的会话。
            if group not in self._allowset:
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

    async def _poll_group(self, group: str) -> list[dict[str, Any]]:
        handle = await self._ui_call(self.driver.open_chat, group)
        snapshot = await self._ui_call(self.driver.snapshot, handle)
        entries = snapshot.get("messages") if isinstance(snapshot, dict) else []
        aligner = self._aligner(group)
        if aligner is None:
            return []
        result = aligner.poll(entries or [])
        frames = list(result.messages)
        if result.resync is not None:
            frames.append(result.resync)
        return frames

    def _aligner(self, group: str) -> SessionAligner | None:
        cached = self._aligners.get(group)
        if cached is not None:
            return cached
        try:
            chat_id = str(self.registry.chat_id(group, kind="group"))
        except Exception as exc:  # noqa: BLE001 - 身份歧义时 fail-closed，不猜
            logger.warning("会话 %r 无法解析稳定 alias，跳过：%s", group, exc)
            return None
        aligner = self._aligner_factory(
            chat_id,
            group,
            account_nickname=self._account_nickname,
            chat_type="group",
            clock=self._clock,
        )
        self._aligners[group] = aligner
        return aligner

    # ------------------------------------------------------------- 健康
    async def _health_frame(self, *, connected: bool) -> dict[str, Any]:
        frame = await self._ui_call(self._probe, connected)
        out = dict(frame) if isinstance(frame, dict) else {}
        out.setdefault("v", 1)
        out["type"] = "health"
        out["connected"] = bool(connected)
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
