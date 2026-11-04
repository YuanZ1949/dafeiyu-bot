"""离线评测：默认假模型，读 YAML case，跑确定性检查并出报告。"""

from __future__ import annotations

from .loader import EvalError, load_cases
from .models import (
    EvalCase,
    EvalCheck,
    EvalContext,
    EvalExpect,
    EvalMessage,
    EvalReport,
    EvalResult,
)
from .recorder import record_cases, write_cases
from .runner import (
    FakeBrain,
    run_case,
    run_case_async,
    run_cases,
    run_cases_async,
)

__all__ = [
    "EvalCase",
    "EvalCheck",
    "EvalContext",
    "EvalError",
    "EvalExpect",
    "EvalMessage",
    "EvalReport",
    "EvalResult",
    "FakeBrain",
    "load_cases",
    "record_cases",
    "run_case",
    "run_case_async",
    "run_cases",
    "run_cases_async",
    "write_cases",
]
