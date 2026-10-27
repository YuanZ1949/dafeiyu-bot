from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection
from http import HTTPStatus
from itertools import count
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from dafeiyu.gateway.base import GatewayError
from dafeiyu.models import ChatType, Message, Segment

logger = logging.getLogger(__name__)

_PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _render_segments(
    raw_segments: list[Any],
) -> tuple[str, list[Segment], str | None, int | None]:
    parts: list[str] = []
    segments: list[Segment] = []
    reply_id: str | None = None
    reply_qq: int | None = None

    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        seg_type = str(item.get("type", ""))
        data = item.get("data")
        if not isinstance(data, dict):
            data = {}
        segments.append(Segment(type=seg_type, data=data))

        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type in _PLACEHOLDERS:
            parts.append(_PLACEHOLDERS[seg_type])
        elif seg_type == "reply":
            reply_id = str(data.get("id", ""))
            reply_qq = _as_int(data.get("qq")) or None
        # 其余段类型（json/xml/未知）一律忽略，不抛异常

    return "".join(parts).strip(), segments, reply_id, reply_qq


def parse_event(
    event: dict[str, Any],
    bot_qq: int,
    bot_message_ids: Collection[str] = (),
) -> Message | None:
    if not isinstance(event, dict) or event.get("post_type") != "message":
        return None

    message_type = event.get("message_type")
    if message_type == "group":
        chat_type = ChatType.GROUP
        chat_id = _as_int(event.get("group_id"))
    elif message_type == "private":
        chat_type = ChatType.PRIVATE
        chat_id = _as_int(event.get("user_id"))
    else:
        return None

    user_id = _as_int(event.get("user_id"))
    sender = event.get("sender")
    if not isinstance(sender, dict):
        sender = {}
    nickname = str(sender.get("card") or sender.get("nickname") or user_id)

    raw_message = event.get("message", "")
    reply_id: str | None = None
    reply_qq: int | None = None
    segments: list[Segment] = []

    if isinstance(raw_message, str):
        content = raw_message.strip()
        at_bot = False
    elif isinstance(raw_message, list):
        content, segments, reply_id, reply_qq = _render_segments(raw_message)
        at_bot = any(
            seg.type == "at" and str(seg.data.get("qq", "")) == str(bot_qq) for seg in segments
        )
    else:
        content = ""
        at_bot = False

    reply_to_bot = (
        reply_id is not None and reply_id in {str(i) for i in bot_message_ids}
    ) or (reply_qq is not None and reply_qq == bot_qq)

    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=str(event.get("message_id", "")),
        segments=segments,
        is_bot=user_id == bot_qq,
        at_bot=at_bot,
        reply_to_bot=reply_to_bot,
        timestamp=_as_int(event.get("time")),
    )


class QQAdapter:
    """OneBot v11 反向 WebSocket 服务端：NapCat 主动连入。"""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        bot_qq: int,
        send_timeout: float = 10.0,
        max_bot_message_ids: int = 500,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._bot_qq = bot_qq
        self._send_timeout = send_timeout
        self.bot_message_ids: deque[int] = deque(maxlen=max_bot_message_ids)
        self._handler: Callable[[Message], Awaitable[None]] | None = None
        self._server: Server | None = None
        self._ws: ServerConnection | None = None
        self._pending: dict[str, tuple[asyncio.Future[dict[str, Any]], str]] = {}
        self._echo = count(1)
        self._tasks: set[asyncio.Task[None]] = set()
        self._bound_port = port
        # 只记录时间戳，供健康快照使用；不保留消息内容。
        self.last_event_at: float | None = None
        self.last_disconnect_at: float | None = None
        self.last_send_at: float | None = None

    @property
    def port(self) -> int:
        return self._bound_port

    @property
    def bot_qq(self) -> int:
        return self._bot_qq

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def on_message(self, handler: Callable[[Message], Awaitable[None]]) -> None:
        self._handler = handler

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await serve(
            self._handle_connection,
            self._host,
            self._port,
            process_request=self._process_request,
        )
        sockets = self._server.sockets
        if sockets:
            self._bound_port = sockets[0].getsockname()[1]
        logger.info("QQ 网关已监听 %s:%s", self._host, self._bound_port)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        self._fail_pending(GatewayError("QQ 网关已关闭"))

    async def send(self, chat_type: ChatType, chat_id: int, text: str) -> None:
        ws = self._ws
        if ws is None:
            raise GatewayError("QQ 网关未连接")
        if chat_type is ChatType.GROUP:
            action, params = "send_group_msg", {"group_id": chat_id, "message": text}
        else:
            action, params = "send_private_msg", {"user_id": chat_id, "message": text}

        echo = str(next(self._echo))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = (future, action)
        try:
            await ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            data = await asyncio.wait_for(future, self._send_timeout)
        except asyncio.TimeoutError as exc:
            raise GatewayError(f"{action} 等待响应超时") from exc
        finally:
            self._pending.pop(echo, None)

        self.last_send_at = time.time()
        message_id = data.get("message_id")
        if message_id is not None:
            try:
                self.bot_message_ids.append(int(message_id))
            except (TypeError, ValueError):
                pass

    def _authorized(self, request: Any) -> bool:
        if not self._token:
            return True
        return request.headers.get("Authorization") == f"Bearer {self._token}"

    def _process_request(self, connection: ServerConnection, request: Any) -> Any:
        if self._authorized(request):
            return None
        return connection.respond(HTTPStatus.FORBIDDEN, "unauthorized\n")

    async def _handle_connection(self, ws: ServerConnection) -> None:
        self._ws = ws
        logger.info("NapCat 已连接")
        try:
            async for raw in ws:
                await self._handle_frame(raw)
        except ConnectionClosed:
            pass
        finally:
            self.last_disconnect_at = time.time()
            if self._ws is ws:
                self._ws = None
            self._fail_pending(GatewayError("QQ 网关连接已断开"))
            logger.info("NapCat 连接已断开")

    async def _handle_frame(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("收到无法解析的 OneBot 帧")
            return
        if not isinstance(data, dict):
            return

        if "post_type" in data:
            event = parse_event(data, self._bot_qq, self.bot_message_ids)
            if event is None or self._handler is None:
                return
            self.last_event_at = time.time()
            task = asyncio.create_task(self._run_handler(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        echo = data.get("echo")
        if echo is None:
            return
        entry = self._pending.pop(str(echo), None)
        if entry is None:
            return
        future, action = entry
        if future.done():
            return
        if data.get("status") != "ok" or data.get("retcode") != 0:
            future.set_exception(
                GatewayError(f"{action} 失败：retcode={data.get('retcode')} status={data.get('status')}")
            )
            return
        result = data.get("data")
        future.set_result(result if isinstance(result, dict) else {})

    async def _run_handler(self, event: Message) -> None:
        assert self._handler is not None
        try:
            await self._handler(event)
        except Exception:
            logger.exception("消息处理器异常")

    def _fail_pending(self, exc: Exception) -> None:
        for future, _action in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
