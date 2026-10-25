"""批次投递：什么时候该回（window）与到点触发（scheduler）。

这里只有"时机"，不碰预算、静音、白黑名单——那些是 service 的事。
"""

from dafeiyu.delivery.scheduler import BatchScheduler
from dafeiyu.delivery.window import ChatWindow, due_at, note, on_flushed

__all__ = ["BatchScheduler", "ChatWindow", "due_at", "note", "on_flushed"]
