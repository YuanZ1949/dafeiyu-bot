import logging
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from dafeiyu.brain.client import BrainError
from dafeiyu.brain.tokens import estimate_tokens
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.workers.summary import (
    MAX_CHUNK_TOKENS,
    MAX_SOURCE_LINES,
    MAX_SPLIT_DEPTH,
    MAX_SUMMARY_CHARS,
    SummaryScheduler,
    _summary_budget,
    completed_weeks,
    summarize_chat,
    summarize_for_rotation,
    summarize_week,
    week_start_of,
)

TZ = timezone(timedelta(hours=8))
YESTERDAY = "2026-10-23"
NOW = datetime(2026, 10, 24, 4, 0, tzinfo=TZ)
BEFORE = datetime(2026, 10, 24, 3, 59, tzinfo=TZ)


class FakeBrain:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def complete(self, system, messages, max_tokens=400, temperature=0.2) -> str:
        self.calls.append({"system": system, "messages": messages})
        return self.reply


def make_store() -> Store:
    store = Store(":memory:")
    store.init_schema()
    return store


def seed(
    store: Store,
    ts: datetime,
    content: str = "今天聊了显卡",
    chat_id: int = 9001,
    *,
    nickname: str = "鱼片",
    message_id: str | None = None,
    reply_to_message_id: str | None = None,
    reply_to_user_id: int | None = None,
) -> None:
    store.add_message(
        Message(
            chat_type=ChatType.GROUP,
            chat_id=chat_id,
            user_id=42,
            nickname=nickname,
            content=content,
            message_id=message_id or f"m{int(ts.timestamp())}",
            reply_to_message_id=reply_to_message_id,
            reply_to_user_id=reply_to_user_id,
            timestamp=int(ts.timestamp()),
        )
    )


@pytest.mark.asyncio
async def test_summarize_truncates_to_200_and_stores():
    store = make_store()
    seed_many(store, 40)
    brain = FakeBrain("鱼" * 250)

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert len(text) == 200
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) == text
    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_summarize_cuts_at_last_sentence_boundary():
    store = make_store()
    seed_many(store, 40)
    kept = "甲" * 90 + "。" + "乙" * 100 + "。"
    brain = FakeBrain(kept + "丙" * 100)

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text == kept


@pytest.mark.asyncio
async def test_summarize_cuts_at_newline_boundary():
    store = make_store()
    seed_many(store, 40)
    kept = "甲" * 120
    brain = FakeBrain(kept + "\n" + "乙" * 120)

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text == kept


@pytest.mark.asyncio
async def test_summarize_hard_cut_marks_ellipsis_without_boundary():
    store = make_store()
    seed_many(store, 40)
    brain = FakeBrain("鱼" * 250)

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert len(text) == MAX_SUMMARY_CHARS
    assert text.endswith("…")


@pytest.mark.asyncio
async def test_summarize_ignores_boundary_in_first_half():
    store = make_store()
    seed_many(store, 40)
    brain = FakeBrain("好。" + "鱼" * 250)

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert len(text) == MAX_SUMMARY_CHARS
    assert text.startswith("好。")
    assert text.endswith("…")


@pytest.mark.asyncio
async def test_rotation_summary_cuts_at_sentence_boundary():
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    start = datetime(2026, 10, 23, tzinfo=TZ)
    messages = store.messages_between(
        ChatType.GROUP,
        9001,
        int(start.timestamp()),
        int((start + timedelta(days=1)).timestamp()),
    )
    kept = "甲" * 90 + "。" + "乙" * 100 + "。"
    brain = FakeBrain(kept + "丙" * 100)

    text = await summarize_for_rotation(brain, store, ChatType.GROUP, 9001, messages)

    assert text == kept


@pytest.mark.asyncio
async def test_summary_line_shows_reply_nickname():
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ), content="今天聊了显卡", nickname="阿猫", message_id="m1")
    seed(
        store,
        datetime(2026, 10, 23, 10, 1, tzinfo=TZ),
        content="这个怎么弄",
        message_id="m2",
        reply_to_message_id="m1",
        reply_to_user_id=7,
    )
    brain = FakeBrain("摘要")

    await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    body = _prompt_lines(brain.calls[0])
    assert body[1] == "[10:01] 鱼片: 这个怎么弄（回复 阿猫）"


@pytest.mark.asyncio
async def test_summary_line_falls_back_to_qq_when_nickname_missing():
    store = make_store()
    seed(
        store,
        datetime(2026, 10, 23, 10, 0, tzinfo=TZ),
        content="这个怎么弄",
        message_id="m2",
        reply_to_message_id="gone",
        reply_to_user_id=12345,
    )
    brain = FakeBrain("摘要")

    await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert _prompt_lines(brain.calls[0])[0].endswith("（回复 QQ12345）")


# ---- A2: 日摘要按当天消息量分档 ----

@pytest.mark.parametrize(
    ("count", "budget"),
    [(0, 150), (30, 150), (31, 200), (200, 200), (201, 300), (900, 300)],
)
def test_summary_budget_tiers(count: int, budget: int) -> None:
    assert _summary_budget(count) == budget


@pytest.mark.asyncio
async def test_quiet_day_uses_150_char_budget():
    store = make_store()
    seed_many(store, 10)
    brain = FakeBrain("鱼" * 400)

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert len(text) == 150
    assert "不超过 150 字" in brain.calls[0]["system"]


@pytest.mark.asyncio
async def test_normal_day_uses_200_char_budget():
    store = make_store()
    seed_many(store, 100)
    brain = FakeBrain("鱼" * 400)

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert len(text) == 200
    assert "不超过 200 字" in brain.calls[0]["system"]


@pytest.mark.asyncio
async def test_busy_day_uses_300_char_budget():
    store = make_store()
    seed_many(store, 300)
    brain = FakeBrain("鱼" * 400)

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert len(text) == 300
    assert "不超过 300 字" in brain.calls[0]["system"]


@pytest.mark.asyncio
async def test_rotation_summary_stays_at_200_chars():
    store = make_store()
    seed_many(store, 10)
    start = datetime(2026, 10, 23, tzinfo=TZ)
    messages = store.messages_between(
        ChatType.GROUP,
        9001,
        int(start.timestamp()),
        int((start + timedelta(days=1)).timestamp()),
    )
    brain = FakeBrain("鱼" * 400)

    text = await summarize_for_rotation(brain, store, ChatType.GROUP, 9001, messages)

    assert len(text) == MAX_SUMMARY_CHARS


@pytest.mark.asyncio
async def test_empty_day_does_not_call_brain():
    store = make_store()
    brain = FakeBrain("不该出现")

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text == ""
    assert brain.calls == []
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) is None


@pytest.mark.asyncio
async def test_scheduler_waits_until_hour():
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    brain = FakeBrain("摘要")
    scheduler = SummaryScheduler(store, brain, clock=lambda: BEFORE, hour=4)

    assert await scheduler.run_pending() == 0
    assert brain.calls == []


@pytest.mark.asyncio
async def test_scheduler_summarizes_yesterday_and_is_idempotent():
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    brain = FakeBrain("摘要")
    scheduler = SummaryScheduler(store, brain, clock=lambda: NOW, hour=4)

    assert await scheduler.run_pending() == 1
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) == "摘要"

    assert await scheduler.run_pending() == 0
    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_scheduler_ignores_day_without_messages():
    store = make_store()
    brain = FakeBrain("摘要")
    scheduler = SummaryScheduler(store, brain, clock=lambda: NOW, hour=4)

    assert await scheduler.run_pending() == 0
    assert brain.calls == []


@pytest.mark.asyncio
async def test_scheduler_uses_brain_from_getter_after_switch():
    """dashboard 换脑只改 service.brain，scheduler 必须每轮现取，不能啃旧引用。"""
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    old = FakeBrain("旧大脑")
    new = FakeBrain("新大脑")
    current = {"brain": old}
    scheduler = SummaryScheduler(
        store,
        old,
        clock=lambda: NOW,
        hour=4,
        brain_getter=lambda: current["brain"],
    )

    current["brain"] = new  # 模拟控制口换脑
    assert await scheduler.run_pending() == 1

    assert old.calls == []
    assert len(new.calls) == 1
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) == "新大脑"


class FailingBrain:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def complete(self, system, messages, max_tokens=400, temperature=0.2) -> str:
        self.calls += 1
        raise self.exc


class FlakyBrain:
    """第一次失败，之后成功。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system, messages, max_tokens=400, temperature=0.2) -> str:
        self.calls += 1
        if self.calls == 1:
            raise BrainError("provider.timeout: upstream timeout")
        return "恢复后的摘要"


@pytest.mark.asyncio
async def test_summarize_chat_provider_failure_warns_without_traceback(caplog):
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    brain = FailingBrain(BrainError("provider.quota: Free usage limit reached"))

    with caplog.at_level(logging.WARNING, logger="dafeiyu.workers.summary"):
        text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text is None
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) is None
    records = [r for r in caplog.records if "provider.quota" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert records[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_summarize_chat_unexpected_failure_keeps_traceback(caplog):
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    brain = FailingBrain(RuntimeError("boom"))

    with caplog.at_level(logging.ERROR, logger="dafeiyu.workers.summary"):
        text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text is None
    records = [r for r in caplog.records if "boom" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is not None


class TimeoutThenAnswer:
    """第一次超时，之后按顺序返回预设答案。"""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def complete(self, system, messages, max_tokens=400, temperature=0.2) -> str:
        self.calls.append({"system": system, "messages": messages})
        if len(self.calls) == 1:
            raise BrainError("OpenCode 超时（120 秒），本条放弃")
        return self.answers[min(len(self.calls) - 2, len(self.answers) - 1)]


def seed_day(store: Store, count: int = 4, chat_id: int = 9001) -> None:
    for i in range(count):
        seed(
            store,
            datetime(2026, 10, 23, 10 + i, 0, tzinfo=TZ),
            content=f"第{i + 1}条消息",
            chat_id=chat_id,
        )


def _prompt_text(call: dict) -> str:
    return call["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_timeout_splits_messages_in_half_then_merges():
    store = make_store()
    seed_day(store, 4)
    brain = TimeoutThenAnswer("前半摘要", "后半摘要", "合并后的摘要")

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text == "合并后的摘要"
    assert len(brain.calls) == 4  # 整段超时 + 两个半段 + 合并
    first_half = _prompt_text(brain.calls[1])
    second_half = _prompt_text(brain.calls[2])
    assert "第1条消息" in first_half and "第2条消息" in first_half
    assert "第3条消息" not in first_half
    assert "第3条消息" in second_half and "第4条消息" in second_half
    merge_prompt = _prompt_text(brain.calls[3])
    assert "前半摘要" in merge_prompt and "后半摘要" in merge_prompt
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) == text


@pytest.mark.asyncio
async def test_timeout_with_single_message_gives_up_without_split():
    store = make_store()
    seed_day(store, 1)
    brain = TimeoutThenAnswer("不该出现")

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text is None
    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_timeout_at_max_depth_gives_up_without_infinite_recursion():
    """切到深度上限仍超时就放弃；不会一路切成单行再无限重试。"""
    store = make_store()
    seed_day(store, 4)

    class AlwaysTimeout:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, max_tokens=400, temperature=0.2):
            self.calls += 1
            raise BrainError("OpenCode 超时（120 秒），本条放弃")

    brain = AlwaysTimeout()
    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text is None
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) is None
    # 整段 → 第一半 → 深度到顶的单行块，超时立刻上抛，不再尝试后半段。
    assert brain.calls <= 2 ** (MAX_SPLIT_DEPTH + 1)


@pytest.mark.asyncio
async def test_merge_timeout_falls_back_to_joined_halves():
    store = make_store()
    seed_day(store, 4)

    class MergeTimeout:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, max_tokens=400, temperature=0.2):
            self.calls += 1
            if self.calls in (1, 4):
                raise BrainError("OpenCode 超时（120 秒），本条放弃")
            return "前半" if self.calls == 2 else "后半"

    brain = MergeTimeout()
    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text == "前半\n后半"
    assert brain.calls == 4


@pytest.mark.asyncio
async def test_scheduler_backs_off_failed_chat_until_window_expires():
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
    brain = FlakyBrain()
    clock = {"t": 0.0}
    scheduler = SummaryScheduler(
        store,
        brain,
        clock=lambda: NOW,
        hour=4,
        backoff_seconds=60,
        monotonic=lambda: clock["t"],
    )

    assert await scheduler.run_pending() == 0  # 第一次失败
    assert brain.calls == 1
    assert await scheduler.run_pending() == 0  # 窗口内直接跳过
    assert brain.calls == 1

    clock["t"] = 60.0
    assert await scheduler.run_pending() == 1
    assert brain.calls == 2
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) == "恢复后的摘要"


# ---- B2/B5: 截断可见、按 token 分块、行首时间 ----

SUMMARY_HEADER = "以下是当天的群聊记录：\n"


def seed_many(
    store: Store,
    count: int,
    *,
    content: str | None = None,
    chat_id: int = 9001,
) -> None:
    base = datetime(2026, 10, 23, 0, 0, tzinfo=TZ)
    for i in range(count):
        seed(
            store,
            base + timedelta(seconds=i),
            content=content if content is not None else f"第{i + 1}条消息",
            chat_id=chat_id,
        )


def _prompt_lines(call: dict) -> list[str]:
    return _prompt_text(call).split(SUMMARY_HEADER, 1)[-1].splitlines()


class AlwaysTimeout:
    """每次都超时，用来钉住递归深度上限。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system, messages, max_tokens=400, temperature=0.2):
        self.calls += 1
        raise BrainError("OpenCode 超时（120 秒），本条放弃")


class FirstChunkTimeout:
    """前两次调用超时（整段 + 第一块），之后成功。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, system, messages, max_tokens=400, temperature=0.2):
        self.calls.append({"system": system, "messages": messages})
        if len(self.calls) <= 2:
            raise BrainError("OpenCode 超时（120 秒），本条放弃")
        return "递归摘要"


@pytest.mark.asyncio
async def test_truncation_notice_counts_hidden_messages():
    store = make_store()
    hidden = 5
    seed_many(store, MAX_SOURCE_LINES + hidden)
    brain = FakeBrain("摘要")

    await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    body = _prompt_lines(brain.calls[0])
    assert body[0] == f"（更早还有 {hidden} 条未展开）"
    assert len(body) == MAX_SOURCE_LINES + 1
    assert "第1条消息" not in body[1]
    assert "第6条消息" in body[1]
    assert "第405条消息" in body[-1]


@pytest.mark.asyncio
async def test_rotation_truncation_notice_counts_hidden_messages():
    store = make_store()
    hidden = 3
    seed_many(store, MAX_SOURCE_LINES + hidden)
    start = datetime(2026, 10, 23, tzinfo=TZ)
    messages = store.messages_between(
        ChatType.GROUP,
        9001,
        int(start.timestamp()),
        int((start + timedelta(days=1)).timestamp()),
    )
    brain = FakeBrain("摘要")

    await summarize_for_rotation(brain, store, ChatType.GROUP, 9001, messages)

    prompt = _prompt_text(brain.calls[0])
    assert f"（更早还有 {hidden} 条未展开）" in prompt


@pytest.mark.asyncio
async def test_summary_prompt_lines_start_with_shanghai_time():
    store = make_store()
    # UTC 00:30 → 上海 08:30，用来证明换算而不是照抄 UTC。
    seed(store, datetime(2026, 10, 23, 0, 30, tzinfo=UTC), content="今天聊了显卡")
    seed(store, datetime(2026, 10, 23, 0, 31, tzinfo=UTC), content="还聊了猫粮")
    brain = FakeBrain("摘要")

    await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    body = _prompt_lines(brain.calls[0])
    assert body == ["[08:30] 鱼片: 今天聊了显卡", "[08:31] 鱼片: 还聊了猫粮"]


@pytest.mark.asyncio
async def test_timeout_splits_by_tokens_and_retries_each_chunk():
    """整段超时后按 token 分块；第一块再超时还能继续细分，最终仍出摘要。"""
    store = make_store()
    seed_many(store, 6, content="鱼" * 600)  # 每行约 400 tokens，两块装不下
    brain = FirstChunkTimeout()

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text == "递归摘要"
    assert store.get_summary(ChatType.GROUP, 9001, YESTERDAY) == "递归摘要"
    assert len(brain.calls) > 2  # 整段超时 → 分块 → 块内再分 → 合并
    body_sizes = [len(_prompt_lines(call)) for call in brain.calls[1:]]
    assert min(body_sizes) == 1  # 真的细分到了单行块
    for call in brain.calls[1:]:
        body = "\n".join(_prompt_lines(call))
        assert estimate_tokens(body) <= MAX_CHUNK_TOKENS + 50


@pytest.mark.asyncio
async def test_split_depth_is_bounded_when_every_call_times_out():
    store = make_store()
    seed_many(store, 60, content="鱼" * 600)
    brain = AlwaysTimeout()

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text is None
    # 深度上限 2：1 次整段 + 每层少量尝试，远小于逐行重试的 60 次。
    assert brain.calls <= 2 ** (MAX_SPLIT_DEPTH + 1)


@pytest.mark.asyncio
async def test_single_overlong_line_gives_up_without_split():
    store = make_store()
    seed(store, datetime(2026, 10, 23, 10, 0, tzinfo=TZ), content="鱼" * 5000)
    brain = AlwaysTimeout()

    text = await summarize_chat(store, brain, ChatType.GROUP, 9001, YESTERDAY)

    assert text is None
    assert brain.calls == 1


# ---- B2: 周汇总 ----

MONDAY = datetime(2026, 10, 26, 4, 0, tzinfo=TZ)


def seed_week(
    store: Store,
    week_start: str = "2026-10-19",
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6),
    chat_id: int = 9001,
) -> None:
    start = date.fromisoformat(week_start)
    for offset in days:
        day = (start + timedelta(days=offset)).isoformat()
        store.upsert_summary(ChatType.GROUP, chat_id, day, f"第{offset + 1}天摘要")


def test_week_start_of_monday_and_sunday() -> None:
    assert week_start_of(date(2026, 10, 19)) == date(2026, 10, 19)
    assert week_start_of(date(2026, 10, 25)) == date(2026, 10, 19)


def test_completed_weeks_skips_current_week_and_crosses_year() -> None:
    assert completed_weeks(date(2026, 10, 24)) == ["2026-10-12", "2026-10-05"]
    assert completed_weeks(date(2026, 1, 5)) == ["2025-12-29", "2025-12-22"]


@pytest.mark.asyncio
async def test_summarize_week_merges_seven_daily_summaries():
    store = make_store()
    seed_week(store)
    brain = FakeBrain("这一周聊了显卡和猫")

    text = await summarize_week(store, brain, ChatType.GROUP, 9001, "2026-10-19")

    assert text == "这一周聊了显卡和猫"
    assert store.get_weekly_summary(ChatType.GROUP, 9001, "2026-10-19") == text
    prompt = _prompt_text(brain.calls[0])
    assert "10-19：第1天摘要" in prompt
    assert "10-25：第7天摘要" in prompt


@pytest.mark.asyncio
async def test_summarize_week_uses_only_existing_days():
    store = make_store()
    seed_week(store, days=(0, 1, 4))
    brain = FakeBrain("这周只聊了三天")

    await summarize_week(store, brain, ChatType.GROUP, 9001, "2026-10-19")

    prompt = _prompt_text(brain.calls[0])
    assert "10-19：第1天摘要" in prompt
    assert "10-20：第2天摘要" in prompt
    assert "10-23：第5天摘要" in prompt
    assert "第3天摘要" not in prompt


@pytest.mark.asyncio
async def test_summarize_week_skips_empty_week():
    store = make_store()
    brain = FakeBrain("不该出现")

    text = await summarize_week(store, brain, ChatType.GROUP, 9001, "2026-10-19")

    assert text == ""
    assert brain.calls == []
    assert store.get_weekly_summary(ChatType.GROUP, 9001, "2026-10-19") is None


@pytest.mark.asyncio
async def test_weekly_summary_clips_to_300_chars():
    store = make_store()
    seed_week(store)
    brain = FakeBrain("鱼" * 400)

    text = await summarize_week(store, brain, ChatType.GROUP, 9001, "2026-10-19")

    assert len(text) == 300


@pytest.mark.asyncio
async def test_scheduler_generates_weekly_summary_once():
    store = make_store()
    seed_week(store)
    brain = FakeBrain("上周回顾")
    scheduler = SummaryScheduler(store, brain, clock=lambda: MONDAY, hour=4)

    assert await scheduler.run_pending() == 1
    assert store.get_weekly_summary(ChatType.GROUP, 9001, "2026-10-19") == "上周回顾"

    assert await scheduler.run_pending() == 0
    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_weekly_failure_backs_off_without_writing():
    store = make_store()
    seed_week(store)
    brain = FlakyBrain()
    clock = {"t": 0.0}
    scheduler = SummaryScheduler(
        store,
        brain,
        clock=lambda: MONDAY,
        hour=4,
        backoff_seconds=60,
        monotonic=lambda: clock["t"],
    )

    assert await scheduler.run_pending() == 0
    assert brain.calls == 1
    assert store.get_weekly_summary(ChatType.GROUP, 9001, "2026-10-19") is None

    assert await scheduler.run_pending() == 0
    assert brain.calls == 1

    clock["t"] = 60.0
    assert await scheduler.run_pending() == 1
    assert brain.calls == 2
    assert store.get_weekly_summary(ChatType.GROUP, 9001, "2026-10-19") == "恢复后的摘要"

