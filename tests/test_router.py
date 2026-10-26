from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from dafeiyu.memory.store import UsageTotals
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

    def usage_between(
        self,
        start_date: str,
        end_date: str,
        *,
        purposes=None,
        chat_type=None,
        chat_id=None,
    ) -> UsageTotals:
        return UsageTotals(self.calls, 0, self.tokens)

    def usage_global_today(self, date: str) -> UsageTotals:
        return UsageTotals(0, 0, 0)

    def usage_month(self, month: str) -> UsageTotals:
        return UsageTotals(0, 0, 0)

    def recent_messages(self, chat_type: ChatType, chat_id: int, limit: int) -> list[Message]:
        return self.msgs[-limit:]


def make_settings(
    *,
    sample_rate: float = 0.05,
    whitelist: tuple[int, ...] = (),
    blacklist: tuple[int, ...] = (),
    daily_calls: int = 200,
    quiet_hours: tuple[str, str] = ("01:00", "07:00"),
    keywords: tuple[str, ...] = ("怎么", "为什么", "崩了"),
) -> SimpleNamespace:
    return SimpleNamespace(
        persona=SimpleNamespace(name="溟月", aliases=("大肥鱼", "小鲸鱼", "溟月", "鱼鱼")),
        triggers=SimpleNamespace(
            cooldown_seconds=60,
            sample_rate=sample_rate,
            quiet_hours=quiet_hours,
            keywords=keywords,
        ),
        groups=SimpleNamespace(whitelist=frozenset(whitelist), blacklist=frozenset(blacklist)),
        budget=SimpleNamespace(
            daily_calls=daily_calls,
            daily_tokens=1000,
            ambient_ratio=0.8,
            global_daily_calls=0,
            global_daily_tokens=0,
            monthly_calls=0,
            monthly_tokens=0,
            monthly_cost_limit=0.0,
            currency="CNY",
            price_input_per_million=0.0,
            price_output_per_million=0.0,
            warning_ratios=(0.5, 0.8, 1.0),
            on_exceed="degrade",
        ),
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
    assert router.should_handle(msg("随便聊聊甲")).call_model is True
    for _ in range(3):
        router.note_no_reply(ChatType.GROUP, 1)
    # 降频后阈值 0.1：0.08 仍通过，0.15 被拒
    assert router.should_handle(msg("随便聊聊乙")).reason == "sample"
    router._rng = FakeRng(0.15)
    assert router.should_handle(msg("随便聊聊丙")).call_model is False


def test_question_keyword_passes_without_sampling() -> None:
    router, _, _ = make_router(sample_rate=0.0, rng=0.99)
    d = router.should_handle(msg("这个怎么弄"))
    assert d.call_model is True
    assert d.reason == "question"


def test_question_bypasses_cooldown() -> None:
    """冷却只压抽样：刚说完话时，明确提问仍然要回。"""
    router, _, clock = make_router(
        sample_rate=0.0, rng=0.99, keywords=("怎么", "什么")
    )
    router.note_bot_spoke(ChatType.GROUP, 1)
    clock.advance(10)
    d = router.should_handle(msg("我问了你什么问题"))
    assert d.call_model is True
    assert d.reason == "question"


def test_cooldown_still_blocks_non_question() -> None:
    router, _, clock = make_router(sample_rate=1.0, rng=0.0)
    router.note_bot_spoke(ChatType.GROUP, 1)
    clock.advance(10)
    d = router.should_handle(msg("随便聊聊"))
    assert d.call_model is False
    assert d.reason == "cooldown"


def test_cooldown_still_blocks_hot_group() -> None:
    router, store, clock = make_router()
    store.msgs = [
        msg("热闹", timestamp=int(clock().timestamp()) - 1) for _ in range(6)
    ]
    router.note_bot_spoke(ChatType.GROUP, 1)
    clock.advance(10)
    d = router.should_handle(msg("随便聊聊"))
    assert d.call_model is False
    assert d.reason == "cooldown"


def test_question_mark_alone_counts_as_question() -> None:
    router, _, _ = make_router(sample_rate=0.0, rng=0.99)
    assert router.should_handle(msg("那你说?")).reason == "question"
    assert router.should_handle(msg("那你说？")).reason == "question"


def test_negated_keyword_is_not_a_question() -> None:
    router, _, _ = make_router(
        sample_rate=0.0, rng=0.99, keywords=("怎么", "什么")
    )
    d = router.should_handle(msg("没什么，就是路过"))
    assert d.call_model is False
    assert d.reason == "sample_skip"


def test_duplicate_question_within_cooldown_is_skipped() -> None:
    """冷却期内同一句话重复投递（并发/平台重发）不该回两遍。"""
    router, _, clock = make_router(
        sample_rate=0.0, rng=0.99, keywords=("怎么", "什么")
    )
    assert router.should_handle(msg("这个怎么弄")).call_model is True
    clock.advance(1)
    d = router.should_handle(msg("这个怎么弄"))
    assert d.call_model is False
    assert d.reason == "duplicate"
    # 换句话仍然算提问
    clock.advance(1)
    assert router.should_handle(msg("那这个呢怎么弄")).reason == "question"


def test_quiet_hours_wrap_across_midnight() -> None:
    router, _, clock = make_router(quiet_hours=("23:00", "07:00"))
    clock.dt = datetime(2026, 10, 24, 2, 0, tzinfo=SHANGHAI)
    assert router.is_quiet(ChatType.GROUP, 1, clock()) is True
    clock.dt = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)
    assert router.is_quiet(ChatType.GROUP, 1, clock()) is False


def test_explain_hard_trigger_has_ordered_checks_and_alias_source() -> None:
    router, _, _ = make_router()
    result = router.explain(msg("大肥鱼你在干嘛"))

    assert result["decision"] == {
        "call_model": True,
        "hard": True,
        "reason": "hard",
    }
    assert result["addressed"] is True
    assert result["hard_source"] == "alias"
    assert [check["key"] for check in result["checks"]] == [
        "self",
        "muted",
        "blacklist",
        "budget",
        "budget_global",
        "budget_monthly",
        "budget_cost",
        "hard",
        "whitelist",
        "quiet",
        "duplicate",
        "question",
        "cooldown",
        "hot",
        "sample",
    ]
    by_key = {check["key"]: check["status"] for check in result["checks"]}
    assert by_key["hard"] == "pass"
    assert by_key["whitelist"] == "skipped"
    assert result["metrics"]["sample_rate"] == 0.05


def test_explain_budget_marks_hard_check_skipped() -> None:
    router, store, _ = make_router(daily_calls=200)
    store.calls = 200

    result = router.explain(msg(at=True))

    assert result["decision"]["reason"] == "budget"
    by_key = {check["key"]: check["status"] for check in result["checks"]}
    assert by_key["budget"] == "blocked"
    assert by_key["hard"] == "skipped"


def test_explain_does_not_mutate_real_state_or_rng() -> None:
    class CountingRng:
        def __init__(self) -> None:
            self.calls = 0

        def random(self) -> float:
            self.calls += 1
            return 0.99

    router, _, clock = make_router(sample_rate=0.9, rng=0.99)
    rng = CountingRng()
    router._rng = rng
    state = router._state(ChatType.GROUP, 1)
    state.last_prompt = "旧问题"
    state.last_prompt_at = clock.dt
    state.no_reply_streak = 2
    before = copy.deepcopy(router._states)

    result = router.explain(msg("新问题？"), now=clock.dt, seed=3)

    assert result["decision"]["reason"] == "question"
    assert router._states == before
    assert rng.calls == 0


def test_explain_simulates_reply_reset_on_clone() -> None:
    router, _, clock = make_router()
    for _ in range(3):
        router.note_no_reply(ChatType.GROUP, 1)
    before = copy.deepcopy(router._states)

    result = router.explain(msg("继续", reply=True), now=clock.dt, seed=0)

    assert result["decision"]["hard"] is True
    assert result["hard_source"] == "reply"
    assert result["metrics"]["no_reply_streak"] == 0
    assert router._states == before


def test_explain_quiet_reason_reports_quiet_metrics() -> None:
    router, _, clock = make_router()
    clock.dt = datetime(2026, 10, 24, 2, 0, tzinfo=SHANGHAI)

    result = router.explain(msg("随便聊聊"), now=clock.dt)

    assert result["decision"]["reason"] == "quiet"
    assert result["metrics"]["in_quiet_hours"] is True
    by_key = {check["key"]: check["status"] for check in result["checks"]}
    assert by_key["quiet"] == "blocked"


def test_explain_sample_is_reproducible_with_seed() -> None:
    router, _, clock = make_router(sample_rate=0.5, rng=0.99)

    first = router.explain(msg("普通消息"), now=clock.dt, seed=1)
    second = router.explain(msg("普通消息"), now=clock.dt, seed=1)

    assert first["decision"]["reason"] == "sample"
    assert first["decision"] == second["decision"]
    assert first["metrics"] == second["metrics"]


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
