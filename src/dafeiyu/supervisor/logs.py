from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from datetime import datetime
from pathlib import Path

from .redact import redact

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
        self._buffers: dict[str, deque[tuple[int, str]]] = {
            s: deque(maxlen=ring) for s in SOURCES
        }
        self._subscribers: dict[str, set[asyncio.Queue[tuple[int, str]]]] = {
            s: set() for s in SOURCES
        }
        self._seqs: dict[str, int] = {s: 0 for s in SOURCES}
        self._rotate_existing()

    def _rotate_existing(self) -> None:
        """每次启动轮转一次，让当前文件 == 本次运行的日志。

        只按大小轮转（10MB）时，单次运行往往几周都到不了阈值，修复之前的
        历史错误会一直留在 ``supervisor.log`` 里，让「文件里没有 401」这类
        判据永远不成立。历史仍然保留在 ``.log.1``。
        """
        for source in SOURCES:
            target = self.dir / f"{source}.log"
            try:
                if target.exists() and target.stat().st_size > 0:
                    self._rotate(target)
            except OSError as exc:
                log.debug("启动轮转 %s 失败：%s", target, exc)

    def write(self, source: str, text: str) -> None:
        # 入口一次脱敏：环 / 落盘 / SSE 共用同一份文本，避免三处规则漂移。
        text = redact(str(text))
        if source not in self._buffers:
            self._buffers[source] = deque(maxlen=self.ring)
            self._subscribers[source] = set()
            self._seqs[source] = 0
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        seq = self._seqs[source]
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line:
                continue
            seq += 1
            entry = f"{stamp} [{source}] {line}"
            event = (seq, entry)
            self._buffers[source].append(event)
            for queue in list(self._subscribers[source]):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass
        self._seqs[source] = seq
        self._append_file(source, text)

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

    def log_path(self, source: str) -> Path:
        """当前来源的日志文件路径；来源必须先过 SOURCES 白名单。"""
        if source not in SOURCES:
            raise ValueError(f"未知日志来源：{source}")
        return self.dir / f"{source}.log"

    def tail_events(self, source: str, limit: int = 200) -> list[tuple[int, str]]:
        buffer = self._buffers.get(source)
        if buffer is None:
            return []
        if limit <= 0:
            return []
        return list(buffer)[-limit:]

    def tail(self, source: str, limit: int = 200) -> list[str]:
        return [line for _, line in self.tail_events(source, limit)]

    def events_after(self, source: str, seq: int) -> list[tuple[int, str]]:
        buffer = self._buffers.get(source)
        if buffer is None:
            return []
        return [(event_seq, line) for event_seq, line in buffer if event_seq > seq]

    def last_seq(self, source: str) -> int:
        return self._seqs.get(source, 0)

    def subscribe(self, source: str) -> asyncio.Queue[tuple[int, str]]:
        queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue(maxsize=self.ring)
        self._subscribers.setdefault(source, set()).add(queue)
        return queue

    def unsubscribe(self, source: str, queue: asyncio.Queue[tuple[int, str]]) -> None:
        bucket = self._subscribers.get(source)
        if bucket is not None:
            bucket.discard(queue)
