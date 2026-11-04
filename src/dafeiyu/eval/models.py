"""评测领域模型：case、单条结果、整份报告。

字段与 spec §3.2/§3.5 对齐；只在评测进程内使用，不入库。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalMessage:
    user_id: str = ""
    nickname: str = ""
    content: str = ""
    at_bot: bool = False
    reply_to_bot: bool = False
    timestamp: int = 0


@dataclass(frozen=True)
class EvalContext:
    summary: str = ""
    impressions: tuple[str, ...] = ()
    continuation: str | None = None


@dataclass(frozen=True)
class EvalExpect:
    speak: bool | None = None
    min_chars: int | None = None
    max_chars: int | None = None
    must_contain_any: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    regex: str | None = None
    safety: bool = False


@dataclass(frozen=True)
class EvalCase:
    id: str
    description: str = ""
    chat_type: str = "group"
    chat_id: str = ""
    context: EvalContext = field(default_factory=EvalContext)
    messages: tuple[EvalMessage, ...] = ()
    expect: EvalExpect = field(default_factory=EvalExpect)


@dataclass(frozen=True)
class EvalCheck:
    name: str
    passed: bool
    detail: str = ""
    # 安全类失败只做保守提示，不假装能替代人工审核。
    needs_review: bool = False


@dataclass(frozen=True)
class EvalResult:
    id: str
    passed: bool
    checks: tuple[EvalCheck, ...] = ()
    reply: str = ""
    tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    # runner 捕获到异常时留痕；有值即视为本 case 失败。
    error: str = ""


@dataclass(frozen=True)
class EvalReport:
    generated_at: str
    mode: str
    model: str
    total: int
    passed: int
    failed: int
    duration_seconds: float
    cases: tuple[EvalResult, ...] = ()
