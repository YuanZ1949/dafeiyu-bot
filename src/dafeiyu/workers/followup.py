"""承诺跟进：对「答应过但没下文」的 promise loop 主动追问一句（默认关）。

三条硬规则：

* **不打断进行中的对话**：loop 创建之后该会话只要还有新的 bot 发言，就说
  明话题已经被接上，本轮直接跳过；
* **每日上限**：计数复用 ``mind_state`` 的
  ``followup/<chat_type>/<chat_id>/<YYYY-MM-DD>`` 键，不新建表；
* **静音/预算门控**与提醒轮询一致。整条链路不调用模型，也不新建 loop。

发送只走 ``Service._send_outbox``：失败会把文本放进 ``pending_outbox`` 由
outbox 负责重试，因此计数在发送前就 +1，避免同一句被重复入队。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..budget import BudgetAction
from ..models import ChatType
from ..timeutil import now_shanghai

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免与 service 循环导入
    from ..service import Service

logger = logging.getLogger(__name__)

__all__ = ["deliver_due_followups", "followup_loop"]

# 单轮最多处理多少条：到期堆积时也不要把一个 tick 拉得太长。
DUE_LIMIT = 20


def _day_key(chat_type: ChatType, chat_id: str) -> str:
    """当日计数的键；与进群欢迎同一范式，按上海日期翻篇。"""
    return f"followup/{chat_type.value}/{chat_id}/{now_shanghai().date().isoformat()}"


def _chat_has_newer_bot_message(
    service: Service, chat_type: ChatType, chat_id: str, created_epoch: int
) -> bool:
    """loop 创建后该会话是否已经有 bot 发言（有 = 对话在继续，别插嘴）。"""
    if created_epoch <= 0:
        # 时间解析不出来时保守跳过：宁可漏一次跟进，也不要打断正在聊的天。
        return True
    messages = service.store.messages_between(
        chat_type,
        chat_id,
        int(created_epoch) + 1,
        int(time.time()) + 3600,
    )
    return any(m.is_bot for m in messages)


async def _deliver_one(service: Service, row: dict[str, Any]) -> bool:
    """推一条承诺；返回是否真的发出了（用于日志/计数）。"""
    loop_id = int(row["id"])
    try:
        chat_type = ChatType(str(row["chat_type"]))
    except ValueError:
        logger.warning("承诺 %s 的 chat_type 非法，跳过", loop_id)
        return False
    chat_id = str(row["chat_id"])
    content = str(row["content"] or "").strip()
    if not content:
        return False
    if service.router.is_quiet(chat_type, chat_id):
        return False
    decision = service.budget_guard.evaluate(
        chat_type, chat_id, addressed=False, purpose="ambient"
    )
    if decision.action is not BudgetAction.ALLOW:
        return False
    if _chat_has_newer_bot_message(
        service, chat_type, chat_id, int(row.get("created_epoch") or 0)
    ):
        logger.debug("承诺 %s: 会话在 loop 创建后已有 bot 发言，跳过", loop_id)
        return False

    cap = service.settings.mind.followup_daily_cap
    key = _day_key(chat_type, chat_id)
    try:
        sent_today = int(service.store.get_mind_state(key, "0") or "0")
    except (TypeError, ValueError):
        sent_today = 0
    if sent_today >= cap:
        logger.debug("承诺 %s: %s 已达每日跟进上限 %s", loop_id, chat_id, cap)
        return False

    # 先计数再发送：发送失败会进 outbox 重试，不能让它再被 tick 命中一次。
    service.store.set_mind_state(key, str(sent_today + 1))
    text = f"上次说的「{content}」后来怎样了？"
    logger.info(
        "承诺跟进 %s/%s loop=%s（当日第 %s/%s 条）",
        chat_type.value,
        chat_id,
        loop_id,
        sent_today + 1,
        cap,
    )
    return bool(
        await service._send_outbox(
            chat_type, chat_id, text, origin=f"followup:{loop_id}"
        )
    )


async def deliver_due_followups(service: Service, *, limit: int = DUE_LIMIT) -> int:
    """处理一轮到期承诺；返回成功发出的条数（开关关时恒为 0）。"""
    mind = service.settings.mind
    if not mind.proactive_followup:
        return 0
    rows = service.store.due_promise_loops(mind.followup_after_hours, limit=limit)
    sent = 0
    for row in rows:
        try:
            if await _deliver_one(service, row):
                sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单行失败不拖垮整轮
            logger.exception("处理承诺 %s 失败", row.get("id"))
    return sent


async def followup_loop(
    service: Service,
    interval: float = 600.0,
    stop: asyncio.Event | None = None,
) -> None:
    """开关关时立即返回；否则启动即扫一轮，之后按 interval 周期执行。"""
    if not service.settings.mind.proactive_followup:
        return
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            sent = await deliver_due_followups(service)
            if sent:
                logger.info("已发送承诺跟进 %s 条", sent)
        except Exception:
            logger.exception("承诺跟进轮询失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
