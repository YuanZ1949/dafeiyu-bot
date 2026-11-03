"""RDP + ``tscon`` 会话交接：让控制台保持解锁，UIA 出站不再怕锁屏。

微信桌面自动化（UIA）只能在**已解锁的交互桌面**上打字。锁屏时有两个办法：

1. 接一个 HTTP hook 后端（见 ``gateway/wechat_bridge/outbound.py``）；
2. 用 RDP 连进机器人那台机器，再把会话交回物理控制台：
   ``tscon <session> /dest:console``。会话回到控制台后**不会重新锁屏**，
   桌面继续可交互，UIA 照常工作；RDP 客户端会立即断开（预期行为）。

本模块只负责第 2 条的可观测部分：

- 判定当前进程是否在 RDP 会话（``WTSClientProtocolType``，失败时回退
  ``SESSIONNAME`` 前缀）；
- 生成可读、可 dry-run 的交接计划（不偷偷执行）；
- 在管理员权限下调用 ``tscon``，并在返回后轮询协议类型确认已交回控制台。

限制与风险：

- ``tscon`` 需要管理员权限；普通权限会明确回 ``needs_admin``；
- 执行后当前 RDP 连接必掉，不适合放进无人值守的自动流程；
- 这本质上是绕过锁屏，只在专用机器人机器上使用；物理安全风险自担。

平台 API 全部惰性加载，本模块在非 Windows 上也能 import（``status`` 会
明确报告 ``unsupported``）。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "HandoffPlan",
    "HandoffResult",
    "SessionState",
    "client_protocol_type",
    "current_session_id",
    "detect",
    "handoff",
    "is_admin",
    "main",
    "plan_handoff",
]

#: WTS_INFO_CLASS 枚举：WTSClientProtocolType。
_WTS_CLIENT_PROTOCOL_TYPE = 16
#: WTSClientProtocolType 的取值（0=控制台，1=ICA，2=RDP）。
_PROTOCOL_CONSOLE = 0
_PROTOCOL_RDP = 2
_PROTOCOL_NAMES = {0: "控制台", 1: "ICA", 2: "RDP"}

#: 交接后轮询确认协议的默认参数。
_VERIFY_ATTEMPTS = 5
_VERIFY_INTERVAL = 0.3


@dataclass(frozen=True)
class SessionState:
    """当前进程所在 Windows 会话的快照。"""

    platform: str
    session_name: str
    session_id: int | None
    protocol: int | None
    is_rdp: bool
    is_admin: bool

    @property
    def supported(self) -> bool:
        return self.platform == "win32"


@dataclass(frozen=True)
class HandoffPlan:
    """``tscon`` 交接计划：先算清楚再决定要不要动手。"""

    action: str
    reason: str
    command: tuple[str, ...] = ()
    state: SessionState | None = None


@dataclass(frozen=True)
class HandoffResult:
    """``handoff()`` 的结果；``verified`` 表示协议已确认不再是 RDP。"""

    ok: bool
    error_code: str | None
    detail: str
    plan: HandoffPlan
    command: tuple[str, ...] = ()
    verified: bool = False


# ------------------------------------------------------------------ 平台探测
def current_session_id() -> int | None:
    """当前进程的 Windows Session ID；非 Windows 或失败返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ProcessIdToSessionId.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
        session = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(
            wintypes.DWORD(os.getpid()), ctypes.byref(session)
        ):
            return None
        return int(session.value)
    except (OSError, AttributeError, ValueError):  # pragma: no cover - 平台相关
        return None


def _wts_query(session_id: int, info_class: int, size: int) -> bytes | None:
    """读一段 WTS 会话信息；失败返回 None（调用方 fail-open）。"""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        wtsapi32.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
        wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        buffer = ctypes.c_void_p()
        returned = wintypes.DWORD()
        if not wtsapi32.WTSQuerySessionInformationW(
            wintypes.HANDLE(0),
            wintypes.DWORD(session_id),
            info_class,
            ctypes.byref(buffer),
            ctypes.byref(returned),
        ):
            return None
        try:
            if not buffer.value or returned.value < size:
                return None
            return ctypes.string_at(buffer, size)
        finally:
            wtsapi32.WTSFreeMemory(buffer)
    except (OSError, AttributeError, ValueError):  # pragma: no cover - 平台相关
        return None


def client_protocol_type(session_id: int | None = None) -> int | None:
    """0=控制台 / 1=ICA / 2=RDP；读不到返回 None。"""
    sid = current_session_id() if session_id is None else session_id
    if sid is None:
        return None
    raw = _wts_query(sid, _WTS_CLIENT_PROTOCOL_TYPE, 2)
    if raw is None:
        return None
    return int.from_bytes(raw, "little")


def is_admin() -> bool:
    """当前进程是否管理员（tscon 的硬性前提）。"""
    if sys.platform != "win32":
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        return bool(shell32.IsUserAnAdmin())
    except (OSError, AttributeError):  # pragma: no cover - 平台相关
        return False


def detect(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> SessionState:
    """汇总当前会话状态；探测失败一律按「不是 RDP」保守处理。"""
    current_platform = sys.platform if platform is None else platform
    environment = os.environ if env is None else env
    name = str(environment.get("SESSIONNAME") or "").strip()
    if current_platform != "win32":
        return SessionState(
            platform=current_platform,
            session_name=name,
            session_id=None,
            protocol=None,
            is_rdp=False,
            is_admin=False,
        )

    session_id = current_session_id()
    protocol = client_protocol_type(session_id)
    if protocol is not None:
        rdp = protocol == _PROTOCOL_RDP
    else:
        # WTS 查询失败时用环境变量兜底：RDP 会话名固定以 RDP- 开头。
        rdp = name.upper().startswith("RDP-")
    return SessionState(
        platform=current_platform,
        session_name=name,
        session_id=session_id,
        protocol=protocol,
        is_rdp=rdp,
        is_admin=is_admin(),
    )


# ------------------------------------------------------------------ 计划/执行
def plan_handoff(state: SessionState) -> HandoffPlan:
    """根据会话状态给出交接计划；不执行任何命令。"""
    if not state.supported:
        return HandoffPlan(
            "unsupported",
            f"{state.platform} 不支持 tscon 会话交接（仅 Windows）",
            state=state,
        )
    if not state.is_rdp:
        shown = state.session_name or "未知"
        return HandoffPlan(
            "noop",
            f"当前不是 RDP 会话（SESSIONNAME={shown}），无需交接",
            state=state,
        )
    if state.session_id is None:
        return HandoffPlan(
            "unknown_session",
            "读不到当前 Session ID，无法构造 tscon 命令",
            state=state,
        )
    command = ("tscon", str(state.session_id), "/dest:console")
    if not state.is_admin:
        return HandoffPlan(
            "needs_admin",
            "tscon 需要管理员权限：用管理员 PowerShell 重新运行 "
            "`python -m dafeiyu.rdp handoff`（或 scripts\\rdp-console.ps1 -Elevate）",
            command,
            state,
        )
    return HandoffPlan(
        "handoff",
        f"把 Session {state.session_id} 交回物理控制台；RDP 连接会立即断开",
        command,
        state,
    )


def _verify_console(
    session_id: int | None,
    *,
    sleep: Callable[[float], None],
    attempts: int,
    interval: float,
) -> bool:
    if session_id is None:
        return False
    for index in range(max(1, attempts)):
        protocol = client_protocol_type(session_id)
        if protocol is not None and protocol != _PROTOCOL_RDP:
            return True
        if index + 1 < attempts:
            sleep(interval)
    return False


def handoff(
    *,
    timeout: float = 15.0,
    runner: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    verify_attempts: int = _VERIFY_ATTEMPTS,
    verify_interval: float = _VERIFY_INTERVAL,
    state: SessionState | None = None,
) -> HandoffResult:
    """按计划执行 ``tscon``；``noop`` 视为成功，其余不可执行状态直接失败。

    ``runner`` / ``sleep`` 可注入，测试不真的执行 tscon、不真的等待。
    """
    current = detect() if state is None else state
    plan = plan_handoff(current)
    if plan.action == "noop":
        return HandoffResult(True, None, plan.reason, plan, verified=True)
    if plan.action != "handoff":
        return HandoffResult(False, plan.action, plan.reason, plan, plan.command)

    run = subprocess.run if runner is None else runner
    command = list(plan.command)
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return HandoffResult(
            False,
            "tscon_missing",
            "找不到 tscon.exe，请确认在 Windows 的 System32 环境里运行",
            plan,
            plan.command,
        )
    except subprocess.TimeoutExpired:
        return HandoffResult(
            False,
            "tscon_timeout",
            f"tscon 超过 {timeout:g} 秒没有返回",
            plan,
            plan.command,
        )
    except OSError as exc:
        return HandoffResult(
            False,
            "tscon_failed",
            f"{type(exc).__name__}: {exc}",
            plan,
            plan.command,
        )

    returncode = int(getattr(completed, "returncode", 1) or 0)
    if returncode != 0:
        stderr = str(getattr(completed, "stderr", "") or "").strip()
        stdout = str(getattr(completed, "stdout", "") or "").strip()
        extra = stderr or stdout
        detail = f"tscon 退出码 {returncode}"
        if extra:
            detail = f"{detail}：{extra}"
        return HandoffResult(
            False, "tscon_failed", detail, plan, plan.command
        )

    verified = _verify_console(
        current.session_id,
        sleep=sleep,
        attempts=verify_attempts,
        interval=verify_interval,
    )
    detail = (
        "会话已交回控制台（RDP 已断开，桌面保持解锁）"
        if verified
        else "tscon 返回 0，但未能确认协议已变为控制台（可能是 WTS 查询失败）"
    )
    return HandoffResult(True, None, detail, plan, plan.command, verified=verified)


# ------------------------------------------------------------------ 输出
def _protocol_text(protocol: int | None) -> str:
    if protocol is None:
        return "未知（WTS 查询失败）"
    return _PROTOCOL_NAMES.get(protocol, f"未知({protocol})")


def _state_payload(state: SessionState, plan: HandoffPlan) -> dict[str, Any]:
    return {
        "session": {
            "platform": state.platform,
            "session_name": state.session_name,
            "session_id": state.session_id,
            "protocol": state.protocol,
            "is_rdp": state.is_rdp,
            "is_admin": state.is_admin,
        },
        "command": list(plan.command),
        "plan": {
            "action": plan.action,
            "reason": plan.reason,
        },
    }


def format_status(state: SessionState, plan: HandoffPlan) -> str:
    sid = "未知" if state.session_id is None else str(state.session_id)
    lines = [
        "RDP / 控制台会话状态",
        f"  平台：{state.platform}",
        f"  会话名：{state.session_name or '（空）'}",
        f"  Session ID：{sid}",
        f"  连接协议：{_protocol_text(state.protocol)}",
        f"  管理员：{'是' if state.is_admin else '否'}",
        f"  计划：{plan.reason}",
    ]
    if plan.command:
        lines.append(f"  命令：{' '.join(plan.command)}")
    return "\n".join(lines)


def format_result(result: HandoffResult) -> str:
    lines = [
        "RDP 会话交接结果",
        f"  结果：{'成功' if result.ok else '失败'}",
        f"  详情：{result.detail}",
    ]
    if result.command:
        lines.append(f"  命令：{' '.join(result.command)}")
    return "\n".join(lines)


def _result_payload(result: HandoffResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "error_code": result.error_code,
        "detail": result.detail,
        "command": list(result.command),
        "verified": result.verified,
        "plan": result.plan.action,
    }


# ------------------------------------------------------------------ 命令行
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dafeiyu.rdp",
        description=(
            "RDP 会话交接：tscon <session> /dest:console，"
            "让控制台保持解锁以便 UIA 发送。"
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "plan", "handoff"],
        help="status=只读状态（默认）；plan=打印命令；handoff=执行交接",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="tscon 超时秒数（默认 15）"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(list(argv) if argv is not None else None)

    state = detect()
    plan = plan_handoff(state)
    if args.action == "handoff":
        result = handoff(timeout=args.timeout, state=state)
        if args.json:
            print(json.dumps(_result_payload(result), ensure_ascii=False))
        else:
            print(format_result(result))
        if result.ok:
            return 0
        return 3 if result.error_code == "needs_admin" else 1

    if args.json:
        print(json.dumps(_state_payload(state, plan), ensure_ascii=False))
    else:
        print(format_status(state, plan))
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动运行
    raise SystemExit(main())
