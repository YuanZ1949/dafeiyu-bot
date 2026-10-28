from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from dafeiyu.supervisor import control_token
from dafeiyu.supervisor.control_token import ControlToken, harden_token_file


def _write(path: Path, value: str = "old-token") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def test_harden_windows_uses_icacls(monkeypatch, tmp_path) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(control_token.os, "name", "nt")
    monkeypatch.setattr(control_token.subprocess, "run", fake_run)
    monkeypatch.setattr(control_token.getpass, "getuser", lambda: "alice")
    path = _write(tmp_path / "supervisor.token")

    harden_token_file(path)

    assert len(calls) == 1
    args = calls[0][0]
    assert args[0] == "icacls"
    assert str(path) in args
    assert "/inheritance:r" in args
    assert "/grant:r" in args
    assert "alice:(R,W,D)" in args
    # DELETE 不能省：加固后的文件要靠它完成替换/删除，父目录未必有 DELETE_CHILD。
    grant = args[args.index("/grant:r") + 1]
    assert "D" in grant


def test_harden_posix_chmods_600(monkeypatch, tmp_path) -> None:
    seen: list[tuple[Path, int]] = []
    monkeypatch.setattr(control_token.os, "name", "posix")
    monkeypatch.setattr(
        control_token.os, "chmod", lambda path, mode: seen.append((path, mode))
    )
    path = _write(tmp_path / "supervisor.token")

    harden_token_file(path)

    assert seen == [(path, 0o600)]


def test_harden_failure_only_logs_warning(monkeypatch, tmp_path, caplog) -> None:
    monkeypatch.setattr(control_token.os, "name", "nt")

    def boom(*_args, **_kwargs):
        raise OSError("icacls 缺失")

    monkeypatch.setattr(control_token.subprocess, "run", boom)
    path = _write(tmp_path / "supervisor.token")

    with caplog.at_level(logging.WARNING, logger="dafeiyu.supervisor.control_token"):
        harden_token_file(path)  # 不能抛异常

    assert any("ACL" in record.message for record in caplog.records)


def test_load_or_create_hardens_existing_file_and_keeps_content(
    monkeypatch, tmp_path
) -> None:
    path = _write(tmp_path / "supervisor.token", "old-token")
    hardened: list[Path] = []
    monkeypatch.setattr(
        control_token, "harden_token_file", lambda item: hardened.append(item)
    )

    holder = ControlToken(path)
    value = holder.load_or_create()

    assert value == "old-token"
    assert holder.value == "old-token"
    assert path.read_text(encoding="utf-8") == "old-token"
    assert hardened == [path]


def test_load_or_create_generates_non_empty_token_and_is_stable(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(control_token, "harden_token_file", lambda _path: None)
    path = tmp_path / "nested" / "supervisor.token"

    holder = ControlToken(path)
    value = holder.load_or_create()

    assert value
    assert holder.value == value
    assert path.read_text(encoding="utf-8") == value
    assert ControlToken(path).load_or_create() == value


def test_rotate_write_failure_keeps_old_value(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(control_token, "harden_token_file", lambda _path: None)
    path = _write(tmp_path / "supervisor.token")
    holder = ControlToken(path)
    old = holder.load_or_create()

    def failing_write_text(self, *_args, **_kwargs):
        raise OSError("磁盘已满")

    monkeypatch.setattr(Path, "write_text", failing_write_text)

    with pytest.raises(OSError):
        holder.rotate()

    assert holder.value == old
    assert path.read_text(encoding="utf-8") == old


def test_rotate_replace_failure_keeps_old_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(control_token, "harden_token_file", lambda _path: None)
    path = _write(tmp_path / "supervisor.token")
    holder = ControlToken(path)
    old = holder.load_or_create()

    def failing_replace(_src, _dst):
        raise OSError("替换失败")

    monkeypatch.setattr(control_token.os, "replace", failing_replace)

    with pytest.raises(OSError):
        holder.rotate()

    assert holder.value == old
    assert path.read_text(encoding="utf-8") == old


def test_set_restores_old_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(control_token, "harden_token_file", lambda _path: None)
    path = tmp_path / "supervisor.token"
    holder = ControlToken(path)
    old = holder.load_or_create()
    new = holder.rotate()

    assert new != old
    assert holder.value == new
    assert path.read_text(encoding="utf-8") == new

    holder.set(old)

    assert holder.value == old
    assert path.read_text(encoding="utf-8") == old


def test_rotate_replaces_token_atomically(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(control_token, "harden_token_file", lambda _path: None)
    path = tmp_path / "supervisor.token"
    holder = ControlToken(path)
    old = holder.load_or_create()

    new = holder.rotate()

    assert new != old
    assert holder.value == new
    assert path.read_text(encoding="utf-8") == new
    assert not list(tmp_path.glob("*.tmp"))


def test_harden_is_called_after_rotation(monkeypatch, tmp_path) -> None:
    hardened: list[Path] = []
    monkeypatch.setattr(
        control_token, "harden_token_file", lambda item: hardened.append(item)
    )
    path = tmp_path / "supervisor.token"
    holder = ControlToken(path)
    holder.load_or_create()

    holder.rotate()

    assert hardened == [path, path]
