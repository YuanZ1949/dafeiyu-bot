"""``scripts/eval_sample.py`` 的回归：时间窗、每会话上限、bot 过滤、只读边界。

全部用 tmp 主库，绝不碰仓库里的 ``data/`` 数据库。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dafeiyu.eval.loader import load_cases
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "eval_sample.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_sample_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eval_sample = _load_module()

BOT_TEXT = "这是机器人自己说的话"
OLD_TEXT = "超出窗口的旧消息"


def sample_settings(db_path: Path) -> SimpleNamespace:
    """与 ``tests/test_eval.py::record_settings`` 同形的配置替身。"""
    return SimpleNamespace(
        db_path=str(db_path),
        persona=SimpleNamespace(max_reply_chars=120),
        behavior=SimpleNamespace(banned_phrases=("违禁词",)),
        triggers=SimpleNamespace(
            batch_seconds=12.0,
            addressed_debounce_seconds=8.0,
            addressed_max_wait_seconds=30.0,
            ambient_min_interval_seconds=15.0,
        ),
    )


def seed_db(db_path: Path, now: int) -> None:
    store = Store(str(db_path))
    store.init_schema()
    day = 86400
    samples = [
        # group/qq:1：人类 3 条（其中 1 条超出时间窗）+ bot 1 条
        (ChatType.GROUP, "qq:1", "qq:1001", "阿甲", "最近人类一", False, now - 300),
        (ChatType.GROUP, "qq:1", "qq:1002", "阿乙", "最近人类二", False, now - 290),
        (ChatType.GROUP, "qq:1", "bot", "大肥鱼", BOT_TEXT, True, now - 280),
        (ChatType.GROUP, "qq:1", "qq:1001", "阿甲", "最近人类三", False, now - 270),
        (ChatType.GROUP, "qq:1", "qq:1001", "阿甲", OLD_TEXT, False, now - 8 * day),
        # private/qq:2：人类 2 条 + bot 1 条
        (ChatType.PRIVATE, "qq:2", "qq:2001", "阿丙", "私聊人类一", False, now - 200),
        (ChatType.PRIVATE, "qq:2", "bot", "大肥鱼", BOT_TEXT, True, now - 190),
        (ChatType.PRIVATE, "qq:2", "qq:2001", "阿丙", "私聊人类二", False, now - 180),
    ]
    for index, (chat_type, chat_id, user_id, nick, content, is_bot, ts) in enumerate(
        samples, start=1
    ):
        store.add_message(
            Message(
                chat_type=chat_type,
                chat_id=chat_id,
                user_id=user_id,
                nickname=nick,
                content=content,
                message_id=f"m{index}",
                is_bot=is_bot,
                timestamp=ts,
            )
        )
    store.close()


def all_contents(cases) -> list[str]:
    return [message.content for case in cases for message in case.messages]


def test_sample_writes_replayable_cases(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "prod.db"
    now = int(time.time())
    seed_db(db, now)
    monkeypatch.setattr(eval_sample, "load_settings", lambda: sample_settings(db))

    out = tmp_path / "samples" / "today.yaml"
    code = eval_sample.main(
        [
            "--db",
            str(db),
            "--days",
            "7",
            "--per-chat",
            "2",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    assert out.is_file()
    cases = load_cases(out)
    assert len(cases) > 0
    assert {case.chat_id for case in cases} == {"qq:1", "qq:2"}
    assert all(case.expect.max_chars == 120 for case in cases)
    assert all(case.expect.must_not_contain == ("违禁词",) for case in cases)

    contents = all_contents(cases)
    # bot 自己的发言绝不能变成回放里的 user 消息
    assert BOT_TEXT not in contents
    # 时间窗外的人类消息也不采
    assert OLD_TEXT not in contents
    # 每会话最多 2 条人类消息：更早的第 3 条被截掉
    group_contents = [
        message.content for case in cases if case.chat_id == "qq:1"
        for message in case.messages
    ]
    assert len(group_contents) <= 2
    assert "最近人类一" not in group_contents
    assert "最近人类三" in group_contents


def test_sample_chat_type_filter(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "prod.db"
    now = int(time.time())
    seed_db(db, now)
    monkeypatch.setattr(eval_sample, "load_settings", lambda: sample_settings(db))

    out = tmp_path / "private.yaml"
    code = eval_sample.main(
        [
            "--db",
            str(db),
            "--chat-type",
            "private",
            "--days",
            "7",
            "--per-chat",
            "5",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    cases = load_cases(out)
    assert cases
    assert {case.chat_type for case in cases} == {"private"}


def test_missing_db_exits_zero_and_writes_nothing(
    tmp_path: Path, capsys
) -> None:
    out = tmp_path / "samples.yaml"
    code = eval_sample.main(
        [
            "--db",
            str(tmp_path / "does-not-exist.db"),
            "--days",
            "7",
            "--per-chat",
            "2",
            "--out",
            str(out),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert not out.exists()
    assert "数据库" in captured.out + captured.err


def test_empty_db_exits_zero_and_writes_nothing(tmp_path: Path, capsys) -> None:
    db = tmp_path / "empty.db"
    store = Store(str(db))
    store.init_schema()
    store.close()

    out = tmp_path / "samples.yaml"
    code = eval_sample.main(
        [
            "--db",
            str(db),
            "--days",
            "7",
            "--per-chat",
            "2",
            "--out",
            str(out),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert not out.exists()
    assert "没有可采样" in captured.out + captured.err


def test_db_without_messages_table_exits_zero(tmp_path: Path, capsys) -> None:
    db = tmp_path / "other.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()

    out = tmp_path / "samples.yaml"
    code = eval_sample.main(
        [
            "--db",
            str(db),
            "--days",
            "7",
            "--per-chat",
            "2",
            "--out",
            str(out),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert not out.exists()
    assert "messages" in captured.out + captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["--days", "0"],
        ["--per-chat", "0"],
        ["--days", "-1"],
        ["--per-chat", "-3"],
    ],
)
def test_invalid_window_arguments_exit_nonzero(
    tmp_path: Path, argv: list[str]
) -> None:
    out = tmp_path / "samples.yaml"
    with pytest.raises(SystemExit) as excinfo:
        eval_sample.main([*argv, "--out", str(out)])

    assert excinfo.value.code != 0
    assert not out.exists()


def test_default_out_is_dated_under_samples_dir() -> None:
    out = eval_sample._default_out()
    assert out.parent == eval_sample.DEFAULT_SAMPLE_DIR
    assert out.suffix == ".yaml"
    assert len(out.stem) == len("YYYY-MM-DD")
