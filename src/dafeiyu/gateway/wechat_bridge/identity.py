"""本地身份表：显示名 -> 稳定别名（`wxg:<alias>`）。

为什么不用显示名当 id：微信群改名、重名、以及「同名群不是同一家」都会让消息串到
别人的记忆里。这里给每个显示名铸一个本地别名，改名的旧条目留墓碑（`retired_at`），
**永不复用**旧别名。

判据只有一条：别名由本进程铸造，显示名只是它当前的标签。由此推出：

- `peers.json` 是唯一真相，原子写（临时文件 + `os.replace`），写失败必须回滚内存，
  否则进程内外的身份就分叉了；
- 同名存活条目 >1 时 `resolve()` 拒绝（fail-closed）：宁可这条消息不发，也不能
  猜「哪个同名群才是目标」；
- 同时出现的两个未登记显示名若成员有交集，两个都铸新别名并报
  `wechat_alias_ambiguous`：可能是改名，也可能是两个真群，自动合并一定错一边；
- `self` 是保留别名，`peers.json` 里永远不出现 `alias == "self"`（机器人自己的
  回复行固定用 `wxg:self`，见 task 11）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "AliasConflictError",
    "IdentityError",
    "Peer",
    "PeerRegistry",
    "SELF_ALIAS",
    "default_peers_path",
]

SCHEMA_VERSION = 1
ALIAS_LEN = 12
SELF_ALIAS = "self"
KINDS = ("group", "friend")
# 轮询命中已登记会话时，last_seen 至少隔这么久才落盘一次。
LAST_SEEN_FLUSH_SECONDS = 60.0


def default_peers_path() -> Path:
    return Path("data/wechat-bridge/peers.json")


class IdentityError(Exception):
    """身份表不可用（文件损坏、字段非法等）。"""


class AliasConflictError(IdentityError):
    """同名存活条目不止一个：拒绝发送，等人工处理。"""

    def __init__(self, display_name: str, aliases: list[str]) -> None:
        self.display_name = display_name
        self.aliases = aliases
        super().__init__(
            f"显示名 {display_name!r} 对应多个存活别名 {aliases}，拒绝自动选择"
        )


@dataclass
class Peer:
    alias: str
    display_name: str
    kind: str
    first_seen: float
    last_seen: float
    retired_at: float | None = None

    @property
    def live(self) -> bool:
        return self.retired_at is None

    def to_json(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "display_name": self.display_name,
            "kind": self.kind,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "retired_at": self.retired_at,
        }

    @classmethod
    def from_json(cls, raw: Any) -> Peer:
        if not isinstance(raw, dict):
            raise IdentityError("peers.json 的条目必须是对象")
        try:
            alias = str(raw["alias"])
            display_name = str(raw["display_name"])
            kind = str(raw.get("kind") or "group")
            first_seen = float(raw["first_seen"])
            last_seen = float(raw.get("last_seen", first_seen))
        except (KeyError, TypeError, ValueError) as exc:
            raise IdentityError(f"peers.json 条目缺字段：{raw!r}") from exc
        if not alias or alias == SELF_ALIAS:
            raise IdentityError(f"非法别名：{alias!r}")
        if kind not in KINDS:
            raise IdentityError(f"非法 kind：{kind!r}")
        retired_raw = raw.get("retired_at")
        retired_at = None if retired_raw is None else float(retired_raw)
        return cls(alias, display_name, kind, first_seen, last_seen, retired_at)


class PeerRegistry:
    """`peers.json` 的读写门面；线程内使用（桥的 UIA 线程串行调用）。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        on_alert: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_peers_path()
        self._clock = clock
        self._on_alert = on_alert
        self._peers: dict[str, Peer] = {}
        # 本进程见过但还没登记的显示名 -> 成员集合（仅供歧义判定，不落盘）。
        self._pending: dict[str, frozenset[str]] = {}
        self._load()

    # ------------------------------------------------------------- 查询
    def all_peers(self) -> list[Peer]:
        return sorted(self._peers.values(), key=lambda p: (p.first_seen, p.alias))

    def by_alias(self, alias: str) -> Peer | None:
        return self._peers.get(alias)

    def live_aliases(self, display_name: str) -> list[str]:
        return sorted(
            peer.alias
            for peer in self._peers.values()
            if peer.display_name == display_name and peer.live
        )

    # ------------------------------------------------------------- 写入
    def resolve(self, display_name: str, *, kind: str = "group") -> str:
        """显示名 -> 别名。未登记则铸造并落盘；同名存活条目 >1 时拒绝。"""
        name = _require_name(display_name)
        _require_kind(kind)
        return self._mutate(lambda: self._resolve_locked(name, kind))

    def observe(
        self,
        display_name: str,
        members: list[str] | set[str] | frozenset[str],
        *,
        kind: str = "group",
    ) -> str:
        """带成员集合的解析：用于发现「两个新显示名其实是同一个群」的歧义。

        歧义时**不合并**：两个名字各铸新别名，并发 `wechat_alias_ambiguous`。
        """
        name = _require_name(display_name)
        _require_kind(kind)
        member_set = frozenset(
            str(member).strip() for member in members if str(member).strip()
        )

        def run() -> str:
            if self.live_aliases(name):
                return self._resolve_locked(name, kind)
            for other_name, other_members in self._pending.items():
                if other_name == name:
                    continue
                if member_set and other_members and member_set & other_members:
                    self._alert(
                        "wechat_alias_ambiguous",
                        {
                            "names": sorted([other_name, name]),
                            "overlap": sorted(member_set & other_members)[:10],
                        },
                    )
            self._pending[name] = member_set
            return self._resolve_locked(name, kind)

        return self._mutate(run)

    def retire(self, display_name: str) -> bool:
        """把该显示名下所有存活条目置墓碑；返回是否真的改动过。"""
        name = _require_name(display_name)

        def run() -> bool:
            now = self._clock()
            changed = False
            for peer in self._peers.values():
                if peer.display_name == name and peer.live:
                    peer.retired_at = now
                    changed = True
            if changed:
                self._save()
            return changed

        return self._mutate(run)

    def chat_id(self, display_name: str, *, kind: str = "group") -> str:
        return f"wxg:{self.resolve(display_name, kind=kind)}"

    # ------------------------------------------------------------- 内部
    def _resolve_locked(self, name: str, kind: str) -> str:
        live = self.live_aliases(name)
        if len(live) > 1:
            # fail-closed：绝不替调用方挑一个同名群。
            self._alert(
                "wechat_alias_ambiguous",
                {"names": [name], "aliases": live, "reason": "multiple_live"},
            )
            raise AliasConflictError(name, live)
        now = self._clock()
        if live:
            peer = self._peers[live[0]]
            # 轮询每几秒一次：last_seen 没必要每次都 fsync，攒够 60s 再落盘。
            if now - peer.last_seen >= LAST_SEEN_FLUSH_SECONDS:
                peer.last_seen = now
                self._save()
            else:
                peer.last_seen = max(peer.last_seen, now)
            return peer.alias

        alias = self._mint_alias()
        self._peers[alias] = Peer(
            alias=alias,
            display_name=name,
            kind=kind,
            first_seen=now,
            last_seen=now,
        )
        self._save()
        return alias

    def _mint_alias(self) -> str:
        while True:
            alias = uuid.uuid4().hex[:ALIAS_LEN]
            # `self` 与既有别名都不能复用：改名留墓碑，别名是身份的锚。
            if alias != SELF_ALIAS and alias not in self._peers:
                return alias

    def _alert(self, code: str, payload: dict[str, Any]) -> None:
        if self._on_alert is not None:
            self._on_alert(code, payload)
        log.warning("%s %s", code, payload)

    def _mutate(self, fn: Callable[[], Any]) -> Any:
        """执行一次可能落盘的修改；落盘失败时把内存回滚到修改前。"""
        backup = {alias: replace(peer) for alias, peer in self._peers.items()}
        pending_backup = dict(self._pending)
        try:
            return fn()
        except BaseException:
            self._peers = backup
            self._pending = pending_backup
            raise

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 静默重建会让所有会话换一套别名，记忆被劈成两半：必须炸。
            raise IdentityError(f"peers.json 无法解析：{self.path}") from exc
        peers = raw.get("peers") if isinstance(raw, dict) else raw
        if not isinstance(peers, list):
            raise IdentityError("peers.json 缺少 peers 列表")
        for item in peers:
            peer = Peer.from_json(item)
            if peer.alias in self._peers:
                raise IdentityError(f"peers.json 别名重复：{peer.alias}")
            self._peers[peer.alias] = peer

    def _save(self) -> None:
        payload = {
            "v": SCHEMA_VERSION,
            "peers": [peer.to_json() for peer in self.all_peers()],
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise


def _require_name(display_name: str) -> str:
    name = str(display_name or "").strip()
    if not name:
        raise IdentityError("显示名不能为空")
    return name


def _require_kind(kind: str) -> None:
    if kind not in KINDS:
        raise IdentityError(f"非法 kind：{kind!r}")
