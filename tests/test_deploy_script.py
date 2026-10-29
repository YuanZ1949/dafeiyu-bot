"""部署脚本：能解析、-Plan 只列文件、绝不碰机密/运行数据。

部署是唯一会同时改动生产文件和进程的动作，失败成本远高于普通代码改动。
这里不真的部署，只钉住两件最容易出事的事：脚本本身语法有效；-Plan 模式
不写目标目录，且发布清单里没有 config.yaml / .env / data / tests。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy.ps1"
POWERSHELL = ("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass")

pytestmark = pytest.mark.skipif(os.name != "nt", reason="部署脚本只在 Windows + PowerShell 下可用")


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*POWERSHELL, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_deploy_script_parses() -> None:
    result = _run(
        [
            "-Command",
            f"[scriptblock]::Create((Get-Content -Raw -LiteralPath '{SCRIPT}')) | Out-Null",
        ]
    )
    assert result.returncode == 0, result.stderr


def test_deploy_plan_lists_payload_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "release"
    target.mkdir()

    result = _run(["-File", str(SCRIPT), "-Target", str(target), "-Plan"])

    assert result.returncode == 0, result.stdout + result.stderr
    payload = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("  ") and line.strip()
    ]
    assert "src/dafeiyu/control.py" in payload
    assert "scripts/start-all.ps1" in payload
    # 机密与运行数据永远不进部署清单。
    assert "config.yaml" not in payload
    assert ".env" not in payload
    assert not any(item.startswith(("data/", "tests/", "logs/", "vendor/")) for item in payload)
    # -Plan 不做任何写入。
    assert list(target.iterdir()) == []


def test_deploy_no_restart_copies_payload_and_keeps_secrets(tmp_path: Path) -> None:
    target = tmp_path / "release"
    (target / "src" / "dafeiyu").mkdir(parents=True)
    (target / "scripts").mkdir()
    (target / "scripts" / "start-all.ps1").write_text("# stub\n", encoding="utf-8-sig")
    (target / ".venv" / "Scripts").mkdir(parents=True)
    (target / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    (target / "config.yaml").write_text("sentinel-config", encoding="utf-8")
    (target / "data").mkdir()
    (target / "data" / "keep.txt").write_text("keep", encoding="utf-8")

    result = _run(["-File", str(SCRIPT), "-Target", str(target), "-NoRestart"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert (target / "src" / "dafeiyu" / "control.py").exists()
    assert (target / "scripts" / "deploy.ps1").exists()
    # 用户机密与运行数据一个字节都不能动。
    assert (target / "config.yaml").read_text(encoding="utf-8") == "sentinel-config"
    assert (target / "data" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert "SHA256 校验通过" in result.stdout
