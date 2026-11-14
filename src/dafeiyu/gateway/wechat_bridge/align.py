"""消息对齐：把「当前窗口」的屏幕快照变成稳定的 occurrence id。

桌面微信没有消息 id，只能按顺序对齐：

- 每次轮询取目标会话最近 K=50 条，形成 `(nickname, text)` 序列；
- 和上一次窗口做**最长后缀 = 前缀**匹配（旧窗口的尾巴 = 新窗口的开头）；
- 只有匹配之后多出来的那几条是「新消息」，按 `seq = 上次+1, +2…` 编号，
  `message_id = f"{session_epoch}:{seq}"`；
- 同一条文本在会话里出现两次是两条消息、两个 seq——**绝不能用文本哈希当 id**，
  否则重复发言会被 `INSERT OR IGNORE` 静默吃掉一条；
- 对齐失败（重叠 < `min(旧, 新) * 50%`，例如用户手动上翻、窗口被清空）说明
  我们失去了连续性：换一个 `session_epoch`、重编号并从 0 开始，发一条 `resync`
  告诉主进程「这是新的一段」，**但不补发历史**（补发会触发批次调度器洪水）。
- 换 epoch 不是可选项：旧 epoch 的 `(epoch, seq)` 已经发给对端，若沿用同一 epoch
  从 0 重编号，对端会把新消息当成重复（被 `INSERT OR IGNORE` 静默吃掉）或乱序。
  换了 epoch 后新旧 id 空间不相交，对端只要按 epoch 丢弃旧序列即可。

对齐只认 `(nickname, text)`，因为这是屏幕上唯一可靠的序列信息；`ts`、`kind`、
引用块都只是附加字段，不参与匹配。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "CHAT_TYPES",
    "MIN_OVERLAP_RATIO",
    "MIN_WIDE_OVERLAP",
    "Entry",
    "K",
    "PollResult",
    "SessionAligner",
]

K = 50
MIN_OVERLAP_RATIO = 0.5
#: 宽窗补读时，旧窗口尾巴至少要连续命中这么多条才认账。
#: 只命中 1 条可能只是「哈哈」这种巧合，会把整段历史当成新消息灌回去。
MIN_WIDE_OVERLAP = 3
AT_ALL_TOKENS = ("@所有人", "@全体成员")
#: 轮询结果里 `messages` 用的是事件信封，不是 models.Message。
CHAT_TYPES = ("group", "private")


@dataclass(frozen=True)
class Entry:
    """一条屏幕消息：只保存参与对齐与事件构造的字段。"""

    nickname: str
    text: str
    kind: str = "text"
    reply_author: str = ""
    reply_text: str = ""
    ts: int = 0
    #: 媒体元数据（本地库发现的可信路径）；只透传，不解析、不读字节。
    media: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PollResult:
    """一次轮询的产物：要发的事件 + 可能需要的 resync。"""

    messages: tuple[dict[str, Any], ...]
    resync: dict[str, Any] | None
    epoch: int


class SessionAligner:
    """单个会话的对齐器；一个会话一个实例，由 UIA 线程串行调用。"""

    def __init__(
        self,
        chat_id: str,
        chat_name: str,
        *,
        account_nickname: str,
        chat_type: str = "group",
        window: int = K,
        epoch: int | None = None,
        clock: Callable[[], float] = time.time,
        user_alias: Callable[[str], str] | None = None,
        self_nicknames: Iterable[str] = (),
    ) -> None:
        if not chat_id.startswith("wxg:") or chat_id == "wxg:":
            raise ValueError(f"chat_id 必须形如 wxg:<alias>，收到 {chat_id!r}")
        if chat_type not in CHAT_TYPES:
            raise ValueError(f"chat_type 必须是 group/private，收到 {chat_type!r}")
        self.chat_id = chat_id
        self.chat_name = str(chat_name or "")
        self.chat_type = chat_type
        self.account_nickname = str(account_nickname or "").strip()
        # bot 在群里的全部身份：账号昵称 + 群昵称/别名。@ 与引用回复都按这组判。
        self.self_nicknames = tuple(
            dict.fromkeys(
                name
                for name in (
                    self.account_nickname,
                    *(str(item).strip() for item in self_nicknames),
                )
                if name
            )
        )
        self.window = int(window)
        self._clock = clock
        self._user_alias = user_alias
        self._epoch = epoch if epoch is not None else _new_epoch()
        self._seq = 0
        self._prev: list[Entry] = []
        self._warned_no_nickname = False
        # 昵称为空的条目（纯文本行）也要有一个稳定的 user_id，否则它们会被并进
        # 「某个人」；这里给每个会话发一个匿名槽位，而不是复用别人的别名。
        self._anonymous_alias = uuid.uuid4().hex[:12]
        self._user_aliases: dict[str, str] = {}

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def seq(self) -> int:
        return self._seq

    def poll(self, entries: Iterable[Any], *, wide: bool = False) -> PollResult:
        """喂一次窗口快照，返回本次要发的事件。

        ``wide=True`` 用于「断档后补读」：``entries`` 可能比 `window` 长得多
        （本地库一次读回几百条），这时不能先截断再对齐——否则旧窗口的尾巴
        已经被截掉，重叠为 0 会误判成 resync 并把整段漏读消息永久丢掉。
        宽窗模式下改为「在整段快照里找旧窗口尾巴」，找到多少就补发它后面的
        全部消息。找不到（缺口大于一次读取上限）仍然走 resync，不猜。
        """
        raw = [_entry_from(raw_entry) for raw_entry in entries]
        current = raw if wide else raw[-self.window :]

        if not self._prev:
            # 第一轮只建立基线：不把「启动前就存在的历史」当成新消息回复。
            self._prev = current[-self.window :]
            return PollResult((), None, self._epoch)

        if wide:
            located = _locate_overlap(self._prev, current)
            if located is None:
                return self._resync(current[-self.window :], reason="overlap_low")
            index, size = located
            fresh = current[index + size :]
        else:
            overlap = _longest_overlap(self._prev, current)
            baseline = min(len(self._prev), len(current))
            if (
                overlap == 0
                or baseline == 0
                or overlap / baseline < MIN_OVERLAP_RATIO
            ):
                return self._resync(
                    current, reason="overlap_low" if baseline else "window_empty"
                )
            fresh = current[overlap:]

        messages = tuple(self._build_message(entry) for entry in fresh)
        self._prev = current[-self.window :]
        return PollResult(messages, None, self._epoch)

    # --------------------------------------------------------- 重启续跑
    def seed(self, entries: Iterable[Any]) -> None:
        """用上一进程留下的窗口当基线，续上停机期间的缺口。

        正常第一轮只建基线（``_prev`` 为空时才这样），否则会把启动前的历史当
        成新消息回复。桥崩溃重启时，停机窗口里的消息已经不在内存里，只有磁盘
        上那份窗口能 diff 出来——所以有 seed 就跳过「只建基线」。
        """
        seeded = [_entry_from(raw) for raw in entries][-self.window :]
        if seeded:
            self._prev = seeded

    def export(self) -> list[dict[str, Any]]:
        """导出当前窗口，供 :meth:`seed` 在重启后还原（只含对齐需要的字段）。"""
        return [
            {
                "nickname": entry.nickname,
                "text": entry.text,
                "kind": entry.kind,
                "reply_author": entry.reply_author,
                "reply_text": entry.reply_text,
                "ts": entry.ts,
                "media": dict(entry.media),
            }
            for entry in self._prev
        ]

    # ------------------------------------------------------------- 内部
    def _resync(self, current: list[Entry], *, reason: str) -> PollResult:
        """失去连续性：换 epoch、从 0 重编号、只发 resync，不补发历史。"""
        old_epoch = self._epoch
        self._epoch = _new_epoch()
        self._seq = 0
        self._prev = current
        log.warning(
            "会话 %s 对齐失败（%s），epoch %s -> %s",
            self.chat_id,
            reason,
            old_epoch,
            self._epoch,
        )
        envelope = {
            "v": 1,
            "type": "resync",
            "channel": "wechat",
            "chat_type": self.chat_type,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "user_id": f"wxg:{self._anonymous_alias}",
            "nickname": None,
            "ts": int(self._clock()),
            "session_epoch": self._epoch,
            "reason": reason,
        }
        return PollResult((), envelope, self._epoch)

    def _build_message(self, entry: Entry) -> dict[str, Any]:
        self._seq += 1
        at_bot, at_all, at_others = self._detect_at(entry.text)
        reply_to_bot, reply_to_text = self._detect_reply(entry)
        return {
            "v": 1,
            "type": "message",
            "channel": "wechat",
            "chat_type": self.chat_type,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "user_id": self._user_id(entry.nickname),
            "nickname": entry.nickname or "未知成员",
            "text": entry.text,
            "kind": entry.kind,
            "at_bot": at_bot,
            "at_all": at_all,
            "at_others": at_others,
            "reply_to_bot": reply_to_bot,
            "reply_to_text": reply_to_text,
            "reply_to_author": entry.reply_author or None,
            "ts": entry.ts or int(self._clock()),
            "message_id": f"{self._epoch}:{self._seq}",
            # 自己的发言必须由桥自己标记：主进程侧还有两层回声过滤，但源头标对了
            # 才不会有「重启后先自我回复一次」的窗口。
            "is_bot": self._is_bot(entry),
            "session_epoch": self._epoch,
            "media": dict(entry.media),
        }

    def _is_bot(self, entry: Entry) -> bool:
        return bool(entry.nickname) and entry.nickname in self.self_nicknames

    def _user_id(self, nickname: str) -> str:
        name = nickname.strip()
        # 回调优先于空昵称兜底：私聊的稳定别名对任意昵称（含空昵称）都成立；
        # 群聊不传回调，这里仍走匿名槽位 / 按昵称缓存的 uuid 逻辑。
        if self._user_alias is not None:
            alias = str(self._user_alias(name) or "").strip()
            if alias:
                return alias if alias.startswith("wxg:") else f"wxg:{alias}"
        if not name:
            return f"wxg:{self._anonymous_alias}"
        cached = self._user_aliases.get(name)
        if cached is None:
            cached = uuid.uuid4().hex[:12]
            self._user_aliases[name] = cached
        return f"wxg:{cached}"

    def _detect_at(self, text: str) -> tuple[bool, bool, bool]:
        """返回 (at_bot, at_all, at_others)。@所有人 永远不算点名机器人。

        ``at_others`` 是「@ 了别的群友」：屏幕上只能看到渲染后的文本，所以
        用「@ 后面那串昵称是否等于本账号的任一身份」来判断；不等就是别人。
        """
        if any(token in text for token in AT_ALL_TOKENS):
            return False, True, False
        if not self.self_nicknames:
            if not self._warned_no_nickname:
                self._warned_no_nickname = True
                log.warning(
                    "account_nickname 为空：会话 %s 的 @ 判定一律 False（拒绝把任何 @ 当点名）",
                    self.chat_id,
                )
            return False, False, _other_mention(text, ())
        at_bot = any(_mentions(text, name) for name in self.self_nicknames)
        return at_bot, False, _other_mention(text, self.self_nicknames)

    def _detect_reply(self, entry: Entry) -> tuple[bool, str | None]:
        """引用块作者 == 本账号昵称才算「回复机器人」。

        读不到引用块就按「不是回复」处理（DEBUG 留痕），绝不猜。
        """
        if not entry.reply_author:
            if entry.reply_text:
                log.debug(
                    "会话 %s 的引用块缺少作者，按非回复处理：%r",
                    self.chat_id,
                    entry.reply_text[:40],
                )
            return False, None
        if not self.self_nicknames:
            return False, None
        if entry.reply_author.strip() not in self.self_nicknames:
            return False, None
        return True, (entry.reply_text or None)


def _new_epoch() -> int:
    """随机的 31 位 int：只在「这一段连续性」内需要唯一。

    协议把 `session_epoch` 定为 int（v1 冻结），所以不用计划里提到的
    「8 位十六进制字符串」；随机而非时间戳，是为了避免同一毫秒内重启两次撞车
    （撞车会让新消息的 id 命中旧行，被 `INSERT OR IGNORE` 静默吃掉）。
    """
    while True:
        value = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        if value:
            return value


def _entry_from(raw: Any) -> Entry:
    """接受驱动层规整后的 dict、`(nickname, text)` 元组或裸字符串。"""
    if isinstance(raw, Entry):
        return raw
    if isinstance(raw, dict):
        reply_raw = raw.get("reply_to")
        if isinstance(reply_raw, dict):
            reply_text = str(reply_raw.get("text") or "")
            reply_author = str(reply_raw.get("nickname") or "")
        else:
            reply_text = str(reply_raw or "")
            reply_author = ""
        return Entry(
            nickname=str(raw.get("nickname") or ""),
            text=str(raw.get("text") or raw.get("content") or ""),
            kind=str(raw.get("kind") or "text"),
            reply_author=str(raw.get("reply_author") or reply_author),
            reply_text=reply_text,
            ts=_int(raw.get("ts")),
            media=(dict(raw["media"]) if isinstance(raw.get("media"), dict) else {}),
        )
    if isinstance(raw, (tuple, list)):
        items = list(raw) + ["", ""]
        return Entry(nickname=str(items[0] or ""), text=str(items[1] or ""))
    return Entry(nickname="", text=str(raw))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _longest_overlap(previous: Sequence[Entry], current: Sequence[Entry]) -> int:
    """旧窗口后缀 == 新窗口前缀的最长长度（只比较 nickname/text）。"""
    limit = min(len(previous), len(current))
    old_keys = [(entry.nickname, entry.text) for entry in previous]
    new_keys = [(entry.nickname, entry.text) for entry in current]
    for size in range(limit, 0, -1):
        if old_keys[-size:] == new_keys[:size]:
            return size
    return 0


def _locate_overlap(
    previous: Sequence[Entry], current: Sequence[Entry]
) -> tuple[int, int] | None:
    """在整段 ``current`` 里找 ``previous`` 的尾巴，返回 ``(起始下标, 长度)``。

    宽窗补读专用：``current`` 可能包含大量旧历史，旧窗口的尾巴不一定落在开头。
    取「最长匹配」，同长度时取最靠后的一次，避免把重复文本当成新消息再发一遍。
    匹配长度不足 :data:`MIN_WIDE_OVERLAP`（或全部旧窗口）时返回 None——
    宁可 resync 也不猜。
    """
    limit = min(len(previous), len(current))
    need = min(MIN_WIDE_OVERLAP, len(previous))
    if limit < need or need <= 0:
        return None
    old_keys = [(entry.nickname, entry.text) for entry in previous]
    new_keys = [(entry.nickname, entry.text) for entry in current]
    for size in range(limit, need - 1, -1):
        tail = old_keys[-size:]
        for start in range(len(new_keys) - size, -1, -1):
            if new_keys[start : start + size] == tail:
                return start, size
    return None


def _mentions(text: str, nickname: str) -> bool:
    """`@昵称` 且后面不是昵称字符（避免 `@小助手2` 命中 `@小助手`）。

    微信 @ 之后常跟 `\\u2005`（四分之一空格），它不属于 `isalnum()`，正好当边界。
    """
    needle = f"@{nickname}"
    start = text.find(needle)
    while start != -1:
        end = start + len(needle)
        if end == len(text) or not _is_name_char(text[end]):
            return True
        start = text.find(needle, start + 1)
    return False


#: `@所有人` / `@全体成员` 里被 @ 的那串字；不算「@ 了别的群友」。
_AT_ALL_NAMES = ("所有人", "全体成员")


def _mention_names(text: str) -> list[str]:
    """把文本里所有 ``@昵称`` 的昵称抠出来（渲染后文本的唯一可靠来源）。"""
    names: list[str] = []
    index = text.find("@")
    while index != -1:
        end = index + 1
        while end < len(text) and _is_name_char(text[end]):
            end += 1
        if end > index + 1:
            names.append(text[index + 1 : end])
        index = text.find("@", index + 1)
    return names


def _other_mention(text: str, self_nicknames: tuple[str, ...]) -> bool:
    """是否 @ 了别的群友：@所有人 不算，@本账号任一身份也不算。"""
    for name in _mention_names(text):
        if name in _AT_ALL_NAMES or name in self_nicknames:
            continue
        return True
    return False


def _is_name_char(char: str) -> bool:
    return char.isalnum() or char == "_"
