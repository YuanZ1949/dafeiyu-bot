from pathlib import Path

import pytest

from dafeiyu import cli
from dafeiyu.config import load_settings
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

CONFIG = """\
persona:
  name: 溟月
  aliases: [大肥鱼]
  max_reply_chars: 120
  max_reply_chars_technical: 300
  os_probability: 0.05
triggers:
  cooldown_seconds: 60
  sample_rate: 0.05
  quiet_hours: ["01:00", "07:00"]
  keywords: [怎么]
groups:
  whitelist: []
  blacklist: []
budget:
  daily_calls: 200
  daily_tokens: 300000
memory:
  window_size: 20
  impression_topk: 5
  extract_every: 20
logging:
  level: INFO
  dir: logs
"""


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Store:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("BOT_QQ", "10001")
    monkeypatch.setenv("DAFEIYU_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    s = Store(str(tmp_path / "test.db"))
    s.init_schema()
    return s


def add(store: Store, chat_id: int, mid: int, chat_type: ChatType = ChatType.GROUP) -> None:
    store.add_message(
        Message(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=20002,
            nickname="鱼片",
            content="在吗",
            message_id=mid,
            segments=[],
            is_bot=False,
            at_bot=False,
            reply_to_bot=False,
            timestamp=1000 + mid,
        )
    )


def test_forget_single_chat(store: Store, capsys) -> None:
    add(store, 1, 101)
    add(store, 2, 102)
    rc = cli.main(["forget", "--chat", "1"])
    assert rc == 0
    assert store.count_messages(ChatType.GROUP, 1) == 0
    assert store.count_messages(ChatType.GROUP, 2) == 1
    assert "清空" in capsys.readouterr().out


def test_forget_all(store: Store) -> None:
    add(store, 1, 101)
    add(store, 2, 102, ChatType.PRIVATE)
    assert cli.main(["forget", "--all"]) == 0
    assert store.count_messages(ChatType.GROUP, 1) == 0
    assert store.count_messages(ChatType.PRIVATE, 2) == 0


def test_stats_prints_calls_and_tokens(store: Store, capsys) -> None:
    store.record_usage(ChatType.GROUP, 1, "reply", 100, 50)
    store.record_usage(ChatType.GROUP, 1, "reply", 200, 80)
    assert cli.main(["stats", "--chat", "1"]) == 0
    out = capsys.readouterr().out
    assert "调用" in out
    assert "token" in out
    assert "2" in out
    assert "430" in out


def test_summary_prints_stored_text(store: Store, capsys) -> None:
    store.upsert_summary(ChatType.GROUP, 1, "2026-10-23", "聊了模型选型和显卡。")
    assert cli.main(["summary", "--chat", "1", "--date", "2026-10-23"]) == 0
    assert "聊了模型选型和显卡" in capsys.readouterr().out


def test_summary_missing_prints_hint(store: Store, capsys) -> None:
    assert cli.main(["summary", "--chat", "1", "--date", "2026-10-23"]) == 0
    assert "没有" in capsys.readouterr().out


def test_unknown_subcommand_returns_2(store: Store) -> None:
    assert cli.main(["nope"]) == 2


def test_config_error_returns_1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    assert cli.main(["stats", "--chat", "1"]) == 1
