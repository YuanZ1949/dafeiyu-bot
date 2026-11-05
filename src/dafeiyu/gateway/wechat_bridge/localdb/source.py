"""本地消息库帧源：把「密钥 + 解密镜像 + repository」包装成轮询循环的入站来源。

设计目标只有一个：**入站别停**。

- 懒加载：第一次 `snapshot()` 才去提密钥、建镜像；之前 UIA 路线照常跑；
- 失败即降级：任何一步（找不到库、提不到密钥、镜像坏了、表缺失）都只返回
  ``None``，由 :class:`~dafeiyu.gateway.wechat_bridge.poller.PollingLoop` 回落到
  UIA 开窗抓屏；降级持续 `retry_seconds` 秒后才重试，避免每轮都去扫进程内存；
- 只在后台线程里跑：解密/扫内存都是同步阻塞 op，调用方必须用
  ``asyncio.to_thread`` 包住，否则会把事件环卡到心跳超时。

**密钥不会离开本模块**：`keys.json` 由 `keyscan` 原子写入镜像目录，日志里只
打「匹配 N 个库」之类的计数。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import keyscan
from .mirror import MirrorSet
from .repository import LocalMessageStore

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_MIRROR_DIR", "DEFAULT_RETRY_SECONDS", "LocalDbSource"]

#: 镜像 + 密钥缓存的默认落盘目录（相对进程 cwd，和 peers.json 同一个父目录）。
DEFAULT_MIRROR_DIR = Path("data/wechat-bridge/localdb")
#: 建库失败后多久再试（秒）。太长会让「用户刚登录微信」等很久，太短又会反复扫内存。
DEFAULT_RETRY_SECONDS = 60.0


class LocalDbSource:
    """按群显示名返回 ``{"chat_name", "messages"}`` 的只读帧源。

    参数全部可注入，方便测试不碰真实微信目录：

    - `db_root`：库目录；留空用 `keyscan.find_db_root()` 自动探测；
    - `mirror_dir`：解密镜像与密钥缓存的落盘目录；
    - `refresh_seconds`：镜像增量同步的最小间隔；
    - `retry_seconds`：初始化失败后的冷却时间；
    - `clock`：单调时钟，测试注入。
    """

    def __init__(
        self,
        *,
        db_root: str | Path | None = None,
        self_display_name: str = "",
        mirror_dir: str | Path = DEFAULT_MIRROR_DIR,
        refresh_seconds: float = 1.0,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        cache_path: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        media_enabled: bool = False,
        media_max_bytes: int = 10 * 1024 * 1024,
        media_storage_root: str = "",
    ) -> None:
        self._db_root = Path(db_root) if db_root else None
        self._self_display_name = str(self_display_name or "")
        self._mirror_dir = Path(mirror_dir)
        self._refresh_seconds = max(0.0, float(refresh_seconds))
        self._retry_seconds = max(0.0, float(retry_seconds))
        self._cache_path = Path(cache_path) if cache_path else self._mirror_dir / "keys.json"
        self._clock = clock
        self._media_enabled = bool(media_enabled)
        self._media_max_bytes = int(media_max_bytes)
        self._media_storage_root = str(media_storage_root or "").strip()

        self._store: LocalMessageStore | None = None
        self._self_username = ""
        self._next_retry = 0.0
        self._building = False
        self._lock = threading.Lock()
        self._last_error = ""
        # 上一次打过日志的错误文本：同样的错误不重复刷屏。
        self._logged_error = ""

    # ------------------------------------------------------------- 状态
    @property
    def available(self) -> bool:
        """已建好且可查询；False 时调用方应继续走 UIA。"""
        return self._store is not None

    @property
    def self_username(self) -> str:
        return self._self_username

    @property
    def last_error(self) -> str:
        return self._last_error

    # ------------------------------------------------------------- 生命周期
    def prepare(self) -> bool:
        """同步建库（可能在扫进程内存，别在事件环里直接调）。幂等。"""
        self._ensure()
        return self.available

    async def prepare_async(self) -> bool:
        """`prepare()` 的线程池包装；`__main__` 用它在启动时预热。"""
        import asyncio

        return await asyncio.to_thread(self.prepare)

    def close(self) -> None:
        """丢弃句柄；下次 `snapshot()` 重新建库（镜像文件保留）。"""
        self._store = None
        self._self_username = ""
        self._next_retry = 0.0

    # ------------------------------------------------------------- 查询
    def snapshot(self, display_name: str) -> dict[str, Any] | None:
        """返回该会话最近 50 条消息；不可用时返回 None（调用方回落 UIA）。"""
        store = self._ensure()
        if store is None:
            return None
        name = str(display_name or "").strip()
        if not name:
            return None
        try:
            ref = store.resolve_any(name)
            if ref is None:
                log.debug("本地库找不到会话 %r（可能改名/未同步），回落 UIA", name)
                return None
            messages = store.recent(ref, 50)
        except Exception as exc:  # noqa: BLE001 - 读失败只能回落 UIA
            self._record_failure(f"读取 {name!r} 失败：{exc}")
            return None
        return {"chat_name": name, "messages": messages}

    def group_names(self) -> list[str]:
        """诊断用：本地库能看到的全部群显示名。"""
        store = self._ensure()
        if store is None:
            return []
        try:
            return list(store.group_names())
        except Exception as exc:  # noqa: BLE001 - 只影响诊断输出
            log.debug("枚举群名失败：%s", exc)
            return []

    def friend_names(self) -> list[str]:
        """本地库能枚举到的好友显示名；不可用时返回空列表。"""
        store = self._ensure()
        if store is None:
            return []
        try:
            return list(store.friend_names())
        except Exception as exc:  # noqa: BLE001 - 只影响私聊范围
            log.debug("枚举好友名失败：%s", exc)
            return []

    def peer_kind(self, display_name: str) -> str | None:
        """会话类型：``group`` / ``friend``；不存在或群/好友重名返回 None。"""
        store = self._ensure()
        if store is None:
            return None
        name = str(display_name or "").strip()
        if not name:
            return None
        try:
            return store.kind_of(name)
        except Exception as exc:  # noqa: BLE001 - 判断失败按「不知道」处理
            log.debug("判断会话 %r 类型失败：%s", name, exc)
            return None

    def resolve_username(self, display_name: str) -> str | None:
        """显示名 -> username（群 ``xxx@chatroom`` 或好友 ``wxid_*``）。

        不可用/歧义时返回 None（调用方 fail-closed）。
        """
        store = self._ensure()
        if store is None:
            return None
        name = str(display_name or "").strip()
        if not name:
            return None
        try:
            ref = store.resolve_any(name)
        except Exception as exc:  # noqa: BLE001 - 解析失败只是「没解析到」
            log.debug("本地库解析会话 %r 失败：%s", name, exc)
            return None
        return str(getattr(ref, "username", "") or "") or None

    def find_self_message(
        self, display_name: str, text: str, *, since: float
    ) -> dict[str, Any] | None:
        """回读自己刚发的消息；本地库不可用/找不到时返回 None。"""
        store = self._ensure()
        if store is None:
            return None
        name = str(display_name or "").strip()
        if not name:
            return None
        try:
            ref = store.resolve_any(name)
            if ref is None:
                return None
            return store.find_self_message(ref, text, since=since)
        except Exception as exc:  # noqa: BLE001 - 读失败只能「未确认」
            log.debug("本地库回读自己消息失败（%r）：%s", name, exc)
            return None

    # ------------------------------------------------------------- 内部
    def _ensure(self) -> LocalMessageStore | None:
        if self._store is not None:
            return self._store
        now = self._clock()
        if now < self._next_retry:
            return None
        # 只让一个线程去扫内存；其它线程直接拿到「还没好」并回落 UIA。
        with self._lock:
            if self._store is not None:
                return self._store
            if self._building:
                return None
            if self._clock() < self._next_retry:
                return None
            self._building = True
        try:
            store = self._build()
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级，不打断轮询
            self._record_failure(str(exc))
            return None
        finally:
            with self._lock:
                self._building = False
        with self._lock:
            self._store = store
            self._last_error = ""
        log.info(
            "本地消息库就绪：%s（群 %d 个，好友 %d 个，镜像 %s）",
            store,
            len(self.group_names()),
            len(self.friend_names()),
            self._mirror_dir,
        )
        return store

    def _build(self) -> LocalMessageStore:
        root = self._db_root or keyscan.find_db_root()
        if root is None:
            raise RuntimeError("找不到微信库目录（Documents\\xwechat_files\\*\\db_storage）")
        keys = keyscan.load_or_extract(root, self._cache_path)
        if not keys:
            raise RuntimeError("没有提取到任何数据库密钥")
        mirrors = MirrorSet(
            root,
            keys,
            self._mirror_dir,
            refresh_seconds=self._refresh_seconds,
        )
        self._self_username = keyscan.self_username(root) or ""
        if not self._self_username:
            log.warning("无法从库目录名解析自己的 wxid，机器人消息可能被当成他人消息")
        return LocalMessageStore(
            mirrors,
            self_username=self._self_username,
            self_display_name=self._self_display_name,
            media_enabled=self._media_enabled,
            media_max_bytes=self._media_max_bytes,
            media_storage_root=self._media_storage_root,
        )

    def _record_failure(self, message: str) -> None:
        self._last_error = message
        self._next_retry = self._clock() + self._retry_seconds
        # 只在错误变化时打 warning，避免每 8 秒刷一条同样的日志。
        if message != self._logged_error:
            self._logged_error = message
            log.warning(
                "本地消息库不可用，回落 UIA（%ss 后重试）：%s",
                int(self._retry_seconds),
                message,
            )

    def __repr__(self) -> str:  # pragma: no cover - 仅日志
        state = "ready" if self.available else "pending"
        return f"<LocalDbSource {state} root={self._db_root or 'auto'}>"
