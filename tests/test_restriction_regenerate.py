"""禁句 regenerate：先改写，最多 2 次；仍命中就删句，删空不发。"""

from __future__ import annotations

import asyncio

from dafeiyu import service as service_module
from dafeiyu.behavior import BehaviorSettings
from dafeiyu.brain.client import BrainReply
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


class RegenBrain:
    """reply 固定，complete_detailed 按脚本逐次返回。"""

    def __init__(self, reply_text: str, script: list[str] | None = None) -> None:
        self.reply_text = reply_text
        self.script = list(script or [])
        self.calls = 0

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        return BrainReply(True, self.reply_text)

    async def complete_detailed(self, system, messages) -> tuple[str, int, int]:
        self.calls += 1
        text = self.script.pop(0) if self.script else ""
        return (text, 12, 8)

    def reset_session(self, chat_type, chat_id) -> None:
        pass


class PlainBrain:
    """没有 complete_detailed 的老 provider。"""

    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        return BrainReply(True, "作为一个AI。今天热。")


def make_settings(
    *,
    mode: str = "regenerate",
    phrases: tuple[str, ...] = ("作为一个AI",),
) -> Settings:
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
        behavior=BehaviorSettings(restriction_mode=mode, banned_phrases=phrases),
    )


def build(tmp_path, brain, *, mode: str = "regenerate", phrases=("作为一个AI",)):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    adapter = FakeAdapter()
    router = Router(make_settings(mode=mode, phrases=phrases), store)
    service = Service(
        make_settings(mode=mode, phrases=phrases),
        store,
        brain,
        {"qq": adapter},
        router,
        "你是溟月",
        injection=InjectionConfig(),
    )
    return service, store, adapter


def make_msg(content: str = "在吗", *, message_id: str = "m1") -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=GROUP,
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        timestamp=0,
    )


async def test_first_clean_rewrite_is_used(tmp_path) -> None:
    brain = RegenBrain("作为一个AI，我觉得挺好。", ["我觉得挺好。"])
    service, _store, _adapter = build(tmp_path, brain)

    text = await service._apply_restrictions(
        ChatType.GROUP, GROUP, brain.reply_text, addressed=False
    )

    assert text == "我觉得挺好。"
    assert brain.calls == 1


async def test_retries_until_clean(tmp_path) -> None:
    brain = RegenBrain(
        "作为一个AI，挺好。", ["作为一个AI，挺好。", "挺好。"]
    )
    service, _store, _adapter = build(tmp_path, brain)

    text = await service._apply_restrictions(
        ChatType.GROUP, GROUP, brain.reply_text, addressed=True
    )

    assert text == "挺好。"
    assert brain.calls == 2


async def test_gives_up_after_two_attempts_and_strips_sentences(tmp_path) -> None:
    brain = RegenBrain("作为一个AI。今天热。", ["作为一个AI。", "作为一个AI。"])
    service, _store, _adapter = build(tmp_path, brain)

    text = await service._apply_restrictions(
        ChatType.GROUP, GROUP, brain.reply_text, addressed=False
    )

    assert text == "今天热。"
    assert brain.calls == 2


async def test_all_sentences_banned_means_silence(tmp_path) -> None:
    brain = RegenBrain("作为一个AI。", ["作为一个AI。", "作为一个AI。"])
    service, _store, adapter = build(tmp_path, brain)

    text = await service._apply_restrictions(
        ChatType.GROUP, GROUP, brain.reply_text, addressed=False
    )

    assert text is None
    assert brain.calls == 2

    sent = await service._send_batch_reply(
        [make_msg()], BrainReply(True, brain.reply_text), ChatType.GROUP, GROUP
    )
    assert sent == []
    assert adapter.sent == []


async def test_missing_complete_detailed_falls_back_to_stripping(tmp_path) -> None:
    service, _store, _adapter = build(tmp_path, PlainBrain())

    text = await service._apply_restrictions(
        ChatType.GROUP, GROUP, "作为一个AI。今天热。", addressed=False
    )

    assert text == "今天热。"


async def test_rewrite_usage_is_booked_as_reply(tmp_path) -> None:
    brain = RegenBrain("作为一个AI，挺好。", ["作为一个AI。", "挺好。"])
    service, store, _adapter = build(tmp_path, brain)

    await service._apply_restrictions(
        ChatType.GROUP, GROUP, brain.reply_text, addressed=True
    )

    calls, tokens = store.usage_today_by_purpose(
        ChatType.GROUP, GROUP, store.today(), ["reply"]
    )
    assert calls == 2
    assert tokens > 0


async def test_rewrite_usage_is_ambient_when_not_addressed(tmp_path) -> None:
    brain = RegenBrain("作为一个AI，挺好。", ["挺好。"])
    service, store, _adapter = build(tmp_path, brain)

    await service._apply_restrictions(
        ChatType.GROUP, GROUP, brain.reply_text, addressed=False
    )

    ambient, _ = store.usage_today_by_purpose(
        ChatType.GROUP, GROUP, store.today(), ["ambient"]
    )
    reply_calls, _ = store.usage_today_by_purpose(
        ChatType.GROUP, GROUP, store.today(), ["reply"]
    )
    assert ambient == 1
    assert reply_calls == 0


async def test_regenerate_end_to_end_sends_clean_text(tmp_path) -> None:
    brain = RegenBrain("作为一个AI，挺好。", ["作为一个AI，挺好。", "挺好。"])
    service, _store, adapter = build(tmp_path, brain)

    await service._send_batch_reply(
        [make_msg()], BrainReply(True, brain.reply_text), ChatType.GROUP, GROUP
    )

    assert adapter.sent == [(ChatType.GROUP, GROUP, "挺好。")]


async def test_timeout_falls_back_without_raising(tmp_path, monkeypatch) -> None:
    class SlowBrain(RegenBrain):
        async def complete_detailed(self, system, messages) -> tuple[str, int, int]:
            self.calls += 1
            await asyncio.sleep(0.1)
            return ("挺好。", 1, 1)

    service, _store, _adapter = build(tmp_path, SlowBrain("作为一个AI。今天热。"))
    monkeypatch.setattr(service_module, "REGENERATE_TIMEOUT_SECONDS", 0.01)

    text = await service._apply_restrictions(
        ChatType.GROUP, GROUP, "作为一个AI。今天热。", addressed=False
    )

    assert text == "今天热。"


async def test_strict_drops_without_calling_the_model(tmp_path) -> None:
    brain = RegenBrain("作为一个AI，挺好。", ["挺好。"])
    service, _store, _adapter = build(tmp_path, brain, mode="strict")

    text = await service._apply_restrictions(
        ChatType.GROUP, GROUP, brain.reply_text, addressed=False
    )

    assert text is None
    assert brain.calls == 0


async def test_soft_passes_through_without_calling_the_model(tmp_path) -> None:
    brain = RegenBrain("作为一个AI，挺好。", ["挺好。"])
    service, _store, _adapter = build(tmp_path, brain, mode="soft")

    text = await service._apply_restrictions(
        ChatType.GROUP, GROUP, brain.reply_text, addressed=False
    )

    assert text == "作为一个AI，挺好。"
    assert brain.calls == 0


async def test_neutral_reply_hitting_the_ban_is_dropped(tmp_path) -> None:
    brain = RegenBrain("今天风好大", [])
    service, store, adapter = build(
        tmp_path, brain, phrases=("嗯，我在。",)
    )
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=GROUP,
            user_id="qq:999",
            nickname="溟月",
            content="今天风好大",
            message_id="b1",
            is_bot=True,
            timestamp=0,
        )
    )

    sent = await service._send_batch_reply(
        [make_msg()], BrainReply(True, "今天风好大"), ChatType.GROUP, GROUP
    )

    assert sent == []
    assert adapter.sent == []
