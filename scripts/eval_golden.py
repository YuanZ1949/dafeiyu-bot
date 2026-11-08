"""接话黄金集评测：offline 只校验数据集，live 才真正调用模型。

黄金集在 ``data/evals/golden_replies.json``：每条给一段群聊现场，人工标注
期望 verdict（``speak`` / ``silent``）。offline 模式只做结构校验和分布统计，
不联网、不调模型；live 模式会把每条现场写进临时 SQLite，复用仓库里的
``build_injection`` / ``build_batch_prompt`` 拼出现场文本，再向大脑发一个
「只输出 JSON」的裁判 prompt。

注意：live 是**近似回放**，不是生产链路的逐字节复刻。真实主链路还会叠加
路由闸门、冷却、预算、静默时段、人设 prompt 与历史会话状态；这里的裁判
prompt 只负责判断「该不该开口」，结果用于横向比较，不应等同于线上行为。

用法（必须用仓库虚环境）::

    .venv\\Scripts\\python scripts/eval_golden.py --mode offline
    .venv\\Scripts\\python scripts/eval_golden.py --mode live --limit 10
    .venv\\Scripts\\python scripts/eval_golden.py --mode live --case direct-question-dinner
    .venv\\Scripts\\python scripts/eval_golden.py --mode offline --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO_ROOT / "data" / "evals" / "golden_replies.json"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "evals" / "results"
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"
DEFAULT_ENV = REPO_ROOT / ".env"
STATUSES = ("passed", "failed", "skipped")

# 临时库里所有 case 共用一个会话 id；不代表任何真实群。
EVAL_CHAT_ID = "eval:golden"
EVAL_CHAT_TYPE = "group"
LIVE_TIMEOUT_SECONDS = 30.0
PROGRESS_EVERY = 5

JUDGE_SYSTEM = """你是群聊接话裁判。下面是一段群聊现场，角色「{name}」是群里的机器人。
请判断：看到这批消息后，她现在应该开口说话（speak），还是保持沉默（silent）。
倾向开口：被点名、被 @、直接向她提问、需要接梗/安慰/回应道谢、连续被无视后再次找她。
倾向沉默：群友互相热聊且没有叫她、纯表情刷屏、重复刷屏、广告、敏感争吵、问题已经有人回答。
只输出 JSON，不要 Markdown，不要额外解释：{{"speak": true 或 false, "reason": "一句话理由"}}"""


def load_dataset(path: str | Path) -> dict[str, Any]:
    """读取黄金集 JSON；文件不存在或 JSON 非法时抛 ``OSError`` / ``ValueError``。"""
    dataset_path = Path(path)
    try:
        text = dataset_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(f"读不到数据集 {dataset_path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"数据集不是合法 JSON: {exc}") from exc
    return data


def validate_dataset(data: Any) -> list[str]:
    """返回错误列表；空列表表示合法。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["数据集顶层必须是 JSON 对象"]
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        errors.append("version 必须等于 1")
    cases = data.get("cases")
    if not isinstance(cases, list):
        errors.append("cases 必须是列表")
        return errors
    seen_ids: set[str] = set()
    for index, case in enumerate(cases):
        where = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{where} 必须是对象")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"{where}.id 不能为空")
        elif case_id in seen_ids:
            errors.append(f"{where}.id 重复：{case_id}")
        else:
            seen_ids.add(case_id)
        if case.get("expect") not in ("speak", "silent"):
            errors.append(f"{where}.expect 必须是 speak 或 silent")
        messages = case.get("messages")
        if not isinstance(messages, list) or not messages:
            errors.append(f"{where}.messages 必须是非空列表")
            continue
        for msg_index, message in enumerate(messages):
            msg_where = f"{where}.messages[{msg_index}]"
            if not isinstance(message, dict):
                errors.append(f"{msg_where} 必须是对象")
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                errors.append(f"{msg_where}.content 不能为空")
            minutes_ago = message.get("minutes_ago")
            if (
                not isinstance(minutes_ago, (int, float))
                or isinstance(minutes_ago, bool)
                or minutes_ago < 0
            ):
                errors.append(f"{msg_where}.minutes_ago 必须 >= 0")
    return errors


def summarize(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """汇总结果；skipped 不计入 accuracy 的分母，分母为 0 时 accuracy=None。"""
    statuses = [str(result.get("status") or "") for result in results]
    passed = statuses.count("passed")
    failed = statuses.count("failed")
    skipped = statuses.count("skipped")
    judged = passed + failed
    return {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "accuracy": (passed / judged) if judged else None,
    }


def _utc_iso() -> str:
    """UTC 时间戳（ISO 8601，带微秒）。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _result_filename(generated_at: str, mode: str) -> str:
    """把 ISO 时间戳压成文件名：Windows 不允许冒号。"""
    stamp = generated_at.replace(":", "").replace("+00:00", "Z")
    return f"{stamp}-{mode}.json"


def statuses_from_results(results: Sequence[dict[str, Any]]) -> dict[str, bool]:
    """把 golden 的 ``results[].status`` 适配成 ``diff_statuses`` 的快照。

    ``skipped`` 直接剔除：离线模式或模型不可用时大量跳过，既不算失败也不算修复。
    """
    statuses: dict[str, bool] = {}
    for result in results:
        case_id = str(result.get("id") or "")
        status = str(result.get("status") or "")
        if not case_id:
            continue
        if status == "passed":
            statuses[case_id] = True
        elif status == "failed":
            statuses[case_id] = False
    return statuses


def load_baseline_statuses(path: str | Path) -> dict[str, bool]:
    """读取本脚本落盘的结果 JSON，返回 ``id → 是否通过``。

    只接受 ``{"results": [{"id": ..., "status": passed|failed|skipped}]}``；
    这是 golden 自己的形状，不是 ``eval.report.load_baseline`` 的 ``cases`` 形状。
    """
    baseline_path = Path(path)
    try:
        text = baseline_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"读不到 baseline 文件 {baseline_path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"baseline 不是合法 JSON {baseline_path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError(
            f"baseline 形状不符 {baseline_path}: 需要 golden 结果文件（顶层 results 列表），"
            "不是 eval 的 cases 文件"
        )
    for index, item in enumerate(data["results"]):
        if not isinstance(item, dict):
            raise ValueError(f"baseline results[{index}] 必须是对象: {baseline_path}")
        case_id = item.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"baseline results[{index}].id 必须是非空字符串: {baseline_path}")
        if item.get("status") not in STATUSES:
            raise ValueError(
                f"baseline results[{index}].status 必须是 passed/failed/skipped: "
                f"{baseline_path}"
            )
    return statuses_from_results(data["results"])


def write_run_result(payload: dict[str, Any], out_dir: str | Path) -> Path:
    """原子写入结果 JSON：同目录 tmp + ``os.replace``。失败时不留下半截文件。"""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / _result_filename(
        str(payload["generated_at"]), str(payload["mode"])
    )
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=".tmp-eval-",
        suffix=".json",
        dir=directory,
        delete=False,
    )
    tmp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return target


def _diff_payload(diff: Any) -> dict[str, Any]:
    return {
        "new": list(diff.new),
        "missing": list(diff.missing),
        "regressed": list(diff.regressed),
        "fixed": list(diff.fixed),
        "new_failures": list(diff.new_failures),
    }


def _distribution(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    expects = {"speak": 0, "silent": 0}
    tags: Counter[str] = Counter()
    for case in cases:
        expect = case.get("expect")
        if expect in expects:
            expects[expect] += 1
        for tag in case.get("tags") or []:
            if isinstance(tag, str) and tag:
                tags[tag] += 1
    return {
        "expect": expects,
        "tags": dict(tags.most_common()),
    }


def _select_cases(
    cases: Sequence[dict[str, Any]], case_id: str, limit: int | None
) -> list[dict[str, Any]]:
    selected = list(cases)
    if case_id:
        selected = [case for case in selected if case.get("id") == case_id]
    if limit is not None:
        selected = selected[: max(0, int(limit))]
    return selected


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _first_message(case: dict[str, Any], limit: int = 48) -> str:
    messages = case.get("messages") or []
    if not messages:
        return "—"
    message = messages[0]
    nickname = str(message.get("nickname") or "")
    content = str(message.get("content") or "")
    text = f"{nickname}: {content}" if nickname else content
    return text if len(text) <= limit else f"{text[:limit]}…"


def _render_offline(
    dataset: Path, cases: Sequence[dict[str, Any]], distribution: dict[str, Any]
) -> str:
    expects = distribution["expect"]
    tags = distribution["tags"]
    lines = [
        "# 接话黄金集 · offline 数据集校验",
        "",
        f"- 数据集：`{dataset}`",
        f"- 条数：{len(cases)}（speak {expects['speak']} / silent {expects['silent']}）",
        "- 标签："
        + ("、".join(f"{tag} {count}" for tag, count in tags.items()) or "—"),
        "",
        "| ID | 标签 | 期望 | 首条消息 |",
        "| --- | --- | --- | --- |",
    ]
    for case in cases:
        lines.append(
            "| {id} | {tags} | {expect} | {first} |".format(
                id=_markdown_cell(case.get("id", "")),
                tags=_markdown_cell("、".join(case.get("tags") or []) or "—"),
                expect=_markdown_cell(case.get("expect", "")),
                first=_markdown_cell(_first_message(case)),
            )
        )
    lines.extend(
        [
            "",
            f"数据集校验通过：{len(cases)} 条。offline 不调用模型；"
            "要测真实判断请用 `--mode live`。",
        ]
    )
    return "\n".join(lines)


def _render_live(
    dataset: Path, results: Sequence[dict[str, Any]], summary: dict[str, Any]
) -> str:
    accuracy = summary["accuracy"]
    accuracy_text = "—" if accuracy is None else f"{accuracy * 100:.1f}%"
    lines = [
        "# 接话黄金集 · live 评测",
        "",
        f"- 数据集：`{dataset}`",
        f"- 总数：{summary['total']}，通过 {summary['passed']}，失败 {summary['failed']}，"
        f"跳过 {summary['skipped']}",
        f"- 准确率：{accuracy_text}（跳过不计入分母）",
        "",
        "| ID | 期望 | 实际 | 状态 | 原因 | 延迟 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        latency = int(result.get("latency_ms") or 0)
        lines.append(
            "| {id} | {expect} | {got} | {status} | {reason} | {latency} |".format(
                id=_markdown_cell(result.get("id", "")),
                expect=_markdown_cell(result.get("expect", "")),
                got=_markdown_cell(result.get("got") or "—"),
                status=_markdown_cell(result.get("status", "")),
                reason=_markdown_cell(result.get("reason") or "—"),
                latency=f"{latency}ms" if latency else "—",
            )
        )
    return "\n".join(lines)


def _skipped_result(case: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "id": str(case.get("id") or ""),
        "expect": str(case.get("expect") or ""),
        "got": None,
        "status": "skipped",
        "reason": reason,
        "latency_ms": 0,
    }


def _parse_verdict(text: Any) -> dict[str, Any] | None:
    """从模型输出里抠出 ``{"speak": ..., "reason": ...}``；失败返回 None。"""
    if not isinstance(text, str):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    speak = payload.get("speak")
    if isinstance(speak, str):
        lowered = speak.strip().casefold()
        if lowered in ("true", "yes", "1", "speak"):
            speak = True
        elif lowered in ("false", "no", "0", "silent"):
            speak = False
        else:
            return None
    if not isinstance(speak, bool):
        return None
    return {
        "speak": speak,
        "reason": str(payload.get("reason") or "")[:200],
    }


def _build_live_client(settings: Any, model_override: str) -> tuple[Any | None, str]:
    """优先用配置里的 provider；opencode 无法复用，回落到 token / 本地模型。"""
    from dafeiyu.brain.client import BrainClient
    from dafeiyu.brain.factory import normalize_provider

    provider = normalize_provider(settings.brain.provider or "")
    candidates: list[str] = []
    if provider in ("token", "local_model"):
        candidates.append(provider)
    for candidate in ("token", "local_model"):
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if candidate == "token":
            if not settings.api_key:
                continue
            return (
                BrainClient(
                    api_key=settings.api_key,
                    base_url=settings.base_url,
                    model=model_override or settings.model,
                    trust_env=False,
                    timeout=LIVE_TIMEOUT_SECONDS,
                    reasoning_effort=settings.reasoning_effort or None,
                ),
                "",
            )
        local = settings.local_model
        chosen = model_override or local.model
        if not local.base_url or not chosen:
            continue
        return (
            BrainClient(
                api_key=local.api_key or "local",
                base_url=local.base_url,
                model=chosen,
                trust_env=False,
                max_tokens=local.max_tokens,
                timeout=local.timeout_seconds or LIVE_TIMEOUT_SECONDS,
                reasoning_effort=local.reasoning_effort or None,
            ),
            "",
        )
    return None, "没有可用的模型配置（缺少 DEEPSEEK_API_KEY 或本地模型），live 整体跳过"


def _seed_case(store: Any, case: dict[str, Any], now: float) -> list[Any]:
    """把 case 的消息写进临时库，返回按原顺序排列的 Message 列表。"""
    from dafeiyu.models import ChatType, Message

    raw_messages = case.get("messages") or []
    messages: list[Any] = []
    for index, item in enumerate(raw_messages):
        try:
            minutes_ago = max(0.0, float(item.get("minutes_ago", 0.0)))
        except (TypeError, ValueError):
            minutes_ago = 0.0
        message = Message(
            chat_type=ChatType.GROUP,
            chat_id=EVAL_CHAT_ID,
            user_id=f"eval-{item.get('nickname') or 'user'}",
            nickname=str(item.get("nickname") or "群友"),
            content=str(item.get("content") or ""),
            message_id=f"{case.get('id', 'case')}-{index}",
            timestamp=int(now - minutes_ago * 60),
            at_bot=bool(case.get("addressed")) and index == len(raw_messages) - 1,
        )
        store.add_message(message)
        messages.append(message)
    return messages


def _build_case_prompt(store: Any, settings: Any, messages: list[Any], today: str) -> str:
    from dafeiyu.memory.context import build_injection
    from dafeiyu.memory.timeline import build_batch_prompt
    from dafeiyu.models import ChatType

    injection = build_injection(
        store,
        None,
        ChatType.GROUP,
        EVAL_CHAT_ID,
        settings.injection,
        today=today,
    )
    return build_batch_prompt(messages, injection)


async def _run_live(
    cases: Sequence[dict[str, Any]], model_override: str
) -> list[dict[str, Any]]:
    try:
        from dafeiyu.config import load_settings

        settings = load_settings(env_file=DEFAULT_ENV, config_file=DEFAULT_CONFIG)
    except Exception as exc:  # noqa: BLE001 - 配置失败整体跳过，不让评测崩
        reason = f"配置读取失败：{type(exc).__name__}: {exc}"
        print(f"[eval] {reason}", file=sys.stderr)
        return [_skipped_result(case, reason) for case in cases]

    try:
        client, reason = _build_live_client(settings, model_override)
    except Exception as exc:  # noqa: BLE001 - 客户端构造失败也整体跳过
        client, reason = None, f"模型客户端创建失败：{type(exc).__name__}: {exc}"
    if client is None:
        print(f"[eval] {reason}", file=sys.stderr)
        return [_skipped_result(case, reason) for case in cases]

    from dafeiyu.memory.store import Store
    from dafeiyu.timeutil import SHANGHAI

    results: list[dict[str, Any]] = []
    today = datetime.now(SHANGHAI).date().isoformat()
    now = time.time()
    persona_name = settings.persona.name or "大肥鱼"
    system = JUDGE_SYSTEM.format(name=persona_name)

    try:
        with tempfile.TemporaryDirectory(prefix="dafeiyu-eval-") as tmp:
            for index, case in enumerate(cases):
                result = _skipped_result(case, "")
                started = time.monotonic()
                try:
                    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(case.get("id") or "case"))
                    store = Store(Path(tmp) / f"{safe_id}.db")
                    store.init_schema()
                    messages = _seed_case(store, case, now)
                    prompt = _build_case_prompt(store, settings, messages, today)
                    raw = await client.complete(
                        system,
                        [{"role": "user", "content": prompt}],
                        max_tokens=200,
                        temperature=0.0,
                    )
                    result["latency_ms"] = int((time.monotonic() - started) * 1000)
                    verdict = _parse_verdict(raw)
                    if verdict is None:
                        result["status"] = "skipped"
                        result["reason"] = "模型输出无法解析为 JSON"
                    else:
                        got = "speak" if verdict["speak"] else "silent"
                        result["got"] = got
                        result["status"] = (
                            "passed" if got == case.get("expect") else "failed"
                        )
                        result["reason"] = verdict["reason"]
                except Exception as exc:  # noqa: BLE001 - 单条失败只记 skipped
                    result["latency_ms"] = int((time.monotonic() - started) * 1000)
                    result["status"] = "skipped"
                    result["reason"] = f"{type(exc).__name__}: {exc}"[:200]
                results.append(result)
                if (index + 1) % PROGRESS_EVERY == 0 or index + 1 == len(cases):
                    print(
                        f"[eval] {index + 1}/{len(cases)} {case.get('id')} → "
                        f"{result['status']}",
                        file=sys.stderr,
                    )
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    return results


def _emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="接话黄金集评测（offline 校验 / live 判分）")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="黄金集 JSON 路径")
    parser.add_argument(
        "--mode", choices=("offline", "live"), default="offline", help="offline 只校验"
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--limit", type=int, default=None, help="最多跑 N 条")
    parser.add_argument("--case", default="", help="只跑指定 case id")
    parser.add_argument("--model", default="", help="live 时覆盖模型名")
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="结果 JSON 落盘目录（默认 data/evals/results）",
    )
    parser.add_argument(
        "--baseline",
        default="",
        help="上一次本脚本落盘的结果 JSON（results 形状），用于回归对比",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="有回归（baseline 通过 → 当前失败）时以退出码 1 结束",
    )
    args = parser.parse_args(argv)

    dataset = Path(args.dataset)
    baseline: dict[str, bool] | None = None
    if args.baseline:
        try:
            baseline = load_baseline_statuses(args.baseline)
        except ValueError as exc:
            print(f"baseline 读取失败：{exc}", file=sys.stderr)
            return 2

    try:
        data = load_dataset(dataset)
    except (OSError, ValueError) as exc:
        print(f"数据集读取失败：{exc}", file=sys.stderr)
        return 2

    errors = validate_dataset(data)
    if errors:
        for error in errors:
            print(f"数据集错误：{error}", file=sys.stderr)
        return 2

    cases = _select_cases(data["cases"], args.case, args.limit)
    distribution = _distribution(cases)

    if args.mode == "offline":
        results = [
            _skipped_result(case, "offline：只校验数据集，不调用模型") for case in cases
        ]
    else:
        results = asyncio.run(_run_live(cases, args.model))

    summary = summarize(results)
    generated_at = _utc_iso()
    model = args.model.strip() or ("offline" if args.mode == "offline" else "")
    payload: dict[str, Any] = {
        "dataset": str(dataset),
        "mode": args.mode,
        "model": model,
        "generated_at": generated_at,
        "summary": summary,
        "results": results,
    }
    try:
        result_path = write_run_result(payload, args.out_dir)
    except OSError as exc:
        print(f"结果写入失败 {args.out_dir}：{exc}", file=sys.stderr)
        return 2

    diff = None
    if baseline is not None:
        from dafeiyu.eval.report import diff_statuses

        diff = diff_statuses(baseline, statuses_from_results(results))

    if args.json:
        output = dict(payload)
        output["distribution"] = distribution
        output["result_path"] = str(result_path)
        if diff is not None:
            output["baseline"] = _diff_payload(diff)
        _emit_json(output)
    else:
        if args.mode == "offline":
            print(_render_offline(dataset, cases, distribution))
        else:
            print(_render_live(dataset, results, summary))
        print(f"\n结果已写入：{result_path}")
        if diff is not None:
            print(
                "Baseline diff：回归 {regressed}，新 case 失败 {new_failures}，"
                "修复 {fixed}，新增 {new}，缺失 {missing}".format(
                    regressed=", ".join(diff.regressed) or "无",
                    new_failures=", ".join(diff.new_failures) or "无",
                    fixed=", ".join(diff.fixed) or "无",
                    new=", ".join(diff.new) or "无",
                    missing=", ".join(diff.missing) or "无",
                )
            )

    if args.fail_on_regression:
        if diff is None:
            print(
                "[eval] 未提供 --baseline，回归门没有比较对象，跳过",
                file=sys.stderr,
            )
        elif diff.regressed:
            print(
                f"[eval] 回归门拦截：{', '.join(diff.regressed)}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
