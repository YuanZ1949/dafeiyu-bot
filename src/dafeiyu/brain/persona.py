from __future__ import annotations

from pathlib import Path
from string import Template

DEFAULT_PERSONA_PATH = Path(__file__).with_name("persona.md")

SYSTEM_TEMPLATE = """$persona

## 当前场景

- 群名：$chat_title
- 今天：$today
- 字数上限：普通回复 $max_chars 字，技术问题 $max_chars_technical 字。
"""


def load_persona(path: Path | None = None) -> str:
    target = path or DEFAULT_PERSONA_PATH
    return target.read_text(encoding="utf-8").strip()


def build_system_prompt(
    persona_text: str,
    chat_title: str,
    today: str,
    max_chars: int,
    max_chars_technical: int,
) -> str:
    return Template(SYSTEM_TEMPLATE).substitute(
        persona=persona_text.strip(),
        chat_title=chat_title,
        today=today,
        max_chars=max_chars,
        max_chars_technical=max_chars_technical,
    )
