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


def _run(
    args: list[str],
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*POWERSHELL, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """把 TEMP/TMP 指向 pytest 的 tmp_path。

    deploy.ps1 用 ``$env:TEMP\\opencode\\deploy-<时间戳>`` 做回滚备份；不隔离的话
    每跑一次测试就往真实的 %TEMP%\\opencode 里堆一个目录，永远不会被清理。
    """
    temp_root = tmp_path / "temp"
    temp_root.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["TEMP"] = str(temp_root)
    env["TMP"] = str(temp_root)
    return env


def _make_release_target(tmp_path: Path) -> Path:
    """造一个最小可部署的 release 目录（不是真的能拉起服务的 venv）。"""
    target = tmp_path / "release"
    (target / "src" / "dafeiyu").mkdir(parents=True)
    (target / "scripts").mkdir()
    (target / "scripts" / "start-all.ps1").write_text("# stub\n", encoding="utf-8-sig")
    (target / ".venv" / "Scripts").mkdir(parents=True)
    (target / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    return target


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

    result = _run(
        ["-File", str(SCRIPT), "-Target", str(target), "-Plan"],
        env=_isolated_env(tmp_path),
    )

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
    target = _make_release_target(tmp_path)
    (target / "config.yaml").write_text("sentinel-config", encoding="utf-8")
    (target / "data").mkdir()
    (target / "data" / "keep.txt").write_text("keep", encoding="utf-8")

    result = _run(
        ["-File", str(SCRIPT), "-Target", str(target), "-NoRestart"],
        env=_isolated_env(tmp_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (target / "src" / "dafeiyu" / "control.py").exists()
    assert (target / "scripts" / "deploy.ps1").exists()
    # 用户机密与运行数据一个字节都不能动。
    assert (target / "config.yaml").read_text(encoding="utf-8") == "sentinel-config"
    assert (target / "data" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert "SHA256 校验通过" in result.stdout


def test_deploy_restart_path_clears_pythonpath_and_requires_new_pid() -> None:
    """读发布清单时 PYTHONPATH 指向源目录，子进程继承后会加载错的 dafeiyu。

    症状：stop.ps1 找的是源目录的 pid 文件，报「未在运行」；start-all 发现
    旧进程还在就跳过启动，健康检查照样通过，但跑的还是旧代码。
    """
    text = SCRIPT.read_text(encoding="utf-8-sig")

    stop_fn = text.split("function Invoke-TargetStop")[1].split("function Invoke-TargetStart")[0]
    assert "Clear-DeployPythonPath" in stop_fn

    start_fn = text.split("function Invoke-TargetStart")[1].split("function Get-SupervisorPid")[0]
    assert "Clear-DeployPythonPath" in start_fn

    # 停不干净就别继续拷文件；启动后必须换 pid，否则这次部署根本没生效。
    assert "仍在运行" in text
    assert "Wait-ForNewSupervisorPid $oldPid" in text
    assert "supervisor 没有被重启" in text


def _deploy_dirs(root: Path) -> set[str]:
    """列出某个目录下 deploy-* 子目录的名字；目录不存在时视为空。"""
    if not root.is_dir():
        return set()
    return {item.name for item in root.glob("deploy-*") if item.is_dir()}


def test_deploy_no_restart_backup_stays_under_fake_temp(tmp_path: Path) -> None:
    """跑一次真实部署（-NoRestart），备份目录只能落在伪造的 TEMP 下。

    真实 %TEMP%\\opencode 里已经堆了历史目录，不能断言它不存在，只能做前后
    快照对比：本次运行不得新增任何 deploy-*。
    """
    target = _make_release_target(tmp_path)
    env = _isolated_env(tmp_path)

    real_temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "")
    assert str(real_temp) and real_temp.is_dir(), "拿不到真实 TEMP，无法做前后快照"
    real_opencode = real_temp / "opencode"
    before = _deploy_dirs(real_opencode)

    result = _run(["-File", str(SCRIPT), "-Target", str(target), "-NoRestart"], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    fake_dirs = _deploy_dirs(tmp_path / "temp" / "opencode")
    assert len(fake_dirs) == 1, f"伪造 TEMP 下应有 1 个 deploy-*，实际：{fake_dirs}"
    after = _deploy_dirs(real_opencode)
    assert after - before == set(), f"真实 TEMP\\opencode 新增了备份目录：{after - before}"


def test_deploy_health_probe_uses_token_and_checks_gateway() -> None:
    """健康检查必须探 /api/health/detail：只看 HTTP 200 会把「没连上 QQ」判成成功。"""
    text = SCRIPT.read_text(encoding="utf-8-sig")

    assert "/api/health/detail" in text
    assert "supervisor.token" in text
    assert "X-Control-Token" in text
    assert "gateway.connected" in text
    # token 文件带尾随空白，读出来必须 Trim 再放进请求头。
    assert ".Trim()" in text
    # 失败原因要能说清是连不上、401，还是网关没连上。
    assert "401" in text
    assert "连接失败" in text


def test_deploy_allow_disconnected_is_opt_in() -> None:
    """-AllowDisconnected 是显式逃生开关：默认必须强判据，不能悄悄放宽。"""
    text = SCRIPT.read_text(encoding="utf-8-sig")

    assert "[switch]$AllowDisconnected" in text
    # 默认关闭：param 里不能带 = $true
    assert "[switch]$AllowDisconnected = $true" not in text
    # 只有显式开关才放行，且要打警告
    assert "if ($AllowDisconnected)" in text
    assert "已按 -AllowDisconnected 放行" in text
    # 强判据仍在：默认分支仍以 gateway.connected 为准
    assert "$gatewayConnected = $true" in text


def test_deploy_runs_target_doctor_after_checksum_before_restart() -> None:
    """部署前体检：SHA256 之后、重启之前跑目标目录的 doctor，非 0 要回滚。

    放太早（拷贝前）检查的是旧代码；放太晚（重启后）已经来不及回滚。
    """
    text = SCRIPT.read_text(encoding="utf-8-sig")

    assert "dafeiyu.cli doctor" in text
    assert "--no-ports" in text
    assert "体检" in text
    assert "体检未通过" in text
    # 不可运行的目标 python（如测试里的假 release）只能跳过，不能报错。
    assert "跳过部署前体检" in text

    checksum = text.index("SHA256 校验通过")
    doctor = text.index("dafeiyu.cli doctor")
    restart = text.index("if (-not (Invoke-TargetStart))")
    assert checksum < doctor < restart, (
        f"部署前体检位置不对：SHA256={checksum}, doctor={doctor}, start={restart}"
    )
