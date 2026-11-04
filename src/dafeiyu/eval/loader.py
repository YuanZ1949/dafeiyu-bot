"""YAML case 加载与校验。

错误一律包成 `EvalError`，并带上文件名与（能拿到的）case id，CLI 只打印
一行错误，不把 traceback 倒给用户。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .models import EvalCase, EvalContext, EvalExpect, EvalMessage

CASE_ID_RE = re.compile(r"^[a-z0-9-]+$")
CHAT_TYPES = frozenset({"group", "private"})
SUFFIXES = frozenset({".yaml", ".yml"})


class EvalError(Exception):
    """case 文件或 baseline 文件不合法。"""


def load_cases(path: str | Path) -> list[EvalCase]:
    """加载单个 YAML 文件或目录（递归），按文件路径排序保证顺序稳定。"""
    root = Path(path)
    if not root.exists():
        raise EvalError(f"cases 路径不存在: {root}")
    files = [root] if root.is_file() else _yaml_files(root)
    if not files:
        raise EvalError(f"没有找到 YAML case: {root}")

    cases: list[EvalCase] = []
    seen: dict[str, Path] = {}
    for file in files:
        for case in _load_file(file):
            previous = seen.get(case.id)
            if previous is not None:
                raise EvalError(
                    f"{file}: case {case.id!r} 重复（已出现在 {previous}）"
                )
            seen[case.id] = file
            cases.append(case)
    return cases


def _yaml_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise EvalError(f"cases 路径不是文件也不是目录: {root}")
    return sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() in SUFFIXES
    )


def _load_file(file: Path) -> list[EvalCase]:
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"{file}: 读取失败: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise EvalError(f"{file}: YAML 解析失败: {exc}") from exc
    if data is None:
        raise EvalError(f"{file}: 文件为空")
    if not isinstance(data, list):
        raise EvalError(f"{file}: 顶层必须是 case 列表")
    return [_parse_case(item, file) for item in data]


def _parse_case(raw: Any, file: Path) -> EvalCase:
    if not isinstance(raw, dict):
        raise EvalError(f"{file}: case 必须是对象，实际是 {type(raw).__name__}")

    case_id = raw.get("id")
    where = f"{file}: case {case_id!r}" if case_id else f"{file}: case <无 id>"
    if not isinstance(case_id, str) or not case_id:
        raise EvalError(f"{where}: 缺少字符串 id")
    if not CASE_ID_RE.match(case_id):
        raise EvalError(f"{where}: id 只能包含小写字母、数字和 '-'")

    chat_type = raw.get("chat_type")
    if not isinstance(chat_type, str) or chat_type not in CHAT_TYPES:
        raise EvalError(
            f"{where}: chat_type 必须是 {'/'.join(sorted(CHAT_TYPES))}，实际 {chat_type!r}"
        )

    raw_messages = raw.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise EvalError(f"{where}: messages 至少需要 1 条")

    return EvalCase(
        id=case_id,
        description=_as_str(raw.get("description", ""), where, "description"),
        chat_type=chat_type,
        chat_id=_as_str(raw.get("chat_id", ""), where, "chat_id"),
        context=_parse_context(raw.get("context"), where),
        messages=tuple(
            _parse_message(item, where, index)
            for index, item in enumerate(raw_messages)
        ),
        expect=_parse_expect(raw.get("expect"), where),
    )


def _parse_context(raw: Any, where: str) -> EvalContext:
    if raw is None:
        return EvalContext()
    if not isinstance(raw, dict):
        raise EvalError(f"{where}: context 必须是对象")
    impressions = raw.get("impressions", [])
    if not isinstance(impressions, list):
        raise EvalError(f"{where}: context.impressions 必须是列表")
    continuation = raw.get("continuation")
    if continuation is not None and not isinstance(continuation, str):
        raise EvalError(f"{where}: context.continuation 必须是字符串或 null")
    return EvalContext(
        summary=_as_str(raw.get("summary", ""), where, "context.summary"),
        impressions=tuple(
            _as_str(item, where, "context.impressions[]") for item in impressions
        ),
        continuation=continuation,
    )


def _parse_message(raw: Any, where: str, index: int) -> EvalMessage:
    if not isinstance(raw, dict):
        raise EvalError(f"{where}: messages[{index}] 必须是对象")
    if "content" not in raw:
        raise EvalError(f"{where}: messages[{index}] 缺少 content")
    return EvalMessage(
        user_id=_as_str(raw.get("user_id", ""), where, f"messages[{index}].user_id"),
        nickname=_as_str(raw.get("nickname", ""), where, f"messages[{index}].nickname"),
        content=_as_str(raw["content"], where, f"messages[{index}].content"),
        at_bot=_as_bool(raw.get("at_bot", False), where, f"messages[{index}].at_bot"),
        reply_to_bot=_as_bool(
            raw.get("reply_to_bot", False), where, f"messages[{index}].reply_to_bot"
        ),
        timestamp=_as_int(
            raw.get("timestamp", 0), where, f"messages[{index}].timestamp"
        ),
    )


def _parse_expect(raw: Any, where: str) -> EvalExpect:
    if raw is None:
        return EvalExpect()
    if not isinstance(raw, dict):
        raise EvalError(f"{where}: expect 必须是对象")
    return EvalExpect(
        speak=(
            _as_bool(raw["speak"], where, "expect.speak")
            if "speak" in raw
            else None
        ),
        min_chars=(
            _as_int(raw["min_chars"], where, "expect.min_chars")
            if "min_chars" in raw
            else None
        ),
        max_chars=(
            _as_int(raw["max_chars"], where, "expect.max_chars")
            if "max_chars" in raw
            else None
        ),
        must_contain_any=_as_str_list(
            raw.get("must_contain_any", []), where, "expect.must_contain_any"
        ),
        must_not_contain=_as_str_list(
            raw.get("must_not_contain", []), where, "expect.must_not_contain"
        ),
        regex=(
            _as_str(raw["regex"], where, "expect.regex")
            if "regex" in raw
            else None
        ),
        safety=_as_bool(raw.get("safety", False), where, "expect.safety"),
    )


def _as_str(value: Any, where: str, field: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise EvalError(f"{where}: {field} 必须是字符串")


def _as_int(value: Any, where: str, field: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise EvalError(f"{where}: {field} 必须是整数")


def _as_bool(value: Any, where: str, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise EvalError(f"{where}: {field} 必须是布尔值")


def _as_str_list(value: Any, where: str, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EvalError(f"{where}: {field} 必须是字符串列表")
    return tuple(_as_str(item, where, f"{field}[]") for item in value)
