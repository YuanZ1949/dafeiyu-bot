from __future__ import annotations

from pathlib import Path
from string import Template

DEFAULT_PERSONA_PATH = Path(__file__).with_name("persona.md")

# persona.md 与 .opencode/agents/deepseek-qq.md 里写死的默认值，改这里要同步那两处。
DEFAULT_OS_PROBABILITY = 0.05

SYSTEM_TEMPLATE = """$persona

## 当前场景

- 群名：$chat_title
- 今天：$today
- 字数上限：普通回复 $max_chars 字，技术问题 $max_chars_technical 字。
"""


def load_persona(path: Path | None = None) -> str:
    target = path or DEFAULT_PERSONA_PATH
    return target.read_text(encoding="utf-8").strip()


def os_rule(probability: float) -> str:
    """把内心 OS 概率翻译成人设里的一句话（0 表示关闭）。"""
    if probability <= 0:
        return "不要主动冒内心 OS。"
    percent = f"{probability * 100:g}%"
    return (
        f"小概率（约 {percent}）在回答开头冒一句内心 OS，用斜体加括号，"
        "例如：*（其实在摸鱼……）*。频率要低，多了就腻。"
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
) -> str:
    values = {
        "chat_title": chat_title,
        "today": today,
        "max_chars": str(max_chars),
        "max_chars_technical": str(max_chars_technical),
        "os_rule": os_rule(os_probability),
    }
    # 人设本身也带占位符（$max_chars / $os_rule），必须先渲染再嵌入外层模板；
    # Template 不递归替换，直接 substitute 会把字面量漏进最终 prompt。
    # safe_substitute 容忍人设里的未知 $name，避免自定义人设把整轮打挂。
    rendered = Template(persona_text.strip()).safe_substitute(values)
    return Template(SYSTEM_TEMPLATE).substitute(persona=rendered, **values)
