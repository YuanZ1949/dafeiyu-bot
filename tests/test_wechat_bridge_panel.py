"""面板白名单（groups.whitelist）接管桥的轮询范围与出站闸门。

面板语义：白名单非空 = 只碰列出的会话；空 = 不限制（本地库枚举到的全部群）。
读不出来时 fail-closed：不轮询、不发送（群范围只有面板一个来源）。

全部用假注册表 / 假本地库，不碰 UIA、不碰真实微信数据。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dafeiyu.gateway.wechat_bridge.__main__ as bridge_main
from dafeiyu.config import WeChatConfig
from dafeiyu.gateway.wechat_bridge import identity as bridge_identity
from dafeiyu.gateway.wechat_bridge import panel as bridge_panel
from dafeiyu.gateway.wechat_bridge import poller as bridge_poller
from dafeiyu.gateway.wechat_bridge.panel import PanelGroups, PanelRuntime
from dafeiyu.gateway.wechat_bridge.poller import PollingLoop
from dafeiyu.hotconfig import WeChatRuntime

ALLOWED = "老友群"


def _wechat(**overrides: Any) -> WeChatConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 0,
        "account_nickname": "小菜鱼",
        "poll_seconds": 0.01,
    }
    defaults.update(overrides)
    return WeChatConfig(**defaults)


class _Peers:
    """假身份表：alias -> (display_name, live, kind)。"""

    def __init__(self, mapping: dict[str, tuple[str, bool, str]]) -> None:
        self._mapping = dict(mapping)

    def by_alias(self, alias: str) -> Any | None:
        item = self._mapping.get(alias)
        if item is None:
            return None
        name, live, kind = item
        return SimpleNamespace(
            alias=alias, display_name=name, live=live, kind=kind
        )


class _LocalDb:
    def __init__(self, groups: list[str]) -> None:
        self._groups = list(groups)
        self.available = True
        self.calls: list[str] = []

    def group_names(self) -> list[str]:
        return list(self._groups)

    def snapshot(self, name: str) -> dict[str, Any] | None:
        self.calls.append(name)
        return {"chat_name": name, "messages": []}


class _Driver:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def open_chat(self, name: str) -> Any:
        self.opened.append(name)
        raise AssertionError(f"不应开窗：{name!r}")

    def snapshot(self, handle: Any) -> dict[str, Any]:
        raise AssertionError("不应快照")


def _loop(
    *,
    panel: Any = None,
    registry: Any = None,
    localdb: Any = None,
    **overrides: Any,
) -> tuple[PollingLoop, _Driver]:
    driver = _Driver()
    loop = PollingLoop(
        _wechat(**overrides),
        driver,
        registry=registry if registry is not None else _Peers({}),
        probe=lambda connected: {"degraded": False},
        localdb=localdb,
        panel=panel,
        clock=lambda: 1.0,
    )
    return loop, driver


# --------------------------------------------------------------- 轮询范围
def test_empty_panel_polls_every_group_localdb_knows() -> None:
    localdb = _LocalDb(["老友群", "同事群", "垃圾桶"])
    loop, _ = _loop(panel=lambda: frozenset(), localdb=localdb)

    assert loop._scope() == ("同事群", "垃圾桶", "老友群")


async def test_empty_panel_targets_ignore_old_allowlist() -> None:
    localdb = _LocalDb(["老友群", "垃圾桶"])
    loop, _ = _loop(panel=lambda: frozenset(), localdb=localdb)

    assert await loop._targets() == ("垃圾桶", "老友群")


def test_panel_whitelist_limits_scope_to_listed_groups() -> None:
    peers = _Peers({"a1": ("同事群", True, "group"), "a2": ("垃圾桶", True, "group")})
    loop, _ = _loop(panel=lambda: frozenset({"wxg:a1"}), registry=peers)

    assert loop._scope() == ("同事群",)


async def test_poll_round_never_touches_groups_outside_panel_scope() -> None:
    peers = _Peers({"a1": ("同事群", True, "group")})
    loop, driver = _loop(panel=lambda: frozenset({"wxg:a1"}), registry=peers)

    frames = [frame async for frame in loop._poll_round(["老友群"])]

    assert frames == []
    assert driver.opened == []


def test_panel_without_wechat_chats_polls_nothing() -> None:
    localdb = _LocalDb(["老友群", "垃圾桶"])
    loop, _ = _loop(panel=lambda: frozenset({"qq:123456"}), localdb=localdb)

    assert loop._scope() == ()


def test_panel_reader_failure_is_fail_closed() -> None:
    def boom() -> frozenset[str] | None:
        raise OSError("disk gone")

    loop, _ = _loop(panel=boom, localdb=_LocalDb(["老友群", "垃圾桶"]))

    assert loop._scope() == ()


def test_no_panel_is_fail_closed() -> None:
    loop, _ = _loop(panel=None, localdb=_LocalDb(["老友群", "垃圾桶"]))

    assert loop._scope() == ()


def test_panel_empty_without_localdb_polls_nothing() -> None:
    loop, _ = _loop(panel=lambda: frozenset(), localdb=None)

    assert loop._scope() == ()


def test_panel_skips_retired_or_unknown_aliases() -> None:
    peers = _Peers({"a1": ("同事群", False, "group"), "a2": ("某人", True, "friend")})
    loop, _ = _loop(
        panel=lambda: frozenset({"wxg:a1", "wxg:a2", "wxg:nobody"}), registry=peers
    )

    assert loop._scope() == ()


# --------------------------------------------------------------- 出站闸门
def test_allows_chat_empty_panel_means_any_group() -> None:
    loop, _ = _loop(panel=lambda: frozenset())

    assert loop.allows_chat("wxg:whatever") is True


def test_allows_chat_empty_panel_still_rejects_non_wechat_channels() -> None:
    loop, _ = _loop(panel=lambda: frozenset())

    assert loop.allows_chat("qq:123456") is False
    assert loop.allows_chat("") is False


def test_allows_chat_non_empty_panel_is_a_membership_check() -> None:
    loop, _ = _loop(panel=lambda: frozenset({"wxg:a1"}))

    assert loop.allows_chat("wxg:a1") is True
    assert loop.allows_chat("wxg:a2") is False


def test_allows_chat_without_panel_is_denied() -> None:
    peers = _Peers(
        {"a1": (ALLOWED, True, "group"), "a2": ("垃圾桶", True, "group")}
    )
    loop, _ = _loop(panel=None, registry=peers)

    # 没有可信白名单就一律拒绝：不退回任何配置清单。
    assert loop.allows_chat("wxg:a1") is False
    assert loop.allows_chat("wxg:a2") is False


def test_resolve_chat_name_obeys_poller_gate() -> None:
    peers = _Peers({"a1": ("垃圾桶", True, "group")})
    allowed = SimpleNamespace(registry=peers, allows_chat=lambda chat_id: True)
    denied = SimpleNamespace(registry=peers, allows_chat=lambda chat_id: False)

    assert bridge_main._resolve_chat_name(allowed, "wxg:a1") == "垃圾桶"
    with pytest.raises(bridge_main._CommandError) as excinfo:
        bridge_main._resolve_chat_name(denied, "wxg:a1")
    assert excinfo.value.code == "chat_not_allowed"


def test_resolve_chat_name_without_gate_allows_live_group_peer() -> None:
    peers = _Peers({"a1": ("垃圾桶", True, "group"), "a2": ("某人", True, "friend")})
    legacy = SimpleNamespace(registry=peers)

    # 没有面板接缝的旧测试替身：注册表已确认是存活群会话，直接放行。
    assert bridge_main._resolve_chat_name(legacy, "wxg:a1") == "垃圾桶"
    with pytest.raises(bridge_main._CommandError) as excinfo:
        bridge_main._resolve_chat_name(legacy, "wxg:a2")
    assert excinfo.value.code == "chat_not_allowed"


# --------------------------------------------------------------- 面板读取
def test_panel_groups_missing_file_means_empty_whitelist(tmp_path: Path) -> None:
    groups = PanelGroups(tmp_path / "settings.local.yaml")

    assert groups.whitelist() == frozenset()


def test_panel_groups_reloads_after_panel_edit(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.yaml"
    path.write_text("groups:\n  whitelist:\n  - wxg:aaa\n", encoding="utf-8")
    groups = PanelGroups(path)

    assert groups.whitelist() == frozenset({"wxg:aaa"})

    path.write_text(
        "groups:\n  whitelist:\n  - wxg:bbb\n  - wxg:ccc\n", encoding="utf-8"
    )

    assert groups.whitelist() == frozenset({"wxg:bbb", "wxg:ccc"})


def test_panel_groups_broken_file_keeps_last_good_value(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.yaml"
    path.write_text("groups:\n  whitelist:\n  - wxg:aaa\n", encoding="utf-8")
    groups = PanelGroups(path)
    assert groups.whitelist() == frozenset({"wxg:aaa"})

    path.write_text("groups: [", encoding="utf-8")

    assert groups.whitelist() == frozenset({"wxg:aaa"})


def test_panel_groups_broken_on_first_read_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.yaml"
    path.write_text("groups: [", encoding="utf-8")

    assert PanelGroups(path).whitelist() is None

# ------------------------------------------------- 微信私聊（好友）范围与闸门
FRIEND = "好友甲"
FRIEND_ALIAS = "f1"


class _FriendLocalDb:
    """群 + 好友都能枚举的假本地库；私聊开关关闭时好友不能被触碰。"""

    available = True

    def __init__(self, groups: list[str], friends: list[str]) -> None:
        self._groups = list(groups)
        self._friends = list(friends)
        self.friend_calls = 0
        self.calls: list[str] = []

    def group_names(self) -> list[str]:
        return list(self._groups)

    def friend_names(self) -> list[str]:
        self.friend_calls += 1
        return list(self._friends)

    def resolve_username(self, name: str) -> str | None:
        if name in self._groups:
            return "111@chatroom"
        if name in self._friends:
            return "wxid_f1"
        return None

    def peer_kind(self, name: str) -> str | None:
        if name in self._friends:
            return "friend"
        if name in self._groups:
            return "group"
        return None

    def snapshot(self, name: str) -> dict[str, Any] | None:
        self.calls.append(name)
        return {"chat_name": name, "messages": []}


def test_private_disabled_keeps_friends_out_of_default_scope() -> None:
    localdb = _FriendLocalDb(["老友群"], [FRIEND])
    loop, _ = _loop(panel=lambda: frozenset(), localdb=localdb)

    assert loop._scope() == ("老友群",)
    # 开关关闭时连枚举都不该发生。
    assert localdb.friend_calls == 0


def test_private_enabled_adds_friends_to_default_scope() -> None:
    localdb = _FriendLocalDb(["老友群"], [FRIEND])
    loop, _ = _loop(panel=lambda: frozenset(), localdb=localdb, private_enabled=True)

    assert loop._scope() == (FRIEND, "老友群")


def test_private_scope_skips_names_shared_by_group_and_friend() -> None:
    localdb = _FriendLocalDb(["同名"], ["同名", FRIEND])
    loop, _ = _loop(panel=lambda: frozenset(), localdb=localdb, private_enabled=True)

    assert loop._scope() == (FRIEND,)


def test_private_scope_drops_friend_without_resolvable_username() -> None:
    class _BadFriend(_FriendLocalDb):
        def resolve_username(self, name: str) -> str | None:
            return None if name == FRIEND else "111@chatroom"

    loop, _ = _loop(
        panel=lambda: frozenset(),
        localdb=_BadFriend(["老友群"], [FRIEND]),
        private_enabled=True,
    )

    assert loop._scope() == ("老友群",)


def test_private_outbound_requires_private_enabled() -> None:
    peers = _Peers({FRIEND_ALIAS: (FRIEND, True, "friend")})
    loop, _ = _loop(
        panel=lambda: frozenset({f"wxg:{FRIEND_ALIAS}"}), registry=peers
    )

    assert loop._scope() == ()
    assert loop.allows_chat(f"wxg:{FRIEND_ALIAS}") is False


def test_private_outbound_allowed_when_enabled_and_listed() -> None:
    peers = _Peers({FRIEND_ALIAS: (FRIEND, True, "friend")})
    loop, _ = _loop(
        panel=lambda: frozenset({f"wxg:{FRIEND_ALIAS}"}),
        registry=peers,
        private_enabled=True,
    )

    assert loop._scope() == (FRIEND,)
    assert loop.allows_chat(f"wxg:{FRIEND_ALIAS}") is True


def test_group_outbound_unaffected_by_private_switch() -> None:
    peers = _Peers({"g1": ("老友群", True, "group")})
    loop, _ = _loop(
        panel=lambda: frozenset({"wxg:g1"}),
        registry=peers,
        private_enabled=False,
    )

    assert loop._scope() == ("老友群",)
    assert loop.allows_chat("wxg:g1") is True


# ------------------------------------------- 私聊热开关的启动默认值（config.yaml）
STARTUP = WeChatRuntime(
    private_enabled=True,
    private_reply=True,
    private_allow=frozenset({"wxg:boot"}),
)


def test_panel_runtime_missing_file_returns_startup_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.yaml"

    assert PanelRuntime(path, defaults=STARTUP).wechat() == STARTUP


def test_panel_runtime_overlay_without_wechat_section_keeps_config_values(
    tmp_path: Path,
) -> None:
    """回归：`settings.local.yaml` 只有 `brain:` 时不能把私聊配置抹成全 False。"""
    path = tmp_path / "settings.local.yaml"
    path.write_text("brain:\n  mode: key\n", encoding="utf-8")

    assert PanelRuntime(path, defaults=STARTUP).wechat() == STARTUP


def test_panel_runtime_overlay_wechat_section_overrides_only_written_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.local.yaml"
    path.write_text("wechat:\n  private_reply: false\n", encoding="utf-8")

    current = PanelRuntime(path, defaults=STARTUP).wechat()

    assert current is not None
    assert current.private_enabled is True
    assert current.private_reply is False
    assert current.private_allow == STARTUP.private_allow


def test_panel_runtime_broken_overlay_without_defaults_is_still_none(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.local.yaml"
    path.write_text("wechat: [", encoding="utf-8")

    assert PanelRuntime(path).wechat() is None


def test_default_poller_factory_keeps_private_enabled_from_config(
    tmp_path: Path, monkeypatch
) -> None:
    """接线回归：覆盖层没有 `wechat:` 段时，轮询范围仍要包含配置里开启的好友。"""
    overlay = tmp_path / "settings.local.yaml"
    overlay.write_text("brain:\n  mode: key\n", encoding="utf-8")
    localdb = _FriendLocalDb(["老友群"], [FRIEND])

    monkeypatch.setattr(bridge_panel, "DEFAULT_PATH", overlay)
    monkeypatch.setattr(
        bridge_main, "_build_localdb_source", lambda wechat: localdb
    )
    monkeypatch.setattr(
        bridge_poller, "DEFAULT_ALIGN_STATE", str(tmp_path / "align-state.json")
    )
    monkeypatch.setattr(bridge_identity, "PeerRegistry", lambda: _Peers({}))

    loop = bridge_main._default_poller_factory(
        _wechat(private_enabled=True), _Driver()
    )

    assert loop._scope() == (FRIEND, "老友群")
