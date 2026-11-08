"""把线上最近的聊天抽样成可回放的评测 case。

与 ``python -m dafeiyu.eval --record`` 的区别：``--record`` 只支持固定
``chat_type`` / ``chat_id``，这里按时间窗自动发现活跃会话并限制每会话条数。

边界：

- **只读**：缺库/缺表/没消息都打印说明并退出 0，绝不创建或迁移数据库；
- **不调用模型**：只做确定性采样，产出直接交给 ``eval.loader.load_cases``；
- **只采人类消息**：``is_bot = 0`` 在 SQL 层过滤，bot 自己的发言不会变成
  回放里的 user 消息。

用法::

    .venv\\Scripts\\python scripts/eval_sample.py --days 7 --per-chat 5
    .venv\\Scripts\\python scripts/eval_sample.py --db data/dafeiyu.db --days 3 --per-chat 3
    .venv\\Scripts\\python scripts/eval_sample.py --chat-type group --out data/evals/samples/1.yaml
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dafeiyu.config import ConfigError, TriggerConfig, load_settings
from dafeiyu.eval.models import EvalCase
from dafeiyu.eval.recorder import (
    _COLUMNS,
    _open_readonly,
    _row_to_message,
    build_cases,
    slug,
    write_cases,
)
from dafeiyu.models import Message

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE_DIR = REPO_ROOT / "data" / "evals" / "samples"
#: 采样只接受 loader 认识的两类会话，避免把未知 chat_type 写进 YAML。
SAMPLE_CHAT_TYPES = ("group", "private")
DEFAULT_MAX_CHARS = 120


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return value


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/eval_sample.py",
        description="从主库只读抽样最近的聊天，产出可回放的评测 case（不调用模型）",
    )
    parser.add_argument(
        "--days", type=_positive_float, default=7.0, help="只看最近 N 天（默认 7）"
    )
    parser.add_argument(
        "--per-chat", type=_positive_int, default=5, help="每个会话最多抽最近 N 条人类消息"
    )
    parser.add_argument(
        "--out", default=None, help="输出 YAML（默认 data/evals/samples/<UTC 日期>.yaml）"
    )
    parser.add_argument(
        "--db", default=None, help="主库路径（默认取配置 db_path）"
    )
    parser.add_argument(
        "--chat-type", choices=SAMPLE_CHAT_TYPES, default=None, help="只采样该会话类型"
    )
    return parser


def _default_out() -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return DEFAULT_SAMPLE_DIR / f"{today}.yaml"


@dataclass
class _SampleParams:
    """采样时复用的评测参数；配置不可用时退回保守默认值。"""

    db_path: Path | None
    max_chars: int
    banned_phrases: tuple[str, ...]
    triggers: Any


def _fallback_triggers() -> Any:
    return TriggerConfig(
        cooldown_seconds=0,
        sample_rate=0.0,
        quiet_hours=("", ""),
        keywords=(),
    )


def _resolve_params(db_override: str | None) -> _SampleParams:
    """读配置；显式给了 ``--db`` 时允许配置缺失，退回默认评测参数。"""
    try:
        settings = load_settings()
    except (ConfigError, OSError) as exc:
        if db_override is None:
            raise
        print(f"提示: 无法加载配置（{exc}），使用默认评测参数", file=sys.stderr)
        return _SampleParams(
            Path(db_override), DEFAULT_MAX_CHARS, (), _fallback_triggers()
        )
    return _SampleParams(
        Path(db_override) if db_override is not None else Path(settings.db_path),
        settings.persona.max_reply_chars,
        tuple(settings.behavior.banned_phrases),
        settings.triggers,
    )


def _discover_chats(
    con: sqlite3.Connection, since: int, chat_type: str | None
) -> list[tuple[str, str]]:
    """找出时间窗内有非 bot 消息的 (chat_type, chat_id)，按最近活跃排序。"""
    sql = (
        "SELECT chat_type, chat_id FROM messages "
        "WHERE is_bot = 0 AND timestamp >= ? "
        "AND chat_type IN (?, ?) "
    )
    params: list[Any] = [since, *SAMPLE_CHAT_TYPES]
    if chat_type is not None:
        sql += "AND chat_type = ? "
        params.append(chat_type)
    sql += "GROUP BY chat_type, chat_id ORDER BY MAX(timestamp) DESC"
    rows = con.execute(sql, params).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def _recent_human_messages(
    con: sqlite3.Connection,
    chat_type: str,
    chat_id: str,
    since: int,
    limit: int,
) -> list[Message]:
    """读某个会话最近 ``limit`` 条人类消息（旧 -> 新），复用 recorder 的行映射。"""
    rows = con.execute(
        f"SELECT {_COLUMNS} FROM messages "
        "WHERE chat_type = ? AND chat_id = ? AND is_bot = 0 AND timestamp >= ? "
        "ORDER BY id DESC LIMIT ?",
        (chat_type, chat_id, since, max(1, int(limit))),
    ).fetchall()
    return [
        _row_to_message(row, chat_type, chat_id) for row in reversed(rows)
    ]


def _latest_summary(
    con: sqlite3.Connection, chat_type: str, chat_id: str
) -> str:
    try:
        row = con.execute(
            "SELECT content FROM session_summaries "
            "WHERE chat_type = ? AND chat_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (chat_type, chat_id),
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row is not None else ""


def _unique_prefix(prefix: str, used: set[str]) -> str:
    """chat_id 归一化后可能撞车（如 ``qq:1`` / ``qq-1``），加序号保证 id 唯一。"""
    candidate = slug(prefix)
    suffix = 2
    while candidate in used:
        candidate = f"{slug(prefix)}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def sample_cases(
    db_path: str | Path,
    *,
    days: float,
    per_chat: int,
    chat_type: str | None,
    max_chars: int,
    banned_phrases: Sequence[str],
    triggers: Any,
    now: int | None = None,
) -> tuple[list[EvalCase], str]:
    """只读采样并构造 case；返回 (cases, 失败原因)，原因为空表示成功。"""
    current = int(time.time()) if now is None else int(now)
    since = int(current - days * 86400)
    con = _open_readonly(db_path)
    if con is None:
        return [], f"找不到可读的数据库：{db_path}（不会创建任何文件）"
    try:
        try:
            chats = _discover_chats(con, since, chat_type)
        except sqlite3.Error as exc:
            return [], f"读取 messages 表失败（可能缺少该表）：{exc}"
        if not chats:
            scope = f"{chat_type} " if chat_type else ""
            return [], f"最近 {days:g} 天没有可采样的{scope}人类消息"
        cases: list[EvalCase] = []
        used_prefixes: set[str] = set()
        for current_type, chat_id in chats:
            try:
                messages = _recent_human_messages(
                    con, current_type, chat_id, since, per_chat
                )
            except sqlite3.Error as exc:
                print(
                    f"提示: 跳过 {current_type}/{chat_id}：读取失败（{exc}）",
                    file=sys.stderr,
                )
                continue
            if not messages:
                continue
            stem = _unique_prefix(f"{current_type}-{slug(chat_id)}", used_prefixes)
            cases.extend(
                build_cases(
                    messages,
                    _latest_summary(con, current_type, chat_id),
                    chat_type=current_type,
                    chat_id=chat_id,
                    stem=stem,
                    max_chars=max_chars,
                    banned_phrases=banned_phrases,
                    triggers=triggers,
                )
            )
    finally:
        con.close()
    if not cases:
        return [], "没有可采样的消息"
    return cases, ""


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        params = _resolve_params(args.db)
    except (ConfigError, OSError) as exc:
        print(f"错误: 无法加载配置: {exc}", file=sys.stderr)
        return 2
    db_path = params.db_path
    if db_path is None:
        print("错误: 既没有 --db，也无法从配置解析 db_path", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else _default_out()
    cases, reason = sample_cases(
        db_path,
        days=args.days,
        per_chat=args.per_chat,
        chat_type=args.chat_type,
        max_chars=params.max_chars,
        banned_phrases=params.banned_phrases,
        triggers=params.triggers,
    )
    if not cases:
        print(reason)
        return 0

    write_cases(out, cases)
    print(f"已采样 {len(cases)} 个 case -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
