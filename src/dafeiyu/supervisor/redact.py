"""日志 / 审计脱敏：纯字符串层面替换，不引入运行时依赖。

两个入口（``LogHub.write``、``AuditLog.write``）只调用 :func:`redact`，
不各自维护规则。规则按顺序执行，前一条整行规则会吃掉行尾，后面的
键值规则就不会再在残值上重复打码。

三个安全头（Set-Cookie / Authorization / X-Control-Token）整行替换为
规范头名 + ``***``：它们的值可能含空格、分号，必须按行尾收口；输出
用规范写法，避免同一份日志里混用 ``authorization`` / ``Authorization``。
其余键名（``api_key``、``Password``、``DEEPSEEK_API_KEY`` 等）保留原始
拼写，只把值换成 ``***``。
"""

from __future__ import annotations

import re

# 整行型规则：值可以含空格、分号、等号，一路吃到行尾（保留行终止符）。
# 头名后的空白只用 [ \t]*（不能用 \s*，否则可能跨行吃到下一行）。
_HEADER_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?im)^([ \t]*)set-cookie[ \t]*:[^\r\n]*"), r"\1Set-Cookie: ***"),
    (
        re.compile(r"(?im)^([ \t]*)authorization[ \t]*:[^\r\n]*"),
        r"\1Authorization: ***",
    ),
    (
        re.compile(r"(?im)^([ \t]*)x-control-token[ \t]*:[^\r\n]*"),
        r"\1X-Control-Token: ***",
    ),
)

# 行内规则：Bearer 令牌、key: value / key=value、大写环境变量键。
_INLINE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?im)(Bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    # 前置边界用 (?<![A-Za-z0-9])：\b 在 ``DEEPSEEK_API_KEY`` 里被下划线挡住，
    # 会漏掉带前缀的凭据；这里只禁止「紧贴字母数字」，允许 ``_`` / ``-`` 前缀。
    (
        re.compile(
            r"(?im)(?<![A-Za-z0-9])"
            r"(api[_-]?key|apikey|(?:secret|private|access)[-_]?key"
            r"|password|passwd|secret|token)\b"
            r"(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2***",
    ),
    (
        re.compile(
            r"(?im)\b([A-Z0-9_]*(?:API_KEY|PASSWORD|SECRET|TOKEN))(\s*=\s*)[^\s,;]+"
        ),
        r"\1\2***",
    ),
    # JSON 形态："token": "sk-1"（opencode 事件流常见）；引号与结构保持原样。
    # 引号值分支必须互斥（\\. 与 [^"\\\n]），否则 ``(?:\\.|[^\n])*?`` 会指数回溯。
    (
        re.compile(
            r'(?im)(["\'](?:[A-Za-z0-9]+[-_])*'
            r"(?:api[-_]?key|apikey|(?:secret|private|access)[-_]?key"
            r"|password|passwd|secret|token"
            r'|authorization|set-cookie|x-control-token)["\']\s*:\s*)'
            r'(["\'])(?:\\.|[^"\\\n])*?\2'
        ),
        r"\1\2***\2",
    ),
)

_RULES = _HEADER_RULES + _INLINE_RULES


def redact(text: str) -> str:
    """脱敏：返回替换后的字符串，原文无敏感内容时原样返回。"""
    result = text
    for pattern, replacement in _RULES:
        result = pattern.sub(replacement, result)
    return result
