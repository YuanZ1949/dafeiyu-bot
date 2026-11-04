"""微信入站媒体的纯本地处理工具（不联网、不引入新依赖）。"""

from __future__ import annotations

from .dat import decode_dat

__all__ = ["decode_dat"]
