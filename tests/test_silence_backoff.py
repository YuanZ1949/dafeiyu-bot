"""冷场快反馈的纯函数判定：与 Service 解耦，T18 eval 可离线复用。

`silence_backoff_decision`（service.py 模块级）：连续 N「轮」无人接 →
非点名批次 hold M 分钟，窗口独立于 ``persona.hold_max_seconds``；
命中 `_known_silent_reason` 的弱批次返回 ``("none", "", age)``，
让既有 ``skip_known_silent`` 路径照常 ``mark_delivered``，省掉模型调用。
"""

from typing import Any

from dafeiyu.service import silence_backoff_decision


def decide(**overrides):
    kwargs: dict[str, Any] = dict(
        enabled=True,
        addressed=False,
        streak=3,
        threshold=3,
        age=100,
        window_seconds=900,
        known_silent=False,
    )
    kwargs.update(overrides)
    return silence_backoff_decision(**kwargs)


def test_cold_streak_holds_a_non_addressed_batch() -> None:
    assert decide() == ("hold", "silence_backoff", 100)


def test_age_past_the_window_releases() -> None:
    assert decide(age=899) == ("hold", "silence_backoff", 899)
    assert decide(age=900) == ("release", "silence_backoff_expired", 900)
    assert decide(age=9000) == ("release", "silence_backoff_expired", 9000)


def test_age_unknown_is_treated_as_not_expired() -> None:
    # timestamp 缺失不能当成 1970 年立刻到期，与 _hold_expired 同一口径。
    assert decide(age=None) == ("hold", "silence_backoff", None)


def test_streak_below_threshold_makes_no_decision() -> None:
    assert decide(streak=2) is None
    assert decide(streak=0) is None


def test_addressed_batches_never_backoff() -> None:
    assert decide(addressed=True) is None


def test_disabled_never_backoffs() -> None:
    assert decide(enabled=False) is None


def test_known_silent_wins_over_the_backoff() -> None:
    # 注定沉默的弱批次不进攒着：交给既有 skip_known_silent 路径 mark_delivered。
    assert decide(known_silent=True) == ("none", "", 100)
    assert decide(known_silent=True, age=1000) == ("none", "", 1000)


def test_known_silent_beats_a_cold_streak() -> None:
    assert decide(known_silent=True, age=1000) == ("none", "", 1000)
