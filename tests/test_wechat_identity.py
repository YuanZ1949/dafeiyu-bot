"""身份表：别名铸造、改名墓碑、同名 fail-closed、原子写回滚。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from dafeiyu.gateway.wechat_bridge import identity
from dafeiyu.gateway.wechat_bridge.identity import (
    AliasConflictError,
    IdentityError,
    PeerRegistry,
)


def make_registry(tmp_path: Path, alerts: list | None = None) -> PeerRegistry:
    return PeerRegistry(
        tmp_path / "data" / "wechat-bridge" / "peers.json",
        clock=lambda: 1000.0,
        on_alert=(lambda code, payload: alerts.append((code, payload)))
        if alerts is not None
        else None,
    )


def test_resolve_is_stable_per_display_name(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)

    first = registry.resolve("测试群")
    second = registry.resolve("测试群")

    assert first == second
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    assert registry.chat_id("测试群") == f"wxg:{first}"


def test_distinct_names_get_distinct_aliases(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)

    assert registry.resolve("甲群") != registry.resolve("乙群")


def test_alias_round_trips_through_peers_json(tmp_path: Path) -> None:
    first = make_registry(tmp_path)
    alias = first.resolve("测试群", kind="group")

    raw = json.loads(first.path.read_text(encoding="utf-8"))
    assert raw["v"] == identity.SCHEMA_VERSION
    assert raw["peers"][0]["alias"] == alias
    assert raw["peers"][0]["display_name"] == "测试群"
    assert raw["peers"][0]["kind"] == "group"
    assert raw["peers"][0]["retired_at"] is None

    reloaded = make_registry(tmp_path)
    assert reloaded.resolve("测试群") == alias
    # 名字没变就不许铸新别名：重启后记忆必须接得上。
    assert len(reloaded.all_peers()) == 1


def test_self_display_name_never_becomes_self_alias(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)

    alias = registry.resolve("self")

    assert alias != "self"
    raw = json.loads(registry.path.read_text(encoding="utf-8"))
    assert all(peer["alias"] != "self" for peer in raw["peers"])


def test_retire_leaves_tombstone_and_mints_new_alias(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    old_alias = registry.resolve("测试群", kind="group")

    assert registry.retire("测试群") is True

    peer = registry.by_alias(old_alias)
    assert peer is not None
    assert peer.retired_at == 1000.0
    assert registry.resolve("测试群") != old_alias
    # 墓碑不影响旧别名可查（历史消息仍要能渲染出会话名）。
    assert registry.by_alias(old_alias).display_name == "测试群"


def test_retire_unknown_name_is_noop(tmp_path: Path) -> None:
    assert make_registry(tmp_path).retire("没见过的群") is False


def test_duplicate_live_display_names_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "peers.json"
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "peers": [
                    {
                        "alias": "aaaaaaaaaaaa",
                        "display_name": "重名群",
                        "kind": "group",
                        "first_seen": 1.0,
                        "last_seen": 1.0,
                        "retired_at": None,
                    },
                    {
                        "alias": "bbbbbbbbbbbb",
                        "display_name": "重名群",
                        "kind": "group",
                        "first_seen": 2.0,
                        "last_seen": 2.0,
                        "retired_at": None,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    alerts: list = []
    registry = PeerRegistry(path, clock=lambda: 1000.0, on_alert=lambda c, p: alerts.append((c, p)))

    with pytest.raises(AliasConflictError) as excinfo:
        registry.resolve("重名群")

    assert excinfo.value.display_name == "重名群"
    assert excinfo.value.aliases == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert alerts[0][0] == "wechat_alias_ambiguous"
    assert alerts[0][1]["reason"] == "multiple_live"


def test_write_crash_keeps_original_file_and_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    registry = make_registry(tmp_path)
    alias = registry.resolve("甲群")
    before = registry.path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("disk full")

    monkeypatch.setattr(identity.os, "replace", boom)

    with pytest.raises(OSError):
        registry.resolve("乙群")

    # 原文件仍可解析且内容未变，临时文件已清理。
    assert registry.path.read_text(encoding="utf-8") == before
    assert json.loads(before)["peers"][0]["alias"] == alias
    assert list(registry.path.parent.glob("*.tmp")) == []
    # 内存也回滚：乙群没有留下半个身份。
    assert len(registry.all_peers()) == 1


def test_observe_flags_ambiguous_rename_without_merging(tmp_path: Path) -> None:
    alerts: list = []
    registry = make_registry(tmp_path, alerts)

    first = registry.observe("群旧名", {"阿猫", "阿狗"})
    second = registry.observe("群新名", {"阿狗", "阿鸡"})

    assert first != second
    assert any(code == "wechat_alias_ambiguous" for code, _ in alerts)
    code, payload = alerts[0]
    assert code == "wechat_alias_ambiguous"
    assert payload["names"] == sorted(["群旧名", "群新名"])
    assert payload["overlap"] == ["阿狗"]


def test_observe_with_disjoint_members_stays_silent(tmp_path: Path) -> None:
    alerts: list = []
    registry = make_registry(tmp_path, alerts)

    registry.observe("甲群", {"阿猫"})
    registry.observe("乙群", {"阿狗"})

    assert alerts == []


def test_corrupt_peers_file_is_loud(tmp_path: Path) -> None:
    path = tmp_path / "peers.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(IdentityError):
        PeerRegistry(path)


def test_rejects_empty_name_and_bad_kind(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)

    with pytest.raises(IdentityError):
        registry.resolve("   ")
    with pytest.raises(IdentityError):
        registry.resolve("测试群", kind="channel")


def test_bool_display_name_is_still_text(tmp_path: Path) -> None:
    """防御性：调用方传错类型时也不能铸出一个空别名。"""
    registry = make_registry(tmp_path)

    alias = registry.resolve(True)  # type: ignore[arg-type]

    assert re.fullmatch(r"[0-9a-f]{12}", alias)
    assert registry.by_alias(alias).display_name == "True"
