"""微信桥 WebSocket 服务器（PART A：传输与生命周期）。

对照 `docs/superpowers/specs/2026-11-01-wechat-bridge-protocol.md` 的 v1 线协议，
并把客户端 `dafeiyu/gateway/wechat.py` 的握手逐字镜像过来：

- 客户端连 `ws://{host}:{port}/wechat-bridge`，带 `Authorization: Bearer <token>`
  （client 侧 `_connect_once` 的 header 形态）；没有子协议，token 就只认 header；
- 事件（message/recall/health/resync）由桥推给所有已连接客户端：`broadcast(frame)`；
- 指令（send_text/send_at）交给可注入的异步 `command_handler`，注入前一律回
  `not_implemented`；`probe` 原生回链路_receipt，`stop` 是带内停机命令；
- 未知回退：非 `1` 的 `v` 按 §6.6 回 `version_mismatch` 再断开；未知 `action` 按
  §6 规则 5 回 `bad_action`（保证 `id` 有回声），两种都绝不崩掉主循环。

PART B（轮询/UIA 线程循环）的接缝：
-  `BridgeServer.broadcast(frame)` 是轮询线程推送事件的唯一入口；
-  `pump_frames(server, source)` 把（同步或异步）帧源灌进 broadcast；
-  `command_handler` 可注入（PART B 接发送器）；
-  `run(..., frame_source=...)`（见 `__main__.py`）驱动整个生命周期。

本模块不碰 pyweixin / pywinauto，也不实现轮询。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

__all__ = [
    "BRIDGE_PATH",
    "PROTOCOL_VERSION",
    "BridgeServer",
    "pump_frames",
]

# 与 wechat.py 客户端逐字一致的三件套。
BRIDGE_PATH = "/wechat-bridge"
PROTOCOL_VERSION = 1
_KNOWN_ACTIONS = ("send_text", "send_at", "probe", "stop", "resume")
_MAX_FRAME = 1 << 20

# 反挂死护栏：服务器内的每一个 await 都有显式超时（发送除外，见下）。
_AWAIT_TIMEOUT = 5.0
# 发送指令单独放宽：Sender 默认 20s 节奏，5s 会在消息仍要发出时先回 timeout，
# 客户端按 §7.6 换新 id 重试就会重复发送。客户端 _SEND_TIMEOUT 必须更大。
_SEND_AWAIT_TIMEOUT = 120.0
_SEND_ACTIONS = ("send_text", "send_at")
_CLOSE_CODE_VERSION_MISMATCH = 1002
_CLOSE_CODE_GOING_AWAY = 1001

#: 异步指令处理器：拿到 (action, params, command_id)，回一条回执字段表（可省缺省）。
CommandHandler = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any] | None]]

_RESULT_KEYS = ("ok", "error_code", "delivered", "message_id")


def _receipt(
    command_id: Any,
    ok: bool,
    error_code: str | None,
    *,
    delivered: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "id": str(command_id),
        "ok": bool(ok),
        "error_code": error_code,
        "delivered": delivered or ("observed" if ok else "failed"),
        "message_id": message_id,
    }


def _command_id(envelope: dict[str, Any]) -> str:
    value = envelope.get("id")
    return value if isinstance(value, str) else ""


class BridgeServer:
    """微信桥服务器：绑定 host:port（测试可传 0），推帧、收指令、算回执。

    生命周期：`start()` -> 绑定端口 -> `request_stop()`（SIGINT 或 `stop` 指令，
    由 `__main__.run` 通过 `on_stop_requested` 接走）-> `stop()` 关连接收摊。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        bridge_token: str = "",
        command_handler: CommandHandler | None = None,
        on_stop_requested: Callable[[], None] | None = None,
        send_timeout: float | None = None,
    ) -> None:
        self.host = host
        self._requested_port = int(port)
        # 空串 = 不验 token（与客户端「没配就不发 header」对应）。
        self._bridge_token = str(bridge_token or "")
        self.command_handler = command_handler
        self._on_stop_requested = on_stop_requested
        # 发送指令的等待上限：默认 120s（Sender 20s 节奏）；测试可注入更短值。
        self._send_timeout = (
            _SEND_AWAIT_TIMEOUT if send_timeout is None else float(send_timeout)
        )
        self._server: Any | None = None
        self._stop: asyncio.Future[None] | None = None
        self._closed = False
        self.port: int | None = None
        self._connections: set[ServerConnection] = set()
        # 健康快照：新客户端一批上来先收到最近一帧，绝不干等下一次周期上报。
        self._latest_health: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def latest_health(self) -> dict[str, Any] | None:
        if self._latest_health is None:
            return None
        return dict(self._latest_health)

    def client_count(self) -> int:
        return len(self._connections)

    # ------------------------------------------------------------- 生命周期
    async def start(self) -> int:
        """绑定端口；port=0 时回实际绑定的端口。重复调用幂等。"""
        if self._server is not None:
            assert self.port is not None
            return self.port
        loop = asyncio.get_running_loop()
        self._stop = loop.create_future()
        server = await serve(
            self._handle_connection,
            self.host,
            self._requested_port,
            max_size=_MAX_FRAME,
            process_request=self._process_request,
        )
        self._server = server
        port = self._requested_port
        for sock in server.sockets:
            port = sock.getsockname()[1]
            break
        self.port = int(port)
        return self.port

    def request_stop(self) -> None:
        """带内停止信号：`stop` 指令或外部调用都会走到这里。幂等。"""
        future = self._stop
        if future is not None and not future.done():
            future.set_result(None)
        if self._on_stop_requested is not None:
            self._on_stop_requested()

    async def stop(self) -> None:
        """关服务器、断开所有客户端；重复调用幂等。"""
        self._closed = True
        server = self._server
        self._server = None
        for connection in list(self._connections):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    connection.close(code=_CLOSE_CODE_GOING_AWAY), _AWAIT_TIMEOUT
                )
        self._connections.clear()
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), _AWAIT_TIMEOUT)
        self.request_stop()

    # --------------------------------------------------------------- 推帧
    async def broadcast(self, frame: dict[str, Any]) -> None:
        """推送事件帧到所有已连接客户端（message/recall/health/resync）。

        帧没有 `v`/`channel` 时补默认值（§2.1 两字段必填）；health 帧留作
        新客户端接入时的首帧快照。断线/超时的客户端直接剔除。
        """
        if not isinstance(frame, dict):
            raise TypeError("broadcast 需要一条 v1 信封 dict")
        envelope: dict[str, Any] = {"v": PROTOCOL_VERSION, "channel": "wechat"}
        envelope.update(frame)
        if envelope.get("type") == "health":
            self._latest_health = dict(envelope)
        payload = json.dumps(envelope, ensure_ascii=False)
        for connection in list(self._connections):
            try:
                await asyncio.wait_for(connection.send(payload), _AWAIT_TIMEOUT)
            except (TimeoutError, ConnectionClosed, OSError, RuntimeError) as exc:
                logger.debug("微信桥广播失败，断开该客户端：%s", exc)
                self._connections.discard(connection)

    # ----------------------------------------------------------- 连接入口
    def _process_request(
        self, connection: ServerConnection, request: Any
    ) -> Any | None:
        """HTTP 升级前的握手检查：路径与 token，镜像 wechat.py 的 header 形态。"""
        if request.path != BRIDGE_PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, "unknown path")
        if not self._bridge_token:
            return None
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {self._bridge_token}"
        # 先编码成 bytes：非 ASCII 的 header 直接喂给 compare_digest 会抛 TypeError，
        # 那会把「token 不对」变成 500 而不是干净的 401。
        if not hmac.compare_digest(
            header.encode("utf-8", "surrogatepass"), expected.encode("utf-8")
        ):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid bridge token")
        return None

    async def _handle_connection(self, connection: ServerConnection) -> None:
        self._connections.add(connection)
        try:
            snapshot = self._latest_health
            if snapshot is not None:
                await self._send(connection, snapshot)
            async for raw in connection:
                reply, close, stop = await self._handle_frame(raw)
                try:
                    if reply is not None:
                        # 回执必须先落网再停机：`stop` 指令的回执被 run() 的
                        # 收摊抢掉的话，客户端只能看到连接被关，永远等不到 ok。
                        await self._send(connection, reply)
                finally:
                    if stop:
                        self.request_stop()
                if close:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            connection.close(
                                code=_CLOSE_CODE_VERSION_MISMATCH, reason="version"
                            ),
                            _AWAIT_TIMEOUT,
                        )
                    break
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("微信桥连接处理异常")
        finally:
            self._connections.discard(connection)

    async def _send(
        self, connection: ServerConnection, envelope: dict[str, Any]
    ) -> None:
        payload = json.dumps(envelope, ensure_ascii=False)
        await asyncio.wait_for(connection.send(payload), _AWAIT_TIMEOUT)

    # --------------------------------------------------------- 帧分派
    async def _handle_frame(
        self, raw: Any
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        """解析一帧；返回 (回执|None, 是否断开, 回执发出后是否停机)。

        停机单独用第三个返回值交给 `_handle_connection`，保证 `stop` 的
        回执先发出去再触发收摊。协议错误不断主循环。
        """
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("微信桥收到无法解析的帧")
            return None, False, False
        if not isinstance(envelope, dict):
            logger.warning("微信桥收到非对象帧")
            return None, False, False

        if envelope.get("v") != PROTOCOL_VERSION:
            logger.warning(
                "微信桥指令版本不匹配：%r（断开连接，§6.6）", envelope.get("v")
            )
            return _receipt(_command_id(envelope), False, "version_mismatch"), True, False

        if "type" in envelope:
            # 指令通道不该收到事件帧；忽略，不打断读循环。
            logger.warning("微信桥忽略指令通道上的事件帧 type=%r", envelope.get("type"))
            return None, False, False

        command_id = _command_id(envelope)
        action = envelope.get("action")
        params = envelope.get("params")

        if action not in _KNOWN_ACTIONS:
            logger.warning("微信桥未知指令 action=%r", action)
            # §6 规则 5 钉死的码是 bad_action（不是自造词），客户端只读 error_code 不判具体值。
            return _receipt(command_id, False, "bad_action"), False, False
        if not isinstance(params, dict):
            return _receipt(command_id, False, "bad_params"), False, False

        if action == "probe":
            return _receipt(command_id, True, None), False, False
        if action == "stop":
            logger.info("微信桥收到 stop 指令，开始停机")
            return _receipt(command_id, True, None), False, True

        return (
            await self._dispatch_command(action, params, command_id),
            False,
            False,
        )

    async def _dispatch_command(
        self, action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        handler = self.command_handler
        result: dict[str, Any] | None = None
        timeout = self._send_timeout if action in _SEND_ACTIONS else _AWAIT_TIMEOUT
        if handler is not None:
            try:
                result = await asyncio.wait_for(
                    handler(action, params, command_id), timeout
                )
            except TimeoutError:
                return _receipt(command_id, False, "timeout")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("微信桥指令处理异常 action=%s", action)
                return _receipt(command_id, False, "internal_error")
        if not isinstance(result, dict):
            # 注入前的默认行为：明确告诉对端这个动作还没实现。
            return _receipt(command_id, False, "not_implemented")
        receipt = _receipt(command_id, False, "not_implemented")
        for key in _RESULT_KEYS:
            if key in result:
                receipt[key] = result[key]
        if receipt["ok"] and receipt["error_code"] == "not_implemented":
            receipt["error_code"] = None
        return receipt


async def pump_frames(
    server: BridgeServer,
    source: Iterable[dict[str, Any]] | AsyncIterable[dict[str, Any]],
    *,
    idle_timeout: float | None = None,
) -> None:
    """把帧源产出的每一帧灌进 `server.broadcast`（PART B 的下发通路）。

    接受同步或异步迭代器；每个 await 都有显式超时，帧源长时间不出帧按停泵
    处理，绝不把整个桥的主循环挂死。

    `idle_timeout` 是「帧源多久不出帧就停泵」的显式接缝：默认 5s 保持原行为；
    轮询循环的帧间隔可以长到 `WeChatConfig.poll_seconds`（默认 8s，且每轮至少
    一帧 health），所以 `__main__` 会传 `max(5s, 2 * poll_seconds)`，否则健康的
    8s 轮询会在空闲 5s 时被误停泵。
    """
    timeout = _AWAIT_TIMEOUT if idle_timeout is None else float(idle_timeout)
    if hasattr(source, "__anext__"):
        async def _iterate() -> AsyncIterable[dict[str, Any]]:
            assert hasattr(source, "__anext__")
            async for frame in source:  # type: ignore[union-attr]
                yield frame

        iterator = _iterate()
    else:

        async def _iterate() -> AsyncIterable[dict[str, Any]]:
            for frame in source:
                yield frame

        iterator = _iterate()

    while True:
        try:
            frame = await asyncio.wait_for(iterator.__anext__(), timeout)
        except StopAsyncIteration:
            return
        except TimeoutError:
            logger.warning("微信桥帧源超时无产出，停止灌帧")
            return
        await asyncio.wait_for(server.broadcast(frame), _AWAIT_TIMEOUT)
