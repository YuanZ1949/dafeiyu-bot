"""CLI：`python -m dafeiyu.eval --cases ... [--out ...]`。

默认 FakeBrain；只有显式 `--live` 才会构造真实 brain。不写主库。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from dafeiyu.config import load_settings

from .loader import EvalError, load_cases
from .models import EvalReport
from .recorder import record_cases
from .report import (
    BaselineDiff,
    diff_reports,
    load_baseline,
    write_baseline,
    write_report,
)
from .runner import FakeBrain, run_cases

DEFAULT_BASELINE = Path("eval/baseline.json")
DEFAULT_RECORDED = Path("eval/cases/recorded.yaml")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dafeiyu.eval",
        description="离线评测：YAML case + 确定性检查，默认假模型",
    )
    parser.add_argument("--cases", default=None, help="case YAML 文件或目录")
    parser.add_argument("--out", default=None, help="报告 JSON 输出路径（同名 .md 一并写）")
    parser.add_argument("--baseline", default=None, help="baseline JSON 路径")
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="有回归（含新 case 失败）时退出码 1",
    )
    parser.add_argument(
        "--live", action="store_true", help="显式使用真实 brain（会联网，默认关闭）"
    )
    parser.add_argument("--model", default="", help="--live 时的模型名")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="把本次报告写成 baseline（默认 eval/baseline.json）",
    )
    parser.add_argument(
        "--record", action="store_true", help="从生产库只读录制 case（不调用模型）"
    )
    parser.add_argument(
        "--chat-type", choices=("group", "private"), default=None, help="录制时的会话类型"
    )
    parser.add_argument("--chat-id", default="", help="录制时的会话 id（如 qq:123）")
    parser.add_argument("--limit", type=int, default=200, help="录制时最多读取的消息条数")
    parser.add_argument(
        "--db", default=None, help="录制时使用的数据库路径（默认取配置 db_path）"
    )
    return parser


def build_live_brain(model: str = "") -> Any:
    """只有 `--live` 走到这里；用内存 Store，绝不碰主库。"""
    from dafeiyu.brain.factory import build_brain
    from dafeiyu.config import load_settings
    from dafeiyu.memory.store import Store

    settings = load_settings()
    if model:
        settings = replace(settings, model=model)
    store = Store(":memory:")
    return build_brain(settings, store, settings.brain.provider, model=model)


def _record_main(args: argparse.Namespace) -> int:
    """`--record` 分支：只读库 + 写 YAML，全程不构造 brain。"""
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - 配置缺失不该倒 traceback
        print(f"错误: 无法加载配置: {exc}", file=sys.stderr)
        return 2

    db_path = Path(args.db) if args.db else Path(settings.db_path)
    out = Path(args.out) if args.out else DEFAULT_RECORDED
    cases = record_cases(
        db_path=db_path,
        chat_type=args.chat_type or "group",
        chat_id=args.chat_id,
        limit=args.limit,
        out=out,
        max_chars=settings.persona.max_reply_chars,
        banned_phrases=settings.behavior.banned_phrases,
        triggers=settings.triggers,
    )
    if not cases:
        print("没有可录制消息")
        return 0
    print(f"已录制 {len(cases)} 个 case -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.record:
        if args.live:
            print("错误: --record 与 --live 互斥", file=sys.stderr)
            return 2
        if not args.chat_type or not args.chat_id.strip():
            print("错误: --record 需要 --chat-type 和 --chat-id", file=sys.stderr)
            return 2
        return _record_main(args)

    if not args.cases:
        print("错误: 缺少 --cases（录制请用 --record）", file=sys.stderr)
        return 2

    try:
        cases = load_cases(args.cases)
    except EvalError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    mode = "live" if args.live else "fake"
    try:
        brain: Any = build_live_brain(args.model) if args.live else FakeBrain()
    except Exception as exc:  # noqa: BLE001 - 配置缺失不该倒 traceback
        print(f"错误: 无法构造 brain: {exc}", file=sys.stderr)
        return 2

    report = run_cases(cases, brain, mode)

    diff: BaselineDiff | None = None
    baseline_path = Path(args.baseline) if args.baseline else None
    # --update-baseline 允许目标文件尚不存在：此时只写，不做 diff。
    if baseline_path is not None and (
        not args.update_baseline or baseline_path.exists()
    ):
        try:
            diff = diff_reports(load_baseline(baseline_path), report)
        except EvalError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2

    if args.out:
        write_report(report, args.out, diff)
    if args.update_baseline:
        target = Path(args.baseline) if args.baseline else DEFAULT_BASELINE
        write_baseline(report, target)

    _print_summary(report, diff)

    if args.fail_on_regression:
        regressed = diff.has_regressions() if diff is not None else report.failed > 0
        if regressed:
            return 1
    return 0


def _print_summary(report: EvalReport, diff: BaselineDiff | None) -> None:
    parts = [
        "评测完成",
        f"mode={report.mode}",
        f"model={report.model}",
        f"total={report.total}",
        f"passed={report.passed}",
        f"failed={report.failed}",
        f"duration={report.duration_seconds:.2f}s",
    ]
    if diff is not None:
        parts.append(
            "diff="
            f"new:{len(diff.new)} missing:{len(diff.missing)} "
            f"regressed:{len(diff.regressed)} fixed:{len(diff.fixed)}"
        )
    print(" ".join(parts))


if __name__ == "__main__":
    raise SystemExit(main())
