"""接话判断离线评估：只读，统计「该不该开口」与「为什么沉默」。

数据来源只有四张表（见 spec §14）：

* ``messages``：入站 / 机器人发言；**正文字段是 content，不是 text**；
* ``chat_state``：当前静音、降频、``no_reply_streak`` 快照；
* ``usage``：按日聚合的 ambient / reply 调用数（只能做交叉校验）；
* ``reply_outcomes``：机器人发言后 180 秒内有没有人接。

数据库里没有「批次日志」，所以本脚本按 ``batch_seconds``（默认 12 秒）把入站
消息切成批次，再用「批次结束后响应窗内有没有机器人发言」反推她是否沉默。
这是评估口径，不是服务端真实判定；局限见报告末尾。

用法（必须用仓库虚环境的 python 跑）::

    .venv\\Scripts\\python scripts/eval_reply_judgment.py
    .venv\\Scripts\\python scripts/eval_reply_judgment.py --days 1 --json
    .venv\\Scripts\\python scripts/eval_reply_judgment.py --chat qq:123456 --out report.md

默认只读打开 ``data/dafeiyu.db``，不写库、不建表、不跑迁移。``--out`` 只写
Markdown 报告文件；控制台输出里 chat_id 一律脱敏成短哈希。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "dafeiyu.db"
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"

# 与 triggers.batch_seconds 默认值一致；批次切分只影响「沉默」的口径。
DEFAULT_BATCH_SECONDS = 12.0
# 机器人发言后的观察窗。与 workers/outcome.py 的 OUTCOME_WINDOW_SECONDS 一致。
DEFAULT_RESPONSE_WINDOW = 180
# 默认关键词 / 静默时段只在读不到 config.yaml 时兜底。
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "怎么",
    "为什么",
    "为啥",
    "求",
    "有没有",
    "是不是",
    "崩了",
    "报错",
    "怎么办",
)
DEFAULT_QUIET: tuple[str, str] = ("23:30", "08:00")
# 否定口语：关键词前一个字落在这些字上时不算提问（与 router._is_question 同口径）。
NEGATION_CHARS = "没不无别"

# 主因归类顺序：越靠前越具体。顺序刻意把「点名未回」放第一，因为这是最该
# 修的漏接；纯表情/媒体其次，用来区分「没话说」与「判断错了」。
PRIMARY_REASONS: tuple[str, ...] = (
    "点名未回",
    "纯表情/媒体",
    "静默时段",
    "静音/降频/预算",
    "关键词/提问",
    "无@普通闲聊",
)
FEATURE_LABELS: tuple[str, ...] = (
    "纯表情/媒体",
    "点名",
    "关键词/提问",
    "静默时段",
    "静音/降频/预算",
    "无@",
)

SHANGHAI = timezone(timedelta(hours=8))
_PLACEHOLDER_RE = re.compile(r"\[[^\[\]\n]{1,16}\]")
_MEDIA_PATH_RE = re.compile(r"[（(]\s*图存[^（）()]*[）)]")

LIMITATIONS: tuple[str, ...] = (
    "沉默批次按「入站批次 + 响应窗内无机器人发言」推定；服务端没有批次日志，"
    "真实批次可能因防抖/合并与本口径不同。",
    "ambient/reply 按批次是否被点名（@、引用、正文别名）推定；usage 只有按日"
    "聚合，无法逐条对齐，报告中同时给出 usage 交叉校验。",
    "reply_outcomes 为空时表示 OutcomeWorker 尚未产出，不代表机器人发言从未冷场。",
    "静默时段取 config triggers.quiet_hours（00:00–00:00 表示未启用）；预算只按"
    "每日调用数估算，不含全局/月度/费用维度。",
    "no_reply_streak 是 chat_state 的当前快照，不是历史分布；连续沉默长度由批次"
    "序列另行推定。",
    "纯表情/媒体按占位符与 Unicode 符号判定，[旺柴] 这类平台表情也算纯表情；"
    "一条媒体消息带说明文字时不算纯表情。",
    "机器人被拆成多条发送时每条分片都记一次发言，发言次数可能略高于实际回合数。",
)


# --------------------------------------------------------------------- 配置


@dataclass(frozen=True)
class Hints:
    """判定用的外部提示；全部来自 config 或命令行，读不到就用默认值。"""

    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    aliases: tuple[str, ...] = ()
    quiet_start: str = DEFAULT_QUIET[0]
    quiet_end: str = DEFAULT_QUIET[1]
    daily_calls: int = 0
    ambient_ratio: float = 0.8
    batch_seconds: float = DEFAULT_BATCH_SECONDS


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _hhmm(value: object, fallback: str) -> str:
    """校验 HH:MM；不合法就回落，绝不因为配置脏值让评估脚本崩掉。"""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return fallback
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return fallback
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


def load_hints(config_path: str | Path | None = None) -> Hints:
    """尽力从 config.yaml 读取判定口径；任何异常都退回默认值。"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 配置缺失/损坏不应阻断离线评估
        return Hints()
    if not isinstance(raw, dict):
        return Hints()

    triggers = raw.get("triggers")
    if not isinstance(triggers, dict):
        triggers = {}
    budget = raw.get("budget")
    if not isinstance(budget, dict):
        budget = {}
    persona = raw.get("persona")
    if not isinstance(persona, dict):
        persona = {}

    quiet = triggers.get("quiet_hours")
    quiet_start, quiet_end = DEFAULT_QUIET
    if isinstance(quiet, (list, tuple)) and len(quiet) == 2:
        quiet_start = _hhmm(quiet[0], quiet_start)
        quiet_end = _hhmm(quiet[1], quiet_end)

    keywords = _str_tuple(triggers.get("keywords")) or DEFAULT_KEYWORDS
    batch_seconds = triggers.get("batch_seconds", DEFAULT_BATCH_SECONDS)
    try:
        batch = float(batch_seconds)
    except (TypeError, ValueError):
        batch = DEFAULT_BATCH_SECONDS
    if batch <= 0:
        batch = DEFAULT_BATCH_SECONDS

    try:
        daily_calls = max(0, int(budget.get("daily_calls") or 0))
    except (TypeError, ValueError):
        daily_calls = 0
    try:
        ambient_ratio = float(budget.get("ambient_ratio", 0.8))
    except (TypeError, ValueError):
        ambient_ratio = 0.8
    if not 0 < ambient_ratio <= 1:
        ambient_ratio = 0.8

    return Hints(
        keywords=keywords,
        aliases=_str_tuple(persona.get("aliases")),
        quiet_start=quiet_start,
        quiet_end=quiet_end,
        daily_calls=daily_calls,
        ambient_ratio=ambient_ratio,
        batch_seconds=batch,
    )


# --------------------------------------------------------------------- 只读库


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """以 ``mode=ro`` + ``query_only`` 打开；不建表、不写 WAL、不跑迁移。"""
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """``PRAGMA table_info`` 的真实列名；表不存在时返回空集合。"""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {str(row[1]) for row in rows}


# --------------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class MessageRow:
    """只保留评估需要的字段；正文仅用于特征判定，不写进报告。"""

    row_id: int
    chat_type: str
    chat_id: str
    ts: int
    content: str
    is_bot: bool
    at_bot: bool
    reply_to_bot: bool
    recalled: bool


@dataclass
class Batch:
    """一个入站批次及其判定特征。"""

    messages: list[MessageRow]
    start_ts: int
    end_ts: int
    addressed: bool
    pure_media: bool
    keyword: bool
    quiet: bool
    throttle: str
    mature: bool = True
    answered: bool = False
    bot_speeches: list[MessageRow] = field(default_factory=list)


@dataclass
class ChatState:
    """chat_state 的只读快照；没有行时全为空/零。"""

    no_reply_streak: int = 0
    ambient_streak: int = 0
    muted_until: float | None = None
    downshift_until: float | None = None


@dataclass
class UsageStats:
    """按会话聚合的 usage；同时保留按日计数供预算归类。"""

    by_purpose: Counter[str] = field(default_factory=Counter)
    calls_by_day: dict[str, int] = field(default_factory=dict)
    ambient_by_day: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------- 读表


def _num(value: object, default: float = 0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _load_messages(
    conn: sqlite3.Connection,
    since: int,
    until: int,
    chat: str | None,
    columns: set[str],
) -> list[MessageRow]:
    if "content" not in columns:
        return []

    def col(name: str, fallback: str = "''") -> str:
        return name if name in columns else f"{fallback} AS {name}"

    sql = f"""
        SELECT id AS row_id, chat_type, chat_id, timestamp, content,
               {col('is_bot', '0')}, {col('at_bot', '0')},
               {col('reply_to_bot', '0')}, {col('recalled_at', 'NULL')}
        FROM messages
        WHERE timestamp >= ? AND timestamp <= ?
    """
    params: list[object] = [since, until]
    if chat is not None:
        sql += " AND chat_id = ?"
        params.append(chat)
    sql += " ORDER BY chat_type, chat_id, timestamp, id"

    rows: list[MessageRow] = []
    for row in conn.execute(sql, params):
        rows.append(
            MessageRow(
                row_id=int(_num(row["row_id"])),
                chat_type=str(row["chat_type"]),
                chat_id=str(row["chat_id"]),
                ts=int(_num(row["timestamp"])),
                content=str(row["content"] or ""),
                is_bot=bool(int(_num(row["is_bot"]))),
                at_bot=bool(int(_num(row["at_bot"]))),
                reply_to_bot=bool(int(_num(row["reply_to_bot"]))),
                recalled=row["recalled_at"] is not None,
            )
        )
    return rows


def _load_chat_state(conn: sqlite3.Connection, columns: set[str]) -> dict[tuple[str, str], ChatState]:
    if not columns:
        return {}

    def col(name: str) -> str:
        return name if name in columns else "NULL"

    sql = f"""
        SELECT chat_type, chat_id, {col('no_reply_streak')} AS no_reply_streak,
               {col('ambient_streak')} AS ambient_streak,
               {col('muted_until')} AS muted_until,
               {col('downshift_until')} AS downshift_until
        FROM chat_state
    """
    states: dict[tuple[str, str], ChatState] = {}
    for row in conn.execute(sql):
        muted = row["muted_until"]
        downshift = row["downshift_until"]
        states[(str(row["chat_type"]), str(row["chat_id"]))] = ChatState(
            no_reply_streak=max(0, int(_num(row["no_reply_streak"]))),
            ambient_streak=max(0, int(_num(row["ambient_streak"]))),
            muted_until=float(muted) if muted is not None else None,
            downshift_until=float(downshift) if downshift is not None else None,
        )
    return states


def _load_usage(
    conn: sqlite3.Connection,
    columns: set[str],
    start_day: str,
    end_day: str,
    chat: str | None,
) -> dict[tuple[str, str], UsageStats]:
    if not {"chat_type", "chat_id", "date", "purpose"} <= columns:
        return {}

    sql = """
        SELECT chat_type, chat_id, date, purpose, COUNT(*) AS calls
        FROM usage
        WHERE date >= ? AND date <= ?
    """
    params: list[object] = [start_day, end_day]
    if chat is not None:
        sql += " AND chat_id = ?"
        params.append(chat)
    sql += " GROUP BY chat_type, chat_id, date, purpose"

    stats: dict[tuple[str, str], UsageStats] = {}
    for row in conn.execute(sql, params):
        key = (str(row["chat_type"]), str(row["chat_id"]))
        item = stats.setdefault(key, UsageStats())
        purpose = str(row["purpose"])
        calls = int(_num(row["calls"]))
        item.by_purpose[purpose] += calls
        day = str(row["date"])
        if purpose in ("reply", "ambient"):
            item.calls_by_day[day] = item.calls_by_day.get(day, 0) + calls
        if purpose == "ambient":
            item.ambient_by_day[day] = item.ambient_by_day.get(day, 0) + calls
    return stats


def _load_outcomes(
    conn: sqlite3.Connection,
    columns: set[str],
    since: int,
    until: int,
    chat: str | None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """按真实列聚合 reply_outcomes；缺列/缺表时返回空。"""
    if not {"chat_type", "chat_id", "spoken_at"} <= columns:
        return {}

    def col(name: str) -> str:
        return f"COALESCE({name}, 0)" if name in columns else "0"

    sql = f"""
        SELECT chat_type, chat_id,
               COUNT(*) AS total,
               COALESCE(SUM({col('engaged')}), 0) AS engaged,
               COALESCE(SUM({col('quoted')}), 0) AS quoted,
               COALESCE(SUM({col('replied')}), 0) AS replied,
               COALESCE(SUM({col('silence_after')}), 0) AS silent
        FROM reply_outcomes
        WHERE spoken_at >= ? AND spoken_at <= ?
    """
    params: list[object] = [since, until]
    if chat is not None:
        sql += " AND chat_id = ?"
        params.append(chat)
    sql += " GROUP BY chat_type, chat_id"

    results: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(sql, params):
        total = int(_num(row["total"]))
        engaged = int(_num(row["engaged"]))
        results[(str(row["chat_type"]), str(row["chat_id"]))] = {
            "total": total,
            "engaged": engaged,
            "quoted": int(_num(row["quoted"])),
            "replied": int(_num(row["replied"])),
            "silent": int(_num(row["silent"])),
            "engaged_rate": engaged / total if total else None,
        }
    return results


# --------------------------------------------------------------------- 特征


def _is_pure_media(text: str) -> bool:
    """只含媒体/表情占位符或 Unicode 符号时算纯表情。

    微信/QQ 的入站媒体在库里的正文是 ``[图片]`` / ``[表情]`` / ``[旺柴]``，
    QQ 图片后面还会跟 ``（图存 <路径>，可用 read 查看）``；这些都不算文字。
    """
    stripped = _MEDIA_PATH_RE.sub("", text)
    stripped = _PLACEHOLDER_RE.sub("", stripped)
    for ch in stripped:
        if ch.isspace():
            continue
        category = unicodedata.category(ch)
        # P=标点 S=符号（emoji） M=组合符 Z=分隔 C=控制/格式
        if category.startswith(("P", "S", "M", "Z", "C")):
            continue
        return False
    return True


def _is_question(text: str, keywords: Sequence[str]) -> bool:
    """问号结尾直接算提问；关键词命中时排除「没什么 / 不怎么」这类否定口语。"""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith(("?", "？")):
        return True
    for word in keywords:
        if not word:
            continue
        start = 0
        while (index := stripped.find(word, start)) != -1:
            if index == 0 or stripped[index - 1] not in NEGATION_CHARS:
                return True
            start = index + 1
    return False


def _hhmm_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _in_quiet_hours(ts: int, hints: Hints) -> bool:
    """跨零点也成立；起止相同表示未启用静默时段。"""
    start = _hhmm_minutes(hints.quiet_start)
    end = _hhmm_minutes(hints.quiet_end)
    if start == end:
        return False
    local = datetime.fromtimestamp(ts, tz=SHANGHAI)
    current = local.hour * 60 + local.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=SHANGHAI).strftime("%Y-%m-%d")


def _is_addressed(message: MessageRow, aliases: Sequence[str]) -> bool:
    if message.at_bot or message.reply_to_bot:
        return True
    text = message.content.casefold()
    return any(alias and alias.casefold() in text for alias in aliases)


def _throttle_reason(
    ts: int,
    state: ChatState,
    usage: UsageStats,
    addressed: bool,
    hints: Hints,
) -> str:
    """静音 > 降频 > 预算；返回空串表示没有运营层面的抑制。"""
    if state.muted_until is not None and ts < state.muted_until:
        return "静音"
    if state.downshift_until is not None and ts < state.downshift_until:
        return "降频"
    if hints.daily_calls <= 0:
        return ""
    day = _day_of(ts)
    total = usage.calls_by_day.get(day, 0)
    ambient = usage.ambient_by_day.get(day, 0)
    ambient_limit = hints.daily_calls * hints.ambient_ratio
    if total >= hints.daily_calls:
        return "预算"
    if not addressed and ambient >= ambient_limit:
        return "预算"
    return ""


# --------------------------------------------------------------------- 批次


def _build_batches(
    inbound: list[MessageRow],
    bot_messages: list[MessageRow],
    state: ChatState,
    usage: UsageStats,
    hints: Hints,
    now_ts: int,
    response_window: int,
) -> tuple[list[Batch], int]:
    """把入站消息切批、回填机器人发言，并判定是否成熟/是否被接。"""
    if not inbound:
        return [], len(bot_messages)

    groups: list[list[MessageRow]] = []
    current: list[MessageRow] = [inbound[0]]
    for message in inbound[1:]:
        if message.ts - current[-1].ts > hints.batch_seconds:
            groups.append(current)
            current = [message]
        else:
            current.append(message)
    groups.append(current)

    batches: list[Batch] = []
    for group in groups:
        addressed = any(_is_addressed(item, hints.aliases) for item in group)
        batches.append(
            Batch(
                messages=group,
                start_ts=group[0].ts,
                end_ts=group[-1].ts,
                addressed=addressed,
                pure_media=all(_is_pure_media(item.content) for item in group),
                keyword=any(_is_question(item.content, hints.keywords) for item in group),
                quiet=_in_quiet_hours(group[0].ts, hints),
                throttle=_throttle_reason(
                    group[0].ts, state, usage, addressed, hints
                ),
            )
        )

    starts = [batch.start_ts for batch in batches]
    unattributed = 0
    for bot in bot_messages:
        index = bisect_right(starts, bot.ts) - 1
        if index < 0:
            unattributed += 1
            continue
        batch = batches[index]
        interval_end = (
            batches[index + 1].start_ts
            if index + 1 < len(batches)
            else batch.end_ts + response_window
        )
        if bot.ts < interval_end:
            batch.bot_speeches.append(bot)
            batch.answered = True
        else:
            # 最后一批过了响应窗，或机器人自发冒泡（没有对应入站）。
            unattributed += 1

    for index, batch in enumerate(batches):
        is_last = index == len(batches) - 1
        batch.mature = (
            not is_last
            or batch.answered
            or now_ts >= batch.end_ts + response_window
        )
    return batches, unattributed


# --------------------------------------------------------------------- 统计


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _primary_reason(batch: Batch) -> str:
    if batch.addressed:
        return "点名未回"
    if batch.pure_media:
        return "纯表情/媒体"
    if batch.quiet:
        return "静默时段"
    if batch.throttle:
        return "静音/降频/预算"
    if batch.keyword:
        return "关键词/提问"
    return "无@普通闲聊"


def _chat_label(chat_type: str, chat_id: str) -> str:
    """chat_id 一律脱敏成短哈希；同一次评估内稳定可对照。"""
    digest = hashlib.sha256(f"{chat_type}:{chat_id}".encode("utf-8")).hexdigest()[:8]
    prefix = "群" if chat_type == "group" else "私聊"
    return f"{prefix} #{digest}"


def _analyze_chat(
    chat_type: str,
    chat_id: str,
    rows: list[MessageRow],
    state: ChatState,
    usage: UsageStats,
    outcomes: dict[str, Any] | None,
    hints: Hints,
    now_ts: int,
    response_window: int,
) -> dict[str, Any]:
    inbound = [item for item in rows if not item.is_bot and not item.recalled]
    recalled = sum(1 for item in rows if not item.is_bot and item.recalled)
    bots = [item for item in rows if item.is_bot]

    batches, unattributed = _build_batches(
        inbound, bots, state, usage, hints, now_ts, response_window
    )

    mature = [batch for batch in batches if batch.mature]
    silent = [batch for batch in mature if not batch.answered]
    pending = [batch for batch in batches if not batch.mature and not batch.answered]
    ambient_batches = [batch for batch in mature if not batch.addressed]
    ambient_silent = [batch for batch in silent if not batch.addressed]
    addressed_batches = [batch for batch in mature if batch.addressed]
    addressed_silent = [batch for batch in silent if batch.addressed]

    primary = Counter[str]()
    features = Counter[str]()
    for batch in silent:
        primary[_primary_reason(batch)] += 1
        if batch.pure_media:
            features["纯表情/媒体"] += 1
        if batch.addressed:
            features["点名"] += 1
        else:
            features["无@"] += 1
        if batch.keyword:
            features["关键词/提问"] += 1
        if batch.quiet:
            features["静默时段"] += 1
        if batch.throttle:
            features["静音/降频/预算"] += 1

    bot_reply = sum(len(batch.bot_speeches) for batch in batches if batch.addressed)
    bot_ambient = len(bots) - bot_reply
    if unattributed:
        # 自发冒泡/无对应入站的发言统一算 ambient，已包含在 bot_ambient 里。
        pass

    # 连续沉默长度：只统计已成熟的批次，避免把「还没到响应窗」误算进最长连续。
    runs: list[int] = []
    run = 0
    for batch in batches:
        if not batch.mature:
            continue
        if batch.answered:
            if run:
                runs.append(run)
            run = 0
        else:
            run += 1
    if run:
        runs.append(run)

    usage_view = {
        "ambient": usage.by_purpose.get("ambient", 0),
        "reply": usage.by_purpose.get("reply", 0),
        "error": usage.by_purpose.get("error", 0),
    }

    return {
        "chat_type": chat_type,
        "chat": _chat_label(chat_type, chat_id),
        "inbound": len(inbound),
        "recalled": recalled,
        "bot_total": len(bots),
        "bot_reply": bot_reply,
        "bot_ambient": bot_ambient,
        "unattributed_bot": unattributed,
        "batches": len(batches),
        "pending_batches": len(pending),
        "silent_batches": len(silent),
        "silence_rate": _rate(len(silent), len(mature)),
        "ambient_batches": len(ambient_batches),
        "ambient_silent": len(ambient_silent),
        "ambient_silence_rate": _rate(len(ambient_silent), len(ambient_batches)),
        "addressed_batches": len(addressed_batches),
        "addressed_silent": len(addressed_silent),
        "addressed_silence_rate": _rate(
            len(addressed_silent), len(addressed_batches)
        ),
        "primary_reasons": {
            reason: primary.get(reason, 0) for reason in PRIMARY_REASONS
        },
        "features": {label: features.get(label, 0) for label in FEATURE_LABELS},
        "no_reply_streak_snapshot": state.no_reply_streak,
        "ambient_streak_snapshot": state.ambient_streak,
        "silent_run_lengths": dict(sorted(Counter(runs).items())),
        "max_silent_run": max(runs, default=0),
        "usage": usage_view,
        "reply_outcomes": outcomes,
        "first_ts": inbound[0].ts if inbound else (rows[0].ts if rows else None),
        "last_ts": inbound[-1].ts if inbound else (rows[-1].ts if rows else None),
    }


def _totals(chats: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "inbound",
        "recalled",
        "bot_total",
        "bot_reply",
        "bot_ambient",
        "unattributed_bot",
        "batches",
        "pending_batches",
        "silent_batches",
        "ambient_batches",
        "ambient_silent",
        "addressed_batches",
        "addressed_silent",
    )
    totals: dict[str, Any] = {key: sum(int(chat[key]) for chat in chats) for key in keys}
    totals["silence_rate"] = _rate(totals["silent_batches"], totals["batches"] - totals["pending_batches"])
    totals["ambient_silence_rate"] = _rate(
        totals["ambient_silent"], totals["ambient_batches"]
    )
    totals["addressed_silence_rate"] = _rate(
        totals["addressed_silent"], totals["addressed_batches"]
    )
    totals["pure_media_in_ambient_silent"] = sum(
        int(chat["primary_reasons"].get("纯表情/媒体", 0)) for chat in chats
    )
    # 纯表情主因只可能落在 ambient 批次（点名批次主因优先记「点名未回」）。
    totals["pure_media_share"] = _rate(
        totals["pure_media_in_ambient_silent"], totals["ambient_silent"]
    )
    return totals


def _summary_text(totals: dict[str, Any]) -> str:
    ambient_batches = int(totals["ambient_batches"])
    if ambient_batches == 0:
        ambient_part = "ambient 场景没有可判定批次"
    else:
        ambient_rate = totals["ambient_silence_rate"] or 0.0
        share = totals["pure_media_share"] or 0.0
        ambient_part = (
            f"ambient 场景沉默率 {ambient_rate * 100:.1f}%，"
            f"其中纯表情占 {share * 100:.1f}%"
            f"（{totals['ambient_silent']}/{ambient_batches} 批沉默）"
        )
    addressed_batches = int(totals["addressed_batches"])
    if addressed_batches:
        addressed_part = (
            f"点名场景沉默率 {(totals['addressed_silence_rate'] or 0.0) * 100:.1f}%"
            f"（{totals['addressed_silent']}/{addressed_batches} 批）"
        )
    else:
        addressed_part = "没有点名批次"
    return f"{ambient_part}；{addressed_part}；机器人共发言 {totals['bot_total']} 次。"


# --------------------------------------------------------------------- 入口


def evaluate(
    db_path: str | Path,
    *,
    days: int = 7,
    chat: str | None = None,
    now_ts: float | None = None,
    response_window: int = DEFAULT_RESPONSE_WINDOW,
    hints: Hints | None = None,
) -> dict[str, Any]:
    """只读评估；返回可直接 JSON 序列化的结构（不含明文 chat_id）。"""
    hints = hints or load_hints()
    if days < 1:
        raise ValueError("days 必须 >= 1")
    now = int(now_ts if now_ts is not None else datetime.now(tz=SHANGHAI).timestamp())
    since = now - days * 86400

    conn = connect_readonly(db_path)
    try:
        message_columns = table_columns(conn, "messages")
        if "content" not in message_columns:
            raise ValueError("messages 表缺少 content 列，无法评估")
        messages = _load_messages(conn, since, now, chat, message_columns)
        states = _load_chat_state(conn, table_columns(conn, "chat_state"))
        usage = _load_usage(
            conn,
            table_columns(conn, "usage"),
            _day_of(since),
            _day_of(now),
            chat,
        )
        outcomes = _load_outcomes(
            conn,
            table_columns(conn, "reply_outcomes"),
            since,
            now,
            chat,
        )
    finally:
        conn.close()

    chats: dict[tuple[str, str], list[MessageRow]] = {}
    for message in messages:
        chats.setdefault((message.chat_type, message.chat_id), []).append(message)

    reports: list[dict[str, Any]] = []
    for (chat_type, chat_id), rows in sorted(chats.items()):
        reports.append(
            _analyze_chat(
                chat_type,
                chat_id,
                rows,
                states.get((chat_type, chat_id), ChatState()),
                usage.get((chat_type, chat_id), UsageStats()),
                outcomes.get((chat_type, chat_id)),
                hints,
                now,
                max(1, int(response_window)),
            )
        )

    totals = _totals(reports)
    # no_reply_streak 分布：当前快照按 0 / 1 / 2 / 3+ 分桶。
    buckets: Counter[str] = Counter()
    for report in reports:
        value = int(report["no_reply_streak_snapshot"])
        buckets["3+" if value >= 3 else str(value)] += 1
    streak_snapshot = {key: buckets.get(key, 0) for key in ("0", "1", "2", "3+")}

    run_counter: Counter[int] = Counter()
    for report in reports:
        for length, count in report["silent_run_lengths"].items():
            run_counter[int(length)] += int(count)

    return {
        "generated_at": datetime.fromtimestamp(now, tz=SHANGHAI).isoformat(
            timespec="seconds"
        ),
        "window": {
            "days": days,
            "start": datetime.fromtimestamp(since, tz=SHANGHAI).isoformat(
                timespec="seconds"
            ),
            "end": datetime.fromtimestamp(now, tz=SHANGHAI).isoformat(
                timespec="seconds"
            ),
            "batch_seconds": hints.batch_seconds,
            "response_window_seconds": max(1, int(response_window)),
            "quiet_hours": [hints.quiet_start, hints.quiet_end],
            "daily_calls_limit": hints.daily_calls,
        },
        "summary": {
            "text": _summary_text(totals),
            "ambient_batches": totals["ambient_batches"],
            "ambient_silent": totals["ambient_silent"],
            "ambient_silence_rate": totals["ambient_silence_rate"],
            "pure_media_in_ambient_silent": totals["pure_media_in_ambient_silent"],
            "pure_media_share": totals["pure_media_share"],
            "addressed_batches": totals["addressed_batches"],
            "addressed_silent": totals["addressed_silent"],
            "addressed_silence_rate": totals["addressed_silence_rate"],
        },
        "totals": totals,
        "chats": reports,
        "no_reply_streak": {
            "snapshot": streak_snapshot,
            "silent_run_lengths": {
                str(length): run_counter[length] for length in sorted(run_counter)
            },
            "max_silent_run": max(run_counter, default=0),
        },
        "limitations": list(LIMITATIONS),
    }


# --------------------------------------------------------------------- 渲染


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _fmt_ts(value: int | float | str | None) -> str:
    """时间戳或 ISO 字符串都按上海时间渲染到分钟；无法解析时原样返回。"""
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value).timestamp()
        except ValueError:
            return value
    return datetime.fromtimestamp(float(value), tz=SHANGHAI).strftime(
        "%Y-%m-%d %H:%M"
    )


def render_markdown(report: dict[str, Any]) -> str:
    """把评估结构渲染成 Markdown；不包含明文 chat_id。"""
    window = report["window"]
    totals = report["totals"]
    chats = report["chats"]
    lines: list[str] = [
        "# 接话判断评估",
        "",
        f"- 窗口：{_fmt_ts(window['start'])} ~ {_fmt_ts(window['end'])}"
        f"（{window['days']} 天）",
        f"- 口径：批次 {window['batch_seconds']} 秒，响应窗 "
        f"{window['response_window_seconds']} 秒；静默时段 "
        f"{window['quiet_hours'][0]}–{window['quiet_hours'][1]}",
        f"- 数据：messages / chat_state / usage / reply_outcomes（只读）",
        "",
        "## 结论",
        "",
        f"**{report['summary']['text']}**",
        "",
        "## 会话总览",
        "",
        "| 会话 | 入站 | 撤回 | 发言 | reply | ambient | 批次 | 待观察 | 沉默 | 沉默率 | 当前 streak |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for chat in chats:
        lines.append(
            "| {chat} | {inbound} | {recalled} | {bot_total} | {bot_reply} | "
            "{bot_ambient} | {batches} | {pending_batches} | {silent_batches} | "
            "{rate} | {streak} |".format(
                chat=chat["chat"],
                inbound=chat["inbound"],
                recalled=chat["recalled"],
                bot_total=chat["bot_total"],
                bot_reply=chat["bot_reply"],
                bot_ambient=chat["bot_ambient"],
                batches=chat["batches"],
                pending_batches=chat["pending_batches"],
                silent_batches=chat["silent_batches"],
                rate=_pct(chat["silence_rate"]),
                streak=chat["no_reply_streak_snapshot"],
            )
        )
    lines.append(
        "| **合计** | {inbound} | {recalled} | {bot_total} | {bot_reply} | "
        "{bot_ambient} | {batches} | {pending_batches} | {silent_batches} | "
        "{rate} | — |".format(
            inbound=totals["inbound"],
            recalled=totals["recalled"],
            bot_total=totals["bot_total"],
            bot_reply=totals["bot_reply"],
            bot_ambient=totals["bot_ambient"],
            batches=totals["batches"],
            pending_batches=totals["pending_batches"],
            silent_batches=totals["silent_batches"],
            rate=_pct(totals["silence_rate"]),
        )
    )

    lines += [
        "",
        "## 沉默批次主因",
        "",
        "| 会话 | " + " | ".join(PRIMARY_REASONS) + " | 合计 |",
        "| --- | " + " | ".join("---:" for _ in PRIMARY_REASONS) + " | ---: |",
    ]
    for chat in chats:
        counts = [int(chat["primary_reasons"].get(reason, 0)) for reason in PRIMARY_REASONS]
        lines.append(
            f"| {chat['chat']} | " + " | ".join(str(value) for value in counts)
            + f" | {sum(counts)} |"
        )
    total_primary = [
        sum(int(chat["primary_reasons"].get(reason, 0)) for chat in chats)
        for reason in PRIMARY_REASONS
    ]
    lines.append(
        "| **合计** | " + " | ".join(str(value) for value in total_primary)
        + f" | {sum(total_primary)} |"
    )

    lines += [
        "",
        "特征命中（可重叠，分母为已判定的沉默批次）：",
        "",
        "| 会话 | " + " | ".join(FEATURE_LABELS) + " |",
        "| --- | " + " | ".join("---:" for _ in FEATURE_LABELS) + " |",
    ]
    for chat in chats:
        values = [int(chat["features"].get(label, 0)) for label in FEATURE_LABELS]
        lines.append(
            f"| {chat['chat']} | " + " | ".join(str(value) for value in values) + " |"
        )

    lines += [
        "",
        "## usage 交叉校验",
        "",
        "| 会话 | ambient 调用 | reply 调用 | error |",
        "| --- | ---: | ---: | ---: |",
    ]
    for chat in chats:
        usage = chat["usage"]
        lines.append(
            f"| {chat['chat']} | {usage['ambient']} | {usage['reply']} | {usage['error']} |"
        )

    lines += [
        "",
        "## 发言后反应（reply_outcomes）",
        "",
    ]
    outcome_rows = [chat for chat in chats if chat["reply_outcomes"]]
    if outcome_rows:
        lines += [
            "| 会话 | 已打标 | 有接话 | 引用 | 回复 | 冷场 | 接话率 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for chat in outcome_rows:
            item = chat["reply_outcomes"]
            lines.append(
                f"| {chat['chat']} | {item['total']} | {item['engaged']} | "
                f"{item['quoted']} | {item['replied']} | {item['silent']} | "
                f"{_pct(item['engaged_rate'])} |"
            )
    else:
        lines.append("暂无打标记录（OutcomeWorker 尚未产出，不等于从未冷场）。")

    streak = report["no_reply_streak"]
    lines += [
        "",
        "## no_reply_streak 分布",
        "",
        "- 当前快照（chat_state，按会话计）："
        + "、".join(
            f"{key} 次 × {value}" for key, value in streak["snapshot"].items()
        ),
        "- 连续沉默长度（按批次推定）："
        + (
            "、".join(
                f"连续 {length} 批 × {count}"
                for length, count in streak["silent_run_lengths"].items()
            )
            if streak["silent_run_lengths"]
            else "无"
        ),
        f"- 最长连续沉默：{streak['max_silent_run']} 批",
        "",
        "## 口径与局限",
        "",
    ]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- CLI


def _parse_quiet_override(value: str) -> tuple[str, str] | None:
    text = value.strip().lower()
    if text in ("off", "none", "disabled"):
        return ("00:00", "00:00")
    if "-" not in text:
        raise ValueError("--quiet-hours 格式应为 HH:MM-HH:MM 或 off")
    start, end = text.split("-", 1)
    start = _hhmm(start.strip(), "")
    end = _hhmm(end.strip(), "")
    if not start or not end:
        raise ValueError("--quiet-hours 格式应为 HH:MM-HH:MM 或 off")
    return (start, end)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="接话判断离线评估（只读 data/dafeiyu.db）",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径（只读打开）")
    parser.add_argument("--days", type=int, default=7, help="回看天数，默认 7")
    parser.add_argument("--chat", default=None, help="只看某个 chat_id（原文，不做模糊匹配）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    parser.add_argument("--out", default=None, help="把报告写入指定路径（UTF-8）")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="读取 keywords/aliases/quiet_hours/预算 的配置文件",
    )
    parser.add_argument(
        "--batch-seconds",
        type=float,
        default=None,
        help="入站批次切分秒数，默认取 triggers.batch_seconds（12）",
    )
    parser.add_argument(
        "--quiet-hours",
        default=None,
        help="覆盖静默时段，格式 HH:MM-HH:MM；off 表示不启用",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.days < 1:
        print("--days 必须 >= 1", file=sys.stderr)
        return 2

    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 2

    hints = load_hints(args.config)
    if args.batch_seconds is not None:
        if args.batch_seconds <= 0:
            print("--batch-seconds 必须 > 0", file=sys.stderr)
            return 2
        hints = replace(hints, batch_seconds=float(args.batch_seconds))
    if args.quiet_hours:
        try:
            quiet = _parse_quiet_override(args.quiet_hours)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if quiet is not None:
            hints = replace(hints, quiet_start=quiet[0], quiet_end=quiet[1])

    try:
        report = evaluate(
            db_path,
            days=args.days,
            chat=args.chat,
            hints=hints,
        )
    except (ValueError, sqlite3.DatabaseError) as exc:
        print(f"评估失败：{exc}", file=sys.stderr)
        return 2

    rendered = (
        json.dumps(report, ensure_ascii=False, indent=2)
        if args.json
        else render_markdown(report)
    )
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + ("\n" if not rendered.endswith("\n") else ""), encoding="utf-8")
        print(f"已写入 {out_path}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
