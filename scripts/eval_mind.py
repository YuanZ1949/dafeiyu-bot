"""确定性心智 eval CLI：8 个离线场景，全部走真实生产函数。

不联网、不调模型、不读真实配置、不写 ``data/``（临时 SQLite 库落在系统
临时目录或 ``--tmp-dir`` 下）。全部场景通过退出码 0，否则 1；无法运行
（临时目录不可写、模块加载失败）时打印可读错误并以退出码 1 结束。

用法（必须用仓库虚环境）::

    .venv\\Scripts\\python scripts/eval_mind.py
    .venv\\Scripts\\python scripts/eval_mind.py --json
    .venv\\Scripts\\python scripts/eval_mind.py --verbose
    .venv\\Scripts\\python scripts/eval_mind.py --tmp-dir D:\\tmp\\eval-mind
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 管道默认走本地码页，中文 JSON 会变成 GBK；统一 UTF-8 输出。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="确定性心智 eval（离线，不调模型）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--verbose", action="store_true", help="打印每个场景的 detail")
    parser.add_argument(
        "--tmp-dir", default="", help="临时目录根（不存在会创建）；默认用系统临时目录"
    )
    args = parser.parse_args(argv)

    try:
        from dafeiyu.eval.mind import run_all
    except Exception as exc:  # noqa: BLE001 - 加载失败给可读错误，不抛 traceback
        print(f"无法加载心智 eval 模块：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    tmp_dir = Path(args.tmp_dir) if args.tmp_dir.strip() else None
    started = time.monotonic()
    try:
        payload = run_all(tmp_dir=tmp_dir)
    except OSError as exc:
        print(f"无法运行心智 eval（临时目录不可用）：{exc}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started

    if args.json:
        output: dict[str, Any] = dict(payload)
        output["elapsed_seconds"] = round(elapsed, 3)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        scenarios = payload["scenarios"]
        print(
            f"确定性心智 eval（离线）：{len(scenarios)} 个场景，耗时 {elapsed:.2f}s"
        )
        for item in scenarios:
            mark = "PASS" if item["passed"] else "FAIL"
            print(f"  [{mark}] {item['name']}")
            if args.verbose or not item["passed"]:
                print(f"        {item['detail']}")
        print("结果：全部通过" if payload["ok"] else "结果：存在失败")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
