"""T6：跨群镜像的媒体路径解析与入站物化。

覆盖：
- ``resolve_media_path`` 的 workspace 相对路径解析、缺失/空路径、未配置目录、
  ``..`` 逃逸；
- ``images.materialize_relay`` 绕过 annotate 门控但要求 images/workspace，
  且绝不修改 ``msg.content``；
- ``media.materialize_relay_attachments`` 在副本上物化 URL 语音/视频，
  ``speech.enabled`` 只门控 voice/record、不门控 video；
- 解不开的微信 ``.dat`` 不会留下可读本地路径。
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import dafeiyu.media as media_module
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    ImagesConfig,
    LoggingConfig,
    MemoryConfig,
    MirrorConfig,
    MirrorPairConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
    WorkspaceConfig,
)
from dafeiyu.images import materialize_relay
from dafeiyu.media import materialize_relay_attachments, resolve_media_path
from dafeiyu.memory.store import Store
from dafeiyu.mirror import FetchFn, MirrorItem, MirrorRouter
from dafeiyu.models import Attachment, ChatType, Message, Segment
from tests.fakes import FakeChannelAdapter

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
SILK = b"#!SILK_V3" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"video-bytes" * 8


def make_settings(
    tmp_path: Path,
    *,
    workspace: bool = True,
    workspace_dir: str | None = None,
    images: bool = True,
    media: bool = False,
    speech: bool = False,
    max_bytes: int = 1024 * 1024,
    max_edge: int = 0,
    retention_days: int = 0,
) -> Settings:
    return cast(
        Settings,
        cast(
            object,
            SimpleNamespace(
                workspace=SimpleNamespace(
                    enabled=workspace,
                    dir=str(tmp_path / "ws")
                    if workspace_dir is None
                    else workspace_dir,
                ),
                wechat=SimpleNamespace(
                    media=SimpleNamespace(enabled=media, max_bytes=max_bytes)
                ),
                images=SimpleNamespace(
                    enabled=images,
                    max_bytes=max_bytes,
                    timeout_seconds=8.0,
                    retention_days=retention_days,
                    max_edge=max_edge,
                ),
                speech=SimpleNamespace(
                    enabled=speech,
                    model="base",
                    language="zh",
                    device="cpu",
                    compute_type="int8",
                    beam_size=1,
                    max_seconds=60,
                    timeout_seconds=10.0,
                    concurrency=1,
                    max_bytes=20 * 1024 * 1024,
                    hf_endpoint="",
                ),
            ),
        ),
    )


def make_message(
    attachments: list[Attachment],
    *,
    content: str = "",
    segments: list[Segment] | None = None,
    chat_id: str = "qq:1001",
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id="qq:2002" if chat_id.startswith("qq:") else "wxg:u1",
        nickname="阿鱼",
        content=content,
        message_id="m-1",
        segments=segments or [],
        attachments=attachments,
    )


# ------------------------------------------------------- resolve_media_path


def test_resolve_relative_returns_real_absolute_path(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "ws" / "images" / "x.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(PNG)

    resolved = resolve_media_path(
        settings, Attachment(kind="image", path="images/x.png")
    )

    assert resolved is not None
    assert resolved == target.resolve()
    assert resolved.is_absolute()
    assert resolved.is_file()


def test_resolve_missing_file_returns_none(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert (
        resolve_media_path(
            settings, Attachment(kind="image", path="images/nope.png")
        )
        is None
    )
    assert resolve_media_path(settings, Attachment(kind="image", path="")) is None


def test_resolve_disabled_workspace_dir_returns_none(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, workspace_dir="")
    outside = tmp_path / "x.png"
    outside.write_bytes(PNG)

    # 空目录不得拼成 cwd；绝对路径在未配置 workspace 时也不解析。
    assert (
        resolve_media_path(
            settings, Attachment(kind="image", path="images/x.png")
        )
        is None
    )
    assert (
        resolve_media_path(settings, Attachment(kind="image", path=str(outside)))
        is None
    )


def test_resolve_traversal_returns_none(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    escape = tmp_path / "escape.txt"
    escape.write_bytes(b"secret")

    assert (
        resolve_media_path(settings, Attachment(kind="file", path="../escape.txt"))
        is None
    )
    assert (
        resolve_media_path(
            settings, Attachment(kind="file", path="images/../../escape.txt")
        )
        is None
    )


def test_resolve_directory_and_empty_file_return_none(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "ws" / "images").mkdir(parents=True)
    (tmp_path / "ws" / "images" / "empty.png").write_bytes(b"")

    # 相对路径：目录与空文件都不可用。
    assert (
        resolve_media_path(settings, Attachment(kind="image", path="images"))
        is None
    )
    assert (
        resolve_media_path(
            settings, Attachment(kind="image", path="images/empty.png")
        )
        is None
    )
    # 绝对路径：真实非空文件可用，空文件不可用，缺失不可用。
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    assert (
        resolve_media_path(settings, Attachment(kind="image", path=str(outside)))
        == outside.resolve()
    )
    empty_abs = tmp_path / "empty-abs.png"
    empty_abs.write_bytes(b"")
    assert (
        resolve_media_path(
            settings, Attachment(kind="image", path=str(empty_abs))
        )
        is None
    )


# ---------------------------------------------- media.materialize_relay_attachments


async def test_materialize_undecodable_dat_has_no_readable_path(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, media=True)
    source = tmp_path / "msg.dat"
    source.write_bytes(b"not-decodable-image-bytes")
    attachment = Attachment(kind="image", path=str(source))
    msg = make_message([attachment], content="[图片]", chat_id="wxg:g1")

    result = await materialize_relay_attachments(
        msg, settings, decode=lambda _data: None
    )

    assert result is not msg
    assert result.attachments[0].path == ""
    assert resolve_media_path(settings, result.attachments[0]) is None
    # 调用方消息零改动。
    assert msg.attachments[0].path == str(source)
    assert msg.content == "[图片]"


async def test_relay_clears_dat_when_workspace_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, workspace=False)
    source = tmp_path / "msg.dat"
    source.write_bytes(b"raw-wechat-dat")
    attachment = Attachment(
        kind="image",
        path=str(source),
        size=14,
        fallbacks=("/x_h.png",),
    )
    msg = make_message([attachment], content="[图片]", chat_id="wxg:g1")

    result = await materialize_relay_attachments(
        msg, settings, decode=lambda _data: None
    )

    # workspace 关闭不等于放行：原始 .dat 路径必须被清空，由出站侧发占位。
    assert result is not msg
    assert result.attachments[0].path == ""
    assert result.attachments[0].size == 0
    assert result.attachments[0].fallbacks == ()
    # 调用方消息零改动。
    assert msg.attachments[0].path == str(source)
    assert msg.attachments[0].size == 14
    assert msg.attachments[0].fallbacks == ("/x_h.png",)


async def test_relay_clears_dat_when_workspace_root_missing(tmp_path: Path) -> None:
    # workspace.enabled=True 但 dir 为空：_workspace_root 返回 None 的真实配置态。
    settings = make_settings(tmp_path, workspace_dir="")
    source = tmp_path / "msg.dat"
    source.write_bytes(b"raw-wechat-dat")
    attachment = Attachment(
        kind="image",
        path=str(source),
        size=14,
        fallbacks=("/x_h.png",),
    )
    msg = make_message([attachment], content="[图片]", chat_id="wxg:g1")

    result = await materialize_relay_attachments(
        msg, settings, decode=lambda _data: None
    )

    assert result is not msg
    assert result.attachments[0].path == ""
    assert result.attachments[0].size == 0
    assert result.attachments[0].fallbacks == ()
    # 调用方消息零改动。
    assert msg.attachments[0].path == str(source)
    assert msg.attachments[0].size == 14
    assert msg.attachments[0].fallbacks == ("/x_h.png",)


async def test_no_content_mutation_on_relay_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, speech=True)

    async def fake_fetch(_url: str, _timeout: float, _max_bytes: int) -> bytes:
        return SILK

    async def fake_transcribe(_path: Path, _settings: object) -> str:
        return "在吗"

    monkeypatch.setattr(media_module.speech, "transcribe", fake_transcribe)
    msg = make_message(
        [Attachment(kind="record", source="https://x/v.silk", duration=2)],
        content="[语音]",
        segments=[Segment("record", {"url": "https://x/v.silk"})],
    )
    before = msg.content
    original_attachments = list(msg.attachments)

    result = await materialize_relay_attachments(msg, settings, fetch=fake_fetch)

    assert result is not msg
    assert msg.content == before
    assert msg.attachments == original_attachments
    assert result.content == before
    assert result.attachments[0].path.startswith("voices/")


async def test_qq_voice_url_materializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, speech=True)
    fetched: list[str] = []

    async def fake_fetch(url: str, _timeout: float, _max_bytes: int) -> bytes:
        fetched.append(url)
        return SILK

    async def fake_transcribe(_path: Path, _settings: object) -> str:
        return "晚安"

    monkeypatch.setattr(media_module.speech, "transcribe", fake_transcribe)
    msg = make_message(
        [Attachment(kind="record", source="https://x/v.silk")],
        content="[语音]",
    )

    result = await materialize_relay_attachments(msg, settings, fetch=fake_fetch)

    attachment = result.attachments[0]
    assert fetched == ["https://x/v.silk"]
    assert attachment.path.startswith("voices/")
    resolved = resolve_media_path(settings, attachment)
    assert resolved is not None
    assert resolved.is_file()
    assert resolved.read_bytes() == SILK


async def test_qq_video_url_materializes_even_when_speech_disabled(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, speech=False)
    fetched: list[str] = []

    async def fake_fetch(url: str, _timeout: float, _max_bytes: int) -> bytes:
        fetched.append(url)
        return MP4

    msg = make_message(
        [Attachment(kind="video", source="https://x/clip.mp4")],
        content="[视频]",
    )

    result = await materialize_relay_attachments(msg, settings, fetch=fake_fetch)

    attachment = result.attachments[0]
    assert fetched == ["https://x/clip.mp4"]
    assert attachment.path.startswith("videos/")
    assert attachment.path.endswith(".mp4")
    resolved = resolve_media_path(settings, attachment)
    assert resolved is not None
    assert resolved.is_file()
    assert resolved.read_bytes() == MP4
    assert not (tmp_path / "ws" / "voices").exists()


async def test_speech_disabled_blocks_voice_but_not_video(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, speech=False)
    fetched: list[str] = []

    async def fake_fetch(url: str, _timeout: float, _max_bytes: int) -> bytes:
        fetched.append(url)
        return MP4

    msg = make_message(
        [
            Attachment(kind="record", source="https://x/v.silk"),
            Attachment(kind="video", source="https://x/clip.mp4"),
        ],
        content="[语音] [视频]",
    )

    result = await materialize_relay_attachments(msg, settings, fetch=fake_fetch)

    voice, video = result.attachments
    assert voice.path == ""
    assert resolve_media_path(settings, voice) is None
    assert "https://x/v.silk" not in fetched
    assert not (tmp_path / "ws" / "voices").exists()
    assert video.path.startswith("videos/")
    assert resolve_media_path(settings, video) is not None
    assert fetched == ["https://x/clip.mp4"]


# ---------------------------------------------------- images.materialize_relay


async def test_images_materialize_relay_url_writes_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    calls: list[str] = []

    async def fake_fetch(url: str, _timeout: float, _max_bytes: int) -> bytes:
        calls.append(url)
        return PNG

    msg = make_message(
        [],
        content="[图片]",
        segments=[Segment("image", {"url": "https://x/a.png"})],
    )

    produced = await materialize_relay(msg, settings, fetch=fake_fetch)

    assert calls == ["https://x/a.png"]
    assert len(produced) == 1
    attachment = produced[0]
    assert attachment.kind == "image"
    assert attachment.path.startswith("images/")
    assert attachment.size == len(PNG)
    assert msg.content == "[图片]"
    assert msg.attachments == []
    resolved = resolve_media_path(settings, attachment)
    assert resolved is not None
    assert resolved.read_bytes() == PNG


async def test_images_materialize_relay_disabled_returns_empty(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, workspace=False)
    msg = make_message(
        [],
        content="[图片]",
        segments=[Segment("image", {"url": "https://x/a.png"})],
    )

    async def boom(_url: str, _timeout: float, _max_bytes: int) -> bytes:
        raise AssertionError("workspace 关闭时不允许下载")

    assert await materialize_relay(msg, settings, fetch=boom) == []
    assert msg.content == "[图片]"
    assert not (tmp_path / "ws").exists()


# ------------------------------------------------- T7：出站媒体投递与占位降级

QQ_CHAT = "qq:1001"
WXG_CHAT = "wxg:group"


def make_router_settings(tmp_path: Path) -> Settings:
    """MirrorRouter 实际读取的真实 Settings（含 persona/mirror/workspace）。"""
    base = Settings(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        bot_qq=999,
        ws_host="127.0.0.1",
        ws_port=8080,
        ws_token="",
        db_path=":memory:",
        persona=PersonaConfig("溟月", ("溟月",), 120, 300, 0.05),
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        mirror=MirrorConfig(
            enabled=True,
            pairs=(MirrorPairConfig(qq=QQ_CHAT, wechat=WXG_CHAT),),
        ),
    )
    return replace(
        base,
        workspace=WorkspaceConfig(dir=str(tmp_path / "ws")),
        images=ImagesConfig(enabled=True),
    )


def make_router(
    tmp_path: Path,
    *,
    qq: FakeChannelAdapter | None = None,
    wechat: FakeChannelAdapter | None = None,
    fetch: FetchFn | None = None,
) -> tuple[MirrorRouter, Store]:
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    adapters = {
        "qq": qq or FakeChannelAdapter(bot_id="qq:999", segments=True),
        "wechat": wechat or FakeChannelAdapter(bot_id="wxg:self", media=True),
    }
    return (
        MirrorRouter(
            make_router_settings(tmp_path),
            store,
            adapters,
            fetch=fetch,
        ),
        store,
    )


def place(tmp_path: Path, relative: str, data: bytes = PNG) -> Path:
    target = tmp_path / "ws" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def make_item(
    *,
    source_channel: str,
    attachments: tuple[Attachment, ...] = (),
    segments: tuple[Segment, ...] = (),
    text_lines: tuple[str, ...] = (),
    is_bot: bool = False,
    kind: str = "text",
    nickname: str = "阿鱼",
) -> MirrorItem:
    return MirrorItem(
        source_chat_id=QQ_CHAT if source_channel == "qq" else WXG_CHAT,
        target_chat_id=WXG_CHAT if source_channel == "qq" else QQ_CHAT,
        source_message_id="m1",
        nickname=nickname,
        is_bot=is_bot,
        text_lines=text_lines,
        attachments=attachments,
        segments=segments,
        source_channel=source_channel,
        created_at=time.time(),
        kind=kind,
    )


def record_order(adapter: FakeChannelAdapter) -> list[str]:
    """包装实例方法，记录实际调用顺序（不改变 FakeChannelAdapter 行为）。"""
    events: list[str] = []
    for name in ("send", "send_segments", "send_media", "upload_file"):
        method = getattr(adapter, name, None)
        if not callable(method):
            continue

        async def wrapper(*args: object, _m=method, _n=name, **kwargs: object):
            events.append(_n)
            return await _m(*args, **kwargs)

        setattr(adapter, name, wrapper)
    return events


def mirror_rows(store: Store, chat_id: str) -> list[str]:
    rows = store._conn.execute(
        "SELECT content FROM messages WHERE chat_id = ? AND mirror = 1", (chat_id,)
    ).fetchall()
    return [row["content"] for row in rows]


async def test_outbound_text_still_uses_inner_send(tmp_path: Path) -> None:
    router, _store = make_router(tmp_path)
    qq = cast(FakeChannelAdapter, router.adapters["qq"])

    await router._deliver(make_item(source_channel="wechat", text_lines=("你好",)))

    assert qq.sent == [(ChatType.GROUP, QQ_CHAT, "阿鱼：你好")]
    assert qq.segments == []
    assert router.stats()["sent"] == 1


async def test_outbound_pure_media_sends_placeholder_then_media(
    tmp_path: Path,
) -> None:
    router, _store = make_router(tmp_path)
    wechat = cast(FakeChannelAdapter, router.adapters["wechat"])
    events = record_order(wechat)
    place(tmp_path, "images/x.png")

    await router._deliver(
        make_item(
            source_channel="qq",
            attachments=(Attachment(kind="image", path="images/x.png", name="x.png"),),
        )
    )

    assert events[:2] == ["send", "send_media"]
    assert [text for _ct, _cid, text in wechat.sent] == ["阿鱼：[图片]"]
    assert len(wechat.media) == 1
    assert wechat.media[0][2:] == (
        "image",
        str((tmp_path / "ws" / "images" / "x.png").resolve()),
        "x.png",
    )


async def test_outbound_placeholder_human_has_nickname_and_bot_not(
    tmp_path: Path,
) -> None:
    router, _store = make_router(tmp_path)
    wechat = cast(FakeChannelAdapter, router.adapters["wechat"])
    place(tmp_path, "images/x.png")
    attachment = Attachment(kind="image", path="images/x.png", name="x.png")

    await router._deliver(
        make_item(source_channel="qq", attachments=(attachment,), is_bot=False)
    )
    await router._deliver(
        make_item(source_channel="qq", attachments=(attachment,), is_bot=True)
    )

    assert [text for _ct, _cid, text in wechat.sent] == ["阿鱼：[图片]", "[图片]"]


async def test_video_wechat_degrades_to_file(tmp_path: Path) -> None:
    """微信 hook 没有视频原语：物化成功的 mp4 按文件发，正文仍保留 [视频] 占位。"""
    router, _store = make_router(tmp_path)
    wechat = cast(FakeChannelAdapter, router.adapters["wechat"])
    path = place(tmp_path, "videos/v.mp4", MP4)

    await router._deliver(
        make_item(
            source_channel="qq",
            attachments=(
                Attachment(kind="video", path="videos/v.mp4", name="v.mp4"),
            ),
        )
    )

    assert [text for _ct, _cid, text in wechat.sent] == ["阿鱼：[视频]"]
    _ct, _cid, kind, sent_path, name = wechat.media[0]
    assert kind == "file"
    assert sent_path == str(path.resolve())
    assert name == "v.mp4"
    assert router.stats()["sent"] == 1


async def test_qq_to_wechat_image_uses_resolved_absolute_path_once(
    tmp_path: Path,
) -> None:
    router, store = make_router(tmp_path)
    wechat = cast(FakeChannelAdapter, router.adapters["wechat"])
    path = place(tmp_path, "images/x.png")

    await router._deliver(
        make_item(
            source_channel="qq",
            attachments=(Attachment(kind="image", path="images/x.png", name="x.png"),),
        )
    )

    assert len(wechat.media) == 1
    _ct, _cid, kind, sent_path, name = wechat.media[0]
    assert kind == "image"
    assert sent_path == str(path.resolve())
    assert name == "x.png"
    assert mirror_rows(store, WXG_CHAT) == ["阿鱼：[图片]"]
    assert router.stats()["sent"] == 1


async def test_wechat_to_qq_image_builds_image_segment(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    qq = cast(FakeChannelAdapter, router.adapters["qq"])
    path = place(tmp_path, "images/x.png")

    await router._deliver(
        make_item(
            source_channel="wechat",
            kind="image",
            attachments=(
                Attachment(kind="image", path=str(path), name="x.png"),
            ),
        )
    )

    assert qq.sent == []
    assert len(qq.segments) == 1
    _ct, _cid, segments = qq.segments[0]
    image_segments = [seg for seg in segments if seg.get("type") == "image"]
    assert image_segments == [{"type": "image", "data": {"file": str(path.resolve())}}]
    text_segments = [seg for seg in segments if seg.get("type") == "text"]
    assert text_segments == [{"type": "text", "data": {"text": "阿鱼：[图片]"}}]
    assert mirror_rows(store, QQ_CHAT) == ["阿鱼：[图片]"]


async def test_unsupported_media_sends_single_placeholder(tmp_path: Path) -> None:
    router, _store = make_router(
        tmp_path, wechat=FakeChannelAdapter(bot_id="wxg:self", media=True, media_ok=False)
    )
    wechat = cast(FakeChannelAdapter, router.adapters["wechat"])
    place(tmp_path, "images/x.png")

    await router._deliver(
        make_item(
            source_channel="qq",
            attachments=(Attachment(kind="image", path="images/x.png", name="x.png"),),
        )
    )

    assert [text for _ct, _cid, text in wechat.sent] == ["阿鱼：[图片]"]
    assert len(wechat.media) == 1
    assert router.stats()["sent"] == 1


async def test_missing_file_sends_placeholder_only(tmp_path: Path) -> None:
    router, _store = make_router(tmp_path)
    wechat = cast(FakeChannelAdapter, router.adapters["wechat"])

    await router._deliver(
        make_item(
            source_channel="qq",
            attachments=(
                Attachment(kind="image", path="images/missing.png", name="missing.png"),
            ),
        )
    )

    assert wechat.media == []
    assert len(wechat.sent) == 1
    text = wechat.sent[0][2]
    assert "[图片]" in text
    assert "missing.png" not in text
    assert "images/" not in text


async def test_sticker_fallback_sends_sticker_then_image(tmp_path: Path) -> None:
    router, _store = make_router(tmp_path)
    wechat = cast(FakeChannelAdapter, router.adapters["wechat"])
    place(tmp_path, "images/s.png")
    kinds: list[str] = []

    async def fake_send_media(
        _chat_type: ChatType, _chat_id: str, kind: str, _path: str, name: str = ""
    ) -> bool:
        kinds.append(kind)
        return kind == "image"

    router.adapters["wechat"].send_media = fake_send_media

    await router._deliver(
        make_item(
            source_channel="qq",
            attachments=(Attachment(kind="sticker", path="images/s.png", name="123"),),
        )
    )

    assert kinds == ["sticker", "image"]
    assert [text for _ct, _cid, text in wechat.sent] == ["阿鱼：[表情]"]
    assert router.stats()["sent"] == 1


class UploadRecorder(FakeChannelAdapter):
    """QQ 形态、额外提供 upload_file（NapCat 真实有此接口）。"""

    def __init__(self) -> None:
        super().__init__(bot_id="qq:999", segments=True)
        self.files: list[tuple[ChatType, str, str, str]] = []

    async def upload_file(
        self, chat_type: ChatType, chat_id: str, path: str, *, name: str = ""
    ) -> None:
        self.files.append((chat_type, chat_id, path, name))


async def test_wechat_to_qq_voice_builds_record_segment(tmp_path: Path) -> None:
    """微信语音落到 QQ 是 record 段，说明行仍保留可读占位。"""
    router, store = make_router(tmp_path)
    qq = cast(FakeChannelAdapter, router.adapters["qq"])
    path = place(tmp_path, "voices/v.slk", SILK)

    await router._deliver(
        make_item(
            source_channel="wechat",
            kind="voice",
            attachments=(
                Attachment(kind="voice", path=str(path), name="v.slk"),
            ),
        )
    )

    assert qq.sent == []
    _ct, _cid, segments = qq.segments[0]
    assert [seg for seg in segments if seg.get("type") == "record"] == [
        {"type": "record", "data": {"file": str(path.resolve())}}
    ]
    assert [seg for seg in segments if seg.get("type") == "text"] == [
        {"type": "text", "data": {"text": "阿鱼：[语音]"}}
    ]
    assert mirror_rows(store, QQ_CHAT) == ["阿鱼：[语音]"]


async def test_wechat_to_qq_video_builds_video_segment(tmp_path: Path) -> None:
    """微信→QQ 的视频不被 wechat-hook 限制：照常走 video 段。"""
    router, _store = make_router(tmp_path)
    qq = cast(FakeChannelAdapter, router.adapters["qq"])
    path = place(tmp_path, "videos/v.mp4", MP4)

    await router._deliver(
        make_item(
            source_channel="wechat",
            kind="video",
            attachments=(
                Attachment(kind="video", path=str(path), name="v.mp4"),
            ),
        )
    )

    _ct, _cid, segments = qq.segments[0]
    assert [seg for seg in segments if seg.get("type") == "video"] == [
        {"type": "video", "data": {"file": str(path.resolve())}}
    ]
    texts = [seg["data"]["text"] for seg in segments if seg.get("type") == "text"]
    assert "阿鱼：[视频]" in texts


async def test_wechat_to_qq_file_uses_upload_file(tmp_path: Path) -> None:
    """文件不塞消息段（NapCat 不收），走 upload_file 并保留文件名。"""
    qq = UploadRecorder()
    router, store = make_router(tmp_path, qq=qq)
    path = place(tmp_path, "files/doc.pdf", b"%PDF-1.4")

    await router._deliver(
        make_item(
            source_channel="wechat",
            kind="file",
            attachments=(
                Attachment(kind="file", path=str(path), name="doc.pdf"),
            ),
        )
    )

    assert qq.files == [(ChatType.GROUP, QQ_CHAT, str(path.resolve()), "doc.pdf")]
    assert [text for _ct, _cid, text in qq.sent] == ["阿鱼：[文件] doc.pdf"]
    assert qq.segments == []
    assert mirror_rows(store, QQ_CHAT) == ["阿鱼：[文件] doc.pdf"]


