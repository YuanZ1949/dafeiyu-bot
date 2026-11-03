import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dafeiyu import cli, env_probe
from dafeiyu.config import WeChatConfig
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


# ---- doctor：微信前置检查与 --json ---------------------------------------


def _doctor_config(**overrides) -> WeChatConfig:
    values = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 8790,
        "bridge_token": "",
        "account_nickname": "小号",
    }
    values.update(overrides)
    return WeChatConfig(**values)


def _patch_doctor_probes(monkeypatch) -> None:
    monkeypatch.setattr(env_probe, "_wechat_pids", lambda: [1234])
    monkeypatch.setattr(env_probe, "_wechat_about", lambda: {"version": "4.1.6.14"})
    monkeypatch.setattr(env_probe, "_wechat_language", lambda: "zh-CN")
    monkeypatch.setattr(env_probe, "_wechat_desktop", lambda: "interactive")
    monkeypatch.setattr(
        env_probe, "_wechat_integrity_state", lambda running: "matching"
    )
    monkeypatch.setattr(env_probe, "_current_session_id", lambda: 1)
    monkeypatch.setattr(env_probe, "_pyweixin_available", lambda: True)
    monkeypatch.setattr(
        env_probe, "_port_free", lambda port, host="127.0.0.1": True
    )


def _patch_doctor_config(monkeypatch, cfg) -> None:
    monkeypatch.setattr(cli, "preflight", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        cli, "load_settings", lambda **kwargs: SimpleNamespace(wechat=cfg)
    )


def test_doctor_json_has_eight_wechat_checks(monkeypatch, capsys) -> None:
    _patch_doctor_config(monkeypatch, _doctor_config())
    _patch_doctor_probes(monkeypatch)

    rc = cli.main(["doctor", "--json"])

    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    payload = json.loads(out)
    assert payload["ok"] is True
    assert [check["id"] for check in payload["checks"]] == list(
        env_probe.WECHAT_CHECK_IDS
    )
    assert all(check["status"] == "PASS" for check in payload["checks"])
    assert rc == 0


def test_doctor_json_missing_nickname_fails(monkeypatch, capsys) -> None:
    _patch_doctor_config(
        monkeypatch, _doctor_config(account_nickname="")
    )
    _patch_doctor_probes(monkeypatch)

    rc = cli.main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert rc == 1
    details = " ".join(check["detail"] for check in payload["checks"])
    assert "account_nickname" in details


def test_doctor_json_disabled_all_skip_ok_true(monkeypatch, capsys) -> None:
    _patch_doctor_config(monkeypatch, _doctor_config(enabled=False))
    # 禁用时不应触发任何真实探测。
    monkeypatch.setattr(
        env_probe, "_wechat_pids", lambda: pytest.fail("不应探测进程")
    )

    rc = cli.main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert rc == 0
    assert [check["status"] for check in payload["checks"]] == ["SKIP"] * 8


def test_doctor_text_hides_bridge_token(monkeypatch, capsys) -> None:
    _patch_doctor_config(
        monkeypatch, _doctor_config(bridge_token="super-secret-token")
    )
    _patch_doctor_probes(monkeypatch)

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "super-secret-token" not in out
    assert "已配置（不显示）" in out
    assert "wechat.process_running" in out
    assert "微信通道生效配置" in out


def test_doctor_json_hides_bridge_token(monkeypatch, capsys) -> None:
    _patch_doctor_config(
        monkeypatch, _doctor_config(bridge_token="super-secret-token")
    )
    _patch_doctor_probes(monkeypatch)

    cli.main(["doctor", "--json"])

    out = capsys.readouterr().out
    assert "super-secret-token" not in out
    payload = json.loads(out)
    assert "bridge_token" not in json.dumps(payload, ensure_ascii=False)
