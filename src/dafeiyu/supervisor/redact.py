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
_HEADER_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?im)^([ \t]*)set-cookie\s*:[^\r\n]*"), r"\1Set-Cookie: ***"),
    (re.compile(r"(?im)^([ \t]*)authorization\s*:[^\r\n]*"), r"\1Authorization: ***"),
    (
        re.compile(r"(?im)^([ \t]*)x-control-token\s*:[^\r\n]*"),
        r"\1X-Control-Token: ***",
    ),
)

# 行内规则：Bearer 令牌、key: value / key=value、大写环境变量键。
_INLINE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?im)(Bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    (
        re.compile(
            r"(?im)\b(api[_-]?key|apikey|password|passwd|secret|token)\b"
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
    (
        re.compile(
            r'(?im)(["\'](?:api[-_]?key|apikey|password|passwd|secret|token'
            r'|authorization|set-cookie|x-control-token)["\']\s*:\s*)'
            r'(["\'])(?:\\.|[^\n])*?\2'
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
