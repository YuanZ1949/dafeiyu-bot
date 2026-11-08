"""``scripts/eval_golden.py`` 的离线契约测试（不联网、不调模型）。

黄金集是评测的基准：数据集本身写坏了，live 跑出来的准确率就没有意义。
这里钉住三条底线：校验能拦住缺字段/重复 id/非法 verdict/空消息；真实黄金集
能通过校验且规模够；汇总口径不把 skipped 算进准确率分母。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from dafeiyu.eval.report import diff_statuses

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "eval_golden.py"
GOLDEN = ROOT / "data" / "evals" / "golden_replies.json"
RESULT_KEYS = {"dataset", "mode", "model", "generated_at", "summary", "results"}


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_golden", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eval_mod = _load_module()


def sample_dataset() -> dict[str, Any]:
    return {
        "version": 1,
        "cases": [
            {
                "id": "case-a",
                "tags": ["点名"],
                "addressed": True,
                "expect": "speak",
                "messages": [
                    {"nickname": "阿猫", "content": "在吗", "minutes_ago": 1.0}
                ],
            },
            {
                "id": "case-b",
                "tags": ["热场"],
                "addressed": False,
                "expect": "silent",
                "messages": [
                    {"nickname": "小北", "content": "今天好热", "minutes_ago": 2.0},
                    {"nickname": "橙子", "content": "是啊", "minutes_ago": 1.0},
                ],
            },
        ],
    }


def test_validate_dataset_accepts_valid_dataset() -> None:
    assert eval_mod.validate_dataset(sample_dataset()) == []


def test_validate_dataset_rejects_missing_fields() -> None:
    data = sample_dataset()
    del data["version"]
    del data["cases"][0]["expect"]
    errors = eval_mod.validate_dataset(data)
    assert errors
    assert any("version" in error for error in errors)
    assert any("expect" in error for error in errors)


def test_validate_dataset_rejects_duplicate_ids() -> None:
    data = sample_dataset()
    data["cases"][1]["id"] = data["cases"][0]["id"]
    errors = eval_mod.validate_dataset(data)
    assert any("重复" in error for error in errors)


def test_validate_dataset_rejects_bad_expect_and_empty_messages() -> None:
    data = sample_dataset()
    data["cases"][0]["expect"] = "maybe"
    data["cases"][1]["messages"] = []
    errors = eval_mod.validate_dataset(data)
    assert any("speak" in error and "silent" in error for error in errors)
    assert any("messages" in error for error in errors)


def test_validate_dataset_rejects_bad_minutes_ago() -> None:
    data = sample_dataset()
    data["cases"][0]["messages"][0]["minutes_ago"] = -1
    errors = eval_mod.validate_dataset(data)
    assert any("minutes_ago" in error for error in errors)


def test_real_golden_dataset_loads_and_is_large_enough() -> None:
    data = eval_mod.load_dataset(GOLDEN)

    assert eval_mod.validate_dataset(data) == []
    cases = data["cases"]
    assert len(cases) >= 18
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    expects = {case["expect"] for case in cases}
    assert expects == {"speak", "silent"}


def test_summarize_normal_counts_and_accuracy() -> None:
    summary = eval_mod.summarize(
        [
            {"status": "passed"},
            {"status": "passed"},
            {"status": "failed"},
            {"status": "skipped"},
        ]
    )

    assert summary == {
        "total": 4,
        "passed": 2,
        "failed": 1,
        "skipped": 1,
        "accuracy": 2 / 3,
    }


def test_summarize_all_skipped_has_no_accuracy() -> None:
    summary = eval_mod.summarize([{"status": "skipped"}, {"status": "skipped"}])

    assert summary["total"] == 2
    assert summary["skipped"] == 2
    assert summary["accuracy"] is None


def test_summarize_all_failed_is_zero_accuracy() -> None:
    summary = eval_mod.summarize([{"status": "failed"}, {"status": "failed"}])

    assert summary["failed"] == 2
    assert summary["accuracy"] == 0.0


def write_dataset(tmp_path: Path) -> Path:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(sample_dataset(), ensure_ascii=False), encoding="utf-8")
    return path


def write_baseline(tmp_path: Path, statuses: dict[str, str]) -> Path:
    payload = {
        "mode": "live",
        "results": [
            {"id": case_id, "status": status} for case_id, status in statuses.items()
        ],
    }
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def link_live(monkeypatch: Any, statuses: dict[str, str]) -> None:
    """把 live 跑批替换成受控状态，避免依赖离线模式（离线全是 skipped）。"""

    async def fake_live(cases: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
        results = []
        for case in cases:
            result = eval_mod._skipped_result(case, "")
            result["status"] = statuses.get(str(case.get("id")), "skipped")
            results.append(result)
        return results

    monkeypatch.setattr(eval_mod, "_run_live", fake_live)


def test_diff_statuses_regressions_new_and_fixed() -> None:
    diff = diff_statuses(
        {"a": True, "b": True, "c": False, "gone": True},
        {"a": False, "b": True, "c": True, "fresh": False},
    )

    assert diff.regressed == ("a",)
    assert diff.fixed == ("c",)
    assert diff.new == ("fresh",)
    assert diff.new_failures == ("fresh",)
    assert diff.missing == ("gone",)
    assert diff.has_regressions()


def test_diff_statuses_skipped_is_excluded_from_diff() -> None:
    # skipped 在适配阶段就被剔除：跳过既不算回归，也不算修复。
    baseline = eval_mod.statuses_from_results(
        [
            {"id": "skip", "status": "skipped"},
            {"id": "pass", "status": "passed"},
        ]
    )
    current = eval_mod.statuses_from_results(
        [
            {"id": "skip", "status": "failed"},
            {"id": "pass", "status": "skipped"},
        ]
    )

    diff = diff_statuses(baseline, current)

    assert baseline == {"pass": True}
    assert current == {"skip": False}
    assert diff.regressed == ()
    assert diff.new == ("skip",)  # baseline 里没有它，当前失败也不触发回归门
    assert diff.missing == ("pass",)  # 当前 skipped 被剔除，不报回归
    # 回归门只看 regressed；skipped 造成的 new_failures 不参与拦截。
    assert diff.new_failures == ("skip",)


def test_offline_run_persists_single_result_with_six_keys(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path)
    out_dir = tmp_path / "results"

    code = eval_mod.main(
        ["--dataset", str(dataset), "--mode", "offline", "--out-dir", str(out_dir)]
    )

    assert code == 0
    files = sorted(out_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name.endswith("-offline.json")
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(payload) == RESULT_KEYS
    assert payload["mode"] == "offline"
    assert payload["model"] == "offline"
    assert [item["status"] for item in payload["results"]] == ["skipped", "skipped"]


def test_fail_on_regression_returns_one(tmp_path: Path, monkeypatch: Any) -> None:
    dataset = write_dataset(tmp_path)
    baseline = write_baseline(tmp_path, {"case-a": "passed", "case-b": "passed"})
    link_live(monkeypatch, {"case-a": "passed", "case-b": "failed"})
    out_dir = tmp_path / "results"

    code = eval_mod.main(
        [
            "--dataset",
            str(dataset),
            "--mode",
            "live",
            "--out-dir",
            str(out_dir),
            "--baseline",
            str(baseline),
            "--fail-on-regression",
        ]
    )

    assert code == 1
    persisted = json.loads(next(out_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert set(persisted) == RESULT_KEYS
    assert persisted["summary"]["failed"] == 1


def test_no_regression_returns_zero(tmp_path: Path, monkeypatch: Any) -> None:
    dataset = write_dataset(tmp_path)
    baseline = write_baseline(tmp_path, {"case-a": "passed", "case-b": "passed"})
    link_live(monkeypatch, {"case-a": "passed", "case-b": "passed"})

    code = eval_mod.main(
        [
            "--dataset",
            str(dataset),
            "--mode",
            "live",
            "--out-dir",
            str(tmp_path / "results"),
            "--baseline",
            str(baseline),
            "--fail-on-regression",
        ]
    )

    assert code == 0


def test_skipped_now_does_not_fail_regression_gate(
    tmp_path: Path, monkeypatch: Any
) -> None:
    dataset = write_dataset(tmp_path)
    baseline = write_baseline(tmp_path, {"case-a": "passed", "case-b": "passed"})
    link_live(monkeypatch, {"case-a": "skipped", "case-b": "passed"})

    code = eval_mod.main(
        [
            "--dataset",
            str(dataset),
            "--mode",
            "live",
            "--out-dir",
            str(tmp_path / "results"),
            "--baseline",
            str(baseline),
            "--fail-on-regression",
        ]
    )

    assert code == 0


def test_baseline_with_eval_cases_shape_errors(
    tmp_path: Path, capsys: Any
) -> None:
    dataset = write_dataset(tmp_path)
    wrong = tmp_path / "baseline.json"
    wrong.write_text(
        json.dumps({"cases": [{"id": "case-a", "passed": True}]}), encoding="utf-8"
    )

    code = eval_mod.main(
        [
            "--dataset",
            str(dataset),
            "--mode",
            "offline",
            "--out-dir",
            str(tmp_path / "results"),
            "--baseline",
            str(wrong),
        ]
    )

    assert code == 2
    assert "results" in capsys.readouterr().err


def test_unwritable_out_dir_errors_without_file(
    tmp_path: Path, capsys: Any
) -> None:
    dataset = write_dataset(tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    code = eval_mod.main(
        ["--dataset", str(dataset), "--mode", "offline", "--out-dir", str(blocker)]
    )

    assert code == 2
    assert "结果写入失败" in capsys.readouterr().err
    assert list(tmp_path.glob("*.json")) == [dataset]


def test_failed_replace_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    dataset = write_dataset(tmp_path)
    out_dir = tmp_path / "results"

    def boom(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(eval_mod.os, "replace", boom)

    code = eval_mod.main(
        ["--dataset", str(dataset), "--mode", "offline", "--out-dir", str(out_dir)]
    )

    assert code == 2
    assert "结果写入失败" in capsys.readouterr().err
    assert list(out_dir.iterdir()) == []

