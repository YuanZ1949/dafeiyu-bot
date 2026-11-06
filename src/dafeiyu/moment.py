"""接话时机判断：这批消息值不值得叫醒主模型，以及靠什么信号判断。

这不是替代模型判断，而是给模型一个「外部时机」：批次到点不等于该说话。
规则只负责回答三个问题——

1. 有人在问问题吗（问句 / 求助词）？
2. 这批是不是纯表情、纯短附和、纯刷屏（没有可接的内容）？
3. 群是不是正聊得火热（这时插嘴最招人烦）？

聊天强度、降频这些既有闸门仍在 Service 里；这里只提供信号，不让模块
自己调模型或发消息。（话题收尾已降级为观测/提示信号，不再拦截。）
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .models import Message

QUESTION_MARKS = ("?", "？")
HELP_WORDS = (
    "怎么",
    "为什么",
    "为啥",
    "求",
    "有没有",
    "是不是",
    "怎么办",
    "报错",
    "崩了",
    "帮忙",
    "求助",
    "教教",
    "问下",
    "请教",
)
TECH_WORDS = (
    "代码",
    "报错",
    "错误",
    "异常",
    "函数",
    "接口",
    "编译",
    "部署",
    "配置",
    "日志",
    "数据库",
    "python",
    "javascript",
    "java",
    "api",
    "bug",
    "error",
    "traceback",
)
# 短到没有任何信息量的消息：表情、单字、纯 emoji、图片占位。
SHORT_CHARS = 4
# 长到足够表达一个观点，才有「接一句」的价值。
SUBSTANTIVE_CHARS = 12

HOT_WINDOW_SECONDS = 60
HOT_MIN_MESSAGES = 5

# 分数阈值只用于提示文案；真正的取不取消由 Service 的强度/降频闸门决定。
# 保留这些常量，方便以后在面板里做成可调项。
QUESTION_SCORE = 0.6
HELP_SCORE = 0.2
HOT_PENALTY = 0.25
SHORT_ONLY_PENALTY = 0.3
FOLLOW_UP_SCORE = 0.5


def is_question(text: str) -> bool:
    """问号结尾，或命中求助词（与 Router 的口径保持一致）。"""
    body = (text or "").strip()
    if not body:
        return False
    if body.endswith(QUESTION_MARKS):
        return True
    return any(word in body for word in HELP_WORDS)


def is_substantive(text: str) -> bool:
    body = (text or "").strip()
    return len(body) >= SUBSTANTIVE_CHARS


# 网关把表情包统一压成占位符：可能是旧的 `[表情]`，也可能带名字/md5 前缀；
# 图片物化成功后还会追加「（图存 ...）」注记，都要当成一个表情整体。
STICKER_PATTERN = re.compile(
    r"^\[表情(?::[^\]\n]*)?\](?:（图存 [^）\n]*）)?$"
)


def is_sticker_content(text: str) -> bool:
    """整条消息是否只是一个表情占位（含物化注记），没有别的文字。"""
    return bool(STICKER_PATTERN.fullmatch((text or "").strip()))


def is_short_only(messages: Sequence[Message]) -> bool:
    """整批都是「哦/哈哈/[图片]」这类没有信息量的人话。"""
    humans = [m for m in messages if not m.is_bot and m.content.strip()]
    if not humans:
        return True
    # 带名字的表情占位（[表情:狗头]）比 4 字长，但同样没有可接的内容。
    return all(
        is_sticker_content(m.content) or len(m.content.strip()) <= SHORT_CHARS
        for m in humans
    )


@dataclass(frozen=True)
class MomentSignals:
    question: bool
    help: bool
    hot: bool
    short_only: bool
    technical: bool
    followed_up: bool
    addressed: bool
    score: float
    pending_loop: str = ""
    # 这批人话里有没有纯表情占位：表情本身信息少，但可能是态度。
    sticker: bool = False

    def hint(self) -> str:
        """注入给模型的一段「时机」说明；没有信号时返回空串。"""
        if self.followed_up:
            return "【时机】对方直接接了你上一句，可以自然回应，别装没看见。"
        if self.addressed:
            # 被点名时不加「别插嘴」类提示，否则会自相矛盾。
            return ""
        if self.question and self.hot:
            return "【时机】有人提问，但群里正聊得热；先判断有没有把握答准，没把握就别插。"
        if self.question:
            return "【时机】群里有人提问；如果你有明确答案可以接，答不上来就别说。"
        if self.pending_loop:
            return f"【时机】这个群还有件事没下文：{self.pending_loop}。接得上就自然问一句，接不上别硬提。"
        if self.short_only:
            if self.sticker:
                return (
                    "【时机】这批多是表情/附和，信息不多；"
                    "若你有同感可简短接一句，不必硬找话题。"
                )
            return "【时机】这批没什么可接的内容，倾向不说。"
        if self.hot:
            return "【时机】群里正聊得热，除非你有明确观点，否则别插嘴。"
        if self.technical:
            return "【时机】话题偏技术；先想清楚再开口，结论优先。"
        return ""

    @property
    def worth_reopen(self) -> bool:
        """降频窗口内是否值得为它重新开口：只认明确的问句/求助/接话。"""
        return self.question or self.help or self.followed_up


def analyze(
    messages: Sequence[Message],
    *,
    now_ts: int,
    addressed: bool = False,
    followed_up: bool = False,
    pending_loop: str = "",
) -> MomentSignals:
    """从一批消息里提取时机信号；纯函数，方便单测和回放。"""
    humans = [m for m in messages if not m.is_bot and m.content.strip()]
    question = any(is_question(m.content) for m in humans)
    help_hit = any(
        any(word in m.content for word in HELP_WORDS) for m in humans
    )
    technical = any(
        any(word in m.content.casefold() for word in TECH_WORDS) for m in humans
    )
    recent = [m for m in humans if now_ts - m.timestamp <= HOT_WINDOW_SECONDS]
    hot = len(recent) >= HOT_MIN_MESSAGES and not addressed
    short_only = is_short_only(messages)
    sticker = any(is_sticker_content(m.content) for m in humans)

    score = 0.0
    if addressed:
        score += 1.0
    if question:
        score += QUESTION_SCORE
    if help_hit:
        score += HELP_SCORE
    if followed_up:
        score += FOLLOW_UP_SCORE
    if hot:
        score -= HOT_PENALTY
    if short_only:
        score -= SHORT_ONLY_PENALTY
    if technical:
        score += 0.1

    return MomentSignals(
        question=question,
        help=help_hit,
        hot=hot,
        short_only=short_only,
        technical=technical,
        followed_up=followed_up,
        addressed=addressed,
        score=round(score, 3),
        pending_loop=pending_loop,
        sticker=sticker,
    )


def reopened_by_question(text: str) -> bool:
    """降频窗口里，只有明确的问句/求助值得重新叫醒模型。"""
    return is_question(text)


# 「接着说」类请求：既不是问句，也不带求助词，但明确是在等 bot 说完。
# 降频窗口里如果把它当普通闲聊吞掉，用户就会看到「说一半没下文」。
CONTINUE_PREFIXES = ("继续", "接着", "说下去", "别停")
CONTINUE_PHRASES = (
    "继续说",
    "继续说下去",
    "接着说",
    "说下去",
    "别停",
    "然后呢",
    "还有呢",
    "讲完",
    "说完",
    # 「点评/说说/细说」是明确的接话请求：降频窗口里吞掉，用户只会看到
    # 「我问了它却不回」（11-06 20:46「里面高赞的说法你点评一下」实测踩过）。
    "点评",
    "说说",
    "细说",
    "展开讲",
    # 直接抱怨没回：这是最强烈的叫醒信号，必须重新交给模型。
    "不回",
    "没回",
    "咋不",
    "理我",
    "说话",
)
CONTINUE_MAX_CHARS = 30


def requests_continuation(text: str) -> bool:
    """短句里明确要求「接着说」，降频窗口里也算重新叫醒模型。"""
    body = (text or "").strip()
    if not body or len(body) > CONTINUE_MAX_CHARS:
        return False
    return body.startswith(CONTINUE_PREFIXES) or any(
        phrase in body for phrase in CONTINUE_PHRASES
    )


def answers_bot_question(
    current: Message, previous: Message | None, *, max_gap_seconds: int = 180
) -> bool:
    """上一条是 bot 的提问、当前这条像在回答它：算隐式点名。

    只认「紧挨着」的一条：中间又有人插话时，当前消息更可能是在接那个人的话。
    长度放宽到 40 字，避免「4060」「对」「不行」这类短答被漏掉。
    """
    if previous is None or not previous.is_bot or current.is_bot:
        return False
    if (
        max_gap_seconds
        and current.timestamp
        and previous.timestamp
        and current.timestamp - previous.timestamp > max_gap_seconds
    ):
        return False
    if not is_question(previous.content):
        return False
    body = (current.content or "").strip()
    return 0 < len(body) <= 40


def follows_bot_expression(
    current: Message, previous: Message | None, *, max_gap_seconds: int = 180
) -> bool:
    """表情紧跟 bot 发言 = 弱回应：不是点名，但值得当作「在跟你说话」看。

    只认「上一条是 bot、这一条是纯表情、间隔不超过 180 秒」的紧邻关系。
    中间隔了别人、或者表情不是紧跟 bot 的，都只当普通刷屏。
    """
    if previous is None or not previous.is_bot or current.is_bot:
        return False
    if (
        max_gap_seconds
        and current.timestamp
        and previous.timestamp
        and current.timestamp - previous.timestamp > max_gap_seconds
    ):
        return False
    return is_sticker_content(current.content)
