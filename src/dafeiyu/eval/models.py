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


#: 离线链接桩支持的三种结局：HTTP 直接成功 / HTTP 失败后浏览器成功 / 两级都失败。
LINK_OUTCOMES: frozenset[str] = frozenset({"http_ok", "browser_ok", "unreadable"})


@dataclass(frozen=True)
class EvalLink:
    """一个 URL 在服务端两级链里的离线结局（绝不联网）。

    ``outcome=http_ok`` 时 HTTP 层成功；``browser_ok`` 表示 HTTP 层失败
    （``status`` 默认为 403）而浏览器层成功；``unreadable`` 表示两层都失败，
    提示词里应出现「服务器读不到」。
    """

    url: str
    outcome: str = "http_ok"
    status: int = 200
    title: str = ""
    text: str = ""


@dataclass(frozen=True)
class EvalBubble:
    """一条跨群转发消息在渲染层的两条断言：线上气泡与入库单行。

    ``wire`` 是完整的线上形态（可多行）；入库形态由旧版规则
    ``f"{nickname}：{content}"`` 现场算出，不在这里重复声明。
    """

    nickname: str
    content: str
    wire: str


@dataclass(frozen=True)
class EvalExpect:
    speak: bool | None = None
    min_chars: int | None = None
    max_chars: int | None = None
    must_contain_any: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    regex: str | None = None
    safety: bool = False
    #: 提示词必须全部包含 / 一个都不能包含的关键词（大小写不敏感）。
    prompt_must_contain: tuple[str, ...] = ()
    prompt_must_not_contain: tuple[str, ...] = ()
    #: 气泡渲染证据：wire 形态与旧版单行形态都在这条检查里核对。
    bubble: EvalBubble | None = None


@dataclass(frozen=True)
class EvalCase:
    id: str
    description: str = ""
    chat_type: str = "group"
    chat_id: str = ""
    context: EvalContext = field(default_factory=EvalContext)
    messages: tuple[EvalMessage, ...] = ()
    expect: EvalExpect = field(default_factory=EvalExpect)
    #: 本 case 里出现的 URL 在离线解析链里的结局；空表示不解析链接。
    links: tuple[EvalLink, ...] = ()


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
