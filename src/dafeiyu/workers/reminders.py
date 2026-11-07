"""提醒轮询：把到期提醒投递到会话，投递语义是 at-most-once。

每一轮先过静音与预算两道门：被拦下时提醒保持 ``pending``，等下一轮再说。
真正投递时走 ``Service._send_outbox``——它失败会把文本放进 ``pending_outbox``
由 outbox 负责重试，因此提醒行无论投递成功与否都必须在 ``finally`` 里标记
``sent``；否则下一轮会再发一次，变成 at-least-once。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..budget import BudgetAction
from ..models import ChatType

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免与 service 循环导入
    from ..service import Service

logger = logging.getLogger(__name__)

__all__ = ["deliver_due_reminders", "reminder_loop"]

# 单轮最多处理多少条：到期堆积时也不要把一个 tick 拉得太长。
DUE_LIMIT = 20


async def _deliver_one(service: Service, row: dict[str, Any]) -> bool:
    """投递一条到期提醒；返回是否真的发出了（用于日志/计数）。"""
    reminder_id = int(row["id"])
    try:
        chat_type = ChatType(str(row["chat_type"]))
    except ValueError:
        # 坏行不能把整个 tick 拖死：直接终态，错误写在行上。
        service.store.mark_reminder_sent(
            reminder_id, last_error=f"invalid chat_type: {row['chat_type']!r}"[:300]
        )
        logger.warning("提醒 %s 的 chat_type 非法，跳过", reminder_id)
        return False
    chat_id = str(row["chat_id"])
    if service.router.is_quiet(chat_type, chat_id):
        return False
    decision = service.budget_guard.evaluate(
        chat_type, chat_id, addressed=False, purpose="ambient"
    )
    if decision.action is not BudgetAction.ALLOW:
        return False

    text = f"提醒：{row['content']}"
    error: str | None = None
    sent = False
    try:
        ok = await service._send_outbox(
            chat_type, chat_id, text, origin=f"reminder:{reminder_id}"
        )
        sent = bool(ok)
        if not ok:
            # _send_outbox 已经把文本放进 pending_outbox（除非 enqueue 自己炸了）。
            error = "send_failed: queued in outbox"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何失败都只记在行上，不重试
        error = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("提醒 %s 投递异常: %s", reminder_id, exc)
    finally:
        # at-most-once：不管成功、失败还是 enqueue 抛异常，都立即终态。
        service.store.mark_reminder_sent(reminder_id, last_error=error)
    return sent


async def deliver_due_reminders(
    service: Service, *, now: float | None = None, limit: int = DUE_LIMIT
) -> int:
    """处理一轮到期提醒；返回成功发出的条数。"""
    moment = time.time() if now is None else float(now)
    rows = service.store.due_reminders(moment, limit=limit)
    sent = 0
    for row in rows:
        try:
            if await _deliver_one(service, row):
                sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单行失败不拖垮整轮
            logger.exception("处理提醒 %s 失败", row.get("id"))
    return sent


async def reminder_loop(
    service: Service,
    interval: float = 30.0,
    stop: asyncio.Event | None = None,
) -> None:
    """启动即扫一轮，之后按 interval 周期执行，直到 stop 被置位。"""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            sent = await deliver_due_reminders(service)
            if sent:
                logger.info("已发送到期提醒 %s 条", sent)
        except Exception:
            logger.exception("提醒轮询失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
