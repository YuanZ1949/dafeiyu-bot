"""从生产库只读录制可回放的评测 case。

原则：

- **只读**：缺库/缺表/没消息都返回空，绝不创建或迁移数据库；
- 批次边界复用线上调度器（``delivery.window`` 的 ``ChatWindow`` / ``due_at``），
  不在这里另造一套阈值；
- 只写确定性字段：``min_chars`` / ``max_chars`` / ``must_not_contain``，
  绝不写 ``speak``，也不调用模型。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from dafeiyu.delivery.window import ChatWindow, due_at, on_flushed
from dafeiyu.delivery.window import note as note_window
from dafeiyu.models import ChatType, Message
from dafeiyu.moment import answers_bot_question

from .models import EvalCase, EvalContext, EvalExpect, EvalMessage

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: 只读查询的列；顺序固定，避免 ``SELECT *`` 依赖 schema 演进。
_COLUMNS = (
    "id, message_id, user_id, nickname, content, is_bot, timestamp, at_bot, reply_to_bot"
)


def slug(value: str) -> str:
    """把文件名变成 loader 接受的 ``^[a-z0-9-]+$`` id 前缀。"""
    cleaned = _SLUG_RE.sub("-", value.strip().casefold()).strip("-")
    return cleaned or "recorded"


def _open_readonly(db_path: str | Path) -> sqlite3.Connection | None:
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error):
        return None


def read_history(
    db_path: str | Path, chat_type: str, chat_id: str, limit: int
) -> tuple[list[Message], str]:
    """读取最近 ``limit`` 条消息（旧 -> 新）与该会话最新摘要。"""
    con = _open_readonly(db_path)
    if con is None:
        return [], ""
    try:
        try:
            rows = con.execute(
                f"SELECT {_COLUMNS} FROM messages "
                "WHERE chat_type = ? AND chat_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (chat_type, chat_id, max(1, int(limit))),
            ).fetchall()
        except sqlite3.Error:
            return [], ""
        try:
            summary_row = con.execute(
                "SELECT content FROM session_summaries "
                "WHERE chat_type = ? AND chat_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (chat_type, chat_id),
            ).fetchone()
        except sqlite3.Error:
            summary_row = None
    finally:
        con.close()

    summary = str(summary_row[0]) if summary_row is not None else ""
    messages = [
        _row_to_message(row, chat_type, chat_id) for row in reversed(rows)
    ]
    return messages, summary


def _row_to_message(row: Sequence[Any], chat_type: str, chat_id: str) -> Message:
    (
        db_id,
        message_id,
        user_id,
        nickname,
        content,
        is_bot,
        timestamp,
        at_bot,
        reply_to_bot,
    ) = row
    return Message(
        chat_type=ChatType(chat_type),
        chat_id=chat_id,
        user_id=str(user_id or ""),
        nickname=str(nickname or ""),
        content=str(content or ""),
        message_id=str(message_id or db_id),
        is_bot=bool(is_bot),
        at_bot=bool(at_bot),
        reply_to_bot=bool(reply_to_bot),
        timestamp=int(timestamp or 0),
    )


def split_batches(messages: Sequence[Message], triggers: Any) -> list[list[Message]]:
    """按线上投递窗口切批：与 ``BatchScheduler`` 使用同一套 ``ChatWindow``。"""
    params = {
        "batch_seconds": triggers.batch_seconds,
        "debounce": triggers.addressed_debounce_seconds,
        "max_wait": triggers.addressed_max_wait_seconds,
        "ambient_gap": triggers.ambient_min_interval_seconds,
    }
    window = ChatWindow()
    batches: list[list[Message]] = []
    current: list[Message] = []
    previous: Message | None = None

    for message in messages:
        now = float(message.timestamp or 0)
        due = due_at(window, **params)
        if due is not None and now >= due and current:
            batches.append(current)
            current = []
            on_flushed(
                window, now=due, addressed=window.addressed_first_at is not None
            )

        addressed = bool(message.at_bot or message.reply_to_bot)
        if not addressed and previous is not None:
            # 隐式点名：紧接在 bot 的提问后回答。与 service 使用同一个 helper。
            addressed = answers_bot_question(message, previous)
        note_window(window, now=now, addressed=addressed)
        current.append(message)
        previous = message

    if current:
        batches.append(current)
    return batches


def build_cases(
    messages: Sequence[Message],
    summary: str,
    *,
    chat_type: str,
    chat_id: str,
    stem: str,
    max_chars: int,
    banned_phrases: Sequence[str],
    triggers: Any,
) -> list[EvalCase]:
    """把消息批次转成 case；纯 bot 批次跳过（EvalMessage 没有 is_bot 字段）。"""
    prefix = slug(stem)
    expect = EvalExpect(
        min_chars=1,
        max_chars=max(1, int(max_chars)),
        must_not_contain=tuple(banned_phrases),
    )
    context = EvalContext(summary=summary)
    cases: list[EvalCase] = []
    for batch in split_batches(messages, triggers):
        humans = [message for message in batch if not message.is_bot]
        if not humans:
            continue
        cases.append(
            EvalCase(
                id=f"{prefix}-{len(cases)}",
                chat_type=chat_type,
                chat_id=chat_id,
                context=context,
                messages=tuple(
                    EvalMessage(
                        user_id=message.user_id,
                        nickname=message.nickname,
                        content=message.content,
                        at_bot=message.at_bot,
                        reply_to_bot=message.reply_to_bot,
                        timestamp=message.timestamp,
                    )
                    for message in humans
                ),
                expect=expect,
            )
        )
    return cases


def cases_to_payload(cases: Sequence[EvalCase]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for case in cases:
        payload.append(
            {
                "id": case.id,
                "chat_type": case.chat_type,
                "chat_id": case.chat_id,
                "context": {"summary": case.context.summary},
                "messages": [
                    {
                        "user_id": message.user_id,
                        "nickname": message.nickname,
                        "content": message.content,
                        "at_bot": message.at_bot,
                        "reply_to_bot": message.reply_to_bot,
                        "timestamp": message.timestamp,
                    }
                    for message in case.messages
                ],
                "expect": {
                    "min_chars": case.expect.min_chars,
                    "max_chars": case.expect.max_chars,
                    "must_not_contain": list(case.expect.must_not_contain),
                },
            }
        )
    return payload


def write_cases(path: str | Path, cases: Sequence[EvalCase]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(
            cases_to_payload(cases),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )


def record_cases(
    *,
    db_path: str | Path,
    chat_type: str,
    chat_id: str,
    limit: int,
    out: str | Path,
    max_chars: int,
    banned_phrases: Sequence[str],
    triggers: Any,
) -> list[EvalCase]:
    """读历史 -> 切批 -> 写 YAML；没有消息时不写文件，返回空列表。"""
    messages, summary = read_history(db_path, chat_type, chat_id, limit)
    target = Path(out)
    cases = build_cases(
        messages,
        summary,
        chat_type=chat_type,
        chat_id=chat_id,
        stem=target.stem,
        max_chars=max_chars,
        banned_phrases=banned_phrases,
        triggers=triggers,
    )
    if cases:
        write_cases(target, cases)
    return cases
