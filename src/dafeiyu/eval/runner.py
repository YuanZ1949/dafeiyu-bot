"""执行 case：默认 FakeBrain 固定回复，异常按 case 隔离。"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from dafeiyu.brain.client import BrainReply
from dafeiyu.models import ChatType

from .checks import all_passed, run_checks
from .models import EvalCase, EvalCheck, EvalReport, EvalResult
from .prompt import build_prompt_async

FAKE_REPLY = "测试回复"


class FakeBrain:
    """默认假模型：固定回复、零网络、零 token。"""

    def __init__(self, reply: str = FAKE_REPLY, *, speak: bool = True) -> None:
        self.reply_text = reply
        self.speak = speak

    async def reply(
        self,
        chat_type: ChatType,
        chat_id: str,
        text: str,
        *,
        on_tool: Any = None,
    ) -> BrainReply:
        return BrainReply(
            speak=self.speak,
            reply=self.reply_text,
            reason="fake",
            prompt_tokens=0,
            completion_tokens=0,
        )

    def reset_session(self, chat_type: ChatType, chat_id: str) -> None:
        return None


async def _call_brain(
    brain: Any, chat_type: ChatType, chat_id: str, prompt: str
) -> BrainReply:
    result = brain.reply(chat_type, chat_id, prompt)
    if inspect.isawaitable(result):
        result = await result
    return result


async def run_case_async(
    case: EvalCase, brain: Any, mode: str = "fake"
) -> EvalResult:
    """跑一个 case；任何异常都转成失败结果，不中断整批。"""
    started = time.perf_counter()
    try:
        prompt = await build_prompt_async(case)
        reply = await _call_brain(
            brain, ChatType(case.chat_type), case.chat_id, prompt
        )
        checks = run_checks(case, reply, prompt)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return EvalResult(
            id=case.id,
            passed=all_passed(checks),
            checks=checks,
            reply=reply.reply,
            tokens=max(0, int(reply.prompt_tokens))
            + max(0, int(reply.completion_tokens)),
            latency_ms=latency_ms,
        )
    except Exception as exc:  # noqa: BLE001 - 单个 case 的坏输入不能拖垮整批
        latency_ms = int((time.perf_counter() - started) * 1000)
        message = f"{type(exc).__name__}: {exc}"
        return EvalResult(
            id=case.id,
            passed=False,
            checks=(EvalCheck("runner", False, message),),
            latency_ms=latency_ms,
            error=message,
        )


def run_case(case: EvalCase, brain: Any, mode: str = "fake") -> EvalResult:
    """同步入口：CLI 与测试都直接拿结果。"""
    return asyncio.run(run_case_async(case, brain, mode))


async def run_cases_async(
    cases: Iterable[EvalCase], brain: Any, mode: str = "fake"
) -> EvalReport:
    started = time.perf_counter()
    results = [await run_case_async(case, brain, mode) for case in cases]
    duration = time.perf_counter() - started
    passed = sum(1 for result in results if result.passed)
    return EvalReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        mode=mode,
        model=getattr(brain, "model", mode),
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        duration_seconds=duration,
        cases=tuple(results),
    )


def run_cases(
    cases: Iterable[EvalCase], brain: Any, mode: str = "fake"
) -> EvalReport:
    return asyncio.run(run_cases_async(cases, brain, mode))
