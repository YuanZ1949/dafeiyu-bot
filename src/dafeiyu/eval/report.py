"""报告输出与 baseline diff：JSON 给机器，Markdown 给人。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .loader import EvalError
from .models import EvalReport, EvalResult


@dataclass(frozen=True)
class BaselineDiff:
    new: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    regressed: tuple[str, ...] = ()
    fixed: tuple[str, ...] = ()
    # 新 case 直接失败：没有历史状态可比，也算回归。
    new_failures: tuple[str, ...] = ()

    def has_regressions(self) -> bool:
        return bool(self.regressed or self.new_failures)


def result_to_dict(result: EvalResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": result.id,
        "passed": result.passed,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "detail": check.detail,
                "needs_review": check.needs_review,
            }
            for check in result.checks
        ],
        "reply": result.reply,
        "tokens": result.tokens,
        "cost": result.cost,
        "latency_ms": result.latency_ms,
    }
    if result.error:
        payload["error"] = result.error
    return payload


def report_to_dict(report: EvalReport) -> dict[str, Any]:
    return {
        "generated_at": report.generated_at,
        "mode": report.mode,
        "model": report.model,
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "duration_seconds": round(report.duration_seconds, 3),
        "cases": [result_to_dict(result) for result in report.cases],
    }


def load_baseline(path: str | Path) -> dict[str, Any]:
    baseline_path = Path(path)
    try:
        text = baseline_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"baseline 读取失败 {baseline_path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvalError(f"baseline 不是合法 JSON {baseline_path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise EvalError(f"baseline 缺少 cases 列表: {baseline_path}")
    for index, case in enumerate(data["cases"]):
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise EvalError(f"baseline cases[{index}] 缺少字符串 id: {baseline_path}")
    return data


def diff_reports(baseline: dict[str, Any], current: EvalReport) -> BaselineDiff:
    """按 case id 比较；baseline 与当前 case 集不同只报 new/missing。"""
    base_status = {
        case["id"]: bool(case.get("passed")) for case in baseline.get("cases", [])
    }
    current_status = {result.id: result.passed for result in current.cases}

    base_ids = set(base_status)
    current_ids = set(current_status)
    common = base_ids & current_ids

    new_ids = sorted(current_ids - base_ids)
    return BaselineDiff(
        new=tuple(new_ids),
        missing=tuple(sorted(base_ids - current_ids)),
        regressed=tuple(
            sorted(
                case_id
                for case_id in common
                if base_status[case_id] and not current_status[case_id]
            )
        ),
        fixed=tuple(
            sorted(
                case_id
                for case_id in common
                if not base_status[case_id] and current_status[case_id]
            )
        ),
        new_failures=tuple(
            case_id for case_id in new_ids if not current_status[case_id]
        ),
    )


def render_markdown(
    report: EvalReport, diff: BaselineDiff | None = None
) -> str:
    rate = (report.passed / report.total * 100) if report.total else 0.0
    average_latency = (
        sum(result.latency_ms for result in report.cases) / len(report.cases)
        if report.cases
        else 0.0
    )
    total_tokens = sum(result.tokens for result in report.cases)
    total_cost = sum(result.cost for result in report.cases)

    lines = [
        "# 评测报告",
        "",
        f"- 生成时间: {report.generated_at}",
        f"- 模式: {report.mode}",
        f"- 模型: {report.model}",
        f"- 通过率: {report.passed}/{report.total} ({rate:.1f}%)",
        f"- 平均延迟: {average_latency:.0f} ms",
        f"- 总 token: {total_tokens}",
        f"- 总费用: {total_cost:.4f}",
        f"- 总耗时: {report.duration_seconds:.2f} s",
        "",
        "## 失败",
        "",
    ]
    failures = [result for result in report.cases if not result.passed]
    if not failures:
        lines.append("无")
    else:
        lines.extend(["| case | check | 详情 |", "| --- | --- | --- |"])
        for result in failures:
            failed_checks = [check for check in result.checks if not check.passed]
            if not failed_checks:
                lines.append(f"| {result.id} | - | - |")
                continue
            for check in failed_checks:
                detail = check.detail.replace("|", "\\|")
                lines.append(f"| {result.id} | {check.name} | {detail} |")

    if diff is not None:
        lines.extend(
            [
                "",
                "## Baseline diff",
                "",
                f"- 新增 case: {_join(diff.new)}",
                f"- 缺失 case: {_join(diff.missing)}",
                f"- 新增失败: {_join(diff.regressed)}",
                f"- 新 case 失败: {_join(diff.new_failures)}",
                f"- 修复: {_join(diff.fixed)}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _join(items: tuple[str, ...]) -> str:
    return ", ".join(items) if items else "无"


def write_report(
    report: EvalReport,
    out: str | Path,
    diff: BaselineDiff | None = None,
) -> tuple[Path, Path]:
    """写 JSON 到 `out`，Markdown 写同目录同名的 `.md`。"""
    json_path = Path(out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path = json_path.with_suffix(".md")
    markdown_path.write_text(render_markdown(report, diff), encoding="utf-8")
    return json_path, markdown_path


def write_baseline(report: EvalReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
