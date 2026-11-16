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


def monkeypatch_topk_1(monkeypatch) -> None:
    """把印象 topk 压到 1：只有 focus 排序生效时，本批发言人的印象才注入。"""
    monkeypatch.setattr(
        "dafeiyu.memory.context.INJECTION_IMPRESSION_TOPK", 1
    )


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


# ---- T12：印象注入按本批发言人排序（focus_user_ids） ----------------------


def test_injection_prioritizes_current_batch_speaker(tmp_path, monkeypatch) -> None:
    """本批只有阿狗发言：topk=1 时注入阿狗的印象，而不是排名更高的阿猫。

    阿猫的印象 confidence 更高、写入更晚（无 focus 时它必排第一），
    所以只有 focus_user_ids 生效时阿狗的印象才会进注入。
    """
    service, store = build(tmp_path)
    monkeypatch_topk_1(monkeypatch)
    store.upsert_impression(
        ChatType.GROUP, GROUP, "qq:3001", "preference", "喜欢钓鱼",
        0.5, subject_nickname="阿狗",
    )
    store.upsert_impression(
        ChatType.GROUP, GROUP, "qq:3000", "preference", "喜欢猫",
        0.9, subject_nickname="阿猫",
    )
    speaker = Message(
        chat_type=ChatType.GROUP,
        chat_id=GROUP,
        user_id="qq:3001",
        nickname="阿狗",
        content="在吗",
        message_id="m1",
        segments=[],
        is_bot=False,
        timestamp=int(NOW.timestamp()),
    )

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [speaker], now=NOW)

    assert "阿狗：喜欢钓鱼" in prompt
    assert "喜欢猫" not in prompt


def test_focus_user_ids_exclude_bot_from_batch(tmp_path, monkeypatch) -> None:
    """本批含 bot 消息时，bot 的 user_id 绝不进入 focus 列表。"""
    service, store = build(tmp_path)
    captured: dict[str, object] = {}

    def spy(chat_type, chat_id, topk, *, focus_user_ids=(), **kwargs):
        captured["focus"] = focus_user_ids
        return []

    monkeypatch.setattr(store, "list_impressions", spy)
    batch = [
        make_msg(),
        Message(
            chat_type=ChatType.GROUP,
            chat_id=GROUP,
            user_id="qq:999",
            nickname="溟月",
            content="这是机器人说的话",
            message_id="b1",
            segments=[],
            is_bot=True,
            timestamp=int(NOW.timestamp()),
        ),
    ]

    service._build_prompt(ChatType.GROUP, GROUP, batch, now=NOW)

    assert captured["focus"] == ["qq:2002"]


def test_empty_batch_falls_back_to_recent_speakers(tmp_path, monkeypatch) -> None:
    """空批次回退旧的「最近 20 条消息」逻辑：只注入最近消息里的发言人。"""
    service, store = build(tmp_path)
    monkeypatch_topk_1(monkeypatch)
    # 阿猫（qq:3000）不在最近消息里且 confidence 更高；阿鱼（qq:2002）刚说过话。
    store.add_message(make_msg("我喜欢钓鱼", message_id="mA"))
    store.upsert_impression(
        ChatType.GROUP, GROUP, "qq:2002", "preference", "喜欢钓鱼",
        0.5, subject_nickname="阿鱼",
    )
    store.upsert_impression(
        ChatType.GROUP, GROUP, "qq:3000", "preference", "喜欢猫",
        0.9, subject_nickname="阿猫",
    )

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [], now=NOW)

    assert "阿鱼：喜欢钓鱼" in prompt
    assert "喜欢猫" not in prompt


def test_build_injection_empty_focus_user_ids_no_raise(tmp_path) -> None:
    """QA failure：focus_user_ids 为空时 build_injection 不抛异常，回退最近消息。"""
    from dafeiyu.memory.context import (
        INJECTION_IMPRESSION_HEADER,
        build_injection,
    )

    store = Store(str(tmp_path / "focus.db"))
    store.init_schema()
    try:
        now = int(NOW.timestamp())
        store.add_message(
            Message(
                ChatType.GROUP, GROUP, "qq:2002", "阿鱼", "在聊鱼缸",
                "m1", timestamp=now,
            )
        )
        store.upsert_impression(
            ChatType.GROUP, GROUP, "qq:2002", "preference", "喜欢钓鱼",
            0.5, subject_nickname="阿鱼",
        )

        text = build_injection(
            store, None, ChatType.GROUP, GROUP, InjectionConfig(),
            today="2026-11-04", focus_user_ids=(),
        )
    finally:
        store.close()

    assert INJECTION_IMPRESSION_HEADER in text
    assert "阿鱼：喜欢钓鱼" in text


def test_private_impression_injection_uses_private_header(tmp_path) -> None:
    """T14：私聊印象段标题是【关于对方】；群聊注入不串私聊内容。"""
    from dafeiyu.memory.context import build_injection

    store = Store(str(tmp_path / "privimp.db"))
    store.init_schema()
    try:
        store.upsert_impression(
            ChatType.PRIVATE, PRIVATE, "qq:2002", "fact", "在写 Rust 编译器",
            0.8, subject_nickname="阿鱼",
        )

        private_text = build_injection(
            store, None, ChatType.PRIVATE, PRIVATE, InjectionConfig(),
            today="2026-11-04",
        )
        group_text = build_injection(
            store, None, ChatType.GROUP, GROUP, InjectionConfig(),
            today="2026-11-04",
        )
    finally:
        store.close()

    assert "【关于对方】" in private_text
    assert "【群友印象】" not in private_text
    assert "在写 Rust 编译器" in private_text
    assert "在写 Rust 编译器" not in group_text
    assert "【关于对方】" not in group_text
