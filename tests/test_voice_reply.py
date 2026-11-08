"""todo 27：opt-in TTS 语音出站的接线与降级矩阵。

覆盖两条发送路径（批次 ``_send_batch_reply`` 与单消息 ``_reply``）：
语音只在「成型后的单段、无 @、TTS 开启、工作区开启」时附加；合成失败、
超限、消息段发送抛错、多段或带 @ 时一律退回原文纯文本，历史/预览/outbox
始终保存原文。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dafeiyu.brain.client import BrainReply
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    SpeechConfig,
    TriggerConfig,
    WorkspaceConfig,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.reply_media import MAX_ATTACHMENT_BYTES
from dafeiyu.router import Router
from dafeiyu.service import Service

SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 8, 12, 0, tzinfo=SHANGHAI)
GROUP = "qq:1001"
PRIVATE = "qq:2002"
WX_GROUP = "wxg:0d5c1f2a3b4c"
REPLY = "你好呀"
VOICE_NAME_RE = re.compile(r"voice-[0-9a-f]{32}\.mp3")


class FakeAdapter:
    """同时支持纯文本与消息段；可选地让 send_segments 抛错。"""

    bot_id = "qq:999"

    def __init__(self, *, segments_error: Exception | None = None) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.segments: list[tuple[ChatType, str, list[dict[str, Any]]]] = []
        self._segments_error = segments_error

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"

    async def send_segments(
        self, chat_type: ChatType, chat_id: str, segments: list[dict[str, Any]]
    ) -> str:
        if self._segments_error is not None:
            raise self._segments_error
        self.segments.append((chat_type, chat_id, segments))
        return f"seg-{len(self.segments)}"


class FakeMediaAdapter:
    """微信形态：没有消息段，只有 send + send_media（返回 bool）。"""

    bot_id = "wxg:self"

    def __init__(
        self,
        *,
        media_ok: bool = True,
        media_error: Exception | None = None,
    ) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.media: list[tuple[ChatType, str, str, str, str]] = []
        self._media_ok = media_ok
        self._media_error = media_error

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"wx-{len(self.sent)}"

    async def send_media(
        self,
        chat_type: ChatType,
        chat_id: str,
        kind: str,
        path: str,
        name: str = "",
    ) -> bool:
        if self._media_error is not None:
            raise self._media_error
        self.media.append((chat_type, chat_id, kind, path, name))
        return self._media_ok


class FakeBrain:
    def __init__(self, reply: BrainReply) -> None:
        self.reply_value = reply
        self.calls = 0

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        self.calls += 1
        return self.reply_value

    def reset_session(self, chat_type, chat_id) -> None:
        pass

    async def aclose(self) -> None:
        pass


def make_settings(tmp_path: Path, *, tts_enabled: bool) -> Settings:
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
        triggers=TriggerConfig(
            60, 0.0, ("01:00", "07:00"), ("怎么",), mode="model", batch_seconds=0.0
        ),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        workspace=WorkspaceConfig(dir=str(tmp_path)),
        speech=SpeechConfig(tts_enabled=tts_enabled),
    )


def build(
    tmp_path: Path,
    *,
    tts_enabled: bool = True,
    adapter: FakeAdapter | None = None,
    reply: BrainReply | None = None,
) -> tuple[Service, Store, FakeBrain, FakeAdapter]:
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(tmp_path, tts_enabled=tts_enabled)
    adapter = adapter or FakeAdapter()
    brain = FakeBrain(reply or BrainReply(True, REPLY, "test", 10, 5, voice=True))
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(settings, store, brain, {"qq": adapter}, router, "你是溟月")
    return service, store, brain, adapter


def build_wechat(
    tmp_path: Path,
    *,
    tts_enabled: bool = True,
    adapter: FakeMediaAdapter | None = None,
    reply: BrainReply | None = None,
) -> tuple[Service, Store, FakeBrain, FakeMediaAdapter]:
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(tmp_path, tts_enabled=tts_enabled)
    adapter = adapter or FakeMediaAdapter()
    brain = FakeBrain(reply or BrainReply(True, REPLY, "test", 10, 5, voice=True))
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(
        settings, store, brain, {"wechat": adapter}, router, "你是溟月"
    )
    return service, store, brain, adapter


def make_msg(
    *,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = GROUP,
    message_id: str = "m1",
    content: str = "在吗",
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=False,
        at_bot=False,
        reply_to_bot=False,
        timestamp=0,
    )


def _ok_tts(calls: list[tuple[str, str]] | None = None):
    async def fake(text: str, path: Path, cfg) -> bool:
        if calls is not None:
            calls.append((text, path.name))
        path.write_bytes(b"ID3" + b"\x00" * 64)
        return True

    return fake


def _fail_tts(calls: list[tuple[str, str]] | None = None):
    async def fake(text: str, path: Path, cfg) -> bool:
        if calls is not None:
            calls.append((text, path.name))
        return False

    return fake


def _recording_tts(calls: list[tuple[str, str]]):
    """成功合成并记录调用；用于断言「根本不该合成」。"""
    return _ok_tts(calls)


def _bot_contents(store: Store, chat_type: ChatType, chat_id: str) -> list[str]:
    return [
        message.content
        for message in store.recent_bot_messages_in(chat_type, chat_id, 10)
    ]


def _mp3_files(tmp_path: Path) -> list[Path]:
    root = tmp_path / "replies"
    return sorted(root.glob("*.mp3")) if root.is_dir() else []


# ---------------------------------------------------------------- 成功路径


async def test_group_voice_success_sends_one_record_without_text(
    tmp_path, monkeypatch
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("dafeiyu.service.synthesize", _ok_tts(calls))
    service, store, brain, adapter = build(tmp_path)

    sent = await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert len(adapter.segments) == 1
    segments = adapter.segments[0][2]
    types = [segment["type"] for segment in segments]
    assert types.count("record") == 1
    assert "text" not in types
    assert segments[-1]["type"] == "record"
    assert adapter.sent == []
    assert sent == [(REPLY, "seg-1")]
    # 历史保存原文（允许追加 [语音] 路径注记），预览/outbox 也只有文本。
    contents = _bot_contents(store, ChatType.GROUP, GROUP)
    assert len(contents) == 1 and REPLY in contents[0]
    assert not store.has_outbox()
    assert calls and calls[0][0] == REPLY


async def test_private_voice_success_uses_reply_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("dafeiyu.service.synthesize", _ok_tts())
    service, store, brain, adapter = build(tmp_path)

    await service._reply(
        make_msg(chat_type=ChatType.PRIVATE, chat_id=PRIVATE, message_id="p1")
    )

    assert brain.calls == 1
    assert len(adapter.segments) == 1
    types = [segment["type"] for segment in adapter.segments[0][2]]
    assert types.count("record") == 1
    assert "text" not in types
    assert adapter.sent == []
    contents = _bot_contents(store, ChatType.PRIVATE, PRIVATE)
    assert len(contents) == 1 and REPLY in contents[0]
    assert not store.has_outbox()


async def test_voice_filename_is_windows_safe(tmp_path, monkeypatch) -> None:
    """message_id 形如 epoch:seq（含 ``:``）也不能进文件名。"""
    monkeypatch.setattr("dafeiyu.service.synthesize", _ok_tts())
    service, _store, brain, adapter = build(tmp_path)

    await service._send_batch_reply(
        [make_msg(message_id="9f3a:12")],
        brain.reply_value,
        ChatType.GROUP,
        GROUP,
    )

    files = _mp3_files(tmp_path)
    assert len(files) == 1
    assert ":" not in files[0].name
    assert VOICE_NAME_RE.fullmatch(files[0].name)
    assert files[0].stat().st_size > 0
    assert adapter.sent == []


# ---------------------------------------------------------------- 降级路径


async def test_tts_failure_falls_back_to_original_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("dafeiyu.service.synthesize", _fail_tts())
    service, store, brain, adapter = build(tmp_path)

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.segments == []
    assert adapter.sent == [(ChatType.GROUP, GROUP, REPLY)]
    contents = _bot_contents(store, ChatType.GROUP, GROUP)
    assert len(contents) == 1 and REPLY in contents[0]
    assert not store.has_outbox()
    assert _mp3_files(tmp_path) == []


async def test_wechat_voice_success_skips_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("dafeiyu.service.synthesize", _ok_tts())
    adapter = FakeMediaAdapter(media_ok=True)
    service, store, brain, media_adapter = build_wechat(
        tmp_path, adapter=adapter
    )

    await service._send_batch_reply(
        [make_msg(chat_id=WX_GROUP)], brain.reply_value, ChatType.GROUP, WX_GROUP
    )

    assert len(media_adapter.media) == 1
    _chat_type, _chat_id, kind, path, name = media_adapter.media[0]
    assert kind == "voice" and Path(path).name == name
    assert media_adapter.sent == []
    contents = _bot_contents(store, ChatType.GROUP, WX_GROUP)
    assert len(contents) == 1 and REPLY in contents[0]
    assert not store.has_outbox()


async def test_wechat_voice_media_failure_falls_back_to_text(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("dafeiyu.service.synthesize", _ok_tts())
    adapter = FakeMediaAdapter(media_ok=False)
    service, store, brain, media_adapter = build_wechat(
        tmp_path, adapter=adapter
    )

    await service._send_batch_reply(
        [make_msg(chat_id=WX_GROUP)], brain.reply_value, ChatType.GROUP, WX_GROUP
    )

    assert len(media_adapter.media) == 1
    assert media_adapter.sent == [(ChatType.GROUP, WX_GROUP, REPLY)]
    contents = _bot_contents(store, ChatType.GROUP, WX_GROUP)
    assert len(contents) == 1 and REPLY in contents[0]
    assert not store.has_outbox()


async def test_segment_send_failure_falls_back_to_original_text(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("dafeiyu.service.synthesize", _ok_tts())
    service, store, brain, adapter = build(
        tmp_path, adapter=FakeAdapter(segments_error=RuntimeError("boom"))
    )

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.segments == []
    assert adapter.sent == [(ChatType.GROUP, GROUP, REPLY)]
    contents = _bot_contents(store, ChatType.GROUP, GROUP)
    assert len(contents) == 1 and REPLY in contents[0]
    assert not store.has_outbox()


async def test_tts_disabled_never_synthesizes(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("dafeiyu.service.synthesize", _recording_tts(calls))
    service, _store, brain, adapter = build(tmp_path, tts_enabled=False)

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.segments == []
    assert adapter.sent == [(ChatType.GROUP, GROUP, REPLY)]
    assert calls == []
    assert _mp3_files(tmp_path) == []


async def test_multi_chunk_reply_stays_text_only(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("dafeiyu.service.synthesize", _recording_tts(calls))
    service, _store, brain, adapter = build(tmp_path)
    monkeypatch.setattr(
        service, "_shape_reply", lambda reply, text, original: [REPLY, "再见呀"]
    )

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.segments == []
    assert [text for _, _, text in adapter.sent] == [REPLY, "再见呀"]
    assert calls == []
    assert _mp3_files(tmp_path) == []


async def test_at_target_disables_voice(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("dafeiyu.service.synthesize", _recording_tts(calls))
    reply = BrainReply(True, REPLY, "test", 10, 5, voice=True, at=("阿鱼",))
    service, _store, _brain, adapter = build(tmp_path, reply=reply)

    await service._send_batch_reply([make_msg()], reply, ChatType.GROUP, GROUP)

    # 带 @ 走消息段：at + text，没有任何 record 段。
    assert len(adapter.segments) == 1
    types = [segment["type"] for segment in adapter.segments[0][2]]
    assert "at" in types and "text" in types
    assert "record" not in types
    assert adapter.sent == []
    assert calls == []
    assert _mp3_files(tmp_path) == []


async def test_synthesize_exception_falls_back_and_cleans_up(
    tmp_path, monkeypatch
) -> None:
    async def boom(text: str, path: Path, cfg) -> bool:
        raise RuntimeError("edge-tts exploded")

    monkeypatch.setattr("dafeiyu.service.synthesize", boom)
    service, _store, brain, adapter = build(tmp_path)

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.segments == []
    assert adapter.sent == [(ChatType.GROUP, GROUP, REPLY)]
    assert _mp3_files(tmp_path) == []
    assert list((tmp_path / "replies").glob("*.tmp")) == []


async def test_oversized_voice_file_falls_back_and_is_removed(
    tmp_path, monkeypatch
) -> None:
    async def huge(text: str, path: Path, cfg) -> bool:
        with path.open("wb") as handle:
            handle.truncate(MAX_ATTACHMENT_BYTES + 1)
        return True

    monkeypatch.setattr("dafeiyu.service.synthesize", huge)
    service, _store, brain, adapter = build(tmp_path)

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.segments == []
    assert adapter.sent == [(ChatType.GROUP, GROUP, REPLY)]
    assert _mp3_files(tmp_path) == []


