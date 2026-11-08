"""reply_media：images/ 附件的会话内归属校验（dev-opportunities todo 33 + C1）。

真实 Store(":memory:") 逐条覆盖：
- 本会话最近消息里出现过的 images/<name> 才能转出去；
- 跨会话、从未出现、store=None、形态越界、软链逃逸、200 条窗口外全部拒绝；
- C1 强制：QQ 入站注记写的是工作区全路径，匹配前必须剥掉 workspace 前缀；
- 记忆层故障只丢 images/ 附件，replies/ 与文本照常。
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dafeiyu.config import Settings, WorkspaceConfig
from dafeiyu.memory.store import Store
from dafeiyu.models import Attachment, ChatType, Message
from dafeiyu.reply_media import resolve_attachments

from tests.test_reply_media import PNG_BYTES, make_reply, make_settings

GROUP = ChatType.GROUP
CHAT_ID = "qq:1001"
OTHER_CHAT_ID = "qq:2002"


def make_msg(message_id: str, content: str, timestamp: int) -> Message:
    return Message(
        chat_type=GROUP,
        chat_id=CHAT_ID,
        user_id="qq:3001",
        nickname="阿明",
        content=content,
        message_id=message_id,
        timestamp=timestamp,
    )


def build_store(*messages: Message) -> Store:
    store = Store(":memory:")
    store.init_schema()
    for message in messages:
        store.add_message(message)
    return store


def write_workspace(tmp_path: Path, *names: str) -> None:
    (tmp_path / "replies").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "replies" / "a.png").write_bytes(PNG_BYTES)
    for name in names:
        (tmp_path / "images" / name).write_bytes(PNG_BYTES)


def resolve_ownership(
    tmp_path: Path,
    store: Any,
    rel: str,
    chat_type: ChatType | None = GROUP,
    chat_id: str = CHAT_ID,
) -> bool:
    reply = make_reply(Attachment(kind="image", path=rel))
    return bool(
        resolve_attachments(
            reply,
            make_settings(tmp_path),
            store=store,
            chat_type=chat_type,
            chat_id=chat_id,
        )
    )


def annotated(settings: Settings, name: str) -> str:
    """images.py 的真实注记格式：``rel = target.as_posix()``（工作区全路径）。"""
    return (Path(settings.workspace.dir) / "images" / name).as_posix()


# ---- 会话归属：成 / 败 -------------------------------------------------------


def test_same_chat_mentioned_image_passes(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png", "b.png", "c.png")
    store = build_store(
        make_msg("m1", "看看这张 [图片]（图存 images/a.png，可用 read 查看）", 1_000)
    )

    assert resolve_ownership(tmp_path, store, "images/a.png")


def test_other_chat_mentioned_image_rejected(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png", "b.png")
    store = Store(":memory:")
    store.init_schema()
    other = replace(
        make_msg("m1", "[图片]（图存 images/b.png）", 1_000), chat_id=OTHER_CHAT_ID
    )
    store.add_message(other)

    assert not resolve_ownership(tmp_path, store, "images/b.png")


def test_never_mentioned_image_rejected(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png", "c.png")
    store = build_store(make_msg("m1", "只提过 images/a.png", 1_000))

    assert not resolve_ownership(tmp_path, store, "images/c.png")


# ---- store=None / 半参调用 ---------------------------------------------------


def test_store_none_replies_ok_but_images_rejected(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png")

    assert resolve_ownership(tmp_path, None, "replies/a.png", GROUP, CHAT_ID)
    assert not resolve_ownership(tmp_path, None, "images/a.png", GROUP, CHAT_ID)


def test_missing_chat_type_rejects_images(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png")
    store = build_store(make_msg("m1", "images/a.png", 1_000))

    assert not resolve_ownership(
        tmp_path, store, "images/a.png", chat_type=None, chat_id=CHAT_ID
    )
    assert not resolve_ownership(
        tmp_path, store, "images/a.png", chat_type=GROUP, chat_id=""
    )
    assert resolve_ownership(tmp_path, store, "images/a.png")


# ---- 形态与软链不求情 --------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "images/../replies/a.png",
        "images\\..\\..\\secret.png",
        "C:/ws/images/a.png",
        "images/C:/x.png",
        "/etc/secret.png",
        "\\\\server\\share\\x.png",
    ],
)
def test_bad_shapes_rejected_even_with_ownership(tmp_path: Path, raw: str) -> None:
    write_workspace(tmp_path, "a.png")
    store = build_store(make_msg("m1", "images/a.png", 1_000))

    assert not resolve_ownership(tmp_path, store, raw)


def test_symlink_out_of_images_rejected(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    link = tmp_path / "images" / "link.png"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"无法创建符号链接：{exc}")
    store = build_store(make_msg("m1", "刚收图 images/link.png", 1_000))

    assert not resolve_ownership(tmp_path, store, "images/link.png")


# ---- 200 条窗口 --------------------------------------------------------------


def test_beyond_ownership_window_rejected(tmp_path: Path) -> None:
    write_workspace(tmp_path, "old.png", "a.png")
    messages = [make_msg("m0", "最早的图 images/old.png", 1)]
    base = int(time.time())
    messages.extend(
        make_msg(f"m{i}", f"普通消息 {i}", base + i) for i in range(1, 200 + 1)
    )
    store = build_store(*messages)

    assert not resolve_ownership(tmp_path, store, "images/old.png")
    # 窗口内出现过的 a.png 仍然放行，证明拒绝确实是窗口所致。
    fresh = replace(messages[-1], content="刚刚 images/a.png", message_id="fresh")
    store.add_message(fresh)
    assert resolve_ownership(tmp_path, store, "images/a.png")


# ---- C1：真实注记的工作区全路径必须能剥掉前缀 --------------------------------


def test_c1_absolute_workspace_prefix_stripped(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png", "b.png")
    settings = make_settings(tmp_path)
    store = build_store(
        make_msg(
            "m1",
            f"[图片]（图存 {annotated(settings, 'a.png')}，可用 read 查看）",
            1_000,
        ),
        replace(
            make_msg(
                "m2",
                f"[图片]（图存 {annotated(settings, 'b.png')}）",
                1_001,
            ),
            chat_id=OTHER_CHAT_ID,
        ),
    )

    assert resolve_ownership(tmp_path, store, "images/a.png")
    assert not resolve_ownership(tmp_path, store, "images/b.png")


def test_c1_relative_workspace_prefix_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "bot-workspace"
    ws.mkdir()
    (ws / "replies").mkdir()
    (ws / "images").mkdir()
    (ws / "images" / "a.png").write_bytes(PNG_BYTES)
    monkeypatch.chdir(tmp_path)
    settings = replace(
        make_settings(tmp_path), workspace=WorkspaceConfig(dir="bot-workspace")
    )
    store = build_store(
        make_msg(
            "m1",
            f"[图片]（图存 {annotated(settings, 'a.png')}，可用 read 查看）",
            1_000,
        ),
    )

    reply = make_reply(Attachment(kind="image", path="images/a.png"))
    assert resolve_attachments(
        reply,
        settings,
        store=store,
        chat_type=GROUP,
        chat_id=CHAT_ID,
    )


def test_windows_backslash_content_normalized(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png")
    windows_content = str(tmp_path / "images" / "a.png")  # 反斜杠全路径
    store = build_store(make_msg("m1", windows_content, 1_000))

    assert resolve_ownership(tmp_path, store, "images/a.png")


def test_token_match_is_exact(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png")
    store = build_store(
        make_msg("m1", "夹带 ximages/a.png 与 images/a.png.bak 都不算", 1_000)
    )

    assert not resolve_ownership(tmp_path, store, "images/a.png")


# ---- 记忆层故障降级 ----------------------------------------------------------


class CrashingStore:
    """recent_messages 恒抛异常的记忆层替身（鸭子类型，勿需 Store 子类）。"""

    def recent_messages(self, *_args: Any, **_kwargs: Any) -> list[Message]:
        raise RuntimeError("memory broken")


def test_recent_messages_failure_drops_only_images(tmp_path: Path) -> None:
    write_workspace(tmp_path, "a.png")

    result = resolve_attachments(
        make_reply(
            Attachment(kind="image", path="replies/a.png"),
            Attachment(kind="image", path="images/a.png"),
        ),
        make_settings(tmp_path),
        store=CrashingStore(),
        chat_type=GROUP,
        chat_id=CHAT_ID,
    )

    assert [item.path for item in result] == [
        (tmp_path / "replies" / "a.png").resolve().as_posix()
    ]
