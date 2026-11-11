"""Workspace 笔记读写：坏字节容错、enforce_limit 原子重写。

历史上 read_segment 用 read_text 严格解码，笔记文件混入非法 UTF-8 字节后
整条记忆注入链路每条消息都炸，且不自愈；与 enforce_limit 的容错口径不一致。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dafeiyu.memory.workspace import Workspace


def test_read_segment_survives_bad_bytes(tmp_path: Path) -> None:
    """笔记文件含非法 UTF-8 字节时不再抛 UnicodeDecodeError，返回容错解码的 str。"""
    ws = Workspace(tmp_path)
    note = tmp_path / "bad.md"
    note.write_bytes("# 标题\n".encode("utf-8") + b"ok\xffbad\n")

    result = ws.read_segment(note, limit_chars=1000)

    assert isinstance(result, str)


def test_enforce_limit_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """os.replace 失败时原文件保持完整，且不残留临时文件。

    enforce_limit 必须走「同目录临时文件 + os.replace」，不能直接整文件重写：
    重写中途崩溃会留下半截文件，被后续读取拿到损坏内容。
    """
    ws = Workspace(tmp_path)
    note = tmp_path / "note.md"
    content = "# 标题\n" + "".join(f"line-{i:03d}\n" for i in range(50))
    note.write_bytes(content.encode("utf-8"))
    original = note.read_bytes()

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        ws.enforce_limit(note, max_bytes=200)

    assert note.read_bytes() == original
    leftovers = [p.name for p in tmp_path.iterdir() if p != note]
    assert leftovers == []
