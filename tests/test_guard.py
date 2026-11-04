"""重复守卫：同一句话最近说过，就改写 / 换中性短句 / 这一片不说。"""

from __future__ import annotations

from dafeiyu.brain.client import BrainReply
from dafeiyu.brain.guard import (
    NEUTRAL_REPLY,
    find_repeat,
    has_leading_os,
    normalize,
    opening_fragments,
    polish_chunk,
    similarity,
    strip_banned_sentences,
    strip_leading_os,
    trim_repeated_opening,
)
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    InjectionConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service

GROUP = "qq:1001"


def make_settings(*, repeat_guard: bool = True) -> Settings:
    return Settings(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        bot_qq=999,
        ws_host="127.0.0.1",
        ws_port=8080,
        ws_token="",
        db_path=":memory:",
        persona=PersonaConfig(
            "溟月", ("大肥鱼",), 120, 300, 0.05, repeat_guard=repeat_guard
        ),
        triggers=TriggerConfig(
            60, 0.0, ("01:00", "07:00"), ("怎么",), mode="model", batch_seconds=0.0
        ),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({GROUP})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        injection=InjectionConfig(),
    )


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return "bot-1"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class FakeBrain:
    def __init__(self, reply: BrainReply) -> None:
        self.reply_value = reply

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        return self.reply_value

    def reset_session(self, chat_type, chat_id) -> None:
        pass


def build(tmp_path, *, repeat_guard: bool = True, brain: FakeBrain | None = None):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings(repeat_guard=repeat_guard)
    adapter = FakeAdapter()
    router = Router(settings, store)
    service = Service(
        settings,
        store,
        brain or FakeBrain(BrainReply(True, "在的")),
        {"qq": adapter},
        router,
        "你是溟月",
        injection=InjectionConfig(),
    )
    return service, store, adapter


def add_bot_message(store: Store, content: str, *, message_id: str = "b1") -> None:
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=GROUP,
            user_id="qq:999",
            nickname="溟月",
            content=content,
            message_id=message_id,
            is_bot=True,
            timestamp=0,
        )
    )


def make_msg(content: str = "在吗") -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=GROUP,
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id="m1",
        timestamp=0,
    )


# ---- 纯函数 ---------------------------------------------------------


def test_normalize_drops_punctuation_and_case() -> None:
    assert normalize(" Hello，世界！ ") == normalize("hello世界")
    assert normalize("！？。") == ""


def test_similarity_identical_and_unrelated() -> None:
    assert similarity("今天风好大，适合睡觉", "今天风好大适合睡觉！") == 1.0
    assert similarity("今天风好大", "这个 bug 我看看") < 0.2


def test_find_repeat_returns_first_match() -> None:
    recent = ["这个确实挺有意思的", "今天风好大"]
    assert find_repeat("这个确实挺有意思的。", recent) == recent[0]
    assert find_repeat("我先吃饭去了", recent) is None


def test_trim_repeated_opening_keeps_the_new_part() -> None:
    recent = ["这个确实挺有意思的"]
    trimmed = trim_repeated_opening("这个确实挺有意思的，不过我不太确定", recent)
    assert trimmed == "不过我不太确定"


def test_trim_gives_up_when_rest_is_too_short() -> None:
    assert trim_repeated_opening("这个确实挺有意思的！", ["这个确实挺有意思的"]) == ""


# ---- polish_chunk ---------------------------------------------------


def test_fresh_reply_passes_through() -> None:
    assert polish_chunk("我先去吃饭了", ["今天风好大"]) == "我先去吃饭了"


def test_repeat_prefers_trimming_the_opening() -> None:
    recent = ["这个确实挺有意思的，我昨天也看了"]
    assert (
        polish_chunk("这个确实挺有意思的，我昨天也看了，不过我不太确定", recent)
        == "不过我不太确定"
    )


def test_repeat_falls_back_to_neutral() -> None:
    assert polish_chunk("今天风好大", ["今天风好大"]) == NEUTRAL_REPLY


def test_repeat_stays_silent_when_neutral_was_used() -> None:
    assert polish_chunk("今天风好大", ["今天风好大", NEUTRAL_REPLY]) is None


# ---- 服务两条发送口 -------------------------------------------------


async def test_batch_path_replaces_repeated_reply(tmp_path) -> None:
    brain = FakeBrain(BrainReply(True, "今天风好大，适合睡觉"))
    service, store, adapter = build(tmp_path, brain=brain)
    add_bot_message(store, "今天风好大，适合睡觉")

    sent = await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert [text for text, _ in sent] == [NEUTRAL_REPLY]
    assert adapter.sent == [(ChatType.GROUP, GROUP, NEUTRAL_REPLY)]


async def test_guard_skips_all_chunks_when_everything_repeats(tmp_path) -> None:
    brain = FakeBrain(BrainReply(True, "好的。"))
    service, store, adapter = build(tmp_path, brain=brain)
    add_bot_message(store, "好的")
    add_bot_message(store, NEUTRAL_REPLY, message_id="b2")

    sent = await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert sent == []
    assert adapter.sent == []


async def test_single_reply_path_uses_the_same_guard(tmp_path) -> None:
    brain = FakeBrain(BrainReply(True, "今天风好大"))
    service, store, adapter = build(tmp_path, brain=brain)
    add_bot_message(store, "今天风好大")

    await service._reply(make_msg())

    assert adapter.sent == [(ChatType.GROUP, GROUP, NEUTRAL_REPLY)]


async def test_guard_can_be_disabled(tmp_path) -> None:
    brain = FakeBrain(BrainReply(True, "今天风好大"))
    service, store, adapter = build(tmp_path, repeat_guard=False, brain=brain)
    add_bot_message(store, "今天风好大")

    await service._reply(make_msg())

    assert adapter.sent == [(ChatType.GROUP, GROUP, "今天风好大")]


# ---- 禁句删句 --------------------------------------------------------------


def test_strip_banned_sentences_drops_only_the_hit_sentence() -> None:
    text = "作为一个AI，我觉得挺好。今天热。"
    assert strip_banned_sentences(text, ["作为一个AI"]) == "今天热。"


def test_strip_banned_sentences_is_case_insensitive() -> None:
    assert strip_banned_sentences("As An AI, I think so.", ["as an ai"]) == ""


def test_strip_banned_sentences_without_phrases_returns_text() -> None:
    assert strip_banned_sentences("今天热。", []) == "今天热。"
    assert strip_banned_sentences("今天热。", ["  "]) == "今天热。"


def test_strip_banned_sentences_all_sentences_hit_returns_empty() -> None:
    assert strip_banned_sentences("作为一个AI。", ["作为一个AI"]) == ""


def test_strip_banned_sentences_keeps_neighbours_and_collapses_spaces() -> None:
    text = "先这样。\n笑死  真的。然后呢？"
    assert strip_banned_sentences(text, ["笑死"]) == "先这样。 然后呢？"


# ---- 行首内心 OS -----------------------------------------------------------


def test_has_leading_os_detects_both_bracket_styles() -> None:
    assert has_leading_os("（摸鱼）今天热")
    assert has_leading_os("  (thinking) 今天热")
    assert not has_leading_os("今天（真）热")
    assert not has_leading_os("")


def test_strip_leading_os_removes_only_the_prefix() -> None:
    assert strip_leading_os("（摸鱼）今天热") == "今天热"
    assert strip_leading_os("(thinking) hello") == "hello"
    assert strip_leading_os("（摸鱼），今天热") == "今天热"


def test_strip_leading_os_pure_os_becomes_empty() -> None:
    assert strip_leading_os("（摸鱼）") == ""
    assert strip_leading_os("(thinking)") == ""


def test_strip_leading_os_leaves_inline_brackets_alone() -> None:
    assert strip_leading_os("今天（真）热") == "今天（真）热"


# ---- 开场负例 --------------------------------------------------------------


def test_opening_fragments_takes_first_alnum_chars_and_dedupes() -> None:
    texts = ["今天好热啊", "今天又热了", "嗯", "😂😂"]
    assert opening_fragments(texts) == ["今天好", "今天又"]


def test_opening_fragments_skips_leading_punctuation_and_os() -> None:
    assert opening_fragments(["（摸鱼）今天热", "……行吧"]) == ["今天热", "行吧"]


def test_opening_fragments_respects_limit() -> None:
    texts = ["今天好热", "明天会冷", "后来下雨"]
    assert opening_fragments(texts, limit=2) == ["今天好", "明天会"]
    assert opening_fragments(texts, limit=0) == []


# ---- 内心 OS 频率抑制（发送链路）------------------------------------------


async def test_batch_reply_strips_leading_os_when_recently_used(tmp_path) -> None:
    brain = FakeBrain(BrainReply(True, "（摊手）我先撤"))
    service, store, adapter = build(tmp_path, brain=brain)
    add_bot_message(store, "（其实在摸鱼……）")

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.sent == [(ChatType.GROUP, GROUP, "我先撤")]


async def test_pure_os_chunk_is_dropped(tmp_path) -> None:
    brain = FakeBrain(BrainReply(True, "（摊手）"))
    service, store, adapter = build(tmp_path, brain=brain)
    add_bot_message(store, "（其实在摸鱼……）")

    sent = await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert sent == []
    assert adapter.sent == []


async def test_leading_os_is_kept_when_recently_unused(tmp_path) -> None:
    brain = FakeBrain(BrainReply(True, "（摊手）我先撤"))
    service, store, adapter = build(tmp_path, brain=brain)
    add_bot_message(store, "今天风好大")

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.sent == [(ChatType.GROUP, GROUP, "（摊手）我先撤")]


async def test_os_suppression_is_independent_of_repeat_guard(tmp_path) -> None:
    brain = FakeBrain(BrainReply(True, "（摊手）我先撤"))
    service, store, adapter = build(tmp_path, repeat_guard=False, brain=brain)
    add_bot_message(store, "（其实在摸鱼……）")

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.sent == [(ChatType.GROUP, GROUP, "我先撤")]


async def test_os_suppression_fails_open_when_store_raises(
    tmp_path, monkeypatch
) -> None:
    brain = FakeBrain(BrainReply(True, "（摊手）我先撤"))
    service, store, adapter = build(tmp_path, brain=brain)

    def boom(*args, **kwargs):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(store, "recent_bot_messages_in", boom)

    await service._send_batch_reply(
        [make_msg()], brain.reply_value, ChatType.GROUP, GROUP
    )

    assert adapter.sent == [(ChatType.GROUP, GROUP, "（摊手）我先撤")]
