from __future__ import annotations

from pathlib import Path

import pytest

from dafeiyu.bootstrap import (
    format_issues,
    has_errors,
    init_config,
    preflight,
)
from dafeiyu.env_probe import EnvProbe

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


@pytest.fixture(autouse=True)
def _stub_opencode_bin(monkeypatch):
    """模板默认 provider=opencode；体检能否找到 opencode.exe 与本文件的断言无关。"""

    monkeypatch.setattr(
        "dafeiyu.bootstrap.resolve_opencode_bin", lambda _bin: Path("opencode.exe")
    )


def _write_env(tmp_path: Path, **values: str) -> Path:
    env = tmp_path / ".env"
    env.write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
    return env


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_init_creates_config_from_example(tmp_path):
    target = tmp_path / "config.yaml"

    result = init_config(target, source=EXAMPLE)

    assert result.created is True
    assert target.read_text(encoding="utf-8").startswith("# 大肥鱼")


def test_init_keeps_existing_file_without_force(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("keep me", encoding="utf-8")

    result = init_config(target, source=EXAMPLE)

    assert result.created is False
    assert target.read_text(encoding="utf-8") == "keep me"


def test_init_force_overwrites_existing_file(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("keep me", encoding="utf-8")

    result = init_config(target, source=EXAMPLE, force=True)

    assert result.created is True
    assert "persona:" in target.read_text(encoding="utf-8")


def test_init_missing_example_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        init_config(tmp_path / "config.yaml", source=tmp_path / "nope.yaml")


def test_preflight_without_config_points_to_init(tmp_path):
    issues = preflight(
        tmp_path / "missing.yaml", tmp_path / ".env", root=tmp_path, check_ports=False
    )

    assert [issue.title for issue in issues] == ["缺少配置文件"]
    assert "dafeiyu init" in issues[0].fix
    assert has_errors(issues)


def test_preflight_reports_missing_bot_qq(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = _write_env(tmp_path, DEEPSEEK_API_KEY="sk-test")

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert has_errors(issues)
    assert "BOT_QQ" in issues[0].detail
    assert ".env" in issues[0].fix


def test_preflight_reports_missing_napcat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = _write_env(tmp_path, BOT_QQ="10001", DEEPSEEK_API_KEY="sk-test")

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert [issue.title for issue in issues] == ["NapCat 未安装"]
    assert "setup-napcat.ps1" in issues[0].fix


def test_preflight_passes_when_everything_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = _write_env(tmp_path, BOT_QQ="10001", DEEPSEEK_API_KEY="sk-test")
    napcat = tmp_path / "vendor" / "napcat"
    napcat.mkdir(parents=True)
    (napcat / "node.exe").write_bytes(b"")

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert issues == []
    assert "体检通过" in format_issues(issues)


def test_preflight_port_check_reads_configured_ports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = _write_env(tmp_path, BOT_QQ="10001", DEEPSEEK_API_KEY="sk-test")
    napcat = tmp_path / "vendor" / "napcat"
    napcat.mkdir(parents=True)
    (napcat / "node.exe").write_bytes(b"")

    issues = preflight(config, env, root=tmp_path, check_ports=True)

    assert [issue.title for issue in issues if issue.title != "端口已被占用"] == []
    for issue in issues:
        assert issue.level in {"error", "warn"}


def _ready_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """除「被测项」以外都正常的临时项目：config + .env + NapCat node.exe。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = _write_env(tmp_path, BOT_QQ="10001", DEEPSEEK_API_KEY="sk-test")
    napcat = tmp_path / "vendor" / "napcat"
    napcat.mkdir(parents=True)
    (napcat / "node.exe").write_bytes(b"")
    return config, env


# ---- P10：.env / config.yaml 编码不是 UTF-8 ---------------------------------


def test_preflight_reports_non_utf8_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = _config(tmp_path)
    env = tmp_path / ".env"
    env.write_bytes("BOT_QQ=10001\n# 旧机器拷来的中文注释\n".encode("gbk"))

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert has_errors(issues)
    assert any("编码" in issue.title for issue in issues)
    assert issues[0].level == "error"
    assert "UTF-8" in issues[0].fix


def test_preflight_reports_non_utf8_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_QQ", raising=False)
    config = tmp_path / "config.yaml"
    config.write_bytes("brain:\n  provider: token\n# 中文注释\n".encode("gbk"))
    env = _write_env(tmp_path, BOT_QQ="10001", DEEPSEEK_API_KEY="sk-test")

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert has_errors(issues)
    assert any("编码" in issue.title for issue in issues)


# ---- P4/P5：沙箱降权（Low 完整性 / temp 不可写） ----------------------------


@pytest.mark.parametrize(
    "probe",
    [
        EnvProbe("low", True, "完整性 SID：S-1-16-4096；临时目录可写"),
        EnvProbe("medium", False, "完整性 SID：S-1-16-8192；临时目录写入失败：EPERM"),
    ],
)
def test_preflight_warns_when_environment_downgraded(tmp_path, monkeypatch, probe):
    config, env = _ready_project(tmp_path, monkeypatch)
    monkeypatch.setattr("dafeiyu.bootstrap.probe_environment", lambda: probe)

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert not has_errors(issues)
    warn = next(
        i for i in issues if i.title == "运行环境疑似被沙箱降权（Low 完整性）"
    )
    assert probe.detail in warn.detail or probe.integrity in warn.detail


# ---- P8：PowerShell 脚本必须 UTF-8 with BOM ---------------------------------


def test_preflight_warns_on_ps1_without_utf8_bom(tmp_path, monkeypatch):
    config, env = _ready_project(tmp_path, monkeypatch)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "foo.ps1").write_bytes("Write-Host '中文'\n".encode("utf-8"))

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert not has_errors(issues)
    titles = [issue.title for issue in issues]
    assert "PowerShell 脚本缺少 UTF-8 BOM" in titles
    bom_issue = next(i for i in issues if i.title == "PowerShell 脚本缺少 UTF-8 BOM")
    assert "foo.ps1" in bom_issue.detail
    assert "P8" in bom_issue.fix


def test_preflight_accepts_ps1_with_utf8_bom(tmp_path, monkeypatch):
    config, env = _ready_project(tmp_path, monkeypatch)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "foo.ps1").write_bytes(
        b"\xef\xbb\xbf" + "Write-Host '中文'\n".encode("utf-8")
    )

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert not any("BOM" in issue.title for issue in issues)


def test_preflight_checks_runtime_launcher_bom(tmp_path, monkeypatch):
    config, env = _ready_project(tmp_path, monkeypatch)
    data = tmp_path / "data"
    data.mkdir()
    (data / "runtime-launcher.ps1").write_bytes("Write-Host '中文'\n".encode("utf-8"))

    issues = preflight(config, env, root=tmp_path, check_ports=False)

    assert any("runtime-launcher.ps1" in issue.detail for issue in issues)
