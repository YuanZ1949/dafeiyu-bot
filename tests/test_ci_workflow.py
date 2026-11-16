"""CI 工作流顺序守卫：门槛步骤必须排在主 pytest 之前。

计划 IS-1 要求每次 push 都能在「lint / 类型 / 面板 E2E / 覆盖率」这几道门槛上
尽早失败，而不是等几十分钟全量单测跑完才发现。这条约定以前只写在文档里，
改 `ci.yml` 时没人拦，所以这里用 PyYAML 解析工作流，把步骤顺序和主 pytest
的关键参数钉死（覆盖率阈值本身由 T23 单独定，这里只断言参数存在）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CI_YML = Path(__file__).resolve().parents[1] / ".gitea" / "workflows" / "ci.yml"


def _steps() -> list[dict[str, Any]]:
    workflow: Any = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    return workflow["jobs"]["test"]["steps"]


def _index(steps: list[dict[str, Any]], name: str) -> int:
    for index, step in enumerate(steps):
        if name in str(step.get("name", "")):
            return index
    names = [step.get("name") for step in steps]
    raise AssertionError(f"ci.yml 缺少步骤 {name!r}；现有步骤：{names}")


def _run(steps: list[dict[str, Any]], name: str) -> str:
    return str(steps[_index(steps, name)].get("run", ""))


def test_gate_steps_exist() -> None:
    steps = _steps()
    names = [step.get("name") for step in steps]
    for expected in ("Lint", "Type check", "Dashboard E2E", "Run pytest"):
        assert any(expected in str(name) for name in names), (expected, names)


def test_gate_steps_run_before_main_pytest() -> None:
    steps = _steps()
    main_pytest = _index(steps, "Run pytest")
    for gate in ("Lint", "Type check", "Dashboard E2E"):
        assert _index(steps, gate) < main_pytest, (
            f"{gate} 必须排在 Run pytest 之前",
            [step.get("name") for step in steps],
        )


def test_main_pytest_step_keeps_coverage_gate() -> None:
    run = _run(_steps(), "Run pytest")
    assert "--cov=dafeiyu" in run, run
    assert "--cov-report=term-missing" in run, run
    assert "--cov-fail-under=" in run, run
    assert "exit $LASTEXITCODE" in run, run


def test_dashboard_e2e_step_is_marked_e2e() -> None:
    run = _run(_steps(), "Dashboard E2E")
    assert "-m e2e tests/test_dashboard_e2e.py" in run, run
    assert "exit $LASTEXITCODE" in run, run
