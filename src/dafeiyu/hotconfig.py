"""
运行时热配置：面板覆盖层 ``wechat:`` 段的只读缓存视图。

桥（poller 的轮询线程）与 bot（service 的事件环）要在各自线程里读这份开关，
所以缓存读写用 ``threading.Lock`` 保护；视图本身按 ``(st_mtime_ns, st_size)``
缓存，面板改完一个轮询周期内生效，不需要重启进程。

语义（P1-5 + P1-6 的核心层）：

- 总闸：``wechat.private_enabled`` / ``wechat.private_reply``（布尔值）；
- 名单：``wechat.private_allow`` / ``wechat.private_deny``。元素是
  ``wxg:<alias>`` 形式的稳定会话 id；deny 优先于 allow；allow 为空 = 不限制。
  这里只做 strip + casefold 归一，wxg 前缀校验由 config.parse_wechat_peer_ids
  在启动与写覆盖层时完成。

容错（消费方都在热路径上，这里绝不向上抛异常）：

- 文件不存在 = 没有覆盖 = 返回 defaults；
- 解析/读取失败 = 返回上一次成功值（没有则 defaults），只 warning 一次；
- 四个键之外的键、类型不对的值一律忽略，不让面板写坏一个字段就停摆。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .settings_local import DEFAULT_PATH, OverrideError, read_overrides

logger = logging.getLogger(__name__)

__all__ = ["WeChatRuntime", "WeChatRuntimeView"]


@dataclass(frozen=True)
class WeChatRuntime:
    """微信私聊的运行时开关快照；字段与覆盖层 ``wechat:`` 段一一对应。"""

    private_enabled: bool = False
    private_reply: bool = False
    private_allow: frozenset[str] = frozenset()
    private_deny: frozenset[str] = frozenset()


class WeChatRuntimeView:
    """``settings.local.yaml`` 里 ``wechat:`` 段的线程安全只读缓存。

    公开契约：``current()`` 永不抛异常，总是返回一个 :class:`WeChatRuntime`。
    ``parsed_ok``（构造以来是否成功解析过至少一次）给需要区分「文件不存在」
    与「文件坏了」的调用方（面板运行时视图）使用。
    """

    def __init__(self, defaults: WeChatRuntime, path: Path | None = None) -> None:
        self.defaults = defaults
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._cached: WeChatRuntime = defaults
        self._broken = False
        self.parsed_ok = False

    def current(self) -> WeChatRuntime:
        with self._lock:
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                # 没有覆盖层 = 面板从没改过 = 全部回落启动默认值。
                self._stamp = None
                self._cached = self.defaults
                self._broken = False
                self.parsed_ok = False
                return self._cached
            except OSError as exc:
                if not self._broken:
                    logger.warning("覆盖层不可读，沿用上次结果：%s", exc)
                    self._broken = True
                return self._cached

            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._stamp:
                return self._cached
            try:
                data = read_overrides(self.path)
            except (OverrideError, OSError) as exc:
                if not self._broken:
                    logger.warning("覆盖层解析失败，沿用上次结果：%s", exc)
                    self._broken = True
                return self._cached

            self._stamp = stamp
            self._broken = False
            self.parsed_ok = True
            self._cached = self._merge(data)
            return self._cached

    def _merge(self, data: dict[str, Any]) -> WeChatRuntime:
        """把覆盖层 wechat 段的合法键盖到 defaults 上；坏值整体忽略该键。"""
        section = data.get("wechat")
        if not isinstance(section, dict):
            return self.defaults
        updates: dict[str, Any] = {}
        for name in ("private_enabled", "private_reply"):
            value = section.get(name)
            if isinstance(value, bool):
                updates[name] = value
        for name in ("private_allow", "private_deny"):
            value = section.get(name)
            if isinstance(value, list) and all(
                isinstance(item, str) for item in value
            ):
                items = {item.strip().casefold() for item in value}
                items.discard("")
                updates[name] = frozenset(items)
        return replace(self.defaults, **updates)
