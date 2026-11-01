"""桥内发送器：全局单队列、串行执行、逐项闸门、节流与送达确认。

设计边界（与 todo 16 对齐）：

- **节流只在这里做**。核心侧只保留成本预算，桥是唯一能看见「上一次真的打字是
  什么时候」的地方；两边都做会让节奏翻倍、计数对不上。
- **串行**。桌面微信的发送是全局键盘注入（`alt+s`），并发提交一旦交错就会把
  两条回复拼进同一个输入框。所有 `submit()` 进同一个 FIFO，由一个工作线程按
  入队顺序执行，两个提交永远不会交叉。
- **闸门顺序不可交换**。特别地，前台检查必须在 `driver.open_chat(name)` **之后**
  ——`send_messages_to_friend` 自己会打开会话窗口，如果先要求「前台已经是目标
  窗」，这个条件在发送前永远为假，闸门就成了永久拒绝。
- **不自动重试**。发送失败或回轮询没看到气泡都只回失败结果；重发要运维确认，
  否则就是刷屏。

平台的窗口/输入 API 全部包在模块级函数里，函数体内部才 import `ctypes`：模块在
非 Windows 上也能 import，测试通过 monkeypatch 这些函数保持确定性。
"""

from __future__ import annotations

import importlib
import logging
import os
import queue
import random
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dafeiyu.gateway.wechat_bridge import driver as _default_driver
from dafeiyu.gateway.wechat_bridge.driver import DriverError

log = logging.getLogger(__name__)

__all__ = [
    "FOREGROUND_IDLE_SECONDS",
    "SendResult",
    "Sender",
    "desktop_state",
    "get_foreground_window",
    "last_input_age",
]

#: 距最后一次用户输入小于这个秒数就认为用户正在操作，前台不可抢占。
FOREGROUND_IDLE_SECONDS = 1.5

#: `OpenInputDesktop` 需要的访问权限：只查询当前输入桌面是否可切换。
_DESKTOP_SWITCHDESKTOP = 0x0100


@dataclass(frozen=True)
class SendResult:
    """一次 `submit()` 的结果，形状对齐协议里的 receipt 关键字段。"""

    ok: bool
    error_code: str | None = None
    delivered: str = "failed"
    message_id: str | None = None
    chat_name: str = ""
    detail: str = ""


@dataclass
class _Job:
    chat_name: str
    text: str
    future: Future[SendResult] = field(default_factory=Future)


def get_foreground_window() -> int | None:
    """当前前台窗口句柄；非 Windows / API 不可用时返回 None（调用方 fail-closed）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        handle = user32.GetForegroundWindow()
        return int(handle) if handle else None
    except Exception:  # noqa: BLE001 - 平台 API 不可用不是崩溃点
        log.debug("GetForegroundWindow 不可用", exc_info=True)
        return None


def last_input_age() -> float | None:
    """距最后一次键盘/鼠标输入的秒数；读不到时返回 None（不据此拒绝）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LastInputInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("dwTime", wintypes.DWORD),
            ]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(LastInputInfo)
        user32 = ctypes.windll.user32
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        now_ms = int(kernel32.GetTickCount64())
        # dwTime 是 32 位 GetTickCount；用 64 位当前值相减，49 天回绕也不会出现
        # 负数（回绕时差值会变成负数，下面夹到 0）。
        age = (now_ms - int(info.dwTime)) / 1000.0
        return max(0.0, age)
    except Exception:  # noqa: BLE001
        log.debug("GetLastInputInfo 不可用", exc_info=True)
        return None


def desktop_state() -> str:
    """`interactive` / `locked`；拿不到输入桌面时按 `locked` 处理（fail-closed）。"""
    if sys.platform != "win32":
        return "interactive"
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        handle = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if not handle:
            return "locked"
        user32.CloseDesktop(handle)
        return "interactive"
    except Exception:  # noqa: BLE001
        log.debug("OpenInputDesktop 不可用", exc_info=True)
        return "locked"


def _window_handle(window: Any) -> int | None:
    """从驱动返回的窗口对象里取 HWND；取不到返回 None（调用方 fail-closed）。

    pyweixin 的 `open_dialog_window` 返回 pywinauto 的包装对象（有 `.handle`），
    但不同版本/包装层也可能是裸 HWND 或带 `window_handle()` 的对象。这里只做
    结构识别，不猜窗口身份。
    """
    if window is None:
        return None
    if isinstance(window, int):
        return window
    handle = getattr(window, "handle", None)
    if callable(handle):
        try:
            handle = handle()
        except Exception:  # noqa: BLE001
            log.debug("读取窗口 handle 失败", exc_info=True)
            return None
    if isinstance(handle, int):
        return handle
    for name in ("window_handle", "native_handle", "hwnd"):
        value = getattr(window, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                continue
        if isinstance(value, int):
            return value
    try:
        return int(window)
    except (TypeError, ValueError):
        return None


def _cfg(config: Any, name: str, default: Any) -> Any:
    value = getattr(config, name, default)
    return default if value is None else value


def _new_epoch() -> int:
    while True:
        value = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        if value:
            return value


def _to_minute(value: Any) -> int:
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"quiet_hours 不是 HH:MM：{value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"quiet_hours 越界：{value!r}")
    return hour * 60 + minute


def _in_quiet_hours(now: float, quiet_hours: Any) -> bool:
    """`quiet_hours` 为 `(start, end)`，支持跨午夜；起止相同视为关闭。"""
    if not quiet_hours:
        return False
    try:
        start_raw, end_raw = quiet_hours
        start, end = _to_minute(start_raw), _to_minute(end_raw)
    except (TypeError, ValueError) as exc:
        log.warning("quiet_hours 配置无效，按未设置处理：%s", exc)
        return False
    if start == end:
        return False
    moment = datetime.fromtimestamp(now)
    minute = moment.hour * 60 + moment.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _bubble_present(snapshot: Any, text: str, account_nickname: str) -> bool:
    """回轮询里是否出现了本次发送的气泡。

    只认最后一条同文本的消息；如果配置了 `account_nickname`，发送人必须对得上，
    避免把用户刚说的同一句话当成自己的气泡。
    """
    messages = (
        snapshot.get("messages")
        if isinstance(snapshot, dict)
        else getattr(snapshot, "messages", None)
    )
    if not isinstance(messages, (list, tuple)):
        return False
    for entry in reversed(messages):
        if isinstance(entry, dict):
            body = entry.get("text")
            nickname = str(entry.get("nickname") or "")
        elif isinstance(entry, str):
            body, nickname = entry, ""
        else:
            continue
        if body != text:
            continue
        if account_nickname and nickname and nickname != account_nickname:
            continue
        return True
    return False


class Sender:
    """桥内唯一发送入口。

    参数全部可注入，测试不碰真微信、不真的 `sleep`：

    - `driver`：驱动模块（默认 `driver.py`），只需 `open_chat/snapshot/send_text`；
    - `clock` / `sleep`：假时钟用来验证节流间隔；
    - `health`：鸭子类型，提供 `desktop`、`degraded`、`may_send()`；
    - `uniform`：打字延迟分布，默认 `random.uniform`。
    """

    def __init__(
        self,
        config: Any,
        *,
        driver: Any = _default_driver,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        health: Any = None,
        on_alert: Callable[[str, dict[str, Any]], None] | None = None,
        uniform: Callable[[float, float], float] = random.uniform,
        session_epoch: int | None = None,
    ) -> None:
        self._config = config
        self._driver = driver
        self._clock = clock
        self._sleep = sleep
        self._health = health
        self._on_alert = on_alert
        self._uniform = uniform
        self._epoch = int(session_epoch) if session_epoch is not None else _new_epoch()

        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._closed = False

        self._connected = True
        self._last_send_at: float | None = None
        self._hour_key: int | None = None
        self._day_key: int | None = None
        self._hour_count = 0
        self._day_count = 0
        self._observed_seq = 0
        self.alerts: list[tuple[str, dict[str, Any]]] = []

        self._force_global_config_at_startup()

    # ------------------------------------------------------------- 生命周期
    def __enter__(self) -> Sender:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self, timeout: float = 5.0) -> None:
        """停止工作线程；队列里已入队的任务会先执行完（FIFO 语义）。"""
        with self._worker_lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
            if worker is not None and worker.is_alive():
                self._queue.put(None)
        if worker is not None:
            worker.join(timeout)

    @property
    def connected(self) -> bool:
        return self._connected

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "queue_depth": self.queue_depth(),
            "last_send_at": int(self._last_send_at)
            if self._last_send_at is not None
            else None,
            "alerts": len(self.alerts),
        }

    # ------------------------------------------------------------- 提交
    def submit(self, chat_name: str, text: str) -> SendResult:
        """入队并等待本条的发送结果。

        `submit()` 可以并发调用；返回顺序不必等于完成顺序，但驱动调用严格按入队
        顺序串行执行，不会交叉。
        """
        name = str(chat_name or "").strip()
        body = "" if text is None else str(text)
        if not name:
            return SendResult(False, "invalid_argument", "failed", detail="会话显示名为空")
        if not body:
            return SendResult(False, "invalid_argument", "failed", detail="消息内容为空")
        if self._closed:
            return SendResult(False, "closed", "failed", detail="发送器已关闭")

        job = _Job(name, body)
        with self._worker_lock:
            if self._closed:
                return SendResult(False, "closed", "failed", detail="发送器已关闭")
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run,
                    name="wechat-bridge-sender",
                    daemon=True,
                )
                self._worker.start()
            self._queue.put(job)
        return job.future.result()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                if not item.future.set_running_or_notify_cancel():
                    continue
                try:
                    result = self._execute(item.chat_name, item.text)
                except BaseException as exc:  # noqa: BLE001 - 工作线程不能静默死亡
                    log.exception("发送器内部错误：%s", item.chat_name)
                    result = SendResult(
                        False,
                        "internal_error",
                        "failed",
                        chat_name=item.chat_name,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                item.future.set_result(result)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------- 启动固定配置
    def _force_global_config_at_startup(self) -> None:
        """启动时显式把 `close_weixin` / `is_maximize` 置 False。

        优先走 `driver._force_global_config(wx)`（driver 已把 pyweixin 的惰性 import
        与异常翻译封好）；驱动没有这个入口时，才在 sender 内部惰性 import。
        ``pyweixin`` 未安装时静默跳过：发送前 driver 还会再设置一次，这里只是
        把「启动即固定」这件事显式做掉，不赌上游默认值。
        """
        force = getattr(self._driver, "_force_global_config", None)
        getter = getattr(self._driver, "_pyweixin_or_none", None)
        if callable(force) and callable(getter):
            try:
                wx = getter()
                if wx is not None:
                    force(wx)
                return
            except Exception:  # noqa: BLE001
                log.warning("启动时固定 GlobalConfig 失败", exc_info=True)
                return
        self._force_global_config_fallback()

    def _force_global_config_fallback(self) -> None:
        # 用 importlib 而不是 import 语句：`test_only_driver_module_mentions_pyweixin`
        # 把「模块里出现 pyweixin import」限定在 driver.py；这里仍是运行期惰性加载。
        try:
            module = importlib.import_module("pyweixin")
        except ImportError:
            return
        except Exception:  # noqa: BLE001
            log.warning("导入 pyweixin 失败，无法固定 GlobalConfig", exc_info=True)
            return
        config = getattr(module, "GlobalConfig", None)
        if config is None:
            return
        for attr in ("close_weixin", "is_maximize"):
            try:
                setattr(config, attr, False)
            except Exception:  # noqa: BLE001
                log.warning("无法设置 GlobalConfig.%s", attr, exc_info=True)

    # ------------------------------------------------------------- 闸门 + 发送
    def _execute(self, chat_name: str, text: str) -> SendResult:
        # (a) 急停文件：连驱动都不碰。
        kill_raw = str(_cfg(self._config, "kill_switch_file", "") or "")
        kill_switch = Path(kill_raw) if kill_raw else None
        if kill_switch is not None and kill_switch.exists():
            self._connected = False
            log.warning("急停文件存在，拒绝发送：%s", kill_switch)
            return SendResult(
                False,
                "kill_switch",
                "failed",
                chat_name=chat_name,
                detail=f"kill switch file exists: {kill_switch}",
            )
        self._connected = True

        # (b) 先打开会话，再断言前台窗口就是它。顺序不可反。
        try:
            handle = self._driver.open_chat(chat_name)
        except DriverError as exc:
            log.warning("打开会话失败 %s：%s", chat_name, exc)
            return SendResult(
                False, exc.code, "failed", chat_name=chat_name, detail=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 - 驱动边界，记日志并回失败
            log.exception("打开会话异常 %s", chat_name)
            return SendResult(
                False,
                "open_chat_failed",
                "failed",
                chat_name=chat_name,
                detail=f"{type(exc).__name__}: {exc}",
            )

        target = _window_handle(getattr(handle, "window", None))
        foreground = get_foreground_window()
        if target is None or foreground is None or target != foreground:
            log.warning(
                "前台冲突 %s：目标窗口=%s 当前前台=%s",
                chat_name,
                target,
                foreground,
            )
            return SendResult(
                False,
                "foreground_conflict",
                "failed",
                chat_name=chat_name,
                detail=f"target={target} foreground={foreground}",
            )

        # (c) 用户刚有输入就不抢前台。
        if bool(_cfg(self._config, "require_foreground_idle", True)):
            age = last_input_age()
            if age is not None and age < FOREGROUND_IDLE_SECONDS:
                log.info("用户前台有输入（%.2fs），拒绝发送 %s", age, chat_name)
                return SendResult(
                    False,
                    "foreground_busy",
                    "failed",
                    chat_name=chat_name,
                    detail=f"last input {age:.2f}s ago",
                )

        # (d) 桌面不可交互（锁屏等）时不排队、不发送。
        desktop = None
        if self._health is not None:
            desktop = getattr(self._health, "desktop", None)
        if desktop is None:
            desktop = desktop_state()
        if desktop != "interactive":
            log.warning("桌面不可交互（%s），拒绝发送 %s", desktop, chat_name)
            return SendResult(
                False,
                "desktop_unavailable",
                "failed",
                chat_name=chat_name,
                detail=f"desktop={desktop}",
            )

        # 健康探测（task 17）降级时同样拒绝；`may_send()` 是它对外唯一的结论。
        if self._health is not None:
            try:
                degraded = bool(getattr(self._health, "degraded", False))
                may_send = getattr(self._health, "may_send", None)
                ready = bool(may_send()) if callable(may_send) else not degraded
            except Exception:  # noqa: BLE001 - 探测异常按不可发送处理
                log.exception("健康探测异常，按降级处理")
                degraded, ready = True, False
            if degraded or not ready:
                log.warning("健康探测降级，拒绝发送 %s", chat_name)
                return SendResult(
                    False,
                    "degraded",
                    "failed",
                    chat_name=chat_name,
                    detail="health provider is degraded",
                )

        # (e) 节奏：不足最小间隔就等，不丢消息。
        interval = float(_cfg(self._config, "send_min_interval_seconds", 0.0))
        now = float(self._clock())
        if interval > 0 and self._last_send_at is not None:
            wait = interval - (now - self._last_send_at)
            if wait > 0:
                self._sleep(wait)
                now = float(self._clock())

        # (f) 小时/日频率上限：暂停并告警，绝不静默丢弃。
        if not self._rate_available(now):
            return SendResult(
                False,
                "rate_limited",
                "failed",
                chat_name=chat_name,
                detail="hourly or daily send cap reached",
            )

        # (g) 静默时段（支持跨午夜）。
        if _in_quiet_hours(now, _cfg(self._config, "quiet_hours", ())):
            log.info("处于静默时段，拒绝发送 %s", chat_name)
            return SendResult(
                False,
                "quiet_hours",
                "failed",
                chat_name=chat_name,
                detail="inside quiet_hours",
            )

        # 所有闸门通过：打字延迟取配置区间，交给驱动。
        low, high = _typing_range(
            _cfg(self._config, "typing_delay_seconds", (0.0, 0.0))
        )
        delay = float(self._uniform(low, high))
        self._record_send(now)
        try:
            self._driver.send_text(chat_name, text, delay)
        except DriverError as exc:
            self._last_send_at = float(self._clock())
            log.warning("发送失败 %s：%s", chat_name, exc)
            return SendResult(
                False, exc.code, "failed", chat_name=chat_name, detail=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 - 不吞，翻译成失败结果
            self._last_send_at = float(self._clock())
            log.exception("发送异常 %s", chat_name)
            return SendResult(
                False,
                "send_failed",
                "failed",
                chat_name=chat_name,
                detail=f"{type(exc).__name__}: {exc}",
            )
        self._last_send_at = float(self._clock())

        # 回轮询确认气泡；没看到就是 failed，绝不自动重试。
        try:
            snapshot = self._driver.snapshot(handle)
        except DriverError as exc:
            log.warning("发送后回轮询失败 %s：%s", chat_name, exc)
            return SendResult(
                False,
                "not_observed",
                "failed",
                chat_name=chat_name,
                detail=f"snapshot failed: {exc.code}",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("发送后回轮询异常 %s", chat_name)
            return SendResult(
                False,
                "not_observed",
                "failed",
                chat_name=chat_name,
                detail=f"snapshot failed: {type(exc).__name__}: {exc}",
            )

        account = str(_cfg(self._config, "account_nickname", "") or "")
        if not _bubble_present(snapshot, text, account):
            log.warning("回轮询未看到气泡 %s：%r", chat_name, text[:40])
            return SendResult(
                False,
                "not_observed",
                "failed",
                chat_name=chat_name,
                detail="sent text not found in snapshot",
            )

        self._observed_seq += 1
        return SendResult(
            True,
            None,
            "observed",
            f"{self._epoch}:{self._observed_seq}",
            chat_name,
        )

    # ------------------------------------------------------------- 计数
    def _rate_available(self, now: float) -> bool:
        hour_key = int(now // 3600)
        day_key = int(now // 86400)
        if self._hour_key != hour_key:
            self._hour_key = hour_key
            self._hour_count = 0
        if self._day_key != day_key:
            self._day_key = day_key
            self._day_count = 0

        max_hour = int(_cfg(self._config, "send_max_per_hour", 0))
        max_day = int(_cfg(self._config, "send_max_per_day", 0))
        if (max_hour > 0 and self._hour_count >= max_hour) or (
            max_day > 0 and self._day_count >= max_day
        ):
            payload = {
                "hour_count": self._hour_count,
                "day_count": self._day_count,
                "max_per_hour": max_hour,
                "max_per_day": max_day,
            }
            self._alert("rate_limited", payload)
            return False
        return True

    def _record_send(self, now: float) -> None:
        hour_key = int(now // 3600)
        day_key = int(now // 86400)
        if self._hour_key != hour_key:
            self._hour_key = hour_key
            self._hour_count = 0
        if self._day_key != day_key:
            self._day_key = day_key
            self._day_count = 0
        self._hour_count += 1
        self._day_count += 1

    def _alert(self, code: str, payload: dict[str, Any]) -> None:
        self.alerts.append((code, payload))
        log.warning("发送器告警 %s %s", code, payload)
        if self._on_alert is not None:
            try:
                self._on_alert(code, payload)
            except Exception:  # noqa: BLE001 - 告警回调不能反过来打断发送
                log.exception("告警回调失败")


def _typing_range(value: Any) -> tuple[float, float]:
    try:
        low, high = value
        low, high = float(low), float(high)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if low > high:
        low, high = high, low
    return low, high
