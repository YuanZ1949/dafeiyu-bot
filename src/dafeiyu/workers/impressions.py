from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from dafeiyu.brain.tokens import resolve_usage
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message

from .failure import FAILURE_BACKOFF_SECONDS, FailureBackoff, log_memory_failure

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
# 抽取窗口在游标之后的新消息前面再带几条上文，帮助模型理解指代；
# 上限固定，避免每次把上一批消息整段重喂。
CONTEXT_MESSAGES = 10
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
    """每 N 条消息（按会话计数）异步抽取一次印象，避免拖慢回复主链路。

    每轮只喂游标之后的新消息，外加最多 CONTEXT_MESSAGES 条上文；
    手动触发（不带 messages）仍取最近 MAX_SOURCE_MESSAGES 条。
    """

    def __init__(
        self,
        store: Store,
        brain,
        every: int = 20,
        backoff_seconds: float = FAILURE_BACKOFF_SECONDS,
        monotonic=time.monotonic,
        brain_getter: Callable[[], Any] | None = None,
        backoff: FailureBackoff | None = None,
    ) -> None:
        self.store = store
        self.brain = brain
        self.every = max(1, every)
        self._last_count: dict[tuple[ChatType, int], int] = {}
        self._backoff = backoff or FailureBackoff(backoff_seconds, monotonic)
        # dashboard 换脑只更新 Service.brain，拿不到 worker 手里的引用；
        # 有 getter 就每次现取，否则印象抽取会一直用启动时的 provider。
        self._brain_getter = brain_getter

    def _current_brain(self):
        return self._brain_getter() if self._brain_getter is not None else self.brain

    @property
    def paused(self) -> bool:
        return self._backoff.is_paused()

    async def maybe_extract(self, chat_type: ChatType, chat_id: int) -> int:
        """按会话累计消息数触发；同一批消息不会重复抽取。"""
        key = (chat_type, chat_id)
        if self._backoff.is_paused():
            return 0
        if self._backoff.should_skip(key):
            return 0
        count = self.store.count_messages(chat_type, chat_id)
        last = self._last_count_for(key)
        if count < last:  # 会话被 /forget 清空过，重新开始计数
            last = 0
        if count < last + self.every:
            return 0
        # 新消息优先；上文只是辅助，积压超过上限时先丢更早的新消息。
        new_count = count - last
        fetch = min(MAX_SOURCE_MESSAGES, new_count + CONTEXT_MESSAGES)
        messages = self.store.recent_messages(chat_type, chat_id, fetch)
        added = await self.extract(chat_type, chat_id, messages=messages)
        if added is None:
            # 失败不推进游标：退避结束后同一批消息还能再试。
            return 0
        self._last_count[key] = count
        self.store.set_impression_cursor(chat_type, chat_id, count)
        return added

    def _last_count_for(self, key: tuple[ChatType, int]) -> int:
        """游标落库：重启后从上次抽到的位置继续，而不是从 0 重跑一遍。"""
        if key not in self._last_count:
            self._last_count[key] = self.store.get_impression_cursor(*key)
        return self._last_count[key]

    async def extract(
        self,
        chat_type: ChatType,
        chat_id: int,
        messages: list[Message] | None = None,
    ) -> int | None:
        """抽取并写入印象；调用失败返回 None，供上层决定是否退避。

        messages 为空时取最近 MAX_SOURCE_MESSAGES 条（面板手动触发走这条路）。
        """
        if messages is None:
            messages = self.store.recent_messages(
                chat_type, chat_id, MAX_SOURCE_MESSAGES
            )
        if not messages:
            return 0

        lines = "\n".join(f"{m.user_id}|{m.nickname}: {m.content}" for m in messages)
        latest_by_user: dict[int, Message] = {}
        for message in messages:
            if not message.is_bot:
                latest_by_user[message.user_id] = message
        brain = self._current_brain()
        detailed = getattr(brain, "complete_detailed", None)
        try:
            if detailed is not None:
                raw, prompt_tokens, completion_tokens = await detailed(
                    EXTRACT_SYSTEM,
                    [{"role": "user", "content": lines}],
                    max_tokens=600,
                )
            else:
                raw = await brain.complete(
                    EXTRACT_SYSTEM,
                    [{"role": "user", "content": lines}],
                    max_tokens=600,
                )
                prompt_tokens = completion_tokens = 0
        except Exception as exc:
            log_memory_failure(log, "印象抽取", exc, chat_type, chat_id)
            self._backoff.mark_failed((chat_type, chat_id), exc)
            return None
        prompt_tokens, completion_tokens, estimated = resolve_usage(
            f"{EXTRACT_SYSTEM}\n{lines}", raw or "", prompt_tokens, completion_tokens
        )
        self.store.record_usage(
            chat_type,
            chat_id,
            "impression",
            prompt_tokens,
            completion_tokens,
            estimated,
        )

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
            source = latest_by_user.get(user_id)
            self.store.upsert_impression(
                chat_type,
                chat_id,
                user_id,
                kind,
                content,
                confidence,
                source_message_id=source.message_id if source else "",
                source_excerpt=source.content[:80] if source else "",
                subject_nickname=source.nickname if source else "",
            )
        self._backoff.mark_ok((chat_type, chat_id))
        return added
