"""回复媒体：附件安全边界 + OneBot 段顺序 + @-名字解析。

验收（message-types-v2 todo 5）：
- 合法 replies/ 文件解析出附件（绝对 posix 路径、真实大小）；
- 越界/超限/缺失/目录/空路径/软链逃逸全部拒绝，且不在工作区留任何痕迹；
- 段顺序固定 reply -> at -> text -> 媒体，file 类不出段，空文本不出段；
- resolve_at_names 只认在场成员：去重、封顶 3、容忍奇形输入。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from dafeiyu.brain.client import BrainReply
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
    WorkspaceConfig,
)
from dafeiyu.models import Attachment, ChatType, Message
from dafeiyu.reply_media import (
    MAX_ATTACHMENT_BYTES,
    build_onebot_segments,
    resolve_at_names,
    resolve_attachments,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        bot_qq=999,
        ws_host="127.0.0.1",
        ws_port=8080,
        ws_token="",
        db_path=":memory:",
        persona=PersonaConfig("溟月", ("大肥鱼",), 120, 300, 0.05),
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        workspace=WorkspaceConfig(dir=str(tmp_path)),
    )


def make_reply(*attachments: Attachment) -> BrainReply:
    return BrainReply(speak=True, reply="在的", attachments=attachments)


def build_workspace(tmp_path: Path) -> None:
    """replies/a.png（合法）+ images/a.png（错误目录）+ replies/empty/（目录）。"""
    (tmp_path / "replies").mkdir()
    (tmp_path / "replies" / "a.png").write_bytes(PNG_BYTES)
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.png").write_bytes(PNG_BYTES)
    (tmp_path / "replies" / "empty").mkdir()


def snapshot(root: Path) -> list[str]:
    """工作区整棵树的相对路径快照：拒绝路径不得留下任何新文件/目录。"""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ---- resolve_attachments：合法路径 -----------------------------------------


def test_valid_replies_file_resolves(tmp_path: Path) -> None:
    build_workspace(tmp_path)

    result = resolve_attachments(
        make_reply(
            Attachment(kind="Image", path="replies/a.png", source="http://x/a.png")
        ),
        make_settings(tmp_path),
    )

    assert len(result) == 1
    item = result[0]
    assert item.path == (tmp_path / "replies" / "a.png").resolve().as_posix()
    assert item.size == len(PNG_BYTES)
    assert item.kind == "image"  # kind 归一化：大小写收敛
    assert item.source == ""
    assert item.name == "a.png"
    assert item.duration == 0


def test_workspace_disabled_rejects_everything(tmp_path: Path) -> None:
    build_workspace(tmp_path)
    settings = replace(make_settings(tmp_path), workspace=WorkspaceConfig(dir=""))

    result = resolve_attachments(
        make_reply(Attachment(kind="image", path="replies/a.png")), settings
    )

    assert result == []


# ---- resolve_attachments：形态级拒绝（resolve 之前） ------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "../../x.png",
        "..\\..\\x.png",  # Windows 反斜杠形态
        "C:\\Windows\\x.png",  # 盘符
        "replies/C:/x.png",  # 夹带盘符
        "/etc/passwd",  # POSIX 根路径
        "\\\\server\\share\\x.png",  # UNC
        "replies/../../../etc",  # 中段 ..
        "",  # 空路径
    ],
)
def test_shape_rejects(tmp_path: Path, raw: str) -> None:
    build_workspace(tmp_path)

    result = resolve_attachments(
        make_reply(Attachment(kind="image", path=raw)), make_settings(tmp_path)
    )

    assert result == []


# ---- resolve_attachments：文件系统级拒绝 ------------------------------------


def test_missing_file_rejected(tmp_path: Path) -> None:
    build_workspace(tmp_path)

    result = resolve_attachments(
        make_reply(Attachment(kind="image", path="replies/ghost.png")),
        make_settings(tmp_path),
    )

    assert result == []


def test_wrong_dir_rejected(tmp_path: Path) -> None:
    # 文件真实存在，只是不在 replies/ 下：必须仍被包含性检查拒绝。
    build_workspace(tmp_path)

    result = resolve_attachments(
        make_reply(Attachment(kind="image", path="images/a.png")),
        make_settings(tmp_path),
    )

    assert result == []


def test_disallowed_suffix_rejected(tmp_path: Path) -> None:
    build_workspace(tmp_path)
    (tmp_path / "replies" / "a.exe").write_bytes(b"MZ...")

    result = resolve_attachments(
        make_reply(Attachment(kind="file", path="replies/a.exe")),
        make_settings(tmp_path),
    )

    assert result == []


def test_oversized_file_rejected(tmp_path: Path) -> None:
    build_workspace(tmp_path)
    big = tmp_path / "replies" / "big.png"
    with big.open("wb") as f:  # 只扩长度不写数据：11 MiB 也瞬间完成
        f.truncate(MAX_ATTACHMENT_BYTES + 1)
    assert big.stat().st_size == MAX_ATTACHMENT_BYTES + 1

    result = resolve_attachments(
        make_reply(Attachment(kind="image", path="replies/big.png")),
        make_settings(tmp_path),
    )

    assert result == []


def test_empty_file_rejected(tmp_path: Path) -> None:
    build_workspace(tmp_path)
    (tmp_path / "replies" / "zero.png").write_bytes(b"")

    result = resolve_attachments(
        make_reply(Attachment(kind="image", path="replies/zero.png")),
        make_settings(tmp_path),
    )

    assert result == []


def test_directory_path_rejected(tmp_path: Path) -> None:
    build_workspace(tmp_path)  # replies/empty 是真实目录

    result = resolve_attachments(
        make_reply(Attachment(kind="file", path="replies/empty")),
        make_settings(tmp_path),
    )

    assert result == []


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    build_workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = tmp_path / "replies" / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError as exc:  # Windows 无符号链接特权时：明确跳过，绝不静默通过
        pytest.skip(f"无法创建符号链接：{exc}")

    result = resolve_attachments(
        make_reply(Attachment(kind="file", path="replies/link.txt")),
        make_settings(tmp_path),
    )

    assert result == []


def test_rejections_leave_workspace_untouched(tmp_path: Path) -> None:
    build_workspace(tmp_path)
    before = snapshot(tmp_path)

    result = resolve_attachments(
        make_reply(
            Attachment(kind="image", path=""),  # 空
            Attachment(kind="image", path="../../x.png"),  # ..
            Attachment(kind="image", path="replies/ghost.png"),  # 缺失
            Attachment(kind="image", path="images/a.png"),  # 越界
        ),
        make_settings(tmp_path),
    )

    assert result == []
    assert snapshot(tmp_path) == before


# ---- build_onebot_segments -------------------------------------------------


def test_segment_order_reply_at_text_image_file_skipped() -> None:
    segments = build_onebot_segments(
        "在的",
        quote_id="42",
        at_qq_ids=("111", "222"),
        attachments=(
            Attachment(kind="image", path="C:/ws/replies/a.png"),
            Attachment(kind="file", path="C:/ws/replies/b.zip"),
        ),
    )

    assert segments == [
        {"type": "reply", "data": {"id": "42"}},
        {"type": "at", "data": {"qq": "111"}},
        {"type": "at", "data": {"qq": "222"}},
        {"type": "text", "data": {"text": "在的"}},
        {"type": "image", "data": {"file": "C:/ws/replies/a.png"}},
    ]


def test_voice_maps_to_record_segment() -> None:
    segments = build_onebot_segments(
        "", attachments=(Attachment(kind="voice", path="C:/ws/replies/a.mp3"),)
    )

    assert segments == [{"type": "record", "data": {"file": "C:/ws/replies/a.mp3"}}]


def test_video_forward_skipped_silently() -> None:
    segments = build_onebot_segments(
        "看",
        attachments=(
            Attachment(kind="video", path="C:/ws/replies/a.mp4"),
            Attachment(kind="forward", path="C:/ws/replies/a.txt"),
        ),
    )

    assert segments == [{"type": "text", "data": {"text": "看"}}]


def test_sticker_with_numeric_name_becomes_face_segment() -> None:
    segments = build_onebot_segments(
        "哈哈", attachments=(Attachment(kind="sticker", name="178"),)
    )

    assert segments == [
        {"type": "text", "data": {"text": "哈哈"}},
        {"type": "face", "data": {"id": "178"}},
    ]


def test_sticker_with_text_name_becomes_mface_segment() -> None:
    segments = build_onebot_segments(
        "", attachments=(Attachment(kind="sticker", name="摸鱼"),)
    )

    assert segments == [{"type": "mface", "data": {"summary": "摸鱼"}}]


def test_sticker_without_name_is_skipped() -> None:
    segments = build_onebot_segments(
        "看", attachments=(Attachment(kind="sticker", path="C:/ws/replies/a.webp"),)
    )

    assert segments == [{"type": "text", "data": {"text": "看"}}]


@pytest.mark.parametrize("name", ["0", "1234567"])
def test_sticker_with_illegal_face_id_is_skipped(name: str) -> None:
    segments = build_onebot_segments(
        "", attachments=(Attachment(kind="sticker", name=name),)
    )

    assert segments == []


def test_sticker_survives_disabled_workspace_but_file_does_not(
    tmp_path: Path,
) -> None:
    settings = replace(make_settings(tmp_path), workspace=WorkspaceConfig(dir=""))
    resolved = resolve_attachments(
        make_reply(
            Attachment(kind="sticker", name="178"),
            Attachment(kind="image", path="replies/a.png"),
        ),
        settings,
    )

    assert [(item.kind, item.name) for item in resolved] == [("sticker", "178")]


def test_sticker_without_name_dropped(tmp_path: Path) -> None:
    resolved = resolve_attachments(
        make_reply(Attachment(kind="sticker")), make_settings(tmp_path)
    )

    assert resolved == []


def test_empty_text_with_attachments_omits_text_segment() -> None:
    segments = build_onebot_segments(
        "", attachments=(Attachment(kind="image", path="C:/ws/replies/a.png"),)
    )

    assert segments == [{"type": "image", "data": {"file": "C:/ws/replies/a.png"}}]


def test_no_quote_no_at_no_attachment_gives_text_only() -> None:
    assert build_onebot_segments("哈哈") == [{"type": "text", "data": {"text": "哈哈"}}]


# ---- resolve_at_names ------------------------------------------------------


def _participant(nickname: str) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="qq:1001",
        user_id="qq:2002",
        nickname=nickname,
        content="x",
        message_id="m1",
    )


def test_resolve_at_names_filters_dedupes_caps() -> None:
    names = ["阿鱼", "小明", "阿鱼", "小黑", "老王", "不在场"]
    participants = [_participant("阿鱼"), "小明", "小黑", "老王"]

    assert resolve_at_names(names, participants) == ["阿鱼", "小明", "小黑"]


def test_resolve_at_names_case_sensitive_and_stripped() -> None:
    assert resolve_at_names(["ab", "AB"], ["AB"]) == ["AB"]  # 大小写敏感
    assert resolve_at_names([" 阿鱼 "], ["阿鱼"]) == ["阿鱼"]  # 两侧去空白后比较


def test_resolve_at_names_tolerates_junk() -> None:
    # 无 nickname 的怪对象、非字符串名字：都不抛错，只按 str 归一参与比较。
    # cast 是故意的：签名声明 Sequence[str]，但运行时必须容忍模型乱给。
    junk_names = cast(
        Sequence[str],
        ["阿鱼", 42, None, ["x"]],
    )
    result = resolve_at_names(junk_names, [_participant("阿鱼"), {}, object()])

    assert result == ["阿鱼"]
