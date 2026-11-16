"""``dafeiyu.eval.mind`` 的契约测试（离线，不调模型不联网）。

钉住四条底线：8 个场景全绿且名字固定；改坏任一期望只会让该场景 failed
（证明断言不是空转）；场景内异常变成 failed 而不是让 run_all 崩；CLI 的
JSON 输出与退出码契约。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "eval_mind.py"

EXPECTED_SCENARIOS = (
    "recall_ranking",
    "silence_backoff",
    "multi_episode",
    "mood_ttl",
    "privacy_delete",
    "private_gate",
    "chat_isolation",
    "quota_reserve",
)


def _load_cli():
    spec = importlib.util.spec_from_file_location("eval_mind_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in result["scenarios"]}


def test_run_all_passes_all_eight_scenarios_under_five_seconds(
    tmp_path: Path,
) -> None:
    from dafeiyu.eval import mind as eval_mind

    started = time.monotonic()
    result = eval_mind.run_all(tmp_dir=tmp_path)
    elapsed = time.monotonic() - started

    assert result["ok"] is True
    scenarios = result["scenarios"]
    assert len(scenarios) == len(EXPECTED_SCENARIOS)
    assert tuple(item["name"] for item in scenarios) == EXPECTED_SCENARIOS
    for item in scenarios:
        assert set(item) == {"name", "passed", "detail"}
        assert item["passed"] is True, item["detail"]
        assert isinstance(item["detail"], str) and item["detail"]
    # 总耗时门槛写进测试：谁把慢路径（真实 sleep / 网络 / 模型）带进来就地红。
    assert elapsed < 5.0, f"心智 eval 耗时 {elapsed:.2f}s，超过 5 秒预算"
    # 外部临时目录真的被用上了：场景的 SQLite 库落在这里。
    assert any(tmp_path.iterdir())


def test_run_all_without_tmp_dir_uses_internal_tempfile() -> None:
    from dafeiyu.eval import mind as eval_mind

    result = eval_mind.run_all()
    assert result["ok"] is True
    assert len(result["scenarios"]) == len(EXPECTED_SCENARIOS)


def test_broken_expectation_fails_only_that_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dafeiyu.eval import mind as eval_mind

    def broken(root: Path):
        return False, "探针：期望被故意改坏"

    monkeypatch.setitem(eval_mind._SCENARIOS, "recall_ranking", broken)
    result = eval_mind.run_all(tmp_dir=tmp_path)

    assert result["ok"] is False
    by_name = _by_name(result)
    assert by_name["recall_ranking"]["passed"] is False
    assert "探针" in by_name["recall_ranking"]["detail"]
    failed = [item["name"] for item in result["scenarios"] if not item["passed"]]
    assert failed == ["recall_ranking"]


def test_scenario_exception_becomes_failed_scenario_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dafeiyu.eval import mind as eval_mind

    def boom(root: Path):
        raise RuntimeError("场景内部爆炸")

    monkeypatch.setitem(eval_mind._SCENARIOS, "mood_ttl", boom)
    result = eval_mind.run_all(tmp_dir=tmp_path)

    assert result["ok"] is False
    by_name = _by_name(result)
    mood = by_name["mood_ttl"]
    assert mood["passed"] is False
    assert "RuntimeError" in mood["detail"]
    assert "场景内部爆炸" in mood["detail"]
    # 其余场景照常跑完，不让 run_all 整体崩。
    assert len(result["scenarios"]) == len(EXPECTED_SCENARIOS)
    assert all(
        item["passed"]
        for item in result["scenarios"]
        if item["name"] != "mood_ttl"
    )


def test_cli_json_all_green_exit_zero(tmp_path: Path, capsys: Any) -> None:
    cli = _load_cli()
    try:
        code = cli.main(["--json", "--tmp-dir", str(tmp_path)])
    finally:
        pass
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert len(payload["scenarios"]) == len(EXPECTED_SCENARIOS)


def test_cli_exit_one_when_a_scenario_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from dafeiyu.eval import mind as eval_mind

    cli = _load_cli()
    monkeypatch.setitem(
        eval_mind._SCENARIOS, "silence_backoff", lambda root: (False, "探针失败")
    )
    code = cli.main(["--json", "--tmp-dir", str(tmp_path)])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    by_name = _by_name(payload)
    assert by_name["silence_backoff"]["passed"] is False


def test_cli_prints_readable_error_on_unusable_tmp_dir(
    tmp_path: Path, capsys: Any
) -> None:
    cli = _load_cli()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("我是文件，不能当目录", encoding="utf-8")
    code = cli.main(["--tmp-dir", str(blocker / "sub")])
    assert code == 1
    captured = capsys.readouterr()
    assert "无法运行" in captured.err
    assert "Traceback" not in captured.err
