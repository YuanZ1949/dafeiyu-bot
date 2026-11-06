"""``scripts/eval_golden.py`` 的离线契约测试（不联网、不调模型）。

黄金集是评测的基准：数据集本身写坏了，live 跑出来的准确率就没有意义。
这里钉住三条底线：校验能拦住缺字段/重复 id/非法 verdict/空消息；真实黄金集
能通过校验且规模够；汇总口径不把 skipped 算进准确率分母。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "eval_golden.py"
GOLDEN = ROOT / "data" / "evals" / "golden_replies.json"


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
