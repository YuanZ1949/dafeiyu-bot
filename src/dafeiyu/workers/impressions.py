from __future__ import annotations

import json
import logging

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType

log = logging.getLogger(__name__)

EXTRACT_SYSTEM = """\
你是群聊信息抽取器。下面每行是一条群消息，格式为 `user_id|昵称: 内容`。
抽取值得长期记住的群友信息：外号/称呼（kind=nickname）、偏好与雷点（kind=preference）、
稳定的事实（kind=fact，如职业、所在城市、养猫）。
不要抽取一次性情绪、玩笑、隐私敏感信息（身份证、住址门牌、手机号、密码）。
只输出 JSON 数组，不要解释、不要代码围栏：
[{"user_id": 123, "kind": "nickname", "content": "外号叫鱼片", "confidence": 0.8}]
content 不超过 60 字。没有值得记的就输出 []。"""

MAX_SOURCE_MESSAGES = 40
MAX_CONTENT_CHARS = 60
VALID_KINDS = {"nickname", "preference", "fact"}


def _strip_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_items(raw: str) -> list[dict]:
    try:
        data = json.loads(_strip_fence(raw))
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _valid_item(item: dict) -> tuple[int, str, str, float] | None:
    user_id = item.get("user_id")
    kind = item.get("kind")
    content = item.get("content")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    if not isinstance(kind, str) or kind not in VALID_KINDS:
        return None
    if not isinstance(content, str):
        return None
    content = content.strip()[:MAX_CONTENT_CHARS]
    if not content:
        return None
    try:
        confidence = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(1.0, max(0.0, confidence))
    return user_id, kind, content, confidence


class ImpressionWorker:
    """每 N 条消息（按会话计数）异步抽取一次印象，避免拖慢回复主链路。"""

    def __init__(self, store: Store, brain, every: int = 20) -> None:
        self.store = store
        self.brain = brain
        self.every = max(1, every)
        self._seen: dict[tuple[ChatType, int], int] = {}

    async def maybe_extract(self, chat_type: ChatType, chat_id: int) -> int:
        key = (chat_type, chat_id)
        count = self._seen.get(key, 0) + 1
        self._seen[key] = count
        if count % self.every:
            return 0
        return await self.extract(chat_type, chat_id)

    async def extract(self, chat_type: ChatType, chat_id: int) -> int:
        messages = self.store.recent_messages(chat_type, chat_id, MAX_SOURCE_MESSAGES)
        if not messages:
            return 0

        lines = "\n".join(f"{m.user_id}|{m.nickname}: {m.content}" for m in messages)
        try:
            raw = await self.brain.complete(
                EXTRACT_SYSTEM,
                [{"role": "user", "content": lines}],
                max_tokens=600,
            )
        except Exception:
            log.exception("印象抽取调用失败 %s/%s", chat_type.value, chat_id)
            return 0

        existing = {
            (im.subject_user_id, im.kind, im.content)
            for im in self.store.list_impressions(chat_type, chat_id, topk=10000)
        }
        added = 0
        for item in _parse_items(raw):
            parsed = _valid_item(item)
            if parsed is None:
                continue
            user_id, kind, content, confidence = parsed
            key = (user_id, kind, content)
            if key not in existing:
                existing.add(key)
                added += 1
            self.store.upsert_impression(
                chat_type, chat_id, user_id, kind, content, confidence
            )
        return added
