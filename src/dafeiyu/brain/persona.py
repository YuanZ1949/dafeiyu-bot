from __future__ import annotations

from pathlib import Path
from string import Template

DEFAULT_PERSONA_PATH = Path(__file__).with_name("persona.md")

# persona.md 与 .opencode/agents/deepseek-qq.md 里写死的默认值，改这里要同步那两处。
DEFAULT_OS_PROBABILITY = 0.05

# persona.md 里只对 opencode 成立的章节（查网页、记笔记、网页注入）。
TOOLS_START = "<!-- tools:start -->"
TOOLS_END = "<!-- tools:end -->"

NO_TOOLS_SECTION = """## 六、你现在没有工具

- 这一轮你只能聊天：不能读写工作区、不能上网查网页、不能执行命令。
- 被问到实时信息、链接内容或本地文件时，直说「这个本鱼现在查不了」，再凭已有知识回答，并说明可能过时。
- 不要假装看过网页、记过笔记或查过文件。"""

SYSTEM_TEMPLATE = """$persona

## 当前场景

- 群名：$chat_title
- 今天：$today
- 长度基调：普通闲聊通常 $max_chars 字上下，技术问题通常 $max_chars_technical 字上下。
  这只是参考，不是硬上限：内容需要就展开，别为凑长度硬撑，也别把答案腰斩。
- 想分条发就把 reply 写成字符串数组，每条一个元素；单条尽量不超过 $reply_hard_part 字。
"""


def load_persona(path: Path | None = None) -> str:
    target = path or DEFAULT_PERSONA_PATH
    return target.read_text(encoding="utf-8").strip()


def strip_tools_section(persona_text: str) -> str:
    """把「查网页 / 记笔记」章节换成「你现在没有工具」。

    这些章节是写给 opencode 的；本地模型和 token 模式没有任何工具，
    照原样注入会让她声称自己能上网、能翻文件。
    """
    start = persona_text.find(TOOLS_START)
    end = persona_text.find(TOOLS_END)
    if start < 0 or end < start:
        return persona_text
    head = persona_text[:start].rstrip()
    tail = persona_text[end + len(TOOLS_END) :].strip()
    parts = [head, NO_TOOLS_SECTION]
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)


def remove_tools_markers(persona_text: str) -> str:
    return persona_text.replace(TOOLS_START, "").replace(TOOLS_END, "")


def os_rule(probability: float) -> str:
    """把内心 OS 概率翻译成人设里的一句话（0 表示关闭）。"""
    if probability <= 0:
        return "不要主动冒内心 OS。"
    percent = f"{probability * 100:g}%"
    return (
        f"小概率（约 {percent}）在回答开头冒一句内心 OS，用中文括号，"
        "例如：（其实在摸鱼……）。频率要低，多了就腻。"
    )


def os_probability_hint(probability: float) -> str:
    """OpenCode 的人设文件写死了默认概率；配置偏离时补一行本轮覆盖。"""
    if abs(probability - DEFAULT_OS_PROBABILITY) < 1e-9:
        return ""
    if probability <= 0:
        return "[人设覆盖] 本轮不要主动使用内心 OS。"
    percent = f"{probability * 100:g}%"
    return f"[人设覆盖] 本轮内心 OS 概率约 {percent}，以本条为准。"


def build_system_prompt(
    persona_text: str,
    chat_title: str,
    today: str,
    max_chars: int,
    max_chars_technical: int,
    os_probability: float = DEFAULT_OS_PROBABILITY,
    *,
    tools_enabled: bool = True,
    reply_hard_part: int = 500,
) -> str:
    values = {
        "chat_title": chat_title,
        "today": today,
        "max_chars": str(max_chars),
        "max_chars_technical": str(max_chars_technical),
        "reply_hard_part": str(reply_hard_part),
        "os_rule": os_rule(os_probability),
    }
    # 人设本身也带占位符（$max_chars / $os_rule），必须先渲染再嵌入外层模板；
    # Template 不递归替换，直接 substitute 会把字面量漏进最终 prompt。
    # safe_substitute 容忍人设里的未知 $name，避免自定义人设把整轮打挂。
    source = (
        remove_tools_markers(persona_text)
        if tools_enabled
        else strip_tools_section(persona_text)
    )
    rendered = Template(source.strip()).safe_substitute(values)
    return Template(SYSTEM_TEMPLATE).substitute(persona=rendered, **values)
