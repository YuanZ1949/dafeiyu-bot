"""桥内健康探测：桌面状态、完整性级别、微信版本/语言与发送闸门。

这个模块只做**只读判断**，不打开 socket、不排队、不持有消息、不 import
pyweixin。发送器在真正打字之前调用 :meth:`HealthProbe.may_send`：

- ``False`` 时给出明确原因（锁屏、版本过低、语言不符、完整性不一致、微信没起）；
- ``True`` 时调用方立刻发，不需要本模块保存任何待发内容。

桌面状态采用 **fail-closed**：``OpenInputDesktop`` / 桌面名 / 前台窗口任何一环
读不出来，一律判 ``"locked"``，绝不默认 ``"interactive"``。这样宁可少发，也不会
在锁屏或安全桌面上盲打。

所有 Windows API 都是惰性调用（模块 import 阶段不碰 ``ctypes.WinDLL``），测试可以
直接 monkeypatch :func:`_detect_desktop` / :func:`_wechat_pids` 等函数。
"""

from __future__ import annotations

import ctypes
import re
import sys
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Callable, Iterator

from dafeiyu.env_probe import _integrity_from_sid, _read_integrity_sid

from . import driver

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查，运行时不 import 重配置模块
    from dafeiyu.config import WeChatConfig

__all__ = [
    "DESKTOP_INTERACTIVE",
    "DESKTOP_LOCKED",
    "DESKTOP_NO_WECHAT",
    "INTEGRITY_MATCHING",
    "INTEGRITY_MISMATCHED",
    "INTEGRITY_UNKNOWN",
    "HealthProbe",
    "probe",
]

PROTOCOL_VERSION = 1

DESKTOP_INTERACTIVE = "interactive"
DESKTOP_LOCKED = "locked"
DESKTOP_NO_WECHAT = "no_wechat"

INTEGRITY_MATCHING = "matching"
INTEGRITY_MISMATCHED = "mismatched"
INTEGRITY_UNKNOWN = "unknown"

_DEFAULT_VERSION_MIN = "4.1.6"
_DEFAULT_LANGUAGE = "zh-CN"
_DEFAULT_POLL_SECONDS = 8.0

# 微信 Windows 客户端进程名：3.x 是 WeChat.exe，4.x（2024 起改版）是 Weixin.exe。
# WECHAT_EXE 保留旧的单名常量向后兼容；实际匹配用 WECHAT_EXES 集合（均小写）。
WECHAT_EXE = "wechat.exe"
WEIXIN_EXE = "weixin.exe"
WECHAT_EXES = frozenset({WECHAT_EXE, WEIXIN_EXE})

# 锁屏 UI 进程名：LockApp.exe（锁屏壳）/ LogonUI.exe（登录安全界面）。
_LOCK_UI_EXES = frozenset({"lockapp.exe", "logonui.exe"})

_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_UOI_NAME = 2
_DESKTOP_READOBJECTS = 0x0001

_VERSION_PART = re.compile(r"\d+")


def _cfg(config: Any | None, name: str, default: Any) -> Any:
    """从 WeChatConfig（或任意鸭子类型）取值；缺失/None 时回落到默认值。"""
    if config is None:
        return default
    value = getattr(config, name, None)
    return default if value is None else value


def _version_tuple(value: Any) -> tuple[int, ...] | None:
    """``"4.1.6.14"`` -> ``(4, 1, 6, 14)``；没有数字时返回 None。"""
    if value is None:
        return None
    parts = _VERSION_PART.findall(str(value))
    if not parts:
        return None
    return tuple(int(part) for part in parts)


def _compare_versions(left: Any, right: Any) -> int | None:
    """按数字段比较版本；短版本补 0 对齐（4.1.6 == 4.1.6.0）。"""
    left_parts = _version_tuple(left)
    right_parts = _version_tuple(right)
    if left_parts is None or right_parts is None:
        return None
    width = max(len(left_parts), len(right_parts))
    left_parts += (0,) * (width - len(left_parts))
    right_parts += (0,) * (width - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)


def _normalize_language(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text.casefold().replace("_", "-")


def _as_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_wechat_exe(name: Any) -> bool:
    """进程名是否是微信客户端：4.x ``Weixin.exe`` / 3.x ``WeChat.exe``，大小写不敏感。"""
    return str(name or "").strip().casefold() in WECHAT_EXES


def _is_lock_ui_exe(name: Any) -> bool:
    """进程镜像名是否属于锁屏 UI（``LockApp.exe`` / ``LogonUI.exe``，大小写不敏感；
    允许传入完整路径，按最后一段 ``\\`` 之后的文件名判断）。"""
    return str(name or "").strip().casefold().rsplit("\\", 1)[-1] in _LOCK_UI_EXES


# ---- Windows 平台封装 -------------------------------------------------
def _win_dll(name: str) -> Any | None:
    """非 Windows 返回 None；Windows 上取 DLL 句柄（不在此处抛异常）。"""
    if sys.platform != "win32":
        return None
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:  # pragma: no cover - 只出现在非标准运行时
        return None
    try:
        return win_dll(name, use_last_error=True)
    except OSError:  # pragma: no cover - Windows 上调用系统 DLL 不会失败
        return None


def _wintypes() -> Any:
    # ctypes.wintypes 只在 Windows 可用，必须在平台判断之后再 import。
    from ctypes import wintypes

    return wintypes


def _open_input_desktop() -> int:
    """打开当前输入桌面；失败（锁屏/无权限）返回 0。"""
    user32 = _win_dll("user32")
    if user32 is None:
        return 0
    wintypes = _wintypes()
    user32.OpenInputDesktop.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    handle = user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    return int(handle or 0)


def _desktop_name(handle: int) -> str:
    """读桌面名：可交互会话是 ``"Default"``，锁屏是 ``"Winlogon"`` 等。"""
    user32 = _win_dll("user32")
    if user32 is None or not handle:
        return ""
    wintypes = _wintypes()
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetUserObjectInformationW.restype = wintypes.BOOL

    needed = wintypes.DWORD(0)
    user32.GetUserObjectInformationW(handle, _UOI_NAME, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        return ""
    # needed 是字节数；create_unicode_buffer 收字符数，多给一倍足够。
    buffer = ctypes.create_unicode_buffer(needed.value)
    if not user32.GetUserObjectInformationW(
        handle,
        _UOI_NAME,
        ctypes.cast(buffer, ctypes.c_void_p),
        ctypes.sizeof(buffer),
        ctypes.byref(needed),
    ):
        return ""
    return buffer.value or ""


def _foreground_window() -> int:
    user32 = _win_dll("user32")
    if user32 is None:
        return 0
    wintypes = _wintypes()
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    return int(user32.GetForegroundWindow() or 0)


def _foreground_pid(hwnd: int) -> int:
    """前台窗口所属进程 pid（GetWindowThreadProcessId）；读不到返回 0。"""
    user32 = _win_dll("user32")
    if user32 is None or not hwnd:
        return 0
    wintypes = _wintypes()
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_image_name(pid: int) -> str:
    """读进程完整映像路径（OpenProcess + QueryFullProcessImageNameW）；失败返回空串。"""
    if sys.platform != "win32" or not pid:
        return ""
    kernel32 = _win_dll("kernel32")
    if kernel32 is None:
        return ""
    wintypes = _wintypes()
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
    )
    if not process:
        return ""
    try:
        size = wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return buffer.value or ""
    finally:
        kernel32.CloseHandle(process)


def _close_desktop(handle: int) -> None:
    user32 = _win_dll("user32")
    if user32 is None or not handle:
        return
    wintypes = _wintypes()
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    user32.CloseDesktop(handle)


def _desktop_locked_by_lock_ui(foreground: int) -> bool:
    """补充检查：前台窗口属于锁屏 UI，或 ``LogonUI.exe`` 正在运行。

    读不到 pid / 映像名视为「未检出」，交给调用方沿用旧判断；只要正面命中
    ``LockApp.exe`` / ``LogonUI.exe`` 就返回 True——锁屏会话里 LockApp 会
    抢占前台，仅凭「Default 桌面 + 有前台」会误判成 interactive。
    """
    pid = _foreground_pid(foreground)
    if pid:
        image = _process_image_name(pid)
        if image and _is_lock_ui_exe(image):
            return True
    # LogonUI.exe 存在即登录安全界面在跑（前台窗口读不到时也能兜住）。
    return any(_is_lock_ui_exe(name) for name in _process_names())


def _detect_desktop() -> str:
    """返回 ``interactive`` / ``locked``；任何不确定都按 locked 处理。"""
    if sys.platform != "win32":
        return DESKTOP_LOCKED

    handle = 0
    try:
        handle = _open_input_desktop()
        if not handle:
            return DESKTOP_LOCKED
        name = _desktop_name(handle)
        foreground = _foreground_window()
    except (OSError, ValueError, AttributeError):
        return DESKTOP_LOCKED
    finally:
        if handle:
            with suppress(OSError, ValueError, AttributeError):
                _close_desktop(handle)

    if (name or "").strip().casefold() != "default":
        return DESKTOP_LOCKED
    if not foreground:
        return DESKTOP_LOCKED
    # 到这里按旧规则已是 interactive；再挡一道假前台：锁屏会话里前台窗口
    # 多半是 LockApp/LogonUI 的。新探针抛错时不能把已确认 interactive 的
    # 结论翻转成 locked（未检出 ≠ 检出）；一旦正面检出锁屏 UI 必须判 locked。
    try:
        if _desktop_locked_by_lock_ui(foreground):
            return DESKTOP_LOCKED
    except (OSError, ValueError, AttributeError):
        pass
    return DESKTOP_INTERACTIVE


def _snap_processes() -> Iterator[tuple[str, int]]:
    """Toolhelp 枚举全部进程，产出 ``(进程名, pid)``；非 Windows 直接结束。

    快照失败（权限/资源不足）向上抛异常，由调用方按各自契约降级——与
    旧 ``_wechat_pids`` 的行为一致。
    """
    if sys.platform != "win32":
        return
    kernel32 = _win_dll("kernel32")
    if kernel32 is None:
        return
    wintypes = _wintypes()

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    snapshot_value = int(snapshot or 0)
    invalid_handle = int(ctypes.c_void_p(-1).value or 0)
    if snapshot_value in (0, invalid_handle):
        return

    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            yield str(entry.szExeFile), int(entry.th32ProcessID)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)


def _process_names() -> list[str]:
    """当前正在运行的进程映像名列表；非 Windows / 快照失败返回空列表。"""
    return [name for name, _ in _snap_processes()]


def _wechat_pids() -> list[int]:
    """枚举微信客户端进程：4.x 是 ``Weixin.exe``，3.x 是 ``WeChat.exe``（大小写
    不敏感）；非 Windows / 失败返回空列表。"""
    return [pid for name, pid in _snap_processes() if _is_wechat_exe(name)]


def _process_integrity_sid(pid: int) -> str:
    """读目标进程令牌的完整性 SID（``S-1-16-*``）；失败返回空串。"""
    if sys.platform != "win32" or not pid:
        return ""
    kernel32 = _win_dll("kernel32")
    advapi32 = _win_dll("advapi32")
    if kernel32 is None or advapi32 is None:
        return ""
    wintypes = _wintypes()

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    process = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
    )
    if not process:
        return ""
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(
            process, _TOKEN_QUERY, ctypes.byref(token)
        ):
            return ""
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size)
        )
        if size.value == 0:
            return ""
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_INTEGRITY_LEVEL,
            ctypes.cast(buffer, ctypes.c_void_p),
            size.value,
            ctypes.byref(size),
        ):
            return ""
        label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(label.Label.Sid, ctypes.byref(sid)):
            return ""
        try:
            return sid.value or ""
        finally:
            kernel32.LocalFree(sid)
    finally:
        if token.value:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


def _own_integrity_sid() -> str:
    """复用 env_probe 的当前进程令牌读取，避免两套实现漂移。"""
    return _read_integrity_sid()


def _integrity_state(running: bool) -> str:
    """把「本进程 vs WeChat.exe」的完整性级别折成三态。"""
    if not running:
        return INTEGRITY_UNKNOWN
    try:
        pids = _wechat_pids()
    except (OSError, ValueError, AttributeError):
        return INTEGRITY_UNKNOWN
    if not pids:
        return INTEGRITY_UNKNOWN

    own_level = _integrity_from_sid(_own_integrity_sid())
    if own_level == INTEGRITY_UNKNOWN:
        return INTEGRITY_UNKNOWN

    saw_match = False
    for pid in pids:
        try:
            level = _integrity_from_sid(_process_integrity_sid(pid))
        except (OSError, ValueError, AttributeError):
            continue
        if level == INTEGRITY_UNKNOWN:
            continue
        if level != own_level:
            return INTEGRITY_MISMATCHED
        saw_match = True
    return INTEGRITY_MATCHING if saw_match else INTEGRITY_UNKNOWN


# ---- 探测与发送闸门 ---------------------------------------------------
def probe(
    config: WeChatConfig | None = None,
    *,
    connected: bool = True,
    queue_depth: int = 0,
    last_event_at: int | None = None,
    last_send_at: int | None = None,
) -> dict[str, Any]:
    """生成一帧 v1 health 信封（字段见协议 §5）。

    ``connected`` 由调用方（持有 socket 的桥主循环）传入；本模块不碰网络。
    """
    version_min = str(_cfg(config, "version_min", _DEFAULT_VERSION_MIN))
    expected_language = _normalize_language(
        _cfg(config, "language", _DEFAULT_LANGUAGE)
    )

    reasons: list[str] = []

    try:
        running = bool(driver.is_running())
    except Exception:  # noqa: BLE001 - 探测失败必须变成健康数据，不能崩主循环
        running = False
        reasons.append("is_running_failed")

    wechat_version: str | None = None
    language: str | None = None
    integrity = INTEGRITY_UNKNOWN
    logged_in = False

    if not running:
        desktop = DESKTOP_NO_WECHAT
        reasons.append("no_wechat")
    else:
        try:
            desktop = _detect_desktop()
        except Exception:  # noqa: BLE001 - 探测失败按锁屏处理（fail-closed）
            desktop = DESKTOP_LOCKED
            reasons.append("desktop_probe_failed")

        try:
            info = driver.about()
        except Exception:  # noqa: BLE001 - 读不到就降级，不吞掉
            info = None
            reasons.append("about_failed")
        if isinstance(info, dict):
            raw_version = info.get("version")
            if raw_version:
                wechat_version = str(raw_version)
            # about() 能读到信息即说明微信已登录并可被驱动访问。
            logged_in = True

        try:
            language = driver.language()
        except Exception:  # noqa: BLE001
            language = None
            reasons.append("language_unknown")

        try:
            integrity = _integrity_state(True)
        except Exception:  # noqa: BLE001 - 读不到完整性时按 unknown 降级
            integrity = INTEGRITY_UNKNOWN

        if desktop != DESKTOP_INTERACTIVE:
            reasons.append("desktop_locked")

        if wechat_version is None:
            reasons.append("version_unknown")
        else:
            comparison = _compare_versions(wechat_version, version_min)
            if comparison is None:
                reasons.append("version_unparsable")
            elif comparison < 0:
                reasons.append("version_too_old")

        if language is not None and _normalize_language(language) != expected_language:
            reasons.append("language_mismatch")

        if integrity == INTEGRITY_MISMATCHED:
            reasons.append("integrity_mismatch")
        elif integrity == INTEGRITY_UNKNOWN:
            reasons.append("integrity_unknown")

    seen: set[str] = set()
    unique_reasons = [r for r in reasons if not (r in seen or seen.add(r))]

    return {
        "v": PROTOCOL_VERSION,
        "type": "health",
        "connected": bool(connected),
        "logged_in": bool(logged_in),
        "desktop": desktop,
        "wechat_version": wechat_version,
        "language": language,
        "integrity": integrity,
        "queue_depth": _as_int_or_none(queue_depth) or 0,
        "last_event_at": _as_int_or_none(last_event_at),
        "last_send_at": _as_int_or_none(last_send_at),
        "degraded": bool(unique_reasons),
        "reason": ";".join(unique_reasons) if unique_reasons else None,
    }


class HealthProbe:
    """有状态的探针壳：缓存最近一次帧，供发送器读取闸门、主循环周期上报。

    只保存时间戳/计数，**不保存消息**。拒绝发送时不给调用方任何「稍后重发」
    的入口：下一次 :meth:`may_send` 就是一次全新探测，锁屏恢复后也不会自动
    补发之前被拒绝的内容。
    """

    def __init__(
        self,
        config: WeChatConfig | None = None,
        *,
        connected: bool | Callable[[], bool] = True,
        poll_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self._connected = connected
        if poll_seconds is None:
            poll_seconds = float(
                _cfg(config, "poll_seconds", _DEFAULT_POLL_SECONDS)
            )
        self.poll_seconds = float(poll_seconds)
        self.queue_depth = 0
        self.last_event_at: int | None = None
        self.last_send_at: int | None = None
        self._last: dict[str, Any] | None = None
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep

    @property
    def connected(self) -> bool:
        value = self._connected
        return bool(value() if callable(value) else value)

    @connected.setter
    def connected(self, value: bool | Callable[[], bool]) -> None:
        self._connected = value

    def probe(self) -> dict[str, Any]:
        frame = probe(
            self.config,
            connected=self.connected,
            queue_depth=self.queue_depth,
            last_event_at=self.last_event_at,
            last_send_at=self.last_send_at,
        )
        self._last = frame
        return frame

    def _frame(self) -> dict[str, Any]:
        return self._last if self._last is not None else self.probe()

    @property
    def degraded(self) -> bool:
        return bool(self._frame()["degraded"])

    @property
    def desktop(self) -> str:
        return str(self._frame()["desktop"])

    def may_send(self) -> tuple[bool, str | None]:
        """发送闸门。无参数、无队列：``False`` 时调用方直接丢弃本条即可。"""
        frame = self.probe()
        if frame["degraded"]:
            return False, frame["reason"] or "degraded"
        return True, None

    def mark_event(self, ts: int | float | None = None) -> None:
        self.last_event_at = int(self._clock() if ts is None else ts)

    def mark_send(self, ts: int | float | None = None) -> None:
        self.last_send_at = int(self._clock() if ts is None else ts)

    def frames(self, iterations: int | None = None) -> Iterator[dict[str, Any]]:
        """周期产出 health 帧；socket 由调用方持有，本模块不建连接。"""
        produced = 0
        while iterations is None or produced < iterations:
            yield self.probe()
            produced += 1
            if iterations is not None and produced >= iterations:
                return
            self._sleep(self.poll_seconds)

    def emit(self, iterations: int | None = None) -> Iterator[dict[str, Any]]:
        yield from self.frames(iterations)
