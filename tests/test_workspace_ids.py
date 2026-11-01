from __future__ import annotations

import pytest

from dafeiyu.config import InjectionConfig
from dafeiyu.memory.context import build_injection
from dafeiyu.memory.store import Store
from dafeiyu.memory.workspace import Workspace
from dafeiyu.models import ChatType

GROUP = ChatType.GROUP
PRIVATE = ChatType.PRIVATE


def test_chat_path_maps_prefixed_ids(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.chat_path(GROUP, "qq:123456").name == "group_qq_123456.md"
    assert ws.chat_path(PRIVATE, "qq:123456").name == "private_qq_123456.md"
    assert ws.chat_path(GROUP, "wxg:0d5c1f2a3b4c").name == "group_wxg_0d5c1f2a3b4c.md"


def test_group_and_private_notes_do_not_collide(tmp_path):
    """同一个 uin 的群聊与私聊是两份笔记，绝不能共用一个文件。"""
    ws = Workspace(tmp_path)
    ws.ensure()
    group_file = ws.chat_path(GROUP, "qq:123456")
    private_file = ws.chat_path(PRIVATE, "qq:123456")
    assert group_file != private_file
    group_file.write_text("# 群聊\n- 群里说的\n", encoding="utf-8")
    private_file.write_text("# 私聊\n- 私底下说的\n", encoding="utf-8")
    assert group_file.exists() and private_file.exists()
    assert "群里说的" in group_file.read_text(encoding="utf-8")
    assert "私底下说的" in private_file.read_text(encoding="utf-8")
    assert "私底下说的" not in group_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["../etc", "qq:../../x", "..", "qq:a/b", "qq:a\\b", ""])
def test_chat_path_rejects_unsafe_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        Workspace(tmp_path).chat_path(GROUP, bad)


def test_chat_path_rejects_int(tmp_path):
    with pytest.raises(TypeError):
        Workspace(tmp_path).chat_path(GROUP, 123456)  # type: ignore[arg-type]


def test_legacy_group_note_is_migrated_on_read(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure()
    legacy = tmp_path / "chats" / "group-123456.md"
    legacy.write_text("# 旧笔记\n- 迁移前的内容\n", encoding="utf-8")

    text = ws.read_for(GROUP, "qq:123456", global_chars=500, chat_chars=500)
    assert "迁移前的内容" in text
    new_file = tmp_path / "chats" / "group_qq_123456.md"
    assert new_file.exists()
    assert new_file.read_text(encoding="utf-8") == legacy.read_text(encoding="utf-8")
    assert legacy.exists()  # 旧文件不删


def test_legacy_private_note_keeps_private_prefix(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure()
    (tmp_path / "chats" / "private-123456.md").write_text(
        "# 旧私聊\n- 私聊旧内容\n", encoding="utf-8"
    )
    text = ws.read_for(PRIVATE, "qq:123456", global_chars=500, chat_chars=500)
    assert "私聊旧内容" in text
    assert (tmp_path / "chats" / "private_qq_123456.md").exists()
    # 群聊不该被这份私聊笔记污染
    assert "私聊旧内容" not in ws.read_for(GROUP, "qq:123456", global_chars=500, chat_chars=500)


def test_wxg_has_no_legacy_fallback(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure()
    assert ws.read_for(GROUP, "wxg:0d5c1f2a3b4c", global_chars=500, chat_chars=500).count(
        "旧"
    ) == 0
    assert not (tmp_path / "chats" / "group-0d5c1f2a3b4c.md").exists()


def test_build_injection_accepts_prefixed_chat_id(tmp_path):
    """漏改时的第一秒症状：build_injection -> read_for -> chat_path 抛 TypeError。"""
    store = Store(tmp_path / "test.db")
    store.init_schema()
    ws = Workspace(tmp_path / "ws")
    ws.ensure()
    text = build_injection(
        store,
        ws,
        GROUP,
        "qq:1",
        InjectionConfig(),
        today="2026-11-01",
    )
    assert isinstance(text, str)
    assert "TypeError" not in text
