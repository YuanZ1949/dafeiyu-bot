"""会话 id 的单一校验点：QQ 数字 id 与微信 ``wxg:`` 别名共用。"""
from __future__ import annotations

import re
from typing import Any

MAX_CHAT_ID_LENGTH = 128
#: 允许字母数字与 _ : . @ -；不含 /、空格、引号等路径/注入字符。
CHAT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_:.@\-]{1,128}$")


def is_valid_chat_id(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if not CHAT_ID_PATTERN.fullmatch(value):
        return False
    # 正则允许 '.'，但路径穿越必须单独拒掉。
    return ".." not in value


def normalize_chat_id(value: Any, *, label: str = "chat_id") -> str:
    """把请求里的 id 归一成字符串；非法时抛 ValueError（消息可直接回 400）。"""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} 格式非法：只允许 1-128 位字母、数字与 _ : . @ -")
    text = str(value).strip()
    if not is_valid_chat_id(text):
        raise ValueError(f"{label} 格式非法：只允许 1-128 位字母、数字与 _ : . @ -")
    return text
