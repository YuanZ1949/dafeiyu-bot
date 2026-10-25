"""token 估算：provider 不返回 usage 时的兜底记账。

OpenCode 的 message info 在部分模型/通道下不带 tokens，记账会退化成 0/0，
``daily_tokens`` 预算就形同虚设。这里按字符估算并让调用方标记 ``estimated``，
估算值不冒充真实用量。

系数取常见分词器的经验值：ASCII 约 4 字符/token，CJK 约 1.5 字符/token。
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_count = sum(1 for char in text if ord(char) < 128)
    wide_count = len(text) - ascii_count
    # ceil(ascii/4 + wide/1.5) = ceil((3*ascii + 8*wide) / 12)，全程整数运算。
    return (3 * ascii_count + 8 * wide_count + 11) // 12


def resolve_usage(
    prompt_text: str,
    output_text: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> tuple[int, int, bool]:
    """只补 0 的那一侧；返回 (prompt, completion, 是否包含估算值)。"""
    estimated = False
    if prompt_tokens <= 0 and prompt_text:
        prompt_tokens = estimate_tokens(prompt_text)
        estimated = True
    if completion_tokens <= 0 and output_text:
        completion_tokens = estimate_tokens(output_text)
        estimated = True
    return prompt_tokens, completion_tokens, estimated
