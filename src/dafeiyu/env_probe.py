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
import importlib.util
import json
import os
import shutil
import socket
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

# 微信通道前置检查的固定 id 与顺序（doctor 输出据此稳定）。
WECHAT_CHECK_IDS = (
    "wechat.process_running",
    "wechat.version_ok",
    "wechat.language_ok",
    "wechat.integrity_match",
    "wechat.desktop_interactive",
    "wechat.driver_importable",
    "wechat.bridge_port_free",
    "wechat.config_ready",
)


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


# ---- 微信通道前置检查 ------------------------------------------------

def _check(check_id: str, status: str, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "detail": detail}


def _as_wechat_config(config):
    """接受 WeChatConfig / Settings / None，统一取到 WeChatConfig（或 None）。"""
    if config is None:
        return None
    inner = getattr(config, "wechat", None)
    return inner if inner is not None else config


def _wechat_pids() -> list[int]:
    from .gateway.wechat_bridge import health

    return list(health._wechat_pids())


def _wechat_about() -> dict:
    from .gateway.wechat_bridge import driver

    return driver.about()


def _wechat_language() -> str:
    from .gateway.wechat_bridge import driver

    return driver.language()


def _wechat_desktop() -> str:
    from .gateway.wechat_bridge import health

    return health._detect_desktop()


def _wechat_integrity_state(running: bool) -> str:
    from .gateway.wechat_bridge import health

    return health._integrity_state(running)


def _compare_versions(left, right):
    from .gateway.wechat_bridge import health

    return health._compare_versions(left, right)


def _normalize_language(value):
    from .gateway.wechat_bridge import health

    return health._normalize_language(value)


def _pyweixin_available() -> bool:
    try:
        return importlib.util.find_spec("pyweixin") is not None
    except (ImportError, ValueError):
        return False


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    # 与 bootstrap._port_free 语义一致：故意不设 SO_REUSEADDR（Windows 下会让
    # 端口被占用时 bind 也成功）；0.0.0.0 / :: / * 用空 host 试所有地址。
    bind_host = "" if host in {"", "0.0.0.0", "::", "*"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind_host, port))
        except OSError:
            return False
    return True


def _current_session_id() -> int | None:
    """返回当前进程所在 Windows Session ID；非 Windows 或失败返回 None。"""
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


def probe_wechat(config=None) -> list[dict[str, str]]:
    """跑 8 项微信通道前置检查，固定顺序返回。

    ``config`` 可以是 ``WeChatConfig``、``Settings``（取 ``.wechat``）或
    ``None``。``enabled=false`` 时 8 项全部 SKIP，不触发任何真实探测。
    """
    cfg = _as_wechat_config(config)
    if cfg is None or not bool(getattr(cfg, "enabled", False)):
        detail = "gateway.wechat.enabled=false，跳过微信前置检查"
        return [_check(cid, "SKIP", detail) for cid in WECHAT_CHECK_IDS]

    version_min = str(getattr(cfg, "version_min", "4.1.6") or "4.1.6")
    expected_language = _normalize_language(
        getattr(cfg, "language", "zh-CN") or "zh-CN"
    )
    checks: list[dict[str, str]] = []
    # 出站走 hook/auto 时不碰 UIA：完整性级别与桌面解锁都不再是前置条件。
    outbound = str(getattr(cfg, "outbound", "uia") or "uia").strip().lower()
    uia_needed = outbound == "uia"

    # (1) process
    pids: list[int] = []
    try:
        pids = _wechat_pids()
    except Exception as exc:  # noqa: BLE001
        checks.append(
            _check("wechat.process_running", "FAIL", f"枚举微信进程失败：{exc}")
        )
    else:
        if pids:
            shown = ", ".join(str(pid) for pid in pids[:5])
            checks.append(
                _check(
                    "wechat.process_running",
                    "PASS",
                    f"发现 {len(pids)} 个 WeChat.exe 进程（PID {shown}）",
                )
            )
        else:
            checks.append(
                _check("wechat.process_running", "FAIL", "未发现 WeChat.exe 进程")
            )
    running = bool(pids)

    driver_ok = False
    try:
        driver_ok = _pyweixin_available()
    except Exception:  # noqa: BLE001
        driver_ok = False

    # (2) version
    if not running:
        checks.append(
            _check("wechat.version_ok", "SKIP", "微信进程未运行，跳过版本检查")
        )
    elif not driver_ok:
        checks.append(
            _check(
                "wechat.version_ok",
                "SKIP",
                "pyweixin 未安装，无法读取微信版本（见 wechat.driver_importable）",
            )
        )
    else:
        try:
            info = _wechat_about()
            version = (
                str((info or {}).get("version") or "").strip()
                if isinstance(info, dict)
                else ""
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                _check("wechat.version_ok", "FAIL", f"读取微信版本失败：{exc}")
            )
        else:
            if not version:
                checks.append(_check("wechat.version_ok", "FAIL", "读不到微信版本"))
            else:
                comparison = _compare_versions(version, version_min)
                if comparison is None:
                    checks.append(
                        _check(
                            "wechat.version_ok",
                            "FAIL",
                            f"微信版本无法解析：{version}",
                        )
                    )
                elif comparison < 0:
                    checks.append(
                        _check(
                            "wechat.version_ok",
                            "FAIL",
                            f"微信版本 {version} 低于要求 {version_min}",
                        )
                    )
                else:
                    checks.append(
                        _check(
                            "wechat.version_ok",
                            "PASS",
                            f"微信版本 {version} >= {version_min}",
                        )
                    )

    # (3) language
    if not running:
        checks.append(
            _check("wechat.language_ok", "SKIP", "微信进程未运行，跳过语言检查")
        )
    elif not driver_ok:
        checks.append(
            _check(
                "wechat.language_ok", "SKIP", "pyweixin 未安装，无法读取界面语言"
            )
        )
    else:
        try:
            actual_language = _wechat_language()
        except Exception as exc:  # noqa: BLE001
            checks.append(
                _check("wechat.language_ok", "FAIL", f"读取微信界面语言失败：{exc}")
            )
        else:
            if _normalize_language(actual_language) != expected_language:
                checks.append(
                    _check(
                        "wechat.language_ok",
                        "FAIL",
                        f"微信界面语言 {actual_language} != 配置 {expected_language}",
                    )
                )
            else:
                checks.append(
                    _check(
                        "wechat.language_ok",
                        "PASS",
                        f"微信界面语言 {actual_language} 与配置一致",
                    )
                )

    # (4) integrity
    if not running:
        checks.append(
            _check("wechat.integrity_match", "SKIP", "微信进程未运行，跳过完整性检查")
        )
    elif not uia_needed:
        checks.append(
            _check(
                "wechat.integrity_match",
                "SKIP",
                f"outbound={outbound}，出站不依赖 UIA 完整性级别",
            )
        )
    else:
        try:
            integrity = _wechat_integrity_state(True)
        except Exception:  # noqa: BLE001
            integrity = "unknown"
        if integrity == "matching":
            checks.append(
                _check(
                    "wechat.integrity_match",
                    "PASS",
                    "桥进程与 WeChat.exe 完整性级别一致",
                )
            )
        elif integrity == "mismatched":
            checks.append(
                _check(
                    "wechat.integrity_match",
                    "FAIL",
                    "桥进程与 WeChat.exe 完整性级别不一致，RPA 可能无法操作微信",
                )
            )
        else:
            checks.append(
                _check(
                    "wechat.integrity_match",
                    "SKIP",
                    "无法读取完整性级别（微信未运行或权限不足）",
                )
            )

    # (5) desktop
    if not running:
        checks.append(
            _check("wechat.desktop_interactive", "SKIP", "微信进程未运行，跳过桌面检查")
        )
    elif not uia_needed:
        checks.append(
            _check(
                "wechat.desktop_interactive",
                "SKIP",
                f"outbound={outbound}，锁屏/后台也能发送",
            )
        )
    else:
        try:
            desktop = _wechat_desktop()
        except Exception:  # noqa: BLE001
            desktop = "locked"
        if desktop != "interactive":
            checks.append(
                _check(
                    "wechat.desktop_interactive",
                    "FAIL",
                    f"微信桌面状态：{desktop}（需要已解锁的交互桌面）",
                )
            )
        else:
            session_id = _current_session_id()
            if session_id == 0:
                checks.append(
                    _check(
                        "wechat.desktop_interactive",
                        "FAIL",
                        "当前进程在 Session 0（服务会话），不是交互桌面",
                    )
                )
            else:
                shown = "未知" if session_id is None else str(session_id)
                session_name = str(os.environ.get("SESSIONNAME") or "")
                if session_name.upper().startswith("RDP-"):
                    detail = (
                        f"桌面可交互（Session {shown}，RDP 会话）；"
                        "断开远程桌面后会锁屏，先运行 python -m dafeiyu.rdp handoff"
                    )
                else:
                    detail = f"桌面可交互（Session {shown}）"
                checks.append(
                    _check(
                        "wechat.desktop_interactive",
                        "PASS",
                        detail,
                    )
                )

    # (6) pyweixin
    if driver_ok:
        checks.append(
            _check("wechat.driver_importable", "PASS", "pyweixin 已安装，可导入")
        )
    else:
        checks.append(
            _check(
                "wechat.driver_importable",
                "FAIL",
                "pyweixin 未安装：pip install 'dafeiyu[wechat]'",
            )
        )

    # (7) port
    host = str(getattr(cfg, "bridge_host", "127.0.0.1") or "127.0.0.1")
    try:
        port = int(getattr(cfg, "bridge_port", 8790))
    except (TypeError, ValueError):
        checks.append(
            _check(
                "wechat.bridge_port_free",
                "FAIL",
                "gateway.wechat.bridge_port 不是整数",
            )
        )
    else:
        try:
            free = _port_free(port, host)
        except Exception as exc:  # noqa: BLE001
            checks.append(
                _check("wechat.bridge_port_free", "FAIL", f"检测端口失败：{exc}")
            )
        else:
            if free:
                checks.append(
                    _check("wechat.bridge_port_free", "PASS", f"{host}:{port} 空闲")
                )
            else:
                checks.append(
                    _check(
                        "wechat.bridge_port_free",
                        "FAIL",
                        f"{host}:{port} 已被占用",
                    )
                )

    # (8) config readiness
    nickname = str(getattr(cfg, "account_nickname", "") or "").strip()
    if not nickname:
        checks.append(
            _check(
                "wechat.config_ready",
                "FAIL",
                "gateway.wechat.enabled=true 但缺少：account_nickname",
            )
        )
    else:
        checks.append(
            _check(
                "wechat.config_ready",
                "PASS",
                f"配置齐备（昵称 {nickname}；群范围以面板白名单 groups.whitelist 为准）",
            )
        )

    return checks


def wechat_ok(checks: list[dict[str, str]]) -> bool:
    """没有 FAIL 即视为通过；SKIP 不算失败。"""
    return not any(check["status"] == "FAIL" for check in checks)


def _bool_text(value) -> str:
    return "true" if bool(value) else "false"


def format_wechat_config(config) -> str:
    """打印微信通道生效配置（bridge_token 只显示是否配置，绝不显示值）。"""
    cfg = _as_wechat_config(config)
    if cfg is None:
        return "微信通道生效配置（gateway.wechat）\n  （未加载配置）"

    host = str(getattr(cfg, "bridge_host", "127.0.0.1") or "127.0.0.1")
    port = getattr(cfg, "bridge_port", 8790)
    token = getattr(cfg, "bridge_token", "") or ""
    nickname = str(getattr(cfg, "account_nickname", "") or "").strip() or "—"
    version_min = str(getattr(cfg, "version_min", "4.1.6") or "4.1.6")
    language = str(getattr(cfg, "language", "zh-CN") or "zh-CN")
    kill_switch_file = str(
        getattr(cfg, "kill_switch_file", "data/wechat.stop") or "data/wechat.stop"
    )
    allow_test_inject = _bool_text(getattr(cfg, "allow_test_inject", False))

    lines = [
        "微信通道生效配置（gateway.wechat）",
        f"  enabled：{_bool_text(getattr(cfg, 'enabled', False))}",
        f"  bridge_host：{host}",
        f"  bridge_port：{port}",
        f"  bridge_token：{'已配置（不显示）' if token else '未配置'}",
        f"  account_nickname：{nickname}",
        "  群范围：面板白名单 groups.whitelist（空 = 不限制）",
        f"  version_min：{version_min}",
        f"  language：{language}",
        f"  kill_switch_file：{kill_switch_file}",
        f"  allow_test_inject：{allow_test_inject}",
    ]
    return "\n".join(lines)


def format_wechat_report(checks: list[dict[str, str]]) -> str:
    """把 8 项微信前置检查排成给人看的报告。"""
    checks = list(checks)
    lines = [f"微信前置检查（{len(checks)} 项）"]
    for check in checks:
        lines.append(f"  [{check['status']}] {check['id']}：{check['detail']}")

    fail_count = sum(1 for check in checks if check["status"] == "FAIL")
    skip_count = sum(1 for check in checks if check["status"] == "SKIP")
    if fail_count:
        lines.append(f"  结论：微信前置检查失败（{fail_count} 项 FAIL）")
    elif skip_count:
        lines.append(
            f"  结论：微信前置检查通过（{len(checks) - skip_count} 项 PASS，"
            f"{skip_count} 项 SKIP）"
        )
    else:
        lines.append(f"  结论：微信前置检查通过（{len(checks)} 项全部 PASS）")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
