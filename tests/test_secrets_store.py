from __future__ import annotations

import os
from pathlib import Path

import pytest

from dafeiyu import secrets_store
from dafeiyu.secrets_store import mask, read_secret, secret_exists, write_secret
from dafeiyu.supervisor.control_token import harden_token_file as real_harden_token_file


@pytest.fixture(autouse=True)
def _no_real_acl(monkeypatch):
    """任何碰到真实路径的用例都不得在测试机上跑 icacls / chmod。

    需要真实 ACL 的用例（``test_real_acl_*``）自己把实现 monkeypatch 回去。
    """
    monkeypatch.setattr(secrets_store, "harden_token_file", lambda p: None)


def test_write_then_read(tmp_path, monkeypatch):
    target = tmp_path / "secrets" / "provider.key"
    hardened: list[tuple[Path, str]] = []

    def spy(p: Path) -> None:
        # 加固发生时文件必须还是空的：密钥只能写进已收紧权限的文件。
        hardened.append((p, p.read_text(encoding="utf-8")))

    monkeypatch.setattr(secrets_store, "harden_token_file", spy)
    write_secret("sk-abcdef123456", target)
    assert read_secret(target) == "sk-abcdef123456"
    assert hardened, "必须先加固临时文件再替换"
    assert all(value == "" for _, value in hardened), "加固时必须还是空文件"
    assert all(p.suffix == ".tmp" for p, _ in hardened)
    assert target not in [p for p, _ in hardened]
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


def test_stale_temp_files_are_swept_on_write(tmp_path, monkeypatch):
    """模拟进程被硬杀留下的临时文件：下一次写入必须清掉它们。"""
    target = tmp_path / "provider.key"
    stale_a = tmp_path / ".provider.key.abc123.tmp"
    stale_b = tmp_path / ".provider.key.def456.tmp"
    stale_a.write_text("sk-leftover", encoding="utf-8")
    stale_b.write_text("sk-leftover", encoding="utf-8")
    # 不相干文件不能误删。
    unrelated = tmp_path / ".other.key.abc.tmp"
    unrelated.write_text("x", encoding="utf-8")

    write_secret("sk-abcdef123456", target)

    assert not stale_a.exists()
    assert not stale_b.exists()
    assert unrelated.exists()
    assert read_secret(target) == "sk-abcdef123456"


def test_stale_temp_sweep_swallows_oserror(tmp_path, monkeypatch):
    """删不掉的残留（被占用等）不能让本次写入失败。"""
    target = tmp_path / "provider.key"
    stale = tmp_path / ".provider.key.abc123.tmp"
    stale.write_text("sk-leftover", encoding="utf-8")

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name.startswith(".provider.key."):
            raise PermissionError(13, "locked", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    write_secret("sk-abcdef123456", target)

    assert stale.exists(), "残留文件应保持原样"
    assert read_secret(target) == "sk-abcdef123456"


@pytest.mark.skipif(os.name != "nt", reason="真实文件 ACL 语义只在 Windows 上验证")
def test_real_acl_write_replace_delete_without_stub(tmp_path, monkeypatch):
    """不打桩 harden_token_file：真实 icacls 下二次写入与空值删除都必须成功。

    加固授予 ``(R,W,D)``，``os.replace`` 的源端删除与 ``unlink`` 不再依赖父目录
    的 DELETE_CHILD，所以这条用例与默认 ``data\\secrets`` 路径走的是同一条成功
    路径，不需要“父目录可删子项”这一前提才能通过。全部操作限制在 ``tmp_path`` 内。
    """
    monkeypatch.setattr(secrets_store, "harden_token_file", real_harden_token_file)
    target = tmp_path / "secrets" / "provider.key"

    write_secret("sk-abcdef123456", target)  # 首次写入
    assert read_secret(target) == "sk-abcdef123456"
    write_secret("sk-zzzzzz999999", target)  # 覆盖 = os.replace 源端删除
    assert read_secret(target) == "sk-zzzzzz999999"
    write_secret("   ", target)  # 空值 = unlink 目标文件
    assert not target.exists()
    assert list(target.parent.iterdir()) == []
