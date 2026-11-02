"""P8 回归：含中文的 .ps1 必须是 UTF-8 with BOM。

PowerShell 5.1 对无 BOM 的 UTF-8 按系统 ANSI（GBK）解码，中文注释的行尾
字节可能和换行组合成非法 GBK 序列，把下一行吞进注释 —— 曾经导致
``$env:OPENCODE_SERVER_PASSWORD = ...`` 整行丢失、opencode 每次随机口令。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "scripts").glob("*.ps1"))
UTF8_BOM = b"\xef\xbb\xbf"


def test_ps1_directory_has_scripts() -> None:
    assert SCRIPTS, f"{ROOT / 'scripts'} 下没有 .ps1，测试本身失效"


def test_ps1_scripts_with_non_ascii_must_start_with_utf8_bom() -> None:
    offenders: list[str] = []
    for path in SCRIPTS:
        data = path.read_bytes()
        if not any(byte > 0x7F for byte in data):
            # 纯 ASCII 脚本不受 PowerShell 5.1 的 ANSI 解码影响。
            continue
        if not data.startswith(UTF8_BOM):
            offenders.append(path.name)
    assert offenders == [], (
        "以下 .ps1 含中文但不是 UTF-8 with BOM，PS 5.1 可能吞行："
        + "、".join(offenders)
    )
