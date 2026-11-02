"""运行时可调的「聊天强度」与「言论限制」。

这两个开关由控制面板写入，既影响本轮注入的提示，也影响主链路是否把
非点名批次送进模型、以及命中限制的回复是否放行。它们必须能在不重启
bot 的情况下生效，所以不放进启动时只读的 Settings，而是由 Service 持有
一份不可变快照，面板更新时整体替换。

注意两个边界：

* 「聊天强度」不是新的闸门系统：quiet 只决定非点名批次要不要送模型，
  active 只放开话题收尾/降频两个软闸门；静音、黑白名单、预算照旧。
* 「言论限制」是关键词级的最后一道网，不承诺语义级审核。真正的约束靠
  注入提示；strict 只是把命中的整条回复丢掉，宁可少说一句。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

INTENSITIES: tuple[str, ...] = ("quiet", "normal", "active")
RESTRICTION_MODES: tuple[str, ...] = ("off", "soft", "strict")

MAX_ITEMS = 100
MAX_ITEM_CHARS = 50

INTENSITY_LABELS = {"quiet": "安静", "normal": "正常", "active": "活跃"}
RESTRICTION_LABELS = {"off": "关闭", "soft": "提示模型", "strict": "严格拦截"}


class BehaviorError(ValueError):
    """面板/配置里的行为设置非法。消息给人看，不回显整段输入。"""


@dataclass(frozen=True)
class BehaviorSettings:
    """一份不可变的聊天行为快照。默认值 = 当前线上行为，零侵入。"""

    chat_intensity: str = "normal"
    restriction_mode: str = "off"
    banned_phrases: tuple[str, ...] = ()
    banned_topics: tuple[str, ...] = ()

    def framing(self) -> str:
        """本轮注入的附加说明；默认配置返回空串，不改变现有 prompt。"""
        lines: list[str] = []
        if self.chat_intensity == "quiet":
            lines.append(
                "【聊天强度：安静】除非被点名、被引用或必须纠正事实，"
                "否则这轮不要主动接话。"
            )
        elif self.chat_intensity == "active":
            lines.append(
                "【聊天强度：活跃】这轮可以更主动地接话；仍然只在有真看法时开口，"
                "不刷屏、不逐条应答。"
            )

        if self.restriction_mode != "off":
            if self.banned_phrases:
                lines.append(
                    "【言论限制】不要说出这些词或句子："
                    + "、".join(self.banned_phrases)
                    + "。"
                )
            if self.banned_topics:
                lines.append(
                    "【言论限制】不要讨论这些话题："
                    + "、".join(self.banned_topics)
                    + "。"
                )
            if self.banned_phrases or self.banned_topics:
                suffix = (
                    "违反的整条回复会被丢弃。"
                    if self.restriction_mode == "strict"
                    else ""
                )
                lines.append("以上是硬性限制，宁可不说也不要触碰。" + suffix)
        return "\n".join(lines)

    def match(self, text: str) -> str | None:
        """返回第一个命中的限制描述；未命中或限制关闭时返回 None。

        匹配是大小写不敏感的子串匹配：短词可能误伤（例如「死」命中
        「死机」），所以面板文案明确写「子串匹配」，由使用者自己控制粒度。
        """
        if self.restriction_mode == "off" or not text:
            return None
        folded = text.casefold()
        for phrase in self.banned_phrases:
            if phrase and phrase.casefold() in folded:
                return f"禁词「{phrase}」"
        for topic in self.banned_topics:
            if topic and topic.casefold() in folded:
                return f"禁话题「{topic}」"
        return None


def _choice(
    value: Any, allowed: tuple[str, ...], default: str, field_name: str
) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise BehaviorError(
            f"{field_name} 必须是字符串，可选值：{' / '.join(allowed)}。"
        )
    normalized = value.strip().casefold()
    if normalized not in allowed:
        raise BehaviorError(f"{field_name} 只支持：{' / '.join(allowed)}。")
    return normalized


def _items(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raise BehaviorError(f"{field_name} 必须是字符串列表。")

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, str):
            raise BehaviorError(f"{field_name} 的每一项都必须是字符串。")
        text = " ".join(item.replace("\r", " ").replace("\n", " ").split())
        if not text:
            continue
        if len(text) > MAX_ITEM_CHARS:
            raise BehaviorError(
                f"{field_name} 的每一项不能超过 {MAX_ITEM_CHARS} 字：{text[:20]}…"
            )
        key = text.casefold()
        if key in seen:
            continue
        if len(cleaned) >= MAX_ITEMS:
            raise BehaviorError(f"{field_name} 最多 {MAX_ITEMS} 项。")
        seen.add(key)
        cleaned.append(text)
    return tuple(cleaned)


def parse_behavior(raw: Mapping[str, Any] | None) -> BehaviorSettings:
    """把配置/面板 JSON 归一成 BehaviorSettings；非法输入抛 BehaviorError。"""
    if raw is None:
        return BehaviorSettings()
    if not isinstance(raw, Mapping):
        raise BehaviorError(
            "behavior 必须是映射（chat_intensity / restriction_mode / …）。"
        )
    intensity = _choice(
        raw.get("chat_intensity"), INTENSITIES, "normal", "behavior.chat_intensity"
    )
    # YAML 1.1 会把裸 off 解析成布尔 False；面板和文档都写字符串，但手改
    # config.yaml 的用户大概率会踩到，这里按「关闭」兼容。
    raw_mode = raw.get("restriction_mode")
    if raw_mode is False:
        raw_mode = "off"
    mode = _choice(raw_mode, RESTRICTION_MODES, "off", "behavior.restriction_mode")
    phrases = _items(raw.get("banned_phrases"), "behavior.banned_phrases")
    topics = _items(raw.get("banned_topics"), "behavior.banned_topics")
    return BehaviorSettings(
        chat_intensity=intensity,
        restriction_mode=mode,
        banned_phrases=phrases,
        banned_topics=topics,
    )


def as_payload(settings: BehaviorSettings) -> dict[str, Any]:
    """面板 API 的稳定响应结构。"""
    return {
        "chat_intensity": settings.chat_intensity,
        "restriction_mode": settings.restriction_mode,
        "banned_phrases": list(settings.banned_phrases),
        "banned_topics": list(settings.banned_topics),
        "intensity_label": INTENSITY_LABELS[settings.chat_intensity],
        "restriction_label": RESTRICTION_LABELS[settings.restriction_mode],
    }
