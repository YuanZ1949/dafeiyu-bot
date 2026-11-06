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


class CountingRng:
    """固定返回值 + 记调用次数：用来锁死「同一条消息只抽一次」。"""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return self.value


class FakeStore:
    def __init__(self) -> None:
        self.calls = 0
        self.tokens = 0
        self.msgs: list[Message] = []
        self.states: dict[tuple[ChatType, int], dict[str, object]] = {}

    def load_chat_state(self, chat_type: ChatType, chat_id: int) -> dict[str, object] | None:
        return self.states.get((chat_type, chat_id))

    def save_chat_state(
        self, chat_type: ChatType, chat_id: int, state: dict[str, object]
    ) -> None:
        self.states[(chat_type, chat_id)] = dict(state)

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
    closed_sample_rate: float = 0.05,
    whitelist: tuple[str, ...] = (),
    blacklist: tuple[str, ...] = (),
    daily_calls: int = 200,
    quiet_hours: tuple[str, str] = ("01:00", "07:00"),
    keywords: tuple[str, ...] = ("怎么", "为什么", "崩了"),
) -> SimpleNamespace:
    return SimpleNamespace(
        persona=SimpleNamespace(name="溟月", aliases=("大肥鱼", "小鲸鱼", "小菜鱼", "溟月", "鱼鱼")),
        triggers=SimpleNamespace(
            cooldown_seconds=60,
            sample_rate=sample_rate,
            closed_sample_rate=closed_sample_rate,
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
    chat_id: str = "qq:1",
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


def test_group_nickname_alias_counts_as_addressed() -> None:
    """微信群里 @ 的是群昵称「小菜鱼」：正文带 @ 也要命中别名。"""
    router, _, _ = make_router()
    assert router.addressed_source(msg("@小菜鱼 分析美股半导体")) == "alias"


def test_alias_as_vocative_is_addressed() -> None:
    router, _, _ = make_router()
    assert router.addressed_source(msg("大肥鱼，你怎么看")) == "alias"
    assert router.addressed_source(msg("大肥鱼在吗")) == "alias"
    assert router.addressed_source(msg("这事你怎么看，大肥鱼")) == "alias"


def test_alias_mention_is_not_addressed() -> None:
    """只是聊到 bot，不算点名，不强制回复。"""
    router, _, _ = make_router()
    text = msg("我刚才看到大肥鱼在别的地方冒泡了")
    assert router.addressed_source(text) is None
    assert router.alias_mention(text) is True
    assert router.should_handle(text).hard is False


def test_alias_vocative_is_not_a_mention() -> None:
    router, _, _ = make_router()
    assert router.alias_mention(msg("大肥鱼在吗")) is False


def test_reply_to_bot_is_hard_trigger() -> None:
    router, _, _ = make_router()
    assert router.should_handle(msg("继续说", reply=True)).hard is True


def test_blacklist_group_never_replies() -> None:
    router, _, _ = make_router(blacklist=("qq:1",))
    d = router.should_handle(msg(at=True))
    assert d.call_model is False
    assert d.reason == "blacklist"


def test_whitelist_blocks_unlisted_group_but_not_hard_trigger() -> None:
    router, _, _ = make_router(whitelist=("qq:999",))
    plain = router.should_handle(msg("随便聊聊"))
    assert plain.call_model is False
    assert plain.reason == "whitelist"
    # 被 @ 时硬触发优先，白名单不再拦截
    assert router.should_handle(msg(at=True)).call_model is True


def test_cooldown_blocks_then_expires() -> None:
    router, _, clock = make_router()
    router.note_bot_spoke(ChatType.GROUP, "qq:1")
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
        router.note_no_reply(ChatType.GROUP, "qq:1")
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
    router.note_bot_spoke(ChatType.GROUP, "qq:1")
    clock.advance(10)
    d = router.should_handle(msg("我问了你什么问题"))
    assert d.call_model is True
    assert d.reason == "question"


def test_cooldown_still_blocks_non_question() -> None:
    router, _, clock = make_router(sample_rate=1.0, rng=0.0)
    router.note_bot_spoke(ChatType.GROUP, "qq:1")
    clock.advance(10)
    d = router.should_handle(msg("随便聊聊"))
    assert d.call_model is False
    assert d.reason == "cooldown"


def test_cooldown_still_blocks_hot_group() -> None:
    router, store, clock = make_router()
    store.msgs = [
        msg("热闹", timestamp=int(clock().timestamp()) - 1) for _ in range(6)
    ]
    router.note_bot_spoke(ChatType.GROUP, "qq:1")
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
    assert router.is_quiet(ChatType.GROUP, "qq:1", clock()) is True
    clock.dt = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)
    assert router.is_quiet(ChatType.GROUP, "qq:1", clock()) is False


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
        "closed",
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
    state = router._state(ChatType.GROUP, "qq:1")
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
        router.note_no_reply(ChatType.GROUP, "qq:1")
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
    router.quiet(ChatType.GROUP, "qq:1", minutes=10)
    assert router.should_handle(msg()).reason == "muted"
    router.wake(ChatType.GROUP, "qq:1")
    assert router.should_handle(msg()).call_model is True


def test_parse_command() -> None:
    assert parse_command("/闭嘴") == Command("闭嘴", [])
    assert parse_command("/吃鱼 小鱼干 蛋糕") == Command("吃鱼", ["小鱼干", "蛋糕"])
    assert parse_command("你好") is None
    assert parse_command("/") is None


# ---- 话题状态机 -------------------------------------------------


def test_bot_message_going_unanswered_closes_thread() -> None:
    router, _, clock = make_router(closed_sample_rate=0.0)
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    clock.advance(300)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is False
    clock.advance(1)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is True
    decision = router.should_handle(msg("随便聊聊"))
    assert decision.call_model is False
    assert decision.reason == "closed"


def test_addressed_message_reopens_closed_thread() -> None:
    router, _, clock = make_router()
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    clock.advance(301)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is True
    router.note_user_message(ChatType.GROUP, "qq:1", addressed=True)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is False
    assert router.should_handle(msg("大肥鱼", at=True)).hard is True


def test_two_ambient_replies_close_thread() -> None:
    router, _, _ = make_router()
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is False
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is True
    assert router.should_handle(msg("在聊什么")).reason == "closed"


def test_closed_window_expires_and_allows_ambient_again() -> None:
    router, _, clock = make_router()
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is True
    clock.advance(600)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is False
    assert router.should_handle(msg("随便聊聊")).call_model is True


def test_ambient_turns_reset_after_addressed_message() -> None:
    router, _, _ = make_router()
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    router.note_user_message(ChatType.GROUP, "qq:1", addressed=True)
    assert router.ambient_turns(ChatType.GROUP, "qq:1") == 0
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    assert router.ambient_turns(ChatType.GROUP, "qq:1") == 1
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is False


def test_user_message_after_bot_keeps_thread_open() -> None:
    router, _, clock = make_router()
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    clock.advance(301)
    # 话题可能已经结束，但这条消息说明还有人接话：先记账再判定，不能关。
    router.note_user_message(ChatType.GROUP, "qq:1")
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is False


def close_thread(router) -> None:
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)


def test_mute_survives_restart() -> None:
    router, store, clock = make_router()
    router.quiet(ChatType.GROUP, "qq:1", 30)

    restarted = Router(make_settings(), store, clock=clock, rng=FakeRng(0.0))
    assert restarted.is_muted(ChatType.GROUP, "qq:1") is True


def test_closed_thread_survives_restart() -> None:
    router, store, clock = make_router()
    close_thread(router)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is True

    restarted = Router(make_settings(), store, clock=clock, rng=FakeRng(0.0))
    assert restarted.is_thread_closed(ChatType.GROUP, "qq:1") is True


def test_expired_closed_window_does_not_block_after_restart() -> None:
    router, store, clock = make_router()
    close_thread(router)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is True
    clock.advance(601)

    restarted = Router(make_settings(), store, clock=clock, rng=FakeRng(0.0))
    assert restarted.is_thread_closed(ChatType.GROUP, "qq:1") is False


def test_downshift_survives_restart() -> None:
    router, store, clock = make_router()
    for _ in range(3):
        router.note_no_reply(ChatType.GROUP, "qq:1")
    assert router.is_downshifted(ChatType.GROUP, "qq:1") is True

    restarted = Router(make_settings(), store, clock=clock, rng=FakeRng(0.0))
    assert restarted.is_downshifted(ChatType.GROUP, "qq:1") is True


def test_expired_downshift_does_not_block_after_restart() -> None:
    router, store, clock = make_router()
    for _ in range(3):
        router.note_no_reply(ChatType.GROUP, "qq:1")
    clock.advance(1801)

    restarted = Router(make_settings(), store, clock=clock, rng=FakeRng(0.0))
    assert restarted.is_downshifted(ChatType.GROUP, "qq:1") is False


def test_duplicate_suppression_survives_restart() -> None:
    router, store, clock = make_router()
    assert router.should_handle(msg("这游戏怎么又崩了")).call_model is True

    restarted = Router(make_settings(), store, clock=clock, rng=FakeRng(0.0))
    decision = restarted.should_handle(msg("这游戏怎么又崩了"))
    assert decision.call_model is False
    assert decision.reason == "duplicate"


def test_closed_thread_samples_once_in_rules_mode() -> None:
    rng = CountingRng(0.0)
    clock = FakeClock(datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI))
    router = Router(make_settings(), FakeStore(), clock=clock, rng=rng)
    close_thread(router)

    decision = router.should_handle(msg("随便聊聊"))

    assert decision.call_model is True
    assert rng.calls == 1


def test_closed_sample_uses_closed_rate() -> None:
    router, _, _ = make_router(rng=0.01, closed_sample_rate=0.05)
    close_thread(router)
    assert router.closed_sample(ChatType.GROUP, "qq:1") is True

    router, _, _ = make_router(rng=0.2, closed_sample_rate=0.05)
    close_thread(router)
    assert router.closed_sample(ChatType.GROUP, "qq:1") is False


def test_closed_sample_rate_zero_never_samples() -> None:
    router, _, _ = make_router(rng=0.0, closed_sample_rate=0.0)
    close_thread(router)
    assert router.closed_sample(ChatType.GROUP, "qq:1") is False


def test_persist_failure_does_not_break_state_updates() -> None:
    class BoomStore(FakeStore):
        def save_chat_state(self, chat_type, chat_id, state):
            raise RuntimeError("db down")

    clock = FakeClock(datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI))
    router = Router(make_settings(), BoomStore(), clock=clock, rng=FakeRng(0.0))
    router.note_bot_spoke(ChatType.GROUP, "qq:1", ambient=True)
    assert router.ambient_turns(ChatType.GROUP, "qq:1") == 1


def test_probe_does_not_persist() -> None:
    router, store, _clock = make_router()
    probe = router.clone(now=datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI))
    probe.quiet(ChatType.GROUP, "qq:1", 10)
    assert (ChatType.GROUP, "qq:1") not in store.states


def test_closed_sampled_message_reaches_model_in_rules_mode() -> None:
    router, _, _ = make_router(rng=0.0, closed_sample_rate=0.05)
    close_thread(router)
    assert router.is_thread_closed(ChatType.GROUP, "qq:1") is True

    decision = router.should_handle(msg("随便聊聊"))
    assert decision.call_model is True
    assert decision.reason == "closed"
