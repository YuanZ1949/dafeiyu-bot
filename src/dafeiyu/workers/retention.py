from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from ..memory.store import Store
from ..timeutil import now_shanghai

logger = logging.getLogger(__name__)


def prune_once(
    store: Store, retention_days: int, now: datetime | None = None
) -> int:
    """按天保留策略清一次；retention_days<=0 表示永久保留，直接返回 0。"""
    if retention_days <= 0:
        return 0
    current = now or now_shanghai()
    cutoff = int((current - timedelta(days=retention_days)).timestamp())
    return store.prune_messages(cutoff)


async def retention_loop(
    store: Store,
    retention_days: int,
    interval: float = 86400,
    stop: asyncio.Event | None = None,
) -> None:
    """启动即清理一次，之后按 interval 周期执行，直到 stop 被置位。"""
    if retention_days <= 0:
        return
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            deleted = prune_once(store, retention_days)
            if deleted:
                logger.info("清理过期消息 %s 条（保留 %s 天）", deleted, retention_days)
        except Exception:
            logger.exception("清理过期消息失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
