"""本地身份表：显示名 -> 稳定别名（`wxg:<alias>`）。

为什么不用显示名当 id：微信群改名、重名、以及「同名群不是同一家」都会让消息串到
别人的记忆里。这里给每个显示名铸一个本地别名，改名的旧条目留墓碑（`retired_at`），
**永不复用**旧别名。

判据只有一条：别名由本进程铸造，显示名只是它当前的标签。由此推出：

- `peers.json` 是唯一真相，原子写（临时文件 + `os.replace`），写失败必须回滚内存，
  否则进程内外的身份就分叉了；
- 同名存活条目 >1 时 `resolve()` 拒绝（fail-closed）：宁可这条消息不发，也不能
  猜「哪个同名群才是目标」；
- `observe()` 带成员集合，用成员重叠度判定「同一个群」还是「两个同名群」：
  同名且成员重叠达到阈值 → 复用 alias（成员增减不算换群）；同名但成员不重叠
  → 铸新 alias 并报 `wechat_alias_ambiguous`，绝不静默共用一份记忆；
- 新显示名与某个已知群成员高度重叠 → 判定改名：复用原 alias（`chat_id` 不变），
  旧名进 `former_names` 当墓碑（历史消息仍能解析回同一会话）；
- 改名命中多个候选、或同名命中多个成员匹配时抛 `AliasConflictError` 并告警，
  绝不猜；
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
# 成员集合的 Jaccard 重叠度阈值：>= 判定为同一个群（改名/成员增减）。
MEMBER_OVERLAP_THRESHOLD = 0.6


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
    #: 最近观测到的群成员显示名；用于同名群区分与改名判定（旧文件没有该字段）。
    members: frozenset[str] = frozenset()
    #: 改名前用过的显示名（墓碑式保留，历史消息仍能解析回同一 alias）。
    former_names: tuple[str, ...] = ()

    @property
    def live(self) -> bool:
        return self.retired_at is None

    def answers_to(self, display_name: str) -> bool:
        return self.display_name == display_name or display_name in self.former_names

    def to_json(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "display_name": self.display_name,
            "kind": self.kind,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "retired_at": self.retired_at,
            "members": sorted(self.members),
            "former_names": list(self.former_names),
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
        members_raw = raw.get("members") or []
        if not isinstance(members_raw, (list, tuple)):
            raise IdentityError(f"peers.json members 必须是列表：{raw!r}")
        members = frozenset(
            str(member).strip() for member in members_raw if str(member).strip()
        )
        former_raw = raw.get("former_names") or []
        if not isinstance(former_raw, (list, tuple)):
            raise IdentityError(f"peers.json former_names 必须是列表：{raw!r}")
        former_names = tuple(
            str(former).strip() for former in former_raw if str(former).strip()
        )
        return cls(
            alias,
            display_name,
            kind,
            first_seen,
            last_seen,
            retired_at,
            members,
            former_names,
        )


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
            if peer.live and peer.answers_to(display_name)
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
        """带成员集合的解析：用成员把「同名不同群」和「改名」区分开。

        - 同名且成员重叠 >= `MEMBER_OVERLAP_THRESHOLD`：同一个群（成员增减也算），
          复用 alias；
        - 同名但成员不重叠：另一个群，**铸新 alias 并报 `wechat_alias_ambiguous`**，
          旧 alias 保留（历史消息仍要渲染）；此后同名存活 >1，`resolve()` fail-closed；
        - 新显示名但与某个已知群成员高度重叠：判定改名，复用原 alias
          （`chat_id` 不变），旧名进 `former_names` 当墓碑；
        - 候选不唯一（改名/同名匹配打平）：抛 `AliasConflictError` + 告警，绝不猜。
        """
        name = _require_name(display_name)
        _require_kind(kind)
        member_set = frozenset(
            str(member).strip() for member in members if str(member).strip()
        )

        def run() -> str:
            same = [peer for peer in self._live(kind) if peer.answers_to(name)]
            if same:
                return self._observe_known_name(name, kind, member_set, same)
            return self._observe_unknown_name(name, kind, member_set)

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
    def _live(self, kind: str) -> list[Peer]:
        return [peer for peer in self._peers.values() if peer.live and peer.kind == kind]

    def _observe_known_name(
        self,
        name: str,
        kind: str,
        member_set: frozenset[str],
        same: list[Peer],
    ) -> str:
        """显示名已知：成员重叠决定「同一个群」还是「同名另一个群」。"""
        known = [peer for peer in same if peer.members]
        if member_set and known:
            matches = [
                peer
                for peer in known
                if _overlap(peer.members, member_set) >= MEMBER_OVERLAP_THRESHOLD
            ]
            if len(matches) > 1:
                aliases = sorted(peer.alias for peer in matches)
                self._alert(
                    "wechat_alias_ambiguous",
                    {"names": [name], "aliases": aliases, "reason": "member_match_tie"},
                )
                raise AliasConflictError(name, aliases)
            if matches:
                return self._remember(matches[0], name, member_set)
            # 同名 + 成员不重叠：是另一个群，绝不静默共用 alias/记忆（IS-2）。
            unknown = next((peer for peer in same if not peer.members), None)
            if unknown is not None:
                # 老条目没记过成员：补记，但仍视为同一个群（无法证伪）。
                return self._remember(unknown, name, member_set)
            alias = self._mint_peer(name, kind, member_set)
            self._alert(
                "wechat_alias_ambiguous",
                {
                    "names": [name],
                    "aliases": sorted([peer.alias for peer in same] + [alias]),
                    "reason": "member_mismatch",
                },
            )
            return alias
        # 成员读不到：退回「同名唯一存活」的旧语义；>1 存活仍然 fail-closed。
        return self._resolve_locked(name, kind)

    def _observe_unknown_name(
        self, name: str, kind: str, member_set: frozenset[str]
    ) -> str:
        """显示名没见过：成员高度重叠 = 改名（复用 alias）；否则铸新并告警。"""
        if member_set:
            ranked = sorted(
                (peer for peer in self._live(kind) if peer.members),
                key=lambda peer: _overlap(peer.members, member_set),
                reverse=True,
            )
            if ranked:
                score = _overlap(ranked[0].members, member_set)
                if score >= MEMBER_OVERLAP_THRESHOLD:
                    ties = [
                        peer
                        for peer in ranked
                        if _overlap(peer.members, member_set) == score
                    ]
                    if len(ties) > 1:
                        aliases = sorted(peer.alias for peer in ties)
                        self._alert(
                            "wechat_alias_ambiguous",
                            {"names": [name], "aliases": aliases, "reason": "rename_tie"},
                        )
                        raise AliasConflictError(name, aliases)
                    return self._remember(ranked[0], name, member_set)
                # 有交集但不到改名阈值：可能是两个真群，各铸新 alias 并告警。
                for peer in ranked:
                    overlap = peer.members & member_set
                    if overlap:
                        self._alert(
                            "wechat_alias_ambiguous",
                            {
                                "names": sorted([peer.display_name, name]),
                                "overlap": sorted(overlap)[:10],
                            },
                        )
        return self._mint_peer(name, kind, member_set)

    def _remember(
        self, peer: Peer, name: str, member_set: frozenset[str]
    ) -> str:
        """确认到既有 alias：合并成员、跟随改名，并按 60s 节流落盘。"""
        now = self._clock()
        changed = False
        if member_set and not member_set.issubset(peer.members):
            peer.members = peer.members | member_set
            changed = True
        if peer.display_name != name:
            former = tuple(old for old in peer.former_names if old != name)
            peer.former_names = former + (peer.display_name,)
            peer.display_name = name
            changed = True
        if now - peer.last_seen >= LAST_SEEN_FLUSH_SECONDS:
            peer.last_seen = now
            changed = True
        else:
            peer.last_seen = max(peer.last_seen, now)
        if changed:
            self._save()
        return peer.alias

    def _mint_peer(self, name: str, kind: str, member_set: frozenset[str]) -> str:
        now = self._clock()
        alias = self._mint_alias()
        self._peers[alias] = Peer(
            alias=alias,
            display_name=name,
            kind=kind,
            first_seen=now,
            last_seen=now,
            members=member_set,
        )
        self._save()
        return alias

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
        try:
            return fn()
        except BaseException:
            self._peers = backup
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


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """成员集合的 Jaccard 重叠度；任一侧未知返回 0，由调用方区分「未知」。"""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _require_name(display_name: str) -> str:
    name = str(display_name or "").strip()
    if not name:
        raise IdentityError("显示名不能为空")
    return name


def _require_kind(kind: str) -> None:
    if kind not in KINDS:
        raise IdentityError(f"非法 kind：{kind!r}")
