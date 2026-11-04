"""回复的确定性后处理：重复句降级。

模型不会记得自己刚说过什么，同一句话在一小时内说三遍是常态。让它自己
检查既增加一轮调用，又不可靠；这里用纯函数做最后一道网，落点在
`shape_reply` / `segment_reply` 之后、`adapter.send` 之前。

判定：归一化（去标点/空白、casefold）后取 3-gram Jaccard，`> 0.6` 视为
重复。命中后只做确定性改写，**绝不重调模型**：

1. 先尝试去掉与旧发言相同的开头（剩下的部分仍自成一句时最自然）；
2. 否则换成一句中性短句；
3. 中性短句本身也重复时，这一片干脆不说（返回 None）。

守卫读不到历史时由调用方 fail-open，不能因为查库失败把正常回复也挡掉。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

# 与最近 50 条自己的发言比对：约等于「一小时内的口头禅」。
REPEAT_WINDOW = 50
# 3-gram Jaccard 阈值；句子越长越稳定，短句只有完全相同才会命中。
REPEAT_THRESHOLD = 0.6
# 去掉重复开头时，重叠至少这么长才值得改；剩余至少这么长才值得发。
MIN_OPENING_CHARS = 6
MIN_REST_CHARS = 4
# 命中后用来替换的中性短句；它自己也重复时会静默。
NEUTRAL_REPLY = "嗯，我在。"
_LEADING_PUNCT = "，,。.!！?？、;；:： \t\r\n\"'“”‘’（）()【】[]"

# ---- 禁句 / 内心 OS / 开场负例 -------------------------------------------
# 禁句按句处理：只删命中的整句，其余照发，既不重调模型也不制造新假话。
SENTENCE_ENDS = "。！？!?；;\n"
# 行首内心 OS：中/英括号包裹，最多 80 字，内容不跨行也不再嵌套括号。
# 与 sender._units 的口径一致：两种括号都算 OS。
_LEADING_OS_RE = re.compile(r"^[（(]([^（）()\r\n]{1,80})[）)]")
# 开场负例只取有辨识度的前几个实字，太短没有区分度。
OPENING_FRAGMENT_CHARS = 3
MIN_OPENING_FRAGMENT_CHARS = 2


def normalize(text: str) -> str:
    """去掉标点、空白并 casefold；只用于比较，不改变要发的文本。"""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in folded if char.isalnum())


def _trigrams(normalized: str) -> set[str]:
    if not normalized:
        return set()
    if len(normalized) < 3:
        # 太短的句子没有 3-gram，退化成「完全相同才算重复」。
        return {normalized}
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def similarity(left: str, right: str) -> float:
    """两个文本的 3-gram Jaccard 相似度，0..1。"""
    first = _trigrams(normalize(left))
    second = _trigrams(normalize(right))
    if not first or not second:
        return 0.0
    union = first | second
    if not union:
        return 0.0
    return len(first & second) / len(union)


def find_repeat(
    text: str, recent: Sequence[str], *, threshold: float = REPEAT_THRESHOLD
) -> str | None:
    """返回第一条与 `text` 足够相似的旧发言；没有返回 None。"""
    if not normalize(text):
        return None
    for previous in recent:
        if similarity(text, previous) > threshold:
            return previous
    return None


def trim_repeated_opening(
    text: str,
    recent: Sequence[str],
    *,
    min_prefix: int = MIN_OPENING_CHARS,
    min_rest: int = MIN_REST_CHARS,
) -> str:
    """去掉与旧发言相同的最长开头；没有可去掉的部分返回空串。

    只在「剩余部分仍像一句完整的话」时才动手：切完剩不到 `min_rest`
    字，就交给中性短句/静默处理，避免发出半截话。
    """
    stripped = text.strip()
    best = ""
    for previous in recent:
        other = previous.strip()
        limit = min(len(stripped), len(other))
        if limit < min_prefix:
            continue
        overlap = 0
        while (
            overlap < limit
            and stripped[overlap].casefold() == other[overlap].casefold()
        ):
            overlap += 1
        if overlap < min_prefix:
            continue
        rest = stripped[overlap:].lstrip(_LEADING_PUNCT)
        if len(rest) < min_rest:
            continue
        # 多个旧发言都能对上时，保留信息量最大的那个剩余部分。
        if len(rest) > len(best):
            best = rest
    return best


def polish_chunk(
    text: str,
    recent: Sequence[str],
    *,
    neutral: str = NEUTRAL_REPLY,
    threshold: float = REPEAT_THRESHOLD,
) -> str | None:
    """对一片待发文本做重复降级；返回 None 表示这一片不该发。"""
    chunk = text.strip()
    if not chunk:
        return None
    if find_repeat(chunk, recent, threshold=threshold) is None:
        return chunk
    trimmed = trim_repeated_opening(chunk, recent)
    if trimmed and find_repeat(trimmed, recent, threshold=threshold) is None:
        return trimmed
    if neutral and find_repeat(neutral, recent, threshold=threshold) is None:
        return neutral
    return None


def _split_sentences(text: str) -> list[str]:
    """按句末标点/换行切句，分隔符留在句尾。"""
    sentences: list[str] = []
    buffer: list[str] = []
    for char in text:
        buffer.append(char)
        if char in SENTENCE_ENDS:
            sentences.append("".join(buffer))
            buffer = []
    if buffer:
        sentences.append("".join(buffer))
    return sentences


def strip_banned_sentences(text: str, phrases: Sequence[str]) -> str:
    """删掉含禁句的整句，保留其余内容；全删返回空串。

    只做确定性降级：命中就整句丢掉，不改写、不补话。空短语会被忽略，
    全部为空时原样返回，方便调用方把「没配禁句」当成关闭。
    """
    body = text or ""
    folded = [phrase.casefold() for phrase in phrases if phrase and phrase.strip()]
    if not folded:
        return body
    kept = [
        sentence
        for sentence in _split_sentences(body)
        if not any(phrase in sentence.casefold() for phrase in folded)
    ]
    merged = "".join(kept).strip()
    return re.sub(r"[ \t\r\n]+", " ", merged).strip()


def has_leading_os(text: str) -> bool:
    """这段文本是否以内心 OS（行首中/英括号段）开头。"""
    return _LEADING_OS_RE.match((text or "").lstrip()) is not None


def strip_leading_os(text: str) -> str:
    """去掉行首的内心 OS 段；只动行首，句中的括号原样保留。

    没有行首 OS 时只做首尾空白清理，不碰任何正文标点。
    """
    body = (text or "").lstrip()
    match = _LEADING_OS_RE.match(body)
    if match is None:
        return body.strip()
    return body[match.end() :].lstrip(_LEADING_PUNCT).strip()


def opening_fragments(texts: Sequence[str], limit: int = 3) -> list[str]:
    """取每条发言的前几个实字，作为「别再这样开头」的负例。

    实字指 ``str.isalnum()`` 通过的字符：标点、空白、emoji 都不算。行首的
    内心 OS 先剥掉，否则负例会变成「摸鱼今」这种没有意义的三字串。片段
    太短（< 2 字）没有辨识度，直接丢弃；结果去重保序。
    """
    if limit <= 0:
        return []
    fragments: list[str] = []
    seen: set[str] = set()
    for text in texts:
        chars: list[str] = []
        for char in strip_leading_os(text):
            if char.isalnum():
                chars.append(char)
                if len(chars) >= OPENING_FRAGMENT_CHARS:
                    break
        fragment = "".join(chars)
        if len(fragment) < MIN_OPENING_FRAGMENT_CHARS:
            continue
        key = fragment.casefold()
        if key in seen:
            continue
        seen.add(key)
        fragments.append(fragment)
        if len(fragments) >= limit:
            break
    return fragments
