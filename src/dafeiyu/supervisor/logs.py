from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SOURCES = ("supervisor", "opencode", "bot", "napcat")
DEFAULT_RING = 2000
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 3


class LogHub:
    """内存环 + 落盘 + SSE 订阅，三件套。只在事件循环线程里写。"""

    def __init__(
        self,
        log_dir: str | Path,
        ring: int = DEFAULT_RING,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backups: int = DEFAULT_BACKUPS,
    ) -> None:
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ring = ring
        self.max_bytes = max_bytes
        self.backups = backups
        self._buffers: dict[str, deque[str]] = {s: deque(maxlen=ring) for s in SOURCES}
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {
            s: set() for s in SOURCES
        }

    def write(self, source: str, text: str) -> None:
        if source not in self._buffers:
            self._buffers[source] = deque(maxlen=self.ring)
            self._subscribers[source] = set()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for raw in str(text).splitlines():
            line = raw.rstrip()
            if not line:
                continue
            entry = f"{stamp} [{source}] {line}"
            self._buffers[source].append(entry)
            for queue in list(self._subscribers[source]):
                try:
                    queue.put_nowait(entry)
                except asyncio.QueueFull:
                    pass
        self._append_file(source, str(text))

    def _append_file(self, source: str, text: str) -> None:
        target = self.dir / f"{source}.log"
        try:
            if target.exists() and target.stat().st_size > self.max_bytes:
                self._rotate(target)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(text if text.endswith("\n") else text + "\n")
        except OSError as exc:
            log.debug("写日志文件 %s 失败：%s", target, exc)

    def _rotate(self, target: Path) -> None:
        for index in range(self.backups - 1, 0, -1):
            older = target.with_name(f"{target.name}.{index}")
            newer = target.with_name(f"{target.name}.{index + 1}")
            if index == self.backups - 1 and newer.exists():
                with contextlib.suppress(OSError):
                    newer.unlink()
            if older.exists():
                with contextlib.suppress(OSError):
                    older.replace(newer)
        with contextlib.suppress(OSError):
            target.replace(target.with_name(f"{target.name}.1"))

    def tail(self, source: str, limit: int = 200) -> list[str]:
        buffer = self._buffers.get(source)
        if buffer is None:
            return []
        if limit <= 0:
            return []
        return list(buffer)[-limit:]

    def subscribe(self, source: str) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.ring)
        self._subscribers.setdefault(source, set()).add(queue)
        return queue

    def unsubscribe(self, source: str, queue: asyncio.Queue[str]) -> None:
        bucket = self._subscribers.get(source)
        if bucket is not None:
            bucket.discard(queue)
