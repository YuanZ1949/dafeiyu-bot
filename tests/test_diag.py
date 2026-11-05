"""诊断包：配置/日志脱敏，以及拒绝清单（数据库、密钥、.env）。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from dafeiyu import diag

CONFIG = """\
persona:
  name: 溟月
gateway:
  bridge_token: "abc def"
  password: hunter2
  model: keep-me
api_key: sk-live-abcdefghijkl
"""


def _make_tree(root: Path) -> Path:
    (root / "logs" / "supervisor").mkdir(parents=True)
    (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (root / "logs" / "dafeiyu.log").write_text(
        "普通日志\n"
        "Authorization: Bearer abc.def.ghi\n"
        "api_key=sk-other-12345678\n"
        "孤立密钥 sk-bare-abcdefghijkl\n",
        encoding="utf-8",
    )
    (root / "logs" / "supervisor" / "bot.log").write_text(
        "bot ready\n", encoding="utf-8"
    )
    (root / "data" / "secrets").mkdir(parents=True)
    (root / "data" / "secrets" / "provider.key").write_text(
        "sk-secret-value", encoding="utf-8"
    )
    (root / "data" / "dafeiyu.db").write_text("sqlite", encoding="utf-8")
    (root / "data" / "supervisor.token").write_text("token", encoding="utf-8")
    (root / ".env").write_text(
        "DEEPSEEK_API_KEY=sk-env-secret\nBOT_QQ=10001\n", encoding="utf-8"
    )
    return root


def test_create_diagnostics_redacts_and_excludes_secrets(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    out = tmp_path / "diag.zip"

    result = diag.create_diagnostics(
        root=root, out=out, log_lines=50, include_doctor=False
    )

    assert out.is_file()
    assert "config.redacted.yaml" in result.entries
    assert "env.keys.txt" in result.entries
    assert all(not name.startswith("data/") for name in result.entries)

    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        assert not any(name.startswith("data/") for name in names)
        assert ".env" not in names
        assert "config.yaml" not in names

        config = archive.read("config.redacted.yaml").decode("utf-8")
        assert "sk-live" not in config
        assert "hunter2" not in config
        assert "abc def" not in config
        assert "keep-me" in config
        assert "api_key: ***" in config

        log = archive.read("logs/dafeiyu.log").decode("utf-8")
        assert "sk-other" not in log
        assert "sk-bare" not in log
        # 公共 redact() 对 Authorization 整行输出规范头名 + ***（见 supervisor/redact.py）。
        assert "Authorization: ***" in log
        assert "Bearer abc" not in log

        env_keys = archive.read("env.keys.txt").decode("utf-8")
        assert "DEEPSEEK_API_KEY" in env_keys
        assert "BOT_QQ" in env_keys
        assert "sk-env-secret" not in env_keys

        for name in names:
            text = archive.read(name).decode("utf-8", errors="replace")
            assert "sk-live" not in text
            assert "sk-env-secret" not in text
            assert "Bearer abc" not in text


def test_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    root = _make_tree(tmp_path)
    out = tmp_path / "out" / "diag.zip"

    rc = diag.main(["--root", str(root), "--out", str(out), "--dry-run"])

    assert rc == 0
    assert not out.exists()
    assert not (root / "data" / "diagnostics").exists()
    printed = capsys.readouterr().out
    assert "[dry-run]" in printed
    assert str(out) in printed
    assert "8788" in printed


def test_tail_lines_keeps_only_requested_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.log"
    path.write_text("1\n2\n3\n4\n5\n", encoding="utf-8")

    assert diag._tail_lines(path, 2) == "4\n5"


def test_deny_list_blocks_data_and_env() -> None:
    assert diag._is_denied("data/dafeiyu.db")
    assert diag._is_denied("data/secrets/provider.key")
    assert diag._is_denied("data/supervisor.token")
    assert diag._is_denied(".env")
    assert diag._is_denied("config.yaml")
    assert not diag._is_denied("config.redacted.yaml")
    assert not diag._is_denied("logs/dafeiyu.log")
