"""辩论账本：跨轮记住「已经让过的点」和「还没解决的分歧」。

只做确定性抽取，不额外调模型：

* **让步**：机器人这轮回复里出现「你说得对 / 这点我认 / 我同意 / 算你对」
  这类明确认账的措辞，且本批有对话对象 → 记一条 ``conceded``；
* **分歧**：回复里出现「但是 / 不过 / 不对 / 问题在于 / 我不同意」这类反驳
  措辞，且有对象 → 记一条 ``open``。

账本按时间衰减（默认 6 小时），同一对象同一类型只保留最新一条，最多 5 条。
注入时压成两三行，防止机器人下一轮把已经认过的点重新翻出来吵。

这里是纯函数层：不读库、不依赖 Service，方便单测与 eval 复用。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .models import Message

#: 账本条目存活时间：超过就当旧话题，不再注入。
LEDGER_TTL_SECONDS = 6 * 3600
#: 账本最多保留几条（让步 + 未解决合计）。
LEDGER_MAX = 5
#: 对方主张摘要的最大字数。
CLAIM_CHARS = 40

CONCEDE_MARKERS: tuple[str, ...] = (
    "你说得对",
    "你说得没错",
    "这点我认",
    "我认了",
    "这我承认",
    "我同意",
    "算你对",
    "有道理",
)
OPEN_MARKERS: tuple[str, ...] = (
    "但是",
    "不过",
    "不对",
    "问题在于",
    "我不同意",
    "我不认同",
    "恰恰相反",
    "你搞错",
)

#: 自动 @ 的触发词：回复里得真的有「在对人说」的意思，才补 @。
REBUTTAL_MARKERS: tuple[str, ...] = (
    "你",
    "您",
    "不对",
    "但是",
    "不过",
    "问题在于",
    "我不同意",
    "恰恰相反",
)


def _clip(text: str, limit: int = CLAIM_CHARS) -> str:
    clean = " ".join((text or "").replace("\n", " ").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "…"


def _load_entries(raw: str, now_ts: int) -> list[dict[str, Any]]:
    """解析并过滤账本：坏 JSON、过期、字段缺失一律丢掉，不抛异常。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        who = item.get("who")
        claim = item.get("claim")
        at = item.get("at")
        if kind not in ("conceded", "open") or not isinstance(who, str):
            continue
        if not isinstance(claim, str):
            continue
        if not isinstance(at, (int, float)):
            continue
        if now_ts - int(at) > LEDGER_TTL_SECONDS:
            continue
        entries.append(
            {
                "kind": kind,
                "who": who.strip(),
                "claim": _clip(claim),
                "at": int(at),
            }
        )
    return entries


def _dump_entries(entries: Sequence[dict[str, Any]]) -> str:
    return json.dumps(list(entries), ensure_ascii=False)


def pick_target(batch: Sequence[Message]) -> tuple[str, str] | None:
    """这一轮机器人主要在回谁：优先最后一条点名/引用消息，否则最后发言人。"""
    addressed = [
        m
        for m in batch
        if not m.is_bot
        and (m.at_bot or m.reply_to_bot)
        and (m.nickname or "").strip()
    ]
    pool = addressed or [m for m in batch if not m.is_bot]
    for message in reversed(pool):
        who = (message.nickname or "").strip()
        if who:
            return who, _clip(message.content)
    return None


def update_ledger(
    raw: str,
    batch: Sequence[Message],
    reply_text: str,
    now_ts: int,
) -> str:
    """根据这轮回复更新账本，返回新的 JSON 字符串。

    只有「明确认账」或「明确反驳」才写；普通接话不动账本，避免噪声。
    """
    entries = _load_entries(raw, now_ts)
    target = pick_target(batch)
    text = reply_text or ""
    if target:
        who, claim = target
        if any(marker in text for marker in CONCEDE_MARKERS):
            kind = "conceded"
        elif any(marker in text for marker in OPEN_MARKERS):
            kind = "open"
        else:
            kind = ""
        if kind:
            # 同一对象同一类型只留最新一条：旧主张已经被回应过，不再堆叠。
            entries = [
                item
                for item in entries
                if not (item["kind"] == kind and item["who"] == who)
            ]
            entries.append(
                {"kind": kind, "who": who, "claim": claim, "at": int(now_ts)}
            )
    entries = entries[-LEDGER_MAX:]
    if not entries:
        # 空账本存空串，别写 "[]"：否则每轮普通接话都会白写一次库。
        return ""
    return _dump_entries(entries)


def render_ledger(raw: str, now_ts: int) -> str:
    """把账本渲染成 prompt 块；没有有效条目返回空串。"""
    entries = _load_entries(raw, now_ts)
    if not entries:
        return ""
    conceded = [e for e in entries if e["kind"] == "conceded"]
    unresolved = [e for e in entries if e["kind"] == "open"]
    lines = ["【辩论账本】"]
    if conceded:
        lines.append(
            "已让步："
            + "；".join(f"{e['who']}「{e['claim']}」" for e in conceded)
        )
    if unresolved:
        lines.append(
            "未解决："
            + "；".join(f"{e['who']}「{e['claim']}」" for e in unresolved)
        )
    lines.append(
        "已经认过的点别再翻；未解决的分歧可以接着打，但先复述对方主张再反驳。"
    )
    return "\n".join(lines)


def pick_auto_at_target(batch: Sequence[Message], reply_text: str) -> str:
    """自动 @ 的候选昵称；不该补 @ 时返回空串。

    条件保守：群里至少两个发言人、回复里确实在对人说（第二人称/反驳词），
    才补一个 @。只有一个发言人时 @ 没意义，直接留空。
    """
    speakers = list(
        dict.fromkeys(
            (m.nickname or "").strip()
            for m in batch
            if not m.is_bot and (m.nickname or "").strip()
        )
    )
    if len(speakers) < 2:
        return ""
    text = reply_text or ""
    if not any(marker in text for marker in REBUTTAL_MARKERS):
        return ""
    addressed = [
        m
        for m in batch
        if not m.is_bot
        and (m.at_bot or m.reply_to_bot)
        and (m.nickname or "").strip()
    ]
    if addressed:
        return (addressed[-1].nickname or "").strip()
    for message in reversed(batch):
        who = (message.nickname or "").strip()
        if not message.is_bot and who:
            return who
    return ""
