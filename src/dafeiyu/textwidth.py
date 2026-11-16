"""文本宽度：显示列宽（纯 stdlib）与气泡对齐用的真实字体像素宽度。

``display_width`` / ``wrap_by_width`` / ``pad_to_width`` 只用 stdlib
``unicodedata``：东亚宽字符（W/F）记 2 列、组合字符记 0 列、其余记 1 列；
不引入 wcwidth 等第三方包。

``load_chat_metrics`` 额外可选依赖 Pillow：按聊天客户端字体（微软雅黑 /
PingFang / Noto CJK）的真实像素宽度对齐气泡。Pillow 或字体缺失时返回
``None``，调用方退回按显示列对齐。
"""

from __future__ import annotations

import os
import sys
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

# 断行优先选在这些标点之后，避免下一行以标点开头。
_BREAK_AFTER = frozenset("，。！？；：、）】》」』…,.;:!?)]}>")


def display_width(s: str) -> int:
    """字符串在等宽终端里的显示列数（东亚宽 2、组合字符 0、其余 1）。"""
    total = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            total += 2
        else:
            total += 1
    return total


def wrap_by_width(text: str, width: int) -> list[str]:
    """按显示宽度把 ``text`` 切成若干行；组合字符不与基字拆开。

    已有的换行先按行拆，各自包宽；每行尽量在标点后断，找不到就硬切。
    空行保留为空串，``width <= 0`` 时不切。
    """
    if width <= 0:
        return [text]
    lines: list[str] = []
    for segment in text.split("\n"):
        current: list[str] = []
        cols = 0
        for cluster in _clusters(segment):
            w = display_width(cluster)
            if current and cols + w > width:
                carried = _break_back(current)
                lines.append("".join(current[: len(current) - len(carried)]))
                current = carried
                cols = sum(display_width(part) for part in current)
            current.append(cluster)
            cols += w
        lines.append("".join(current))
    return lines


def pad_to_width(text: str, width: int) -> str:
    """用空格把 ``text`` 右侧补到给定显示宽度（宽度不足时不动）。"""
    return text + " " * (width - display_width(text))


def _clusters(segment: str) -> list[str]:
    """把 ``segment`` 切成不可拆的字符簇：基字 + 跟随的组合字符。"""
    clusters: list[str] = []
    for ch in segment:
        if unicodedata.combining(ch) and clusters:
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def _break_back(clusters: list[str]) -> list[str]:
    """把最后一个可断标点之后的簇挪到下一行；没有就硬切（返回空）。"""
    for index in range(len(clusters) - 1, 0, -1):
        if clusters[index - 1] in _BREAK_AFTER:
            return clusters[index:]
    return []


# ------------------------------------------------------- 真实字体像素宽度
#
# 气泡的顶/底栏是 ``─``、侧栏是 ``│``、补白是空格，中文是 CJK 宽度。比例字体
# 下这四种字符的像素宽度互不相同（雅黑 16px：``─``/``│`` 11px、空格 5px、
# CJK 16px），按显示列数补空格只能保证「列数相等」，右边框会错开几十像素。
# 这里用 Pillow 读真实字体度量，让调用方能按像素对齐。

_LINUX_CHAT_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)
_LINUX_FALLBACK_FONTS = (
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
)


def _font_candidates() -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """``(聊天字体候选, 缺字回退候选)``；只给路径，不检查存在性。"""
    if sys.platform == "win32":
        root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        chat = tuple(
            root / name
            for name in (
                "msyh.ttc",
                "msyhbd.ttc",
                "Deng.ttf",
                "simhei.ttf",
                "simsun.ttc",
            )
        )
        fallback = tuple(
            root / name
            for name in ("seguiemj.ttf", "seguihis.ttf", "seguisym.ttf")
        )
        return chat, fallback
    if sys.platform == "darwin":
        return (
            (
                Path("/System/Library/Fonts/PingFang.ttc"),
                Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
                Path("/System/Library/Fonts/STHeiti Medium.ttc"),
            ),
            (
                Path("/System/Library/Fonts/Apple Color Emoji.ttc"),
                Path("/System/Library/Fonts/Apple Symbols.ttf"),
                Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
            ),
        )
    return (
        tuple(Path(path) for path in _LINUX_CHAT_FONTS),
        tuple(Path(path) for path in _LINUX_FALLBACK_FONTS),
    )


@lru_cache(maxsize=16)
def _load_font(path: str, size: int) -> Any | None:
    try:
        from PIL import ImageFont
    except Exception:  # noqa: BLE001 - 没装 Pillow 就退回按列对齐
        return None
    try:
        return ImageFont.truetype(path, size)
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=16)
def _missing_reference(font: Any) -> tuple[tuple[int, int], bytes] | None:
    """缺字（.notdef）位图：用私用区码点取一份基准，逐字对比即可判断有无字形。

    字体连私有区都渲染不出来时返回 ``None``：调用方按「有字形」处理，
    宁可少修正几个像素，也不让字体异常炸掉整条镜像消息。
    """
    try:
        mask = font.getmask("\ue123")
        return (mask.size, bytes(mask))
    except Exception:  # noqa: BLE001
        return None


def _missing_glyph(font: Any, ch: str) -> bool:
    reference = _missing_reference(font)
    if reference is None:
        return False
    try:
        mask = font.getmask(ch)
    except Exception:  # noqa: BLE001 - 渲染失败当作「有字形」，不做修正
        return False
    return (mask.size, bytes(mask)) == reference


def _zero_width(ch: str) -> bool:
    """组合字符 / 变体选择符 / 零宽控制符：不占前进宽度，也不做缺字修正。"""
    if unicodedata.combining(ch):
        return True
    code = ord(ch)
    return (
        0x200B <= code <= 0x200F
        or 0x2060 <= code <= 0x206F
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
        or code == 0xFEFF
    )


class PixelMeasurer(Protocol):
    """只要求 ``width(text) -> int``：``PixelMetrics`` 与测试替身都按结构匹配。"""

    def width(self, text: str) -> int: ...


@dataclass
class PixelMetrics:
    """按真实字体度量像素宽度（气泡对齐用）。

    ``width`` 先整串测主字体，再逐字修正缺字宽度：``getlength`` 对缺字返回
    .notdef 的宽度（雅黑 16px 下 11px），而聊天客户端会用系统字体回退
    （emoji 走 Segoe UI Emoji 22px、圣书体走 Segoe UI Historic 16px）。
    不修正的话，含 emoji 的行仍会错开几像素。
    """

    primary: Any
    fallbacks: tuple[Any, ...] = ()
    _deltas: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def width(self, text: str) -> int:
        if not text:
            return 0
        try:
            total = float(self.primary.getlength(text))
        except Exception:  # noqa: BLE001 - 字体异常只降级，不炸渲染
            return 0
        # 逐字累加修正量：缺字按回退字体度量，变体选择符 / 零宽控制符从
        # .notdef 宽度里扣回去（渲染时它们不占位）。同一字符的增量有缓存。
        for ch in text:
            if _zero_width(ch):
                total -= self._advance(self.primary, ch)
            else:
                total += self._delta(ch)
        return int(round(total))

    @staticmethod
    def _advance(font: Any, ch: str) -> float:
        try:
            return float(font.getlength(ch))
        except Exception:  # noqa: BLE001
            return 0.0

    def _delta(self, ch: str) -> int:
        try:
            return self._deltas[ch]
        except KeyError:
            pass
        delta = 0
        if _missing_glyph(self.primary, ch):
            primary_width = self._advance(self.primary, ch)
            for font in self.fallbacks:
                if _missing_glyph(font, ch):
                    continue
                delta = int(round(self._advance(font, ch) - primary_width))
                break
        self._deltas[ch] = delta
        return delta


@lru_cache(maxsize=4)
def load_chat_metrics(size: int = 16) -> PixelMetrics | None:
    """加载聊天字体度量；没有 Pillow / 字体时返回 ``None``。

    字号只影响绝对像素，不影响同一字体内部的宽度比例；16px 与桌面微信默认
    字号一致，对齐误差不随客户端字号放大。
    """
    chat, fallback = _font_candidates()
    primary = None
    for path in chat:
        if path.is_file():
            primary = _load_font(str(path), size)
            if primary is not None:
                break
    if primary is None:
        return None
    loaded = tuple(
        font
        for font in (
            _load_font(str(path), size)
            for path in fallback
            if path.is_file()
        )
        if font is not None
    )
    return PixelMetrics(primary, loaded)
