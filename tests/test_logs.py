from __future__ import annotations

from pathlib import Path

from dafeiyu.supervisor.logs import LogHub


def test_existing_log_is_rotated_on_startup(tmp_path: Path) -> None:
    """每次启动轮转：本次运行的日志 == 当前文件，历史留在 .1。"""
    (tmp_path / "supervisor.log").write_text("old 401 line\n", encoding="utf-8")

    LogHub(tmp_path)

    assert (tmp_path / "supervisor.log.1").read_text(encoding="utf-8") == "old 401 line\n"
    assert not (tmp_path / "supervisor.log").exists()


def test_fresh_dir_does_not_create_backup(tmp_path: Path) -> None:
    LogHub(tmp_path)

    assert not (tmp_path / "supervisor.log.1").exists()


def test_empty_log_is_not_rotated(tmp_path: Path) -> None:
    """空文件不必占一个备份名额。"""
    (tmp_path / "supervisor.log").write_text("", encoding="utf-8")

    LogHub(tmp_path)

    assert (tmp_path / "supervisor.log").exists()
    assert not (tmp_path / "supervisor.log.1").exists()


def test_writes_after_rotation_land_in_current_file(tmp_path: Path) -> None:
    (tmp_path / "supervisor.log").write_text("old\n", encoding="utf-8")

    hub = LogHub(tmp_path)
    hub.write("supervisor", "new line")

    assert "new line" in (tmp_path / "supervisor.log").read_text(encoding="utf-8")
    assert "new line" not in (tmp_path / "supervisor.log.1").read_text(encoding="utf-8")
