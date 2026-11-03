"""面板白名单视图：``data/settings.local.yaml`` 的 ``groups.whitelist``。

面板（supervisor dashboard）改的是覆盖层，`config.yaml` 保持用户手写；
桥是独立进程，启动后不会再读配置。所以这里按 mtime 缓存地重读，让面板
改动在一个轮询间隔内生效，不需要重启桥。

语义与面板一致（``GET /api/groups`` 的 ``empty_means: "all"``）：

- 白名单非空：只轮询/只回复列出的会话（``wxg:<alias>`` 形式）；
- 白名单为空：不限制（桥轮询本地库能枚举到的全部群）。

读不出来时返回 ``None``，调用方退回 `gateway.wechat.group_allowlist`——
宁可少轮询，也不能因为一次 YAML 解析失败就把「全部群」打开。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...settings_local import DEFAULT_PATH, OverrideError, read_overrides

log = logging.getLogger(__name__)

__all__ = ["PanelGroups"]


class PanelGroups:
    """面板白名单的只读缓存；线程内使用（轮询循环 + 发送闸门同进程）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._stamp: tuple[int, int] | None = None
        self._whitelist: frozenset[str] = frozenset()
        self._broken = False

    def whitelist(self) -> frozenset[str] | None:
        """当前面板白名单；``None`` = 读不出来（调用方按配置 allowlist 处理）。"""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            # 没有覆盖层 = 面板从没改过 = 白名单为空（面板语义：不限制）。
            self._stamp = None
            self._broken = False
            self._whitelist = frozenset()
            return frozenset()
        except OSError as exc:
            if not self._broken:
                log.warning("面板白名单不可读，沿用上次结果：%s", exc)
                self._broken = True
            return self._whitelist if self._stamp is not None else None

        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return self._whitelist
        try:
            data = read_overrides(self.path)
        except (OverrideError, OSError) as exc:
            # 面板正写到一半或手改坏了：保留上一次成功读到的值。
            if not self._broken:
                log.warning("面板白名单解析失败，沿用上次结果：%s", exc)
                self._broken = True
            return self._whitelist if self._stamp is not None else None

        self._stamp = stamp
        self._broken = False
        section = data.get("groups")
        raw = section.get("whitelist") if isinstance(section, dict) else None
        values = raw if isinstance(raw, (list, tuple, set, frozenset)) else ()
        self._whitelist = frozenset(
            str(value).strip() for value in values if str(value).strip()
        )
        return self._whitelist
