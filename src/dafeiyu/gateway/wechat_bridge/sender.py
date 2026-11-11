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

import logging
import os
import queue
import random
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from dafeiyu.gateway.wechat_bridge import driver as _default_driver
from dafeiyu.gateway.wechat_bridge.driver import DriverError

log = logging.getLogger(__name__)

__all__ = [
    "FOREGROUND_IDLE_SECONDS",
    "Prepared",
    "SendResult",
    "Sender",
    "UiaTransport",
    "bump_foreground",
    "desktop_state",
    "get_foreground_window",
    "last_input_age",
    "wechat_main_window",
]

#: 距最后一次用户输入小于这个秒数就认为用户正在操作，前台不可抢占。
FOREGROUND_IDLE_SECONDS = 1.5

#: 比较「我们注入的时刻」与「最后一次输入的时刻」时允许的时钟误差（秒）。
_SELF_INPUT_TOLERANCE_SECONDS = 0.05

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
    #: 传输层能**证明**这条消息没发出去（连接被拒 / 后端明确 sent:false）。
    #: `auto` 只在这种情况下回落 UIA；超时等结果不明的情况绝不回落。
    not_sent: bool = False


@dataclass
class _Job:
    chat_name: str
    text: str
    future: Future[SendResult] = field(default_factory=Future)
    #: 要 @ 的成员显示名；空 = 普通文本。传输层不支持时回 `unsupported_at`。
    at: tuple[str, ...] = ()


def _completed(result: SendResult) -> Future[SendResult]:
    """已完成的 future：把校验失败/已关闭也统一成 future 接口。"""
    future: Future[SendResult] = Future()
    future.set_result(result)
    return future


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
    except Exception:
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
    except Exception:
        log.debug("GetLastInputInfo 不可用", exc_info=True)
        return None


def input_was_self_injected(age: float, driver: Any) -> bool:
    """最近一次系统输入是不是桥自己注入的（而不是人）。

    `GetLastInputInfo` 只告诉我们「多久没输入」，不区分来源；pyautogui 用
    SendInput 注入按键，同样会刷新这个计时。驱动的
    `last_synthetic_input_age()` 给出「我们上一次注入距现在多久」：若我们的注入
    时刻不早于最后一次输入（`own_age <= age`），这次输入就是我们自己的，不该
    拦住回复。驱动没提供该钩子、或探测抛异常时一律按「是用户输入」处理
    （fail-closed，宁可少发一条也不抢人的前台）。
    """
    probe = getattr(driver, "last_synthetic_input_age", None)
    if not callable(probe):
        return False
    try:
        own = probe()
    except Exception:
        log.debug("last_synthetic_input_age 探测失败", exc_info=True)
        return False
    if own is None:
        return False
    if isinstance(own, bool) or not isinstance(own, (int, float)):
        return False
    return float(own) <= age + _SELF_INPUT_TOLERANCE_SECONDS


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
    except Exception:
        log.debug("OpenInputDesktop 不可用", exc_info=True)
        return "locked"


#: 微信主进程可执行名（小写）；4.x 是 `Weixin.exe`，3.x 是 `WeChat.exe`。
_WECHAT_PROCESS_NAMES = frozenset({"weixin.exe", "wechat.exe"})

#: 微信主窗口被更高完整性窗口压住时，用「最小化 → 还原」把它顶回前台。
#: 中完整性进程对提权窗口做不了 `SetForegroundWindow`（UIPI 拦截，err=5），
#: 但对同完整性的微信窗口做 ShowWindow 是允许的。
_SW_MINIMIZE = 6
_SW_RESTORE = 9
_GW_OWNER = 4


def wechat_main_window() -> int | None:
    """找微信的可见主窗口 HWND（同进程名里面积最大的顶层窗口）。

    不按窗口类名/标题硬编码：4.1 的主窗口类名是 `Qt51514QWindowIcon`，
    3.x 是 `mmui::MainWindow`，标题还可能带未读计数。找不到时返回 None，
    调用方 fail-closed（不猜窗口，不误顶别的应用）。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        import psutil

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindow.restype = wintypes.HWND

        names: dict[int, str] = {}

        def _name_of(hwnd: Any) -> str:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            pid_value = int(pid.value)
            if pid_value not in names:
                try:
                    names[pid_value] = psutil.Process(pid_value).name().lower()
                except Exception:  # noqa: BLE001 - 读不到进程名就当不是微信
                    names[pid_value] = ""
            return names[pid_value]

        best: list[tuple[int, int]] = []

        def _visit(hwnd: Any, _param: Any) -> bool:
            if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
                return True
            # 过滤工具栏/托盘图标这类被主窗口拥有的弹出窗口。
            if user32.GetWindow(wintypes.HWND(hwnd), _GW_OWNER):
                return True
            if _name_of(hwnd) not in _WECHAT_PROCESS_NAMES:
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
                return True
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area <= 0:
                return True
            if not best or area > best[0][0]:
                best[:] = [(area, int(hwnd))]
            return True

        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(enum_proc(_visit), 0)
    except Exception:
        log.debug("枚举微信窗口失败", exc_info=True)
        return None
    return best[0][1] if best else None


def bump_foreground(handle: int) -> bool:
    """把 `handle` 顶到前台（最小化后还原来突破前台锁），成功返回 True。

    这是 UIPI 场景下中完整性进程唯一还能用的「抢前台」手段：前台是提权
    窗口时 `SetForegroundWindow` / `AttachThreadInput` 都会 err=5，但对
    同完整性的微信窗口做 `SW_MINIMIZE` + `SW_RESTORE` 是允许的。
    """
    if sys.platform != "win32" or not handle:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = wintypes.HWND(int(handle))
        if not user32.IsWindow(hwnd):
            return False
        user32.ShowWindow(hwnd, _SW_MINIMIZE)
        time.sleep(0.15)
        user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.25)
        current = user32.GetForegroundWindow()
        return bool(current) and int(current) == int(handle)
    except Exception:
        log.debug("顶前台失败 hwnd=%s", handle, exc_info=True)
        return False


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
        except Exception:
            log.debug("读取窗口 handle 失败", exc_info=True)
            return None
    if isinstance(handle, int):
        return handle
    for name in ("window_handle", "native_handle", "hwnd"):
        value = getattr(window, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                # 某个候选属性取不到就试下一个（仅调试级日志，避免刷屏）。
                log.debug("读取窗口 handle 属性 %s 失败", name, exc_info=True)
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


@dataclass(frozen=True)
class Prepared:
    """一次发送的准备结果：会话显示名 + 传输层需要的句柄/目标 id。

    UIA 传输把 `ChatHandle` 放进 ``handle``；hook 传输把群 username
    （``xxx@chatroom``）放进 ``target``。Sender 不解释这些字段，只原样交回。
    """

    chat_name: str
    handle: Any = None
    target: str = ""


class UiaTransport:
    """UIA 桌面传输：开窗 -> 前台断言 -> 锁屏/健康闸门 -> 打字 -> 回轮询确认。

    闸门顺序不可交换：前台检查必须在 ``driver.open_chat`` **之后**——驱动自己
    会打开会话窗口，先要求「前台已经是目标窗」会变成永久拒绝。

    驱动调用必须跑在 STA/UIA 线程上（生产路径由 `_UiThreadDriver` 保证），
    本类只负责顺序与判定。
    """

    name = "uia"
    #: 需要桌面可交互；Sender 据此决定是否做 UIA 准备。
    requires_desktop = True
    #: 可能回落到 UIA（auto 传输用），启动时仍需固定 GlobalConfig。
    may_use_desktop = True

    def __init__(
        self,
        config: Any,
        *,
        driver: Any = _default_driver,
        health: Any = None,
        session_epoch: int | None = None,
    ) -> None:
        self._config = config
        self._driver = driver
        self._health = health
        self._epoch = int(session_epoch) if session_epoch is not None else _new_epoch()
        self._observed_seq = 0

    # ------------------------------------------------------------- 准备
    @staticmethod
    def _open_chat_failure(chat_name: str, exc: BaseException) -> SendResult:
        if isinstance(exc, DriverError):
            return SendResult(
                False, exc.code, "failed", chat_name=chat_name, detail=str(exc)
            )
        log.error("打开会话异常 %s", chat_name, exc_info=exc)
        return SendResult(
            False,
            "open_chat_failed",
            "failed",
            chat_name=chat_name,
            detail=f"{type(exc).__name__}: {exc}",
        )

    @staticmethod
    def _recover_foreground(chat_name: str, preferred: int | None = None) -> int | None:
        """把微信窗口顶回前台；返回顶成功的 HWND，失败返回 None。

        先试 `preferred`（已知的目标会话窗口），再退回微信主窗口。只做一次，
        不循环重试：这一步是兜底，不是重试框架。
        """
        candidates: list[int] = []
        if preferred:
            candidates.append(int(preferred))
        main = wechat_main_window()
        if main and main not in candidates:
            candidates.append(int(main))
        for hwnd in candidates:
            if bump_foreground(hwnd):
                log.info("已把微信窗口顶回前台 %s（hwnd=%s）", chat_name, hwnd)
                return hwnd
        log.info("顶前台未生效 %s：候选窗口=%s", chat_name, candidates)
        return None

    def prepare(self, chat_name: str) -> Prepared | SendResult:
        """开窗并跑完所有「真的可以打字了吗」的闸门。"""
        # (b) 先打开会话，再断言前台窗口就是它。顺序不可反。
        try:
            handle = self._driver.open_chat(chat_name)
        except Exception as exc:  # noqa: BLE001 - 驱动层异常类型不稳定，统一折成错误码
            # 常见于前台被提权窗口占住（UIPI）：桌面没锁，但点击注入被拦。
            # 把微信顶回前台后重试一次；错误码语义不变。
            log.warning("打开会话失败 %s：%s；尝试把微信顶回前台后重试一次", chat_name, exc)
            if self._recover_foreground(chat_name) is None:
                return self._open_chat_failure(chat_name, exc)
            try:
                handle = self._driver.open_chat(chat_name)
            except Exception as retry_exc:  # noqa: BLE001 - 同上，重试失败仍折成错误码
                log.warning("重试打开会话仍失败 %s：%s", chat_name, retry_exc)
                return self._open_chat_failure(chat_name, retry_exc)

        target = _window_handle(getattr(handle, "window", None))
        foreground = get_foreground_window()
        if target is None or foreground is None or target != foreground:
            log.warning(
                "前台冲突 %s：目标窗口=%s 当前前台=%s",
                chat_name,
                target,
                foreground,
            )
            if target is not None:
                self._recover_foreground(chat_name, preferred=target)
                foreground = get_foreground_window()
            if target is None or foreground is None or target != foreground:
                return SendResult(
                    False,
                    "foreground_conflict",
                    "failed",
                    chat_name=chat_name,
                    detail=f"target={target} foreground={foreground}",
                )

        # (c) 用户刚有输入就不抢前台。注意：pyautogui 的按键同样会刷新系统空闲
        #     计时，轮询刚翻完消息时这里的 age 会是 0.0x 秒——必须先把「自己按的」
        #     排除掉，否则每条回复都会被自己的轮询拦成 foreground_busy。
        if bool(_cfg(self._config, "require_foreground_idle", True)):
            age = last_input_age()
            if (
                age is not None
                and age < FOREGROUND_IDLE_SECONDS
                and not input_was_self_injected(age, self._driver)
            ):
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
            except Exception:
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

        return Prepared(chat_name=chat_name, handle=handle)

    # ------------------------------------------------------------- 发送
    def send_media(
        self, chat_name: str, kind: str, path: str, name: str = ""
    ) -> SendResult:
        """UIA 通道明确不支持媒体发送：返回 `unsupported_media`，不碰桌面。"""
        del kind, path, name
        return SendResult(
            False,
            "unsupported_media",
            "failed",
            chat_name=str(chat_name or ""),
            detail="UIA 通道不支持媒体发送",
            not_sent=True,
        )

    def send(self, prepared: Prepared, text: str, delay: float) -> SendResult:
        chat_name = prepared.chat_name
        try:
            self._driver.send_text(chat_name, text, delay)
        except DriverError as exc:
            log.warning("发送失败 %s：%s", chat_name, exc)
            return SendResult(
                False, exc.code, "failed", chat_name=chat_name, detail=str(exc)
            )
        except Exception as exc:
            log.exception("发送异常 %s", chat_name)
            return SendResult(
                False,
                "send_failed",
                "failed",
                chat_name=chat_name,
                detail=f"{type(exc).__name__}: {exc}",
            )

        # 回轮询确认气泡；没看到就是 failed，绝不自动重试。
        try:
            snapshot = self._driver.snapshot(prepared.handle)
        except DriverError as exc:
            log.warning("发送后回轮询失败 %s：%s", chat_name, exc)
            return SendResult(
                False,
                "not_observed",
                "failed",
                chat_name=chat_name,
                detail=f"snapshot failed: {exc.code}",
            )
        except Exception as exc:
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


class Sender:
    """桥内唯一发送入口。

    参数全部可注入，测试不碰真微信、不真的 `sleep`：

    - `driver`：驱动模块（默认 `driver.py`），只需 `open_chat/snapshot/send_text`；
    - `clock` / `sleep`：假时钟用来验证节流间隔；
    - `health`：鸭子类型，提供 `desktop`、`degraded`、`may_send()`；
    - `uniform`：打字延迟分布，默认 `random.uniform`；
    - `transport`：出站传输（默认 `UiaTransport`）；hook/auto 等后端由
      `outbound.build_transport()` 构造，必须提供 `prepare(chat_name)` /
      `send(prepared, text, delay)` 与 `requires_desktop`。
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
        transport: Any = None,
    ) -> None:
        self._config = config
        self._driver = driver
        self._clock = clock
        self._sleep = sleep
        self._health = health
        self._on_alert = on_alert
        self._uniform = uniform
        self._epoch = int(session_epoch) if session_epoch is not None else _new_epoch()
        self._transport = (
            transport
            if transport is not None
            else UiaTransport(
                config,
                driver=driver,
                health=health,
                session_epoch=self._epoch,
            )
        )

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
        self.alerts: deque[tuple[str, dict[str, Any]]] = deque(maxlen=256)

        # hook 后端不需要 UIA：不要为了「固定 GlobalConfig」去 import pyweixin。
        if getattr(self._transport, "requires_desktop", False) or getattr(
            self._transport, "may_use_desktop", False
        ):
            self._force_global_config_at_startup()

    # ------------------------------------------------------------- 生命周期
    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self, timeout: float = 5.0) -> None:
        """停止工作线程；队列里已入队的任务会先执行完（FIFO 语义）。

        幂等：abort() 之后仍会 join 工作线程（旧实现看到 ``_closed`` 就直接
        return，会让 stop 路径漏掉 join）。
        """
        with self._worker_lock:
            self._closed = True
            worker = self._worker
            if worker is not None and worker.is_alive():
                self._queue.put(None)
        if worker is not None:
            worker.join(timeout)

    def abort(self) -> int:
        """非阻塞地关闭发送器并丢弃队列里还没开始的发送，返回丢弃条数。

        与 ``close()`` 的 FIFO 语义不同：停机时不能把队列里的发送一条条发完
        （20s 节奏下可能拖几分钟）。已入队的 future 用 ``closed`` 结果完成，
        而不是 cancel —— ``concurrent.futures.CancelledError`` 会让调用方误报
        internal_error。正在 ``_execute`` 里的单次发送无法从外部打断，但驱动
        代理会在下一次 UI 调用前发现桥已停止并失败。
        """
        cancelled = 0
        with self._worker_lock:
            self._closed = True
            worker = self._worker
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if item is not None and item.future.set_running_or_notify_cancel():
                        item.future.set_result(
                            SendResult(
                                False,
                                "closed",
                                "failed",
                                chat_name=item.chat_name,
                                detail="发送器已中止",
                            )
                        )
                        cancelled += 1
                finally:
                    self._queue.task_done()
            if worker is not None and worker.is_alive():
                self._queue.put(None)
        return cancelled

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def transport(self) -> Any:
        """当前传输后端（UIA / hook / auto），供桥做能力探测。"""
        return self._transport

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
    def submit(
        self, chat_name: str, text: str, at: tuple[str, ...] = ()
    ) -> SendResult:
        """入队并等待本条的发送结果。

        `submit()` 可以并发调用；返回顺序不必等于完成顺序，但驱动调用严格按入队
        顺序串行执行，不会交叉。
        """
        return self.submit_async(chat_name, text, at).result()

    def submit_async(
        self, chat_name: str, text: str, at: tuple[str, ...] = ()
    ) -> Future[SendResult]:
        """入队并立即返回 future；不阻塞调用线程。

        生产桥用 ``await asyncio.wrap_future(sender.submit_async(...))``：事件
        循环线程只等 future，真正的驱动调用仍在 Sender 自己的 worker 上（再经
        UiThread 代理回到 STA 线程）。等待超时取消 future 时，尚未被 worker
        取走的发送会被跳过，不会出现「回执超时但消息仍发出」。
        """
        name = str(chat_name or "").strip()
        body = "" if text is None else str(text)
        if not name:
            return _completed(
                SendResult(False, "invalid_argument", "failed", detail="会话显示名为空")
            )
        if not body:
            return _completed(
                SendResult(False, "invalid_argument", "failed", detail="消息内容为空")
            )
        if self._closed:
            return _completed(
                SendResult(False, "closed", "failed", detail="发送器已关闭")
            )
        mentions = tuple(str(item or "").strip() for item in at if str(item or "").strip())

        job = _Job(name, body, at=mentions)
        with self._worker_lock:
            if self._closed:
                return _completed(
                    SendResult(False, "closed", "failed", detail="发送器已关闭")
                )
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run,
                    name="wechat-bridge-sender",
                    daemon=True,
                )
                self._worker.start()
            self._queue.put(job)
        return job.future

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                if not item.future.set_running_or_notify_cancel():
                    continue
                try:
                    result = self._execute(item.chat_name, item.text, item.at)
                except BaseException as exc:
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

        全部走 driver 的能力：优先 `driver.force_global_config()`（生产路径由
        `_UiThreadDriver` 路由到 STA/UIA 线程），其次兼容 `_force_global_config` +
        `_pyweixin_or_none` 这对旧入口。sender 自身（以及任何非 driver 模块）绝不
        加载 pyweixin；两条路都不可用时只记告警，因为 `send_text` 前还会再固定一次。
        """
        force_all = getattr(self._driver, "force_global_config", None)
        if callable(force_all):
            try:
                force_all()
            except Exception:
                log.warning("启动时固定 GlobalConfig 失败", exc_info=True)
            return
        force = getattr(self._driver, "_force_global_config", None)
        getter = getattr(self._driver, "_pyweixin_or_none", None)
        if callable(force) and callable(getter):
            try:
                wx = getter()
                if wx is not None:
                    force(wx)
            except Exception:
                log.warning("启动时固定 GlobalConfig 失败", exc_info=True)
            return
        log.warning("driver 缺少 force_global_config 能力，跳过启动固定 GlobalConfig")

    # ------------------------------------------------------------- 闸门 + 发送
    def _execute(
        self, chat_name: str, text: str, at: tuple[str, ...] = ()
    ) -> SendResult:
        # 已 abort/close：worker 可能刚取到 job 还没开始执行，直接回 closed。
        if self._closed:
            return SendResult(
                False, "closed", "failed", chat_name=chat_name, detail="发送器已关闭"
            )

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

        # (a2) @ 定向：先在传输层准备之前判定能力，UIA 不支持时不要白开一次窗。
        at_handler = getattr(self._transport, "send_at", None) if at else None
        if at and not callable(at_handler):
            return SendResult(
                False,
                "unsupported_at",
                "failed",
                chat_name=chat_name,
                detail="当前传输后端不支持定向 @",
                not_sent=True,
            )

        # (b) 传输层准备：UIA 在这里开窗/断言前台/检查锁屏与健康；
        #     hook 在这里解析群 username。失败直接回失败，绝不继续排期。
        try:
            prepared = self._transport.prepare(chat_name)
        except DriverError as exc:
            log.warning("发送准备失败 %s：%s", chat_name, exc)
            return SendResult(
                False, exc.code, "failed", chat_name=chat_name, detail=str(exc)
            )
        except Exception as exc:
            log.exception("发送准备异常 %s", chat_name)
            return SendResult(
                False,
                "prepare_failed",
                "failed",
                chat_name=chat_name,
                detail=f"{type(exc).__name__}: {exc}",
            )
        if isinstance(prepared, SendResult):
            return prepared

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

        # 所有闸门通过：打字延迟取配置区间，交给传输层。
        low, high = _typing_range(
            _cfg(self._config, "typing_delay_seconds", (0.0, 0.0))
        )
        delay = float(self._uniform(low, high))

        self._record_send(now)
        try:
            if callable(at_handler):
                result = at_handler(prepared, text, at, delay)
            else:
                result = self._transport.send(prepared, text, delay)
        except DriverError as exc:
            log.warning("发送失败 %s：%s", chat_name, exc)
            result = SendResult(
                False, exc.code, "failed", chat_name=chat_name, detail=str(exc)
            )
        except Exception as exc:
            log.exception("发送异常 %s", chat_name)
            result = SendResult(
                False,
                "send_failed",
                "failed",
                chat_name=chat_name,
                detail=f"{type(exc).__name__}: {exc}",
            )
        # 与旧路径一致：只要真的发起过发送（无论成功/失败），都刷新节流时钟。
        self._last_send_at = float(self._clock())
        if not isinstance(result, SendResult):
            log.error("传输层返回了非 SendResult：%r", type(result))
            return SendResult(
                False,
                "internal_error",
                "failed",
                chat_name=chat_name,
                detail="transport returned a non-SendResult value",
            )
        return result

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
            except Exception:
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
