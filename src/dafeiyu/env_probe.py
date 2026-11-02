"""运行环境自检：进程完整性级别 + 临时目录可写性（P4/P5）。

**只允许 import 标准库**：``scripts/setup.ps1`` 在安装依赖之前就会用系统
Python 执行 ``python -m dafeiyu.env_probe``，那时 yaml/dotenv 都还没装。

Windows 下 DSH 沙箱可能给工作区打上 Low 完整性标签，里面的
python.exe / node.exe / opencode.exe 也跟着变成 Low 完整性，写
``%TEMP%`` 和用户目录直接 EPERM —— 看起来像杀毒软件拦截，实际不是
（见 docs/deployment-notes-windows.md 的 P4/P5）。这里把判断固化成代码，
让安装和体检阶段能早点给出正确提示。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

LOW = "low"
MEDIUM = "medium"
HIGH = "high"
SYSTEM = "system"
UNKNOWN = "unknown"

# TokenIntegrityLevel 返回的完整性 SID 最后一级 subauthority（十进制）：
# 4096 = Low，8192 = Medium，12288 = High，16384 = System。
_INTEGRITY_BY_RID: dict[int, str] = {
    0x1000: LOW,
    0x2000: MEDIUM,
    0x3000: HIGH,
    0x4000: SYSTEM,
}

_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25

# 临时目录探测失败时，detail 里统一带上这个前缀，方便测试与提示文案复用。
TEMP_FAILURE_PREFIX = "临时目录写入失败"


@dataclass(frozen=True)
class EnvProbe:
    """一次环境探测的结果。integrity 取 low/medium/high/system/unknown。"""

    integrity: str
    temp_writable: bool
    detail: str


def _integrity_from_sid(sid: str) -> str:
    """把 ``S-1-16-4096`` 这类完整性 SID 映射成 low/medium/high/system。

    非完整性 SID（不是 ``S-1-16-*``）或无法解析时返回 ``unknown``。
    """
    parts = [part for part in (sid or "").strip().split("-") if part]
    if len(parts) != 4 or parts[0].upper() != "S" or parts[1] != "1" or parts[2] != "16":
        return UNKNOWN
    try:
        rid = int(parts[3])
    except ValueError:
        return UNKNOWN
    return _INTEGRITY_BY_RID.get(rid, UNKNOWN)


def _read_integrity_sid() -> str:
    """读当前进程令牌的 IntegrityLevel SID；非 Windows 或读取失败返回空串。"""
    if sys.platform != "win32":
        return ""
    try:
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - 只出现在极端精简的运行时
        return ""

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:  # pragma: no cover - Windows 上不会发生
        return ""

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

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

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        return ""
    try:
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
        kernel32.CloseHandle(token)


def _temp_write_probe() -> None:
    """在系统临时目录建目录 -> 写一个小文件 -> 清理；任何 OSError 都向上抛。"""
    directory = tempfile.mkdtemp(prefix="dafeiyu-env-")
    try:
        (Path(directory) / "probe.txt").write_text("ok", encoding="utf-8")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def probe_environment() -> EnvProbe:
    """探测当前进程的完整性级别与临时目录可写性。"""
    sid = _read_integrity_sid()
    integrity = _integrity_from_sid(sid) if sid else UNKNOWN

    temp_writable = True
    detail_parts: list[str] = []
    if sid:
        detail_parts.append(f"完整性 SID：{sid}")
    else:
        detail_parts.append("未能读取进程完整性级别")
    try:
        _temp_write_probe()
    except OSError as exc:
        temp_writable = False
        detail_parts.append(f"{TEMP_FAILURE_PREFIX}：{exc}")
    else:
        detail_parts.append("临时目录可写")

    return EnvProbe(
        integrity=integrity,
        temp_writable=temp_writable,
        detail="；".join(detail_parts),
    )


def _has_problem(probe: EnvProbe) -> bool:
    return probe.integrity == LOW or not probe.temp_writable


def format_report(probe: EnvProbe) -> str:
    """把探测结果排成给人看的中文报告。"""
    lines = ["运行环境自检（沙箱 / 完整性级别）"]
    lines.append(f"  进程完整性级别：{probe.integrity}")
    lines.append(f"  临时目录可写：{'是' if probe.temp_writable else '否'}")
    if probe.detail:
        lines.append(f"  详情：{probe.detail}")
    if _has_problem(probe):
        lines.append("")
        lines.append("发现问题：当前进程可能被沙箱降权，写用户目录 / 临时目录会 EPERM。")
        lines.append("这通常不是杀毒软件拦截 —— 先查完整性级别，别怀疑杀软。")
        lines.append("详见 docs/deployment-notes-windows.md 的 P4/P5：换一个干净工作区，")
        lines.append("或把 DSH 权限切成「完全访问」后重新创建 .venv。")
    else:
        lines.append("")
        lines.append("结论：环境正常，可以继续安装 / 启动。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dafeiyu.env_probe",
        description="检查 Windows 沙箱/权限是否会导致写临时目录 EPERM（P4/P5）。",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出探测结果")
    args = parser.parse_args(argv)

    probe = probe_environment()
    if args.json:
        payload = {
            "integrity": probe.integrity,
            "temp_writable": probe.temp_writable,
            "detail": probe.detail,
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(format_report(probe))
    return 1 if _has_problem(probe) else 0


if __name__ == "__main__":
    raise SystemExit(main())
