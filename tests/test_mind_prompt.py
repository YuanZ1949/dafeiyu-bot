"""prompt 里的心智提示：未了事提示与开场负例。

两条都只在 `mind.enabled` 打开时注入；默认配置必须与今天完全一致。
查库失败时 fail-open，prompt 仍要正常生成。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    InjectionConfig,
    LoggingConfig,
    MemoryConfig,
    MindConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message, OpenLoop
from dafeiyu.router import Router
from dafeiyu.service import Service

SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=SHANGHAI)
GROUP = "qq:1001"
PRIVATE = "qq:2002"
PENDING_SENTENCE = "这个群还有件事没下文"
NEGATIVE_HEADER = "【别再用这些开头】"


class FakeAdapter:
    bot_id = "qq:999"

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        return "bot-1"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def make_settings(*, mind_enabled: bool) -> Settings:
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
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({GROUP})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        injection=InjectionConfig(),
        mind=MindConfig(enabled=mind_enabled),
    )


def build(tmp_path, *, mind_enabled: bool = True):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(mind_enabled=mind_enabled)
    adapter = FakeAdapter()
    router = Router(settings, store, clock=lambda: NOW)
    service = Service(
        settings,
        store,
        object(),
        {"qq": adapter},
        router,
        "你是溟月",
        injection=InjectionConfig(),
    )
    return service, store


def make_msg(
    content: str = "在吗",
    *,
    chat_type: ChatType = ChatType.GROUP,
    chat_id: str = GROUP,
    message_id: str = "m1",
    at_others: bool = False,
    at_bot: bool = False,
    timestamp: int | None = None,
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
        timestamp=int(NOW.timestamp()) if timestamp is None else timestamp,
        at_others=at_others,
        at_bot=at_bot,
    )


def add_bot_line(store: Store, content: str, message_id: str) -> None:
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=GROUP,
            user_id="qq:999",
            nickname="溟月",
            content=content,
            message_id=message_id,
            segments=[],
            is_bot=True,
            timestamp=int(NOW.timestamp()),
        )
    )


def test_group_prompt_surfaces_pending_open_loop(tmp_path) -> None:
    service, store = build(tmp_path)
    store.add_open_loop(
        OpenLoop(ChatType.GROUP, GROUP, kind="question", content="显卡驱动那个坑")
    )

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [make_msg()], now=NOW)

    assert PENDING_SENTENCE in prompt
    assert "显卡驱动那个坑" in prompt
    # 有未了事时不再说「别插嘴」，否则 prompt 自相矛盾。
    assert "别插嘴" not in prompt


def test_pending_loop_hint_is_capped_at_40_chars(tmp_path) -> None:
    service, store = build(tmp_path)
    store.add_open_loop(
        OpenLoop(ChatType.GROUP, GROUP, kind="question", content="很长的未了事" * 20)
    )

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [make_msg()], now=NOW)

    assert "很长的未了事" * 4 in prompt
    assert "很长的未了事" * 9 not in prompt


def test_private_prompt_has_no_pending_loop(tmp_path) -> None:
    service, store = build(tmp_path)
    store.add_open_loop(
        OpenLoop(ChatType.GROUP, GROUP, kind="question", content="显卡驱动那个坑")
    )

    prompt = service._build_prompt(
        ChatType.PRIVATE,
        PRIVATE,
        [make_msg(chat_type=ChatType.PRIVATE, chat_id=PRIVATE)],
        now=NOW,
    )

    assert PENDING_SENTENCE not in prompt


def test_pending_loop_is_skipped_when_mind_disabled(tmp_path) -> None:
    service, store = build(tmp_path, mind_enabled=False)
    store.add_open_loop(
        OpenLoop(ChatType.GROUP, GROUP, kind="question", content="显卡驱动那个坑")
    )

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [make_msg()], now=NOW)

    assert PENDING_SENTENCE not in prompt


def test_prompt_lists_opening_negatives_from_recent_bot_lines(tmp_path) -> None:
    service, store = build(tmp_path)
    add_bot_line(store, "今天风好大", "b1")
    add_bot_line(store, "今天又热了", "b2")
    add_bot_line(store, "后来下雨", "b3")

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [make_msg()], now=NOW)

    assert NEGATIVE_HEADER in prompt
    body = prompt.split(NEGATIVE_HEADER, 1)[1].splitlines()[0]
    # recent_bot_messages_in 是新→旧；负例顺序跟随它。
    assert body.split("、") == ["后来下", "今天又", "今天风"]


def test_no_opening_negatives_without_bot_history(tmp_path) -> None:
    service, _store = build(tmp_path)

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [make_msg()], now=NOW)

    assert NEGATIVE_HEADER not in prompt


def test_prompt_fails_open_when_mind_lookups_raise(tmp_path, monkeypatch) -> None:
    service, store = build(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(store, "latest_open_loop", boom)
    monkeypatch.setattr(store, "recent_bot_messages_in", boom)

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [make_msg()], now=NOW)

    assert PENDING_SENTENCE not in prompt
    assert NEGATIVE_HEADER not in prompt
    assert "在吗" in prompt


def test_prompt_warns_when_message_at_someone_else(tmp_path) -> None:
    """@ 别人不是叫本鱼：批次提示要让模型先看清对象。"""
    service, _store = build(tmp_path)

    prompt = service._build_prompt(
        ChatType.GROUP,
        GROUP,
        [make_msg("@阿狗 你看这个", at_others=True)],
        now=NOW,
    )

    assert "本批有消息 @ 的是别的群友" in prompt
    assert "（@了别的群友）" in prompt


def test_prompt_skips_other_at_hint_when_bot_is_also_mentioned(tmp_path) -> None:
    service, _store = build(tmp_path)

    prompt = service._build_prompt(
        ChatType.GROUP,
        GROUP,
        [make_msg("@阿狗 @溟月 你俩看看", at_others=True, at_bot=True)],
        now=NOW,
    )

    assert "本批有消息 @ 的是别的群友" not in prompt


def test_prompt_marks_long_pause_before_batch(tmp_path) -> None:
    service, store = build(tmp_path)
    previous = make_msg("两小时前的话题", message_id="m0", timestamp=int(NOW.timestamp()) - 7200)
    current = make_msg("现在说的新事", message_id="m1")
    store.add_message(previous)
    store.add_message(current)

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [current], now=NOW)

    assert "【停顿 2 小时】" in prompt
