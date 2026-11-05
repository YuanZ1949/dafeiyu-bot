"""``scripts/eval_reply_judgment.py`` 的离线契约测试（不碰真实库）。

脚本是只读评估工具，最容易坏在两处：SQL 列名对齐和「沉默批次」的判定
口径。这里用临时 sqlite 造最小表结构，验证主因归类、待观察批次、脱敏
渲染与 CLI 的出错路径。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "eval_reply_judgment.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_reply_judgment", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eval_mod = _load_module()

SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    content TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    at_bot INTEGER NOT NULL DEFAULT 0,
    reply_to_bot INTEGER NOT NULL DEFAULT 0,
    recalled_at REAL
);
CREATE TABLE chat_state (
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    no_reply_streak INTEGER NOT NULL DEFAULT 0,
    ambient_streak INTEGER NOT NULL DEFAULT 0,
    muted_until REAL,
    downshift_until REAL
);
CREATE TABLE usage (
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    date TEXT NOT NULL,
    purpose TEXT NOT NULL
);
CREATE TABLE reply_outcomes (
    chat_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    spoken_at INTEGER NOT NULL,
    engaged INTEGER NOT NULL DEFAULT 0,
    quoted INTEGER NOT NULL DEFAULT 0,
    replied INTEGER NOT NULL DEFAULT 0,
    silence_after INTEGER NOT NULL DEFAULT 0
);
"""


def make_db(tmp_path: Path) -> Path:
    path = tmp_path / "eval.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return path


def insert_message(
    path: Path,
    *,
    row_id: int,
    chat_id: str,
    ts: int,
    content: str,
    chat_type: str = "group",
    is_bot: int = 0,
    at_bot: int = 0,
    reply_to_bot: int = 0,
    recalled_at: float | None = None,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO messages (id, chat_type, chat_id, timestamp, content, "
            "is_bot, at_bot, reply_to_bot, recalled_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                chat_type,
                chat_id,
                ts,
                content,
                is_bot,
                at_bot,
                reply_to_bot,
                recalled_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def insert_rows(path: Path, sql: str, params: tuple[object, ...]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def quiet_off_hints() -> object:
    return eval_mod.Hints(
        keywords=("怎么", "为什么"),
        quiet_start="00:00",
        quiet_end="00:00",
        batch_seconds=12.0,
    )


def test_evaluate_classifies_addressed_and_pure_media(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    now = int(time.time())

    insert_message(
        db, row_id=1, chat_id="qq:1", ts=now - 600, content="在吗", at_bot=1
    )
    insert_message(db, row_id=2, chat_id="qq:2", ts=now - 500, content="[表情]")
    insert_rows(
        db,
        "INSERT INTO chat_state (chat_type, chat_id, no_reply_streak, ambient_streak) "
        "VALUES (?,?,?,?)",
        ("group", "qq:1", 2, 1),
    )
    insert_rows(
        db,
        "INSERT INTO reply_outcomes (chat_type, chat_id, spoken_at, engaged, quoted, "
        "replied, silence_after) VALUES (?,?,?,?,?,?,?)",
        ("group", "qq:1", now - 700, 1, 0, 0, 0),
    )

    report = eval_mod.evaluate(
        db, days=1, now_ts=now, hints=quiet_off_hints()
    )

    first, second = report["chats"]
    assert first["silent_batches"] == 1
    assert first["primary_reasons"]["点名未回"] == 1
    assert first["no_reply_streak_snapshot"] == 2
    assert first["reply_outcomes"]["engaged_rate"] == 1.0
    assert second["silent_batches"] == 1
    assert second["primary_reasons"]["纯表情/媒体"] == 1
    assert report["totals"]["silent_batches"] == 2


def test_recent_batch_is_pending_not_silent(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    now = int(time.time())

    insert_message(db, row_id=1, chat_id="qq:9", ts=now - 10, content="在吗")

    report = eval_mod.evaluate(
        db, days=1, now_ts=now, hints=quiet_off_hints()
    )

    chat = report["chats"][0]
    assert chat["batches"] == 1
    assert chat["pending_batches"] == 1
    assert chat["silent_batches"] == 0


def test_bot_reply_marks_batch_answered(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    now = int(time.time())

    insert_message(db, row_id=1, chat_id="qq:9", ts=now - 600, content="在吗")
    insert_message(
        db,
        row_id=2,
        chat_id="qq:9",
        ts=now - 590,
        content="在的",
        is_bot=1,
    )

    report = eval_mod.evaluate(
        db, days=1, now_ts=now, hints=quiet_off_hints()
    )

    chat = report["chats"][0]
    assert chat["silent_batches"] == 0
    assert chat["bot_total"] == 1
    assert report["totals"]["silent_batches"] == 0


def test_recalled_message_excluded_from_inbound(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    now = int(time.time())

    insert_message(
        db,
        row_id=1,
        chat_id="qq:9",
        ts=now - 600,
        content="撤回的消息",
        recalled_at=float(now - 599),
    )

    report = eval_mod.evaluate(
        db, days=1, now_ts=now, hints=quiet_off_hints()
    )

    chat = report["chats"][0]
    assert chat["inbound"] == 0
    assert chat["recalled"] == 1


def test_render_markdown_redacts_chat_id(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    now = int(time.time())
    insert_message(db, row_id=1, chat_id="qq:123456", ts=now - 600, content="[表情]")

    report = eval_mod.evaluate(
        db, days=1, now_ts=now, hints=quiet_off_hints()
    )
    rendered = eval_mod.render_markdown(report)

    assert "qq:123456" not in rendered
    assert "接话判断评估" in rendered
    assert "纯表情/媒体" in rendered


def test_evaluate_rejects_missing_content_column(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="content"):
        eval_mod.evaluate(db, days=1, hints=quiet_off_hints())


def test_cli_json_output_and_error_paths(tmp_path: Path, capsys) -> None:
    db = make_db(tmp_path)
    now = int(time.time())
    insert_message(db, row_id=1, chat_id="qq:123456", ts=now - 600, content="在吗")
    missing = tmp_path / "nope.db"

    code = eval_mod.main(
        [
            "--db",
            str(db),
            "--days",
            "1",
            "--json",
            "--config",
            str(tmp_path / "missing.yaml"),
            "--quiet-hours",
            "off",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["window"]["days"] == 1
    assert "qq:123456" not in json.dumps(out, ensure_ascii=False)

    assert eval_mod.main(["--db", str(missing)]) == 2
    assert eval_mod.main(["--db", str(db), "--days", "0"]) == 2
    assert (
        eval_mod.main(["--db", str(db), "--days", "1", "--batch-seconds", "0"]) == 2
    )
