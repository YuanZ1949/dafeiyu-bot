"""微信桌面桥的 adapter 端（主进程侧）。

按 `docs/superpowers/specs/2026-11-01-wechat-bridge-protocol.md` 的 v1 信封实现：

- 连接 `ws://{bridge_host}:{bridge_port}/wechat-bridge`，可选 `Authorization: Bearer`；
- `message` / `recall` / `health` / `resync` 四类事件转成进程内的 `Message` /
  `RecallEvent` / 健康字段；
- 发送走 `send_text` 指令 + 同 `id` 回执（照抄 OneBot 的 echo/pending 模式）；
- 三层回声过滤：`is_bot:true`、本地 (chat, text, 时间窗)、`resync` 清空。

本模块**不允许** import pyweixin / pywinauto / pyautogui：微信桥的桌面依赖
只存在于 `gateway/wechat_bridge/`，这里必须能在没装桥依赖的机器上导入。

选型已在 2026-11-01 复核后定稿：走**免费 UIA 桥**（`wechat_bridge` + `pyweixin`，
PyPI 包名 `pywechat127`，支持微信 4.1.6+），不再保留「三条候选路径」的说法。
hook 系方案均已不可用（WeChatFerry 2026-07-10 归档、Gewechat 停止维护、
wxhook 锁 3.9.5.81），wxauto 开源版止于 3.9.X，WeChatPadPro 付费且受 DMCA 压力，
腾讯官方 ClawBot/iLink 仅支持 1v1 私聊、进不了群。代价与边界（无真实 message_id、
无撤回、同名群需人工区分、锁屏掉线、封号自担）见
`docs/superpowers/specs/2026-10-24-im-bot-design.md` 第 3.1 节「已更新」段落。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from ..config import ConfigError, WeChatConfig
from ..models import Attachment, ChatType, Message, RecallEvent
from .base import GatewayError

logger = logging.getLogger(__name__)

_BRIDGE_PATH = "/wechat-bridge"
_PROTOCOL_VERSION = 1
_SEND_TIMEOUT = 150.0  # 必须大于桥服务器的发送超时 120s，否则客户端先重试造成重复发送
_STOP_TIMEOUT = 1.0
_MAX_FRAME = 1 << 20
_RECONNECT_DELAYS = (1, 2, 4, 8, 16, 30)
_CHAT_TYPES = {"group": ChatType.GROUP, "private": ChatType.PRIVATE}
#: 信封 kind 白名单：只认这些；缺省/未知一律按 text 处理。
_MESSAGE_KINDS = frozenset(
    {
        "text",
        "image",
        "voice",
        "video",
        "file",
        "sticker",
        "card",
        "location",
        "call",
        "link",
        "quote",
        "record",
        "forward",
    }
)
_DESKTOPS = ("interactive", "locked", "no_wechat")
_SENT_ECHO_MAX = 200


class WeChatAdapter:
    """微信桥客户端：字符串 id（`wxg:<alias>`）与 `wxg:self` 身份。"""

    def __init__(self, config: WeChatConfig) -> None:
        self._config = config
        self._handler: Callable[[Message], Awaitable[None]] | None = None
        self._recall_handler: Callable[[RecallEvent], Awaitable[None]] | None = None
        self._ws: Any | None = None
        self._task: asyncio.Task[None] | None = None
        # 入站 message/recall 在后台任务里处理，收帧循环从此不被 handler 阻塞
        # （否则 handler 里 await send 的回执永远读不进来 → 150s 超时 + 重复回复）。
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        # 每 (chat_type, chat_id) 一把锁保证单聊内按到达顺序串行；asyncio.Lock
        # 的 acquire 是 FIFO，任务创建顺序即到达顺序。
        self._chat_locks: dict[tuple[ChatType, str], asyncio.Lock] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._sent_echo: deque[tuple[str, str, float]] = deque(maxlen=_SENT_ECHO_MAX)
        self._echo_seed: Callable[[], Sequence[tuple[str, str, int]]] | None = None
        self._stopping = False
        self._socket_open = False
        self._health_connected = True
        self._last_health_at: float | None = None
        self._desktop = "interactive"
        # 只公开快照需要的状态，不保留消息正文。
        self.logged_in = False
        self.degraded = False
        self.queue_depth = 0
        self.wechat_version: str | None = None
        self.language: str | None = None
        self.last_event_at: float | None = None
        self.last_disconnect_at: float | None = None
        self.last_send_at: float | None = None

    # ----------------------------------------------------------- 身份/状态
    @property
    def bot_id(self) -> str:
        """机器人自己的稳定身份：不随微信显示名变化。"""
        return "wxg:self"

    @property
    def desktop(self) -> str:
        return self._desktop

    @property
    def connected(self) -> bool:
        if not self._socket_open or not self._health_connected:
            return False
        if self._last_health_at is None:
            return False
        # 桥进程卡死时 socket 仍开着：以健康帧的新鲜度兜底。
        return (time.monotonic() - self._last_health_at) <= (
            3 * self._config.poll_seconds
        )

    def on_message(self, handler: Callable[[Message], Awaitable[None]]) -> None:
        self._handler = handler

    def on_recall(self, handler: Callable[[RecallEvent], Awaitable[None]]) -> None:
        self._recall_handler = handler

    def set_echo_seed(
        self, provider: Callable[[], Sequence[tuple[str, str, int]]] | None
    ) -> None:
        """注册回声重建源（通常来自 Service → store.recent_bot_messages）。

        provider 返回 ``(chat_id, text, timestamp)``；`timestamp` 是本地 epoch
        秒（messages 表落库值，不是 UTC 的 created_at），resync 时按它比较窗口。
        """
        self._echo_seed = provider

    # ------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self._validate_config()
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._run_forever(), name="wechat-adapter"
        )

    async def stop(self) -> None:
        self._stopping = True
        await self._send_stop()
        ws, self._ws = self._ws, None
        self._socket_open = False
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # 兜底：连接断开的 teardown 已收过一轮，这里保证 stop() 返回时必空。
        await self._cancel_inbound_tasks()
        self._fail_pending(GatewayError("微信桥已关闭"))

    def enter_kill_switch(self) -> None:
        """急停：立即在本地标记断线并失败在途发送，不关 WS、不杀任务。"""
        self._health_connected = False
        self.last_disconnect_at = time.time()
        self._fail_pending(GatewayError("微信已急停"))

    async def resume(self) -> None:
        """让桥进程恢复轮询（kill 文件必须已被删除，由调用方保证）。"""
        ws = self._ws
        if ws is None:
            raise GatewayError("微信桥未连接")
        command_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        envelope = {
            "v": _PROTOCOL_VERSION,
            "id": command_id,
            "action": "resume",
            "params": {},
        }
        try:
            await ws.send(json.dumps(envelope, ensure_ascii=False))
            receipt = await asyncio.wait_for(future, _SEND_TIMEOUT)
        except TimeoutError as exc:
            raise GatewayError("微信恢复等待回执超时") from exc
        except ConnectionClosed as exc:
            raise GatewayError("微信桥连接已断开") from exc
        finally:
            self._pending.pop(command_id, None)
        if not bool(receipt.get("ok", False)):
            raise GatewayError(f"微信恢复失败：{receipt.get('error_code') or 'unknown'}")
        self._health_connected = True
        self._last_health_at = time.monotonic()
        logger.info("微信通道已恢复轮询")

    def _validate_config(self) -> None:
        """就绪度在加载阶段只告警，真正拒绝发生在这里（doctor 仍能出报告）。"""
        if not self._config.account_nickname:
            raise ConfigError(
                "缺少 gateway.wechat.account_nickname：无法判定是否被 @"
            )

    async def _run_forever(self) -> None:
        delay_index = 0
        while not self._stopping:
            try:
                await self._connect_once()
                delay_index = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 断线是常态，退避重连
                logger.warning("微信桥连接失败：%s", exc)
            if self._stopping:
                break
            delay = _RECONNECT_DELAYS[min(delay_index, len(_RECONNECT_DELAYS) - 1)]
            delay_index += 1
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        headers = None
        if self._config.bridge_token:
            headers = {"Authorization": f"Bearer {self._config.bridge_token}"}
        uri = (
            f"ws://{self._config.bridge_host}:{self._config.bridge_port}"
            f"{_BRIDGE_PATH}"
        )
        async with connect(
            uri, additional_headers=headers, max_size=_MAX_FRAME
        ) as ws:
            self._ws = ws
            self._socket_open = True
            self._health_connected = True
            self._last_health_at = time.monotonic()
            logger.info("微信桥已连接 %s", uri)
            try:
                async for raw in ws:
                    envelope = self._parse_frame(raw)
                    if envelope is None:
                        continue
                    # message/recall 转后台任务：handler 里可能 await send 等回执，
                    # 回执/健康/resync 必须仍在本收帧循环里立刻处理。
                    if self._schedule_inbound(envelope):
                        continue
                    await self.dispatch_envelope(envelope)
            except ConnectionClosed:
                pass
            finally:
                self._socket_open = False
                self._ws = None
                self.last_disconnect_at = time.time()
                # 只失败在途回执让 handler 自然收场（send 抛错 → 服务层记入
                # 重试队列）；后台任务不取消，断线抖动不许丢掉半路的回复，
                # 彻底停机时才由 stop() 统一收割。
                self._fail_pending(GatewayError("微信桥连接已断开"))
                logger.info("微信桥连接已断开")

    def _parse_frame(self, raw: str | bytes) -> dict[str, Any] | None:
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("收到无法解析的微信桥帧")
            return None
        if not isinstance(envelope, dict):
            return None
        return envelope

    # --------------------------------------------------------- 入站后台任务
    def _schedule_inbound(self, envelope: dict[str, Any]) -> bool:
        """message/recall 且 handler 已登记时转后台任务；返回是否已接手。

        受理判定（parse + 回声过滤）在这里同步做：last_event_at 保持「信封被
        接受即更新、回声/无效不更新」的语义，且写在调度之前；后台任务里
        dispatch_envelope 会对同一信封重复判定，结果一致，无副作用。
        """
        kind = envelope.get("type")
        if kind == "message" and self._handler is not None:
            message = self._parse_message(envelope)
            if message is None or self._is_echo(message.chat_id, message.content):
                return False
            self.last_event_at = time.time()
            self._spawn_inbound(
                (message.chat_type, message.chat_id), envelope
            )
            return True
        if kind == "recall" and self._recall_handler is not None:
            event = self._parse_recall(envelope)
            if event is None:
                return False
            self.last_event_at = time.time()
            self._spawn_inbound((event.chat_type, event.chat_id), envelope)
            return True
        return False

    def _spawn_inbound(
        self, key: tuple[ChatType, str], envelope: dict[str, Any]
    ) -> None:
        lock = self._chat_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[key] = lock
        task = asyncio.create_task(
            self._run_inbound(lock, envelope), name="wechat-inbound"
        )
        self._inbound_tasks.add(task)
        task.add_done_callback(self._on_inbound_done)

    async def _run_inbound(
        self, lock: asyncio.Lock, envelope: dict[str, Any]
    ) -> None:
        # 同一 chat 拿同一把锁 → 到达顺序即处理顺序；不同 chat 各自的锁互不阻塞。
        async with lock:
            await self.dispatch_envelope(envelope)

    def _on_inbound_done(self, task: asyncio.Task[None]) -> None:
        self._inbound_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception("微信入站处理任务异常", exc_info=exc)

    async def _cancel_inbound_tasks(self) -> None:
        tasks, self._inbound_tasks = list(self._inbound_tasks), set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------- 信封分派
    async def dispatch_envelope(self, envelope: dict[str, Any]) -> None:
        """处理一封已解析的 JSON 信封。

        公开 seam：WS 收帧循环、task 12 的契约测试与 F3 的注入接口共用它，
        不额外引入生产行为。
        """
        if not isinstance(envelope, dict):
            return
        if envelope.get("v") != _PROTOCOL_VERSION:
            logger.warning("微信桥信封版本不匹配：%r", envelope.get("v"))
            return

        kind = envelope.get("type")
        if kind == "message":
            message = self._parse_message(envelope)
            if message is None:
                return
            if self._is_echo(message.chat_id, message.content):
                logger.debug("回声过滤命中 %s", message.message_id)
                return
            self.last_event_at = time.time()
            if self._handler is not None:
                await self._handler(message)
            return
        if kind == "recall":
            event = self._parse_recall(envelope)
            if event is None:
                return
            self.last_event_at = time.time()
            if self._recall_handler is not None:
                await self._recall_handler(event)
            return
        if kind == "health":
            self._apply_health(envelope)
            return
        if kind == "resync":
            # 只清本地回声表，绝不回溯补发历史（否则批次调度器洪水）；
            # 随后按 seed 取回 messages 窗口内的机器人消息重建回声表。
            # 种子的 timestamp 是本地 epoch 秒（不是 created_at 的 UTC），
            # 窗口判断直接按它算。core 不回填、不触发批次调度器。
            self._sent_echo.clear()
            logger.info(
                "收到 resync（reason=%s），已清空回声表",
                envelope.get("reason"),
            )
            if self._echo_seed is not None:
                try:
                    seed_rows = self._echo_seed()
                except Exception:
                    logger.warning("回声重建 seed 失败，按空回声表继续", exc_info=True)
                    return
                now_real = time.time()
                now_mono = time.monotonic()
                # 必须按 timestamp 从旧到新入队：`_is_echo` 的窗口清理只从
                # 队首 popleft，队首一旦不是最旧的一条，过期条目就永远删不掉，
                # 旧回复会被误判成回声（store 的 seed 是 newest-first）。
                for chat_id, text, ts in sorted(seed_rows, key=lambda row: float(row[2])):
                    age = max(0.0, now_real - float(ts))
                    if age > self._config.echo_window_seconds:
                        continue
                    self._sent_echo.append((str(chat_id), str(text), now_mono - age))
            return
        if "id" in envelope:
            self._dispatch_receipt(envelope)
            return
        logger.warning("未知微信桥信封 type=%r", kind)

    def _attachments(
        self, envelope: dict[str, Any], kind: str, content: str
    ) -> list[Attachment]:
        """本地库发现的媒体优先；没有 media 时保持旧的占位附件形状。"""
        media = envelope.get("media")
        if isinstance(media, dict):
            path = str(media.get("path") or "").strip()
            if path:
                name = str(media.get("name") or "").strip()
                if not name:
                    name = content[:60] or path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                return [
                    Attachment(
                        kind=kind,
                        path=path,
                        name=name,
                        size=_int(media.get("size")),
                        duration=_int(media.get("duration")),
                    )
                ]
        if kind != "text" and content:
            return [Attachment(kind=kind, name=content[:60])]
        return []

    def _parse_message(self, envelope: dict[str, Any]) -> Message | None:
        # 第一层回声过滤：桥自己标记的 bot 消息直接丢弃。
        if bool(envelope.get("is_bot", False)):
            return None
        chat_id = str(envelope.get("chat_id") or "")
        message_id = str(envelope.get("message_id") or "")
        if not chat_id.startswith("wxg:") or not message_id:
            logger.warning(
                "微信消息缺 chat_id/message_id，已丢弃：%r",
                envelope.get("message_id"),
            )
            return None
        chat_type = _CHAT_TYPES.get(str(envelope.get("chat_type") or ""))
        if chat_type is None:
            logger.warning("微信消息 chat_type 未知：%r", envelope.get("chat_type"))
            return None
        raw_kind = str(envelope.get("kind") or "text").strip().lower()
        kind = raw_kind if raw_kind in _MESSAGE_KINDS else "text"
        content = str(envelope.get("text") or "")
        attachments = self._attachments(envelope, kind, content)
        return Message(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=str(envelope.get("user_id") or ""),
            nickname=str(envelope.get("nickname") or ""),
            content=content,
            message_id=message_id,
            at_bot=bool(envelope.get("at_bot", False)),
            reply_to_bot=bool(envelope.get("reply_to_bot", False)),
            reply_to_text=(
                str(envelope["reply_to_text"])
                if envelope.get("reply_to_text") is not None
                else None
            ),
            timestamp=_int(envelope.get("ts")),
            kind=kind,
            attachments=attachments,
        )

    def _parse_recall(self, envelope: dict[str, Any]) -> RecallEvent | None:
        chat_id = str(envelope.get("chat_id") or "")
        message_id = str(envelope.get("message_id") or "")
        if not chat_id.startswith("wxg:") or not message_id:
            logger.warning("微信撤回事件缺 chat_id/message_id，已丢弃")
            return None
        chat_type = _CHAT_TYPES.get(str(envelope.get("chat_type") or "group"))
        if chat_type is None:
            return None
        return RecallEvent(
            chat_type=chat_type,
            chat_id=chat_id,
            message_id=message_id,
            operator_id=str(envelope.get("user_id") or ""),
        )

    def _apply_health(self, envelope: dict[str, Any]) -> None:
        self._last_health_at = time.monotonic()
        self._health_connected = bool(envelope.get("connected", False))
        self.logged_in = bool(envelope.get("logged_in", False))
        desktop = str(envelope.get("desktop") or "no_wechat")
        self._desktop = desktop if desktop in _DESKTOPS else "no_wechat"
        self.degraded = bool(envelope.get("degraded", False))
        self.queue_depth = _int(envelope.get("queue_depth"))
        version = envelope.get("wechat_version")
        self.wechat_version = str(version) if version is not None else None
        language = envelope.get("language")
        self.language = str(language) if language is not None else None
        event_at = _float_or_none(envelope.get("last_event_at"))
        if event_at is not None and (
            self.last_event_at is None or event_at > self.last_event_at
        ):
            self.last_event_at = event_at
        send_at = _float_or_none(envelope.get("last_send_at"))
        # 健康帧只是补充；send() 自己写的 last_send_at 才是 F3 的判据。
        if send_at is not None and (
            self.last_send_at is None or send_at > self.last_send_at
        ):
            self.last_send_at = send_at

    def _dispatch_receipt(self, envelope: dict[str, Any]) -> None:
        receipt_id = str(envelope.get("id") or "")
        future = self._pending.pop(receipt_id, None)
        if future is None or future.done():
            return
        future.set_result(envelope)

    def _is_echo(self, chat_id: str, text: str) -> bool:
        """第二层回声过滤：同 chat + 同文本 + 在窗口内。"""
        now = time.monotonic()
        window = self._config.echo_window_seconds
        while self._sent_echo and now - self._sent_echo[0][2] > window:
            self._sent_echo.popleft()
        return any(
            cid == chat_id and sent_text == text
            for cid, sent_text, _at in self._sent_echo
        )

    # ----------------------------------------------------------------- 发送
    def _desktop_required(self) -> bool:
        """出站是否依赖可交互桌面：只有 uia 后端是。

        `hook` / `auto` 不靠 UIA 打字，锁屏时仍应把指令发给桥；真正的失败由桥
        回执里的 error_code 表达，而不是在客户端被 desktop 闸门直接拦掉。
        """
        return bool(getattr(self._config, "outbound_requires_desktop", True))

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str | None:
        _ok, value = await self._send_action(
            "send_text", {"chat_id": chat_id, "text": text}, echo_text=text
        )
        return value

    async def send_at(
        self, chat_type: ChatType, chat_id: str, text: str, at: list[str]
    ) -> str | None:
        """带 @ 的文本；`at` 为空时完全等价普通 send（不发空数组帧）。"""
        if not at:
            return await self.send(chat_type, chat_id, text)
        _ok, value = await self._send_action(
            "send_at",
            {"chat_id": chat_id, "text": text, "at": list(at)},
            echo_text=text,
        )
        return value

    async def send_media(
        self, chat_type: ChatType, chat_id: str, kind: str, path: str, name: str = ""
    ) -> bool:
        """发媒体；桥明确回 `unsupported_media` 时返回 False，不抛错也不重试。"""
        ok, _value = await self._send_action(
            "send_media",
            {"chat_id": chat_id, "kind": kind, "path": path, "name": name},
            unsupported_media=True,
        )
        return ok

    async def _send_action(
        self,
        action: str,
        params: dict[str, Any],
        *,
        echo_text: str | None = None,
        unsupported_media: bool = False,
    ) -> tuple[bool, str | None]:
        """所有出站动作的唯一实现：连接/桌面闸门/回执/回声表只此一份。"""
        if not self.connected:
            raise GatewayError("微信桥未连接")
        if self._desktop != "interactive" and self._desktop_required():
            raise GatewayError(f"微信桌面不可交互：{self._desktop}")
        ws = self._ws
        if ws is None:
            raise GatewayError("微信桥未连接")

        command_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[command_id] = future
        envelope = {
            "v": _PROTOCOL_VERSION,
            "id": command_id,
            "action": action,
            "params": params,
        }
        try:
            await ws.send(json.dumps(envelope, ensure_ascii=False))
            receipt = await asyncio.wait_for(future, _SEND_TIMEOUT)
        except TimeoutError as exc:
            raise GatewayError("微信发送等待回执超时") from exc
        except ConnectionClosed as exc:
            raise GatewayError("微信桥连接已断开") from exc
        finally:
            self._pending.pop(command_id, None)

        ok = bool(receipt.get("ok", False))
        delivered = str(receipt.get("delivered") or "failed")
        error_code = str(receipt.get("error_code") or "")
        if not ok or delivered == "failed":
            if unsupported_media and error_code == "unsupported_media":
                # 明确不支持：不重试、不回落、不记回声，调用方继续发文本。
                logger.info(
                    "微信桥不支持媒体发送 %s kind=%s",
                    params.get("chat_id"),
                    params.get("kind"),
                )
                return False, None
            raise GatewayError(f"微信发送失败：{error_code or delivered}")

        message_id = receipt.get("message_id")
        value = str(message_id) if message_id is not None else None
        self.last_send_at = time.time()
        if echo_text is not None:
            # 成功（typed/observed）才记回声表；媒体没有可匹配文本，不记。
            self._sent_echo.append(
                (str(params["chat_id"]), echo_text, time.monotonic())
            )
            if delivered == "observed":
                logger.info("微信消息已送达 %s msg_id=%s", params["chat_id"], value)
            else:
                logger.warning(
                    "微信回执仅 typed（未确认气泡）%s msg_id=%s",
                    params["chat_id"],
                    value,
                )
        else:
            logger.info("微信媒体已发送 %s kind=%s", params.get("chat_id"), params.get("kind"))
        return True, value

    async def _send_stop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        envelope = {
            "v": _PROTOCOL_VERSION,
            "id": uuid.uuid4().hex,
            "action": "stop",
            "params": {},
        }
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                ws.send(json.dumps(envelope, ensure_ascii=False)), _STOP_TIMEOUT
            )

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
