from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from dafeiyu.models import ChatType, Message
from dafeiyu.router import Command, Router, parse_command

SHANGHAI = timezone(timedelta(hours=8))


class FakeClock:
    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, seconds: int) -> None:
        self.dt += timedelta(seconds=seconds)


class FakeRng:
    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


class FakeStore:
    def __init__(self) -> None:
        self.calls = 0
        self.tokens = 0
        self.msgs: list[Message] = []

    def usage_today(self, chat_type: ChatType, chat_id: int, today: str) -> tuple[int, int]:
        return self.calls, self.tokens

    def recent_messages(self, chat_type: ChatType, chat_id: int, limit: int) -> list[Message]:
        return self.msgs[-limit:]


def make_settings(
    *,
    sample_rate: float = 0.05,
    whitelist: tuple[int, ...] = (),
    blacklist: tuple[int, ...] = (),
    daily_calls: int = 200,
    quiet_hours: tuple[str, str] = ("01:00", "07:00"),
) -> SimpleNamespace:
    return SimpleNamespace(
        persona=SimpleNamespace(name="溟月", aliases=("大肥鱼", "小鲸鱼", "溟月", "鱼鱼")),
        triggers=SimpleNamespace(
            cooldown_seconds=60,
            sample_rate=sample_rate,
            quiet_hours=quiet_hours,
            keywords=("怎么", "为什么", "崩了"),
        ),
        groups=SimpleNamespace(whitelist=frozenset(whitelist), blacklist=frozenset(blacklist)),
        budget=SimpleNamespace(daily_calls=daily_calls, daily_tokens=1000),
    )


def make_router(**kwargs) -> tuple[Router, FakeStore, FakeClock]:
    store = FakeStore()
    clock = FakeClock(datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI))
    rng = kwargs.pop("rng", 0.0)
    router = Router(make_settings(**kwargs), store, clock=clock, rng=FakeRng(rng))
    return router, store, clock


def msg(
    content: str = "在吗",
    *,
    at: bool = False,
    reply: bool = False,
    chat_id: int = 1,
    chat_type: ChatType = ChatType.GROUP,
    is_bot: bool = False,
    timestamp: int = 0,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=2,
        nickname="阿伟",
        content=content,
        message_id="m1",
        segments=[],
        is_bot=is_bot,
        at_bot=at,
        reply_to_bot=reply,
        timestamp=timestamp,
    )


def test_at_bot_is_hard_trigger() -> None:
    router, _, _ = make_router()
    d = router.should_handle(msg(at=True))
    assert d.call_model is True
    assert d.hard is True


def test_alias_in_text_is_hard_trigger() -> None:
    router, _, _ = make_router()
    assert router.should_handle(msg("大肥鱼你在干嘛")).hard is True


def test_reply_to_bot_is_hard_trigger() -> None:
    router, _, _ = make_router()
    assert router.should_handle(msg("继续说", reply=True)).hard is True


def test_blacklist_group_never_replies() -> None:
    router, _, _ = make_router(blacklist=(1,))
    d = router.should_handle(msg(at=True))
    assert d.call_model is False
    assert d.reason == "blacklist"


def test_whitelist_blocks_unlisted_group_but_not_hard_trigger() -> None:
    router, _, _ = make_router(whitelist=(999,))
    plain = router.should_handle(msg("随便聊聊"))
    assert plain.call_model is False
    assert plain.reason == "whitelist"
    # 被 @ 时硬触发优先，白名单不再拦截
    assert router.should_handle(msg(at=True)).call_model is True


def test_cooldown_blocks_then_expires() -> None:
    router, _, clock = make_router()
    router.note_bot_spoke(ChatType.GROUP, 1)
    clock.advance(30)
    assert router.should_handle(msg("随便聊聊")).call_model is False
    clock.advance(31)
    assert router.should_handle(msg("随便聊聊")).call_model is True


def test_quiet_hours_block_non_hard() -> None:
    router, _, clock = make_router()
    clock.dt = datetime(2026, 10, 24, 2, 0, tzinfo=SHANGHAI)
    d = router.should_handle(msg("随便聊聊"))
    assert d.call_model is False
    assert d.reason == "quiet"


def test_budget_blocks_even_hard_trigger() -> None:
    router, store, _ = make_router(daily_calls=200)
    store.calls = 200
    d = router.should_handle(msg(at=True))
    assert d.call_model is False
    assert d.reason == "budget"


def test_sample_rate_allows_low_roll() -> None:
    router, _, _ = make_router(sample_rate=0.05, rng=0.01)
    d = router.should_handle(msg("随便聊聊"))
    assert d.call_model is True
    assert d.reason == "sample"


def test_downshift_halves_sample_rate() -> None:
    router, _, _ = make_router(sample_rate=0.2, rng=0.08)
    assert router.should_handle(msg("随便聊聊")).call_model is True
    for _ in range(3):
        router.note_no_reply(ChatType.GROUP, 1)
    # 降频后阈值 0.1：0.08 仍通过，0.15 被拒
    assert router.should_handle(msg("随便聊聊")).reason == "sample"
    router._rng = FakeRng(0.15)
    assert router.should_handle(msg("随便聊聊")).call_model is False


def test_question_keyword_passes_without_sampling() -> None:
    router, _, _ = make_router(sample_rate=0.0, rng=0.99)
    d = router.should_handle(msg("这个怎么弄"))
    assert d.call_model is True
    assert d.reason == "question"


def test_quiet_hours_wrap_across_midnight() -> None:
    router, _, clock = make_router(quiet_hours=("23:00", "07:00"))
    clock.dt = datetime(2026, 10, 24, 2, 0, tzinfo=SHANGHAI)
    assert router.is_quiet(ChatType.GROUP, 1, clock()) is True
    clock.dt = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)
    assert router.is_quiet(ChatType.GROUP, 1, clock()) is False


def test_mute_and_wake() -> None:
    router, _, _ = make_router()
    router.quiet(ChatType.GROUP, 1, minutes=10)
    assert router.should_handle(msg()).reason == "muted"
    router.wake(ChatType.GROUP, 1)
    assert router.should_handle(msg()).call_model is True


def test_parse_command() -> None:
    assert parse_command("/闭嘴") == Command("闭嘴", [])
    assert parse_command("/吃鱼 小鱼干 蛋糕") == Command("吃鱼", ["小鱼干", "蛋糕"])
    assert parse_command("你好") is None
    assert parse_command("/") is None
