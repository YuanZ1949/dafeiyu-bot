"""面板白名单（groups.whitelist）接管桥的轮询范围与出站闸门。

面板语义：白名单非空 = 只碰列出的会话；空 = 不限制（本地库枚举到的全部群）。
读不出来时退回 `gateway.wechat.group_allowlist`，fail-closed。

全部用假注册表 / 假本地库，不碰 UIA、不碰真实微信数据。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dafeiyu.gateway.wechat_bridge.__main__ as bridge_main
from dafeiyu.config import WeChatConfig
from dafeiyu.gateway.wechat_bridge.panel import PanelGroups
from dafeiyu.gateway.wechat_bridge.poller import PollingLoop

ALLOWED = "老友群"


def _wechat(**overrides: Any) -> WeChatConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 0,
        "account_nickname": "小菜鱼",
        "group_allowlist": frozenset({ALLOWED}),
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


def test_panel_reader_failure_falls_back_to_config_allowlist() -> None:
    def boom() -> frozenset[str] | None:
        raise OSError("disk gone")

    loop, _ = _loop(panel=boom, localdb=_LocalDb(["老友群", "垃圾桶"]))

    assert loop._scope() == (ALLOWED,)


def test_no_panel_keeps_config_allowlist_scope() -> None:
    loop, _ = _loop(panel=None, localdb=_LocalDb(["老友群", "垃圾桶"]))

    assert loop._scope() == (ALLOWED,)


def test_panel_without_localdb_falls_back_to_allowlist_when_empty() -> None:
    loop, _ = _loop(panel=lambda: frozenset(), localdb=None)

    assert loop._scope() == (ALLOWED,)


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


def test_allows_chat_without_panel_keeps_legacy_allowlist() -> None:
    peers = _Peers(
        {"a1": (ALLOWED, True, "group"), "a2": ("垃圾桶", True, "group")}
    )
    loop, _ = _loop(panel=None, registry=peers)

    assert loop.allows_chat("wxg:a1") is True
    assert loop.allows_chat("wxg:a2") is False


def test_resolve_chat_name_obeys_poller_gate() -> None:
    peers = _Peers({"a1": ("垃圾桶", True, "group")})
    allowed = SimpleNamespace(registry=peers, allows_chat=lambda chat_id: True)
    denied = SimpleNamespace(registry=peers, allows_chat=lambda chat_id: False)

    assert bridge_main._resolve_chat_name(allowed, (), "wxg:a1") == "垃圾桶"
    with pytest.raises(bridge_main._CommandError) as excinfo:
        bridge_main._resolve_chat_name(denied, (), "wxg:a1")
    assert excinfo.value.code == "chat_not_allowed"


def test_resolve_chat_name_without_gate_falls_back_to_allowlist_argument() -> None:
    peers = _Peers({"a1": ("垃圾桶", True, "group")})
    legacy = SimpleNamespace(registry=peers)

    assert bridge_main._resolve_chat_name(legacy, ("垃圾桶",), "wxg:a1") == "垃圾桶"
    with pytest.raises(bridge_main._CommandError) as excinfo:
        bridge_main._resolve_chat_name(legacy, (ALLOWED,), "wxg:a1")
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
