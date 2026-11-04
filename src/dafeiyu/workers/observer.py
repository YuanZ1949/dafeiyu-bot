"""P1 旁路观察者：只看群聊在聊什么，不回复、不参与主链路。

每轮按会话读取游标之后的新消息，调用一个「只观察」的模型，把结果拆成：

* episode：正在聊的事（同一段对话更新同一行）；
* open_loop：没有下文的事（谁问了没接、谁答应了没做）。

任何失败都只记日志并交给 ``FailureBackoff``，绝不影响回复主链路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from dafeiyu.brain.tokens import resolve_usage
from dafeiyu.config import MindConfig
from dafeiyu.memory.store import EPISODE_PHASES, OPEN_LOOP_KINDS, Store
from dafeiyu.models import ChatType, Episode, Message, OpenLoop, Participant
from dafeiyu.timeutil import today_shanghai

from .failure import FAILURE_BACKOFF_SECONDS, FailureBackoff, log_memory_failure

log = logging.getLogger(__name__)

OBSERVER_SYSTEM = """\
你是群聊旁观者。下面每行是一条群消息，格式为 `user_id|昵称: 内容`。
你的任务是观察这群人正在聊什么，不回复、不参与、不评价。只输出严格 JSON，
不要代码围栏、不要解释。

JSON schema：
{"topic": "一句话话题", "phase": "opening|discussion|climax|winding|closed",
 "summary": "两三句话说明现在聊到哪了",
 "participants": [{"user_id": "qq:1", "nickname": "阿猫"}],
 "open_loops": [{"kind": "question|promise|plan|running_joke",
                 "user_id": "qq:1", "content": "谁问了什么 / 答应了什么"}],
 "mood": "群里的气氛", "importance": 0.5}

规则：
- topic 不超过 40 字，summary 不超过 120 字，mood 不超过 20 字，
  importance 取 0 到 1。
- participants 只列本次发言的人；user_id 必须原样复制输入行前缀
  （含 "qq:"/"wxg:"），不得编造；没有就输出空数组。
- open_loops 只记「有人问了没人接」「有人答应了没做」「群里在等什么」，
  每条 content 不超过 80 字；没有就输出空数组，宁可少记，不要臆测。
- 不要记录隐私（手机号、住址、密码），不要输出 schema 之外的字段。"""

MAX_SOURCE_MESSAGES = 40
# 新消息前面多带几条上文，帮助模型理解指代；上限固定，避免整段重喂。
CONTEXT_MESSAGES = 10
MAX_TOPIC_CHARS = 40
MAX_SUMMARY_CHARS = 120
MAX_MOOD_CHARS = 20
MAX_LOOP_CHARS = 80
MAX_PARTICIPANTS = 8
MAX_OPEN_LOOPS = 3
MAX_LINE_CHARS = 200
# 超过这个空档就视为换了一段对话：新 episode 重新开一行，而不是把上午的事续上。
EPISODE_GAP_SECONDS = 1800
OPEN_LOOP_TTL_DAYS = 7
OBSERVER_MAX_TOKENS = 600


def _clip(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if len(text) <= limit else text[:limit]


def _chronological(messages: list[Message]) -> list[Message]:
    """统一成时间正序。

    ``Store.recent_messages`` 当前返回时间正序，但历史/替身实现可能给新消息在
    前的顺序；按首尾时间戳判断一次，避免喂给模型的行是倒着的。
    """
    if len(messages) > 1 and messages[0].timestamp > messages[-1].timestamp:
        return list(reversed(messages))
    return list(messages)


def _parse_payload(text: str) -> dict[str, Any] | None:
    """模型输出不可信：只认最外层那个 JSON 对象，其它杂质一律丢掉。"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_marker(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    start = data.get("start")
    if not isinstance(start, str) or not start:
        return None
    return data


def _marker_timestamp(marker: dict[str, Any], key: str) -> int | None:
    value = marker.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ObserverWorker:
    """每群周期性观察一次：产出 episode（正在聊什么）与 open_loop（没有下文的事）。

    设计约束（spec §6.3）：默认只用本地模型、单群每日调用上限、全局并发 1、
    独立 ``FailureBackoff``。因此 ``run_once`` 严格串行，任何单群失败只记日志，
    绝不影响主链路。
    """

    def __init__(
        self,
        store: Store,
        brain,
        mind: MindConfig,
        *,
        backoff: FailureBackoff | None = None,
        now=time.time,
        brain_getter: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store
        self.brain = brain
        self.mind = mind
        self._backoff = backoff or FailureBackoff(FAILURE_BACKOFF_SECONDS)
        self._now = now
        self._brain_getter = brain_getter

    @property
    def paused(self) -> bool:
        return self._backoff.is_paused()

    def _current_brain(self):
        return self._brain_getter() if self._brain_getter is not None else self.brain

    def _enabled(self) -> bool:
        return bool(
            self.mind.enabled
            and self.mind.observer_enabled
            and self.mind.episodes_enabled
        )

    async def run_once(self) -> int:
        """扫一轮活跃群；返回真正调用过模型的会话数。"""
        if not self._enabled() or self._backoff.is_paused():
            return 0

        now_ts = int(self._now())
        window = max(60, int(self.mind.observer_interval_seconds) * 2)
        observed = 0
        # 严格串行：一次只调一个模型，避免观察和回复抢并发。
        for chat_type, chat_id in self.store.active_chats_between(
            now_ts - window, now_ts + 1
        ):
            if chat_type != ChatType.GROUP:
                continue
            if self._backoff.is_paused():
                break
            try:
                if await self.observe_chat(chat_type, chat_id):
                    observed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Observer 观察失败：%s %s", chat_type.value, chat_id)
        return observed

    async def observe_chat(self, chat_type: ChatType, chat_id: str) -> bool:
        # 只观察群聊：私聊不进入记忆。
        if chat_type != ChatType.GROUP or not self._enabled():
            return False
        if self._backoff.is_paused():
            return False
        key = (chat_type, chat_id)
        if self._backoff.should_skip(key):
            return False

        # 顺手清掉过期未了事：即使这轮不调用模型也要做。
        self.store.expire_open_loops(
            chat_type, chat_id, days=OPEN_LOOP_TTL_DAYS
        )

        total = self.store.count_messages(chat_type, chat_id)
        cursor = self.store.observer_cursor(chat_type, chat_id)
        if total < cursor:  # 会话被 /forget 清空过，重新计数
            cursor = 0
        new_count = total - cursor
        if new_count < int(self.mind.observer_min_messages):
            return False

        today = today_shanghai()
        if (
            self.store.observer_calls_today(chat_type, chat_id, today)
            >= int(self.mind.observer_daily_calls)
        ):
            return False

        fetch = min(MAX_SOURCE_MESSAGES, new_count + CONTEXT_MESSAGES)
        messages = _chronological(
            self.store.recent_messages(chat_type, chat_id, fetch)
        )
        # 游标之后的新消息才是本次要判断的「新一段」；更早的只作为上文。
        fresh = messages[-new_count:] if new_count else messages
        fresh_human = [message for message in fresh if not message.is_bot]
        human = [message for message in messages if not message.is_bot]
        if not human:
            # 整段都是自己的发言：不喂、不花钱。
            return False
        if not fresh_human:
            # 新消息全是机器人：没有可观察内容，直接跳过这一批。
            self.store.set_observer_cursor(chat_type, chat_id, total)
            return False

        body = "\n".join(
            f"{message.user_id}|{message.nickname}: "
            f"{message.content[:MAX_LINE_CHARS]}"
            for message in human
        )
        prompt_text = f"{OBSERVER_SYSTEM}\n{body}"
        try:
            content, prompt_tokens, completion_tokens = await self._current_brain().complete_detailed(
                OBSERVER_SYSTEM,
                [{"role": "user", "content": body}],
                max_tokens=OBSERVER_MAX_TOKENS,
                temperature=0.2,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 旁路失败只退避，不影响主链路
            self._backoff.mark_failed(key, exc)
            log_memory_failure(log, "observer", exc, chat_type, chat_id)
            return False

        content = content or ""
        prompt_tokens, completion_tokens, estimated = resolve_usage(
            prompt_text, content, prompt_tokens, completion_tokens
        )
        self.store.record_usage(
            chat_type,
            chat_id,
            "observer",
            prompt_tokens,
            completion_tokens,
            estimated,
        )
        self.store.bump_observer_calls(chat_type, chat_id, today)

        payload = _parse_payload(content)
        if payload is None:
            # 调用已经花掉了：推进游标避免对同一批消息反复烧额度。
            self.store.set_observer_cursor(chat_type, chat_id, total)
            self._backoff.mark_failed(key, ValueError("observer 输出不是合法 JSON"))
            log.warning("Observer 输出解析失败：%s %s", chat_type.value, chat_id)
            return False

        self._write_episode(chat_type, chat_id, human, fresh_human, payload)
        self._write_open_loops(chat_type, chat_id, human, payload)

        self.store.set_observer_cursor(chat_type, chat_id, total)
        self._backoff.mark_ok(key)
        return True

    # ---- 落库 ---------------------------------------------------------

    def _episode_marker_key(self, chat_type: ChatType, chat_id: str) -> str:
        return f"observer/episode/{chat_type.value}/{chat_id}"

    def _write_episode(
        self,
        chat_type: ChatType,
        chat_id: str,
        human: list[Message],
        fresh_human: list[Message],
        payload: dict[str, Any],
    ) -> None:
        marker_key = self._episode_marker_key(chat_type, chat_id)
        marker = _load_marker(self.store.get_mind_state(marker_key))
        first = fresh_human[0]
        last = fresh_human[-1]
        marker_last_ts = (
            _marker_timestamp(marker, "last_ts") if marker is not None else None
        )
        # 没有标记、标记坏了、或中间隔了 30 分钟以上：开一段新对话。
        replace = (
            marker is None
            or marker_last_ts is None
            or first.timestamp - marker_last_ts > EPISODE_GAP_SECONDS
        )
        if marker is None or replace:
            start_id = first.message_id
            start_ts = int(first.timestamp)
        else:
            start_id = str(marker.get("start") or first.message_id)
            start_ts = _marker_timestamp(marker, "start_ts")
            if start_ts is None:
                start_ts = int(first.timestamp)

        participants = self._participants(human, payload)
        phase = payload.get("phase")
        phase = phase.strip() if isinstance(phase, str) else ""
        if phase not in EPISODE_PHASES:
            phase = "discussion"
        try:
            importance = float(payload.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5

        episode = Episode(
            chat_type=chat_type,
            chat_id=chat_id,
            start_message_id=start_id,
            last_message_id=last.message_id,
            topic=_clip(payload.get("topic"), MAX_TOPIC_CHARS),
            summary=_clip(payload.get("summary"), MAX_SUMMARY_CHARS),
            participants=participants,
            phase=phase,
            mood=_clip(payload.get("mood"), MAX_MOOD_CHARS),
            importance=min(1.0, max(0.0, importance)),
            status="open",
            started_at=start_ts,
        )
        episode_id = self.store.save_episode(episode)
        # 同一会话只允许一条 open：新一段一开，旧的必须闭。
        self.store.close_open_episodes(chat_type, chat_id, keep_id=episode_id)
        self.store.set_mind_state(
            marker_key,
            json.dumps(
                {
                    "start": start_id,
                    "start_ts": start_ts,
                    "last_ts": int(last.timestamp),
                },
                ensure_ascii=False,
            ),
        )

    def _participants(
        self, human: list[Message], payload: dict[str, Any]
    ) -> tuple[Participant, ...]:
        """只认输入里真出现过的人：模型编出来的 id 一律丢掉。"""
        nicknames: dict[str, str] = {}
        for message in human:
            if message.user_id:
                # 用最新的群昵称；模型给的昵称只在输入缺失时兜底。
                nicknames[message.user_id] = message.nickname
        people: list[Participant] = []
        seen: set[str] = set()
        raw = payload.get("participants")
        if isinstance(raw, list):
            for item in raw:
                if len(people) >= MAX_PARTICIPANTS:
                    break
                if not isinstance(item, dict):
                    continue
                uid = item.get("user_id")
                if not isinstance(uid, str) or uid not in nicknames:
                    continue
                if uid in seen:
                    continue
                seen.add(uid)
                people.append(
                    Participant(
                        user_id=uid,
                        nickname=nicknames[uid]
                        or _clip(item.get("nickname"), 40),
                    )
                )
        return tuple(people)

    def _write_open_loops(
        self,
        chat_type: ChatType,
        chat_id: str,
        human: list[Message],
        payload: dict[str, Any],
    ) -> None:
        valid_users = {message.user_id for message in human}
        valid_ids = {message.message_id for message in human}
        latest_of: dict[str, Message] = {}
        for message in human:
            latest_of[message.user_id] = message
        raw = payload.get("open_loops")
        if not isinstance(raw, list):
            return
        recorded = 0
        for item in raw:
            if recorded >= MAX_OPEN_LOOPS:
                break
            if not isinstance(item, dict):
                continue
            content = _clip(item.get("content"), MAX_LOOP_CHARS + 1)
            # 空内容或超长内容直接丢弃，不截断成半句话。
            if not content or len(content) > MAX_LOOP_CHARS:
                continue
            kind = item.get("kind")
            if not isinstance(kind, str) or kind not in OPEN_LOOP_KINDS:
                kind = "question"
            raw_user = item.get("user_id")
            user_id = (
                raw_user
                if isinstance(raw_user, str) and raw_user in valid_users
                else None
            )
            raw_source = item.get("source_message_id")
            if isinstance(raw_source, str) and raw_source in valid_ids:
                source_id = raw_source
            elif not raw_source and user_id is not None:
                source = latest_of.get(user_id)
                source_id = source.message_id if source else ""
            else:
                source_id = ""
            try:
                self.store.add_open_loop(
                    OpenLoop(
                        chat_type=chat_type,
                        chat_id=chat_id,
                        kind=kind,
                        content=content,
                        subject_user_id=user_id,
                        source_message_id=source_id,
                    )
                )
            except ValueError:
                continue
            recorded += 1


async def observer_loop(
    worker: ObserverWorker,
    interval_seconds: float = 600,
    stop: asyncio.Event | None = None,
) -> None:
    """后台长期任务：周期性观察，直到 stop 被置位。"""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            await worker.run_once()
        except Exception:
            log.exception("Observer 循环执行失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
