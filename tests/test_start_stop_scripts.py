"""启停脚本：hook 端口必须跟配置走，读配置不能被 PS 5.1 的 stderr 重定向打断。

2026-11-14 部署时踩到两个坑，都靠人工才绕过：

1. ``stop.ps1`` 调 ``start-wechat-hook.ps1 -Stop`` 时不传 ``-Port``，默认按 30011 找，
   而实际 hook 在 ``wechat.hook_url`` 的端口（30012）上 —— 脚本报「未在运行」，
   hook 却还在 LISTENING。
2. ``start-all.ps1`` 用 ``& $python -c ... 2>$null`` 在原生命令上重定向 stderr，
   PS 5.1 会把它变成 NativeCommandError；而 ``import dafeiyu`` 往 stderr 打一行
   能力探测，于是每次读配置都进 catch，静默退回默认值并跳过 hook 启动。

这里钉四件事：三个脚本都能被 PS 解析；``stop.ps1`` 的 ``-Stop`` 一定带 ``-Port``；
读配置只剩 ``Invoke-PythonRead`` 这一个原生调用入口；该 helper 在
``$ErrorActionPreference = "Stop"`` 下确实能拿到配置值（真跑 PS，不 mock）。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
START_ALL = SCRIPTS_DIR / "start-all.ps1"
STOP = SCRIPTS_DIR / "stop.ps1"
START_HOOK = SCRIPTS_DIR / "start-wechat-hook.ps1"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

POWERSHELL = ("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass")
HOOK_PORT_CODE = (
    "from urllib.parse import urlsplit; "
    "from dafeiyu.config import load_settings; "
    "print(urlsplit(load_settings().wechat.hook_url).port or 30011)"
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="启停脚本只在 Windows + PowerShell 下可用")


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*POWERSHELL, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _text(path: Path) -> str:
    """按 UTF-8（容忍 BOM）读脚本，并把换行统一成 \\n 便于按行断言。"""
    return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


def _code_lines(text: str) -> list[str]:
    """去掉整行注释，只留会被 PowerShell 执行的文本（注释里会提到 2>$null 这类反例）。"""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _parse_errors(path: Path) -> list[str]:
    cmd = (
        "$errs = $null; $tokens = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errs)"
        " | Out-Null; "
        '$errs | ForEach-Object { "L{0}: {1}" -f $_.Extent.StartLineNumber, $_.Message }'
    )
    proc = _run(["-Command", cmd], timeout=90)
    assert proc.returncode == 0, f"解析 {path.name} 的 PowerShell 调用失败：{proc.stderr}"
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _extract_helper(text: str) -> str:
    """取出 Invoke-PythonRead 的完整定义（到行首的右花括号为止）。"""
    match = re.search(r"^function Invoke-PythonRead.*?^}", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "脚本里找不到 function Invoke-PythonRead"
    return match.group(0)


# --------------------------------------------------------------------------- 静态断言


def test_three_lifecycle_scripts_parse() -> None:
    for script in (START_ALL, STOP, START_HOOK):
        assert script.is_file(), f"缺少脚本 {script}"
        errors = _parse_errors(script)
        assert errors == [], f"{script.name} 语法错误：{errors}"


def test_stop_stops_hook_on_configured_port() -> None:
    text = _text(STOP)
    code = "\n".join(_code_lines(text))
    # 端口来源必须是配置里的 wechat.hook_url，而不是默认 30011。
    assert "wechat.hook_url" in code, "stop.ps1 没按 wechat.hook_url 解析 hook 端口"
    assert "urlsplit" in code, "stop.ps1 没从 hook_url 里取端口"
    # 所有 -Stop 调用都必须同时给出 -Port，否则又回去停 30011 了。
    stop_calls = [ln for ln in code.splitlines() if "-Stop" in ln]
    assert stop_calls, "stop.ps1 里没有 -Stop 调用"
    for line in stop_calls:
        assert "-Port" in line, f"stop.ps1 的 -Stop 调用没带 -Port：{line.strip()}"


def test_start_all_reads_config_only_through_helper() -> None:
    text = _text(START_ALL)
    code_lines = _code_lines(text)
    code = "\n".join(code_lines)
    assert "function Invoke-PythonRead" in code, "start-all.ps1 缺 Invoke-PythonRead"
    # 原生 python 调用只能是 helper 内部那一处；散落各处就说明有人绕过了 helper。
    assert code.count("& $python -c") == 1, "start-all.ps1 里有 helper 之外的原生 python 调用"
    assert "2>$null" not in code, "start-all.ps1 仍在原生命令上重定向 stderr"
    # helper 的三根支柱：临时放宽 Stop、合并 stderr、只取字符串行。
    helper = _extract_helper(text)
    assert '$ErrorActionPreference = "Continue"' in helper
    assert "2>&1" in helper
    assert "$_ -is [string]" in helper
    # 四处配置读取（面板端口/浏览器、hook 开关、hook 端口、账号）都得走 helper。
    assert "urlsplit(load_settings().wechat.hook_url)" in code
    assert "w.outbound in ('hook', 'auto')" in code


def test_stop_also_uses_helper_for_port() -> None:
    code = "\n".join(_code_lines(_text(STOP)))
    assert "function Invoke-PythonRead" in code, "stop.ps1 缺 Invoke-PythonRead"
    assert "& $python -c" not in code.replace(_extract_helper(_text(STOP)), ""), (
        "stop.ps1 的配置读取没有全部走 helper"
    )


def test_hook_script_accepts_port() -> None:
    code = "\n".join(_code_lines(_text(START_HOOK)))
    assert "[int]$Port = 30011" in code, "start-wechat-hook.ps1 不再接受 -Port，两个调用方会失效"


# --------------------------------------------------------------------------- 行为断言


@pytest.fixture(scope="module")
def expected_hook_port() -> str:
    if not PYTHON.is_file():
        pytest.skip(f"没有虚拟环境：{PYTHON}")
    proc = subprocess.run(
        [str(PYTHON), "-c", HOOK_PORT_CODE],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, f"直连读配置失败：{proc.stderr}"
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"直连读配置没有 stdout：stderr={proc.stderr}"
    return lines[-1]


def _run_helper_from(script: Path, tmp_path: Path, expected: str) -> str:
    """用脚本里真实的 helper 在 $ErrorActionPreference = "Stop" 下读配置。"""
    composed = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$python = '{PYTHON}'\n"
        f"{_extract_helper(_text(script))}\n"
        f"$value = Invoke-PythonRead '{HOOK_PORT_CODE}'\n"
        'Write-Output "VALUE=[$value]"\n'
        'Write-Output "PREF=[$ErrorActionPreference]"\n'
    )
    probe = tmp_path / f"probe_{script.stem}.ps1"
    _ = probe.write_text(composed, encoding="utf-8-sig")
    proc = _run(["-File", str(probe)], timeout=120)
    assert proc.returncode == 0, f"{script.name} 的 helper 跑挂了：{proc.stdout}\n{proc.stderr}"
    assert f"VALUE=[{expected}]" in proc.stdout, (
        f"{script.name} 的 helper 没读到配置端口 {expected}：\n{proc.stdout}\n{proc.stderr}"
    )
    # helper 用完必须把偏好恢复，别把 Continue 漏给后续 step。
    assert "PREF=[Stop]" in proc.stdout
    return proc.stdout


def test_helper_survives_ps51_stderr_merge(tmp_path: Path, expected_hook_port: str) -> None:
    """旧写法（Stop + 2>$null）在 PS 5.1 会抛异常；helper 必须还能拿到值。"""
    proc = _run(["-Command", "$PSVersionTable.PSVersion.Major"], timeout=60)
    assert proc.returncode == 0
    if proc.stdout.strip() != "5":
        pytest.skip("这条坑只在 Windows PowerShell 5.1 上成立")
    for script in (START_ALL, STOP):
        _ = _run_helper_from(script, tmp_path, expected_hook_port)
