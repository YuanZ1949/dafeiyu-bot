from __future__ import annotations

from pathlib import Path

import pytest

from dafeiyu import secrets_store
from dafeiyu.secrets_store import mask, read_secret, secret_exists, write_secret


@pytest.fixture(autouse=True)
def _no_real_acl(monkeypatch):
    """任何碰到真实路径的用例都不得在测试机上跑 icacls / chmod。"""
    monkeypatch.setattr(secrets_store, "harden_token_file", lambda p: None)


def test_write_then_read(tmp_path, monkeypatch):
    target = tmp_path / "secrets" / "provider.key"
    calls = []
    monkeypatch.setattr(secrets_store, "harden_token_file", lambda p: calls.append(p))
    write_secret("sk-abcdef123456", target)
    assert read_secret(target) == "sk-abcdef123456"
    assert calls, "必须先加固临时文件再替换"
    assert "sk-abcdef123456" not in "".join(x.name for x in target.parent.iterdir())


def test_empty_value_removes_file(tmp_path):
    target = tmp_path / "provider.key"
    write_secret("sk-x", target)
    write_secret("   ", target)
    assert not target.exists()


def test_mask_short_value():
    assert mask("short") == "***"


def test_write_creates_parent_directory(tmp_path):
    target = tmp_path / "a" / "b" / "provider.key"
    write_secret("sk-abcdef123456", target)
    assert target.is_file()
    assert read_secret(target) == "sk-abcdef123456"


def test_missing_file_semantics(tmp_path):
    """缺失即“未配置”：读到 ""，且用空值再写一次不能失败也不能造出空文件。"""
    missing = tmp_path / "provider.key"
    assert read_secret(missing) == ""
    assert secret_exists(missing) is False
    assert write_secret("", missing) == missing
    assert not missing.exists()


def test_write_twice_overwrites_and_cleans_up_temp(tmp_path, monkeypatch):
    target = tmp_path / "provider.key"
    calls: list[Path] = []
    monkeypatch.setattr(secrets_store, "harden_token_file", lambda p: calls.append(p))

    write_secret("sk-abcdef123456", target)
    write_secret("sk-zzzzzz999999", target)

    assert read_secret(target) == "sk-zzzzzz999999"
    assert list(tmp_path.rglob("*.tmp")) == []
    assert list(tmp_path.iterdir()) == [target]
    # 加固的是临时文件，最终路径在替换前从未出现在磁盘上。
    assert calls and all(p.suffix == ".tmp" for p in calls)
    assert target not in calls


def test_mask_boundary_lengths():
    assert mask("12345678") == "***"
    assert mask("123456789") == "1234***6789"
