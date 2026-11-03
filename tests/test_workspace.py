from __future__ import annotations

import pytest

from dafeiyu.memory.workspace import Workspace
from dafeiyu.models import ChatType


def test_missing_file_is_empty(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure()
    # ensure() 会写 MEMORY.md 模板，所以这里用一个确实还没写过的会话笔记。
    assert ws.read_segment(ws.chat_path(ChatType.GROUP, "qq:1"), 100) == ""


def test_truncation_keeps_tail_and_counts(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text("# 记忆\n" + "\n".join(f"- 第{i}条" for i in range(60)), encoding="utf-8")
    text = Workspace(tmp_path).read_segment(p, limit_chars=60)
    assert text.startswith("# 记忆") and "第59条" in text and "第0条" not in text
    assert "未展开" in text


def test_single_giant_line_does_not_explode(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text("# t\n" + "长" * 100_000, encoding="utf-8")
    out = Workspace(tmp_path).read_segment(p, limit_chars=50)
    assert len(out) < 200  # 单行按字符硬切，不整行复制


def test_chat_path_carries_channel_prefix(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.chat_path(ChatType.GROUP, "qq:20001").name == "group_qq_20001.md"
    assert ws.chat_path(ChatType.PRIVATE, "qq:504567327").name == "private_qq_504567327.md"
    assert ws.chat_path(ChatType.GROUP, "wxg:0d5c1f2a3b4c").name == "group_wxg_0d5c1f2a3b4c.md"
    assert ws.chat_path(ChatType.GROUP, "qq:20001") != ws.chat_path(
        ChatType.PRIVATE, "qq:20001"
    )
    with pytest.raises(TypeError):
        ws.chat_path(ChatType.GROUP, 20001)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ws.chat_path(ChatType.GROUP, "..\\..\\x")


def test_enforce_limit_keeps_title_comment_and_tail(tmp_path):
    p = tmp_path / "chats" / "group-1.md"
    p.parent.mkdir(parents=True)
    p.write_text("# t\n" + ("x" * 200 + "\n") * 50, encoding="utf-8")
    assert Workspace(tmp_path).enforce_limit(p, max_bytes=1024) is True
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# t" and lines[1].startswith("<!-- truncated ")
    assert p.stat().st_size <= 1024
