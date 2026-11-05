"""WeChatRuntime / WeChatRuntimeView：覆盖层 ``wechat:`` 段的热读取契约。

覆盖：默认值、mtime 变更热更新、坏 YAML 保留上次成功值、文件删除回落默认、
坏键值整体忽略。全部走临时文件，不碰真实的 data/settings.local.yaml。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from dafeiyu.hotconfig import WeChatRuntime, WeChatRuntimeView


def _write(path: Path, text: str) -> None:
    # Windows/overlay 写入可能落在同一 tick：轻微等待保证 (mtime_ns, size) 变化。
    time.sleep(0.02)
    path.write_text(text, encoding="utf-8")


DEFAULTS = WeChatRuntime(
    private_enabled=True,
    private_reply=True,
    private_allow=frozenset({"wxg:boot"}),
)


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    view = WeChatRuntimeView(DEFAULTS, tmp_path / "settings.local.yaml")

    assert view.current() == DEFAULTS


def test_overlay_hot_update_within_one_mtime_change(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.yaml"
    view = WeChatRuntimeView(DEFAULTS, path)

    assert view.current() == DEFAULTS

    _write(
        path,
        "wechat:\n"
        "  private_enabled: false\n"
        "  private_reply: false\n"
        "  private_allow: [WXG:AbC]\n"
        "  private_deny: [wxg:spam]\n"
        "brain:\n  mode: key\n",
    )
    current = view.current()

    assert current == WeChatRuntime(
        private_enabled=False,
        private_reply=False,
        private_allow=frozenset({"wxg:abc"}),
        private_deny=frozenset({"wxg:spam"}),
    )
    assert view.parsed_ok is True

    # 同样内容（stamp 相同）读出来不变；覆盖层重写后只写一个键也生效
    # （缺失键回落到启动默认）。
    _write(path, "wechat:\n  private_reply: true\n")
    assert view.current() == WeChatRuntime(
        private_enabled=DEFAULTS.private_enabled,
        private_reply=True,
        private_allow=DEFAULTS.private_allow,
        private_deny=DEFAULTS.private_deny,
    )


def test_broken_yaml_keeps_last_good_value(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.yaml"
    _write(path, "wechat:\n  private_enabled: false\n")
    view = WeChatRuntimeView(DEFAULTS, path)
    good = view.current()
    assert good.private_enabled is False

    _write(path, "wechat: [")

    assert view.current() == good


def test_deleted_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.yaml"
    _write(path, "wechat:\n  private_enabled: false\n  private_reply: false\n")
    view = WeChatRuntimeView(DEFAULTS, path)
    assert view.current().private_reply is False

    path.unlink()

    assert view.current() == DEFAULTS


def test_broken_on_first_read_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.yaml"
    _write(path, "wechat: [")

    assert WeChatRuntimeView(DEFAULTS, path).current() == DEFAULTS


def test_bad_typed_keys_are_ignored(tmp_path: Path) -> None:
    """类型不对的键整体忽略；not wechat 段 / 四键之外无动作。"""
    path = tmp_path / "settings.local.yaml"
    _write(
        path,
        "wechat:\n"
        "  private_enabled: 1\n"
        "  private_reply: [x]\n"
        "  private_allow: \"wxg:not-a-list\"\n"
        "  private_deny: [123, wxg:ok]\n"
        "  unknown_key: true\n",
    )
    view = WeChatRuntimeView(DEFAULTS, path)

    assert view.current() == DEFAULTS


def test_allow_deny_elements_strip_and_casefold(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.yaml"
    _write(
        path,
        "wechat:\n"
        "  private_allow: [\"  WXG:AbC \", wxg:abc, wxg:def]\n"
        "  private_deny: [\"\"]\n",
    )
    current = WeChatRuntimeView(DEFAULTS, path).current()

    assert current.private_allow == frozenset({"wxg:abc", "wxg:def"})
    assert current.private_deny == frozenset()


def test_read_failure_warns_only_once(tmp_path: Path, caplog) -> None:
    """读取失败只 warning 一次；随后仍返回上次成功值且不抛异常。"""
    target = tmp_path / "adir"
    target.mkdir()  # stat 成功但 read_text 抛 IsADirectoryError
    view = WeChatRuntimeView(DEFAULTS, target)

    with caplog.at_level(logging.WARNING):
        first = view.current()
        second = view.current()

    assert first == second == DEFAULTS
    assert len(caplog.records) == 1
