from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChatWindow:
    """一个会话当前的待投递窗口。时间都是 epoch 秒（time.time() 口径）。"""

    first_queued_at: float | None = None
    # 第一条点名消息：硬上限从这里起算。
    addressed_first_at: float | None = None
    # 最后一条点名消息：防抖每次都被它重置。
    addressed_last_at: float | None = None
    # 上一次非点名发言：决定下一条非点名消息最早什么时候能发。
    last_ambient_at: float | None = None


def note(window: ChatWindow, *, now: float, addressed: bool) -> None:
    if window.first_queued_at is None:
        window.first_queued_at = now
    if addressed:
        if window.addressed_first_at is None:
            window.addressed_first_at = now
        window.addressed_last_at = now


def due_at(
    window: ChatWindow,
    *,
    batch_seconds: float,
    debounce: float,
    max_wait: float,
    ambient_gap: float,
) -> float | None:
    if window.first_queued_at is None:
        return None
    if window.addressed_first_at is not None and window.addressed_last_at is not None:
        # 有人点名：等群里说完（防抖），但第一条点名起 max_wait 秒必须回。
        return min(window.addressed_last_at + debounce, window.addressed_first_at + max_wait)
    due = window.first_queued_at + batch_seconds
    if window.last_ambient_at is not None:
        # 刚发过言就别急着再插话。
        due = max(due, window.last_ambient_at + ambient_gap)
    return due


def on_flushed(window: ChatWindow, *, now: float, addressed: bool) -> None:
    window.first_queued_at = None
    window.addressed_first_at = None
    window.addressed_last_at = None
    if not addressed:
        window.last_ambient_at = now
