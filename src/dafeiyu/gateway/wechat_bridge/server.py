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
import os
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast

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
_KNOWN_ACTIONS = (
    "send_text",
    "send_at",
    "send_media",
    "probe",
    "stop",
    "resume",
    "ack",
    "hello",
)
_MAX_FRAME = 1 << 20

#: 入站事件帧（message/recall）的未确认缓冲上限：有界重投窗口（P3-11），
#: ack 到位后从头裁剪；窗口之外的帧为已知残余（健康帧 `unacked_count` 可见）。
UNACKED_MAX = 200
#: 入站水位默认落盘路径：与 `DEFAULT_KILL_SWITCH` / 对齐状态同一 `data/` 约定。
DEFAULT_INBOUND_STATE = "data/wechat-bridge/inbound_state.json"
#: 旧版水位落盘节流常量（每 N 帧一次）。保留名字供旧调用方/测试引用，
#: 现在未确认帧每帧都落盘（缓冲上限 200 条），不再依赖它。
_INBOUND_SAVE_EVERY = 50
#: 每个连接记录的已发 seq 历史上限：广播重试与 hello 重投按它对同一连接去重。
_CONN_SEQ_MAX = 2048

# 反挂死护栏：服务器内的每一个 await 都有显式超时（发送除外，见下）。
_AWAIT_TIMEOUT = 5.0
# 发送指令单独放宽：Sender 默认 20s 节奏，5s 会在消息仍要发出时先回 timeout，
# 客户端按 §7.6 换新 id 重试就会重复发送。客户端 _SEND_TIMEOUT 必须更大。
_SEND_AWAIT_TIMEOUT = 120.0
_SEND_ACTIONS = ("send_text", "send_at", "send_media")

# 进程内发送去重（兜底「发送成功、回执丢失」窗口）：
# params 里带非空 dedupe_key 的指令按 (action, chat_id, dedupe_key) 认领；
# 已完成结果在 TTL 内直接重放（回执 id 用当前请求的），在途请求合流到同一执行。
# TTL/容量是模块级常量：可读、可 monkeypatch 便于测试，不落盘——桥重启丢缓存
# 是已知残余窗口。
DEDUPE_TTL_SECONDS = 3600.0
DEDUPE_MAX_ENTRIES = 1024
_CLOSE_CODE_VERSION_MISMATCH = 1002
_CLOSE_CODE_GOING_AWAY = 1001

#: 异步指令处理器：拿到 (action, params, command_id)，回一条回执字段表（可省缺省）。
CommandHandler = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any] | None]]

_RESULT_KEYS = ("ok", "error_code", "delivered", "message_id")

#: 共享执行的结局：(handler 回执字段表|None, 失败码|None)。error 非 None 表示
#: 执行抛异常/超时——失败结局绝不缓存，后续同 key 请求重新执行。
_DedupeOutcome = tuple[dict[str, Any] | None, str | None]


class _DedupeEntry:
    """一个 dedupe key 的共享状态：在途任务或已完成结局。

    创建时挂 task；任务结束时要么写出 outcome 进入 TTL 缓存，
    要么把自己从表里摘掉（失败不缓存）。
    """

    __slots__ = ("task", "outcome", "done_at")

    def __init__(self, task: "asyncio.Task[_DedupeOutcome]") -> None:
        self.task = task
        self.outcome: _DedupeOutcome | None = None
        self.done_at = 0.0


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


def _receipt_from_result(
    command_id: Any, result: dict[str, Any] | None
) -> dict[str, Any]:
    """把 handler 结局表折算成回执：id 永远是当前请求的 id。"""
    receipt = _receipt(command_id, False, "not_implemented")
    if result is not None:
        for key in _RESULT_KEYS:
            if key in result:
                receipt[key] = result[key]
    if receipt["ok"] and receipt["error_code"] == "not_implemented":
        receipt["error_code"] = None
    return receipt


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
        broadcast_timeout: float | None = None,
        state_path: str | Path | None = None,
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
        # 单连接广播发送的上限：测试可注入更短值，默认与 5s 护栏一致。
        self._broadcast_timeout: float = (
            _AWAIT_TIMEOUT if broadcast_timeout is None else float(broadcast_timeout)
        )
        # 入站水位文件：显式传入才落盘（生产入口传 DEFAULT_INBOUND_STATE，
        # 测试传 tmp_path）。None = 纯内存水位，绝不写生产文件——否则没传参的
        # 测试会把伪造帧写进 data/wechat-bridge/inbound_state.json。
        self.state_path = Path(str(state_path)) if state_path else None
        self._server: Any | None = None
        self._stop: asyncio.Future[None] | None = None
        self._closed: bool = False
        self.port: int | None = None
        self._connections: set[ServerConnection] = set()
        # 客户端在场信号：有客户端置于 set、无人/已收摊时 clear。
        # wait_for_client 的等待者靠它挂起/唤醒（Python 3.10+ Event 不绑 loop）。
        self._client_event: asyncio.Event = asyncio.Event()
        # 健康快照：新客户端一批上来先收到最近一帧，绝不干等下一次周期上报。
        self._latest_health: dict[str, Any] | None = None
        # 发送去重表：key = (action, chat_id, dedupe_key)。只在 params 带非空
        # dedupe_key 时启用；无 key 指令完全不受影响。
        self._dedupe: dict[tuple[str, str, str], _DedupeEntry] = {}
        # 入站帧水位（P3-10/P3-11）：seq 单调递增只发给 message/recall 事件帧，
        # hello/ack 指令维护未确认缓冲；水位落盘让崩溃/重启后 seq 不回退。
        self._inbound_seq: int = 0
        self._inbound_acked: int = 0
        self._unacked: deque[tuple[int, dict[str, Any]]] = deque(maxlen=UNACKED_MAX)
        # 首次缓冲溢出时被挤掉的最旧 seq（None = 没有丢帧）；随水位落盘。
        self._inbound_gap_from: int | None = None
        # 每个连接已发事件帧 seq 的有界历史：广播 pending 重试与 hello 重投
        # 在此汇合，同一帧对同一连接绝不出两遍。
        self._conn_seq_hist: dict[ServerConnection, deque[int]] = {}
        # 每连接 hello 高水位：只有首个 hello 触发重投，重复 hello 只更新水位。
        self._conn_hello: dict[ServerConnection, int] = {}
        self._load_inbound_state()

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

    @property
    def has_clients(self) -> bool:
        """当前是否有至少一个客户端连接在场（帧泵的暂停/重试信号）。"""
        return bool(self._connections)

    @property
    def closed(self) -> bool:
        """服务器是否已收摊（`stop()` 之后为 True；帧泵以此退出客户端等待）。"""
        return self._closed

    def _refresh_clients(self) -> None:
        """翻动在场信号：场所变化必须唤醒/挂起 wait_for_client 的等待者。

        已收摊时显式 set：让还挂在 wait() 上的泵至少醒来一次，
        由 `wait_for_client` 的循环条件判断退出，不会原地死等。
        """
        if self._closed or self._connections:
            self._client_event.set()
        else:
            self._client_event.clear()

    async def wait_for_client(self) -> None:
        """阻塞到至少有一个客户端连接（或服务器已收摊）。

        供帧泵在无人接收帧时暂停拉取帧源：连接建立/断开都会翻动内部
        Event，先查后等之间接入的客户端不会漏（add 时已置位）。
        可重复调用；收摊后立即返回，调用方用 `has_clients`/`closed` 判断。
        """
        while not self._connections and not self._closed:
            await self._client_event.wait()

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
        self._refresh_clients()
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), _AWAIT_TIMEOUT)
        self.request_stop()

    # --------------------------------------------------------------- 推帧
    async def broadcast(self, frame: dict[str, Any]) -> int:
        """推送事件帧到所有已连接客户端，返回成功送达的连接数。

        帧没有 `v`/`channel` 时补默认值（§2.1 两字段必填）；health 帧留作
        新客户端接入时的首帧快照。逐连接限时发送：慢/断线客户端直接剔除
        （同步翻动在场信号），所以单个收不动的客户端只会让本连接丢弃，
        绝不会把 TimeoutError/CancelledError 泄到广播之外——帧泵以此判断
        delivered==0，把帧留到客户端回来重试而不是杀掉泵。
        """
        if not isinstance(frame, dict):
            raise TypeError("broadcast 需要一条 v1 信封 dict")
        envelope: dict[str, Any] = {"v": PROTOCOL_VERSION, "channel": "wechat"}
        envelope.update(frame)
        if envelope.get("type") in ("message", "recall"):
            self._stamp_inbound(envelope, frame)
        if envelope.get("type") == "health":
            # 健康帧暴露入站水位：丢帧窗口 = inbound_seq - inbound_acked。
            envelope["inbound_seq"] = self._inbound_seq
            envelope["inbound_acked"] = self._inbound_acked
            envelope["unacked_count"] = len(self._unacked)
            self._latest_health = dict(envelope)
        payload = json.dumps(envelope, ensure_ascii=False)
        sent = 0
        for connection in list(self._connections):
            seq = envelope.get("seq")
            if type(seq) is int and seq in self._conn_seq_hist.get(connection, ()):
                # 该连接已拿到这个 seq（通常 hello 重投先到）：跳过即算送达，
                # 让帧泵的 pending 重试拿到确定答案，同一帧不重复投递。
                sent += 1
                continue
            try:
                await asyncio.wait_for(
                    connection.send(payload), self._broadcast_timeout
                )
            except (TimeoutError, ConnectionClosed, OSError, RuntimeError) as exc:
                logger.debug("微信桥广播失败，断开该客户端：%s", exc)
                self._connections.discard(connection)
                self._refresh_clients()
            else:
                sent += 1
                if type(seq) is int:
                    history = self._conn_seq_hist.get(connection)
                    if history is None:
                        history = deque(maxlen=_CONN_SEQ_MAX)
                        self._conn_seq_hist[connection] = history
                    history.append(seq)
        return sent

    def _stamp_inbound(
        self, envelope: dict[str, Any], frame: dict[str, Any]
    ) -> None:
        """给 message/recall 事件帧分配单调递增 seq 并进入未确认缓冲（P3-10）。

        health/startup 帧永不带 seq。已带合法 seq 的帧（帧泵 pending 对同一
        dict 的重试）沿用原 seq，不再发号、不再入缓冲——seq 同时写回调用方
        的帧对象，重试广播才能认得它。
        """
        seq = frame.get("seq")
        if type(seq) is int:
            return
        self._inbound_seq += 1
        frame["seq"] = self._inbound_seq
        envelope["seq"] = self._inbound_seq
        if len(self._unacked) >= UNACKED_MAX:
            # deque(maxlen) 会静默挤掉最旧的未确认帧：记下账目并告警，
            # 否则桥和 bot 都不知道丢了什么、这些消息再也不会重投。
            dropped = self._unacked[0][0]
            if self._inbound_gap_from is None:
                self._inbound_gap_from = dropped
            logger.warning(
                "微信桥未确认帧缓冲已满（%d），挤掉最旧帧 seq=%d，当前 inbound_seq=%d；该帧不会再重投",
                UNACKED_MAX,
                dropped,
                self._inbound_seq,
            )
        self._unacked.append((self._inbound_seq, envelope))
        # 未确认帧随水位一起落盘：硬杀进程后仍能靠 hello 重投把它补出去。
        # 缓冲上限 200 条，文件很小；失败只记 debug，不影响发帧。
        self._save_inbound_state()

    # ------------------------------------------------------- 入站水位（P3-11）
    def _load_inbound_state(self) -> None:
        """启动时恢复水位与未确认帧；缺文件/损坏一律从 0 开始并告警，绝不崩溃。"""
        path = self.state_path
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text("utf-8"))
            if isinstance(raw, dict):
                seq = int(raw.get("seq", 0))
                acked = int(raw.get("acked", 0))
                if seq >= 0 and 0 <= acked <= seq:
                    self._inbound_seq = seq
                    self._inbound_acked = acked
                    # 只有正整数才恢复；坏值保持 None，不带进运行状态。
                    gap = raw.get("gap_from")
                    if type(gap) is int and gap > 0:
                        self._inbound_gap_from = gap
                    self._restore_unacked(raw.get("frames"), seq, acked)
                    # 已 ack 追平的 gap 视为已恢复（ack 后丢帧已无意义）。
                    if (
                        self._inbound_gap_from is not None
                        and self._inbound_gap_from <= self._inbound_acked
                    ):
                        self._inbound_gap_from = None
                    return
        except Exception as exc:  # noqa: BLE001 - 水位只影响重投，不许影响启动
            logger.warning("微信桥入站水位文件损坏，按 0 处理：%s", exc)
            return
        logger.warning("微信桥入站水位文件内容非法，按 0 处理：%s", path)

    def _restore_unacked(self, frames: Any, seq: int, acked: int) -> None:
        """校验并恢复 `frames` 里的未确认帧：非法条目跳过、重复 seq 只留第一条。

        旧格式（无 `frames`）按空缓冲处理，不告警。恢复结果按 seq 升序，
        最多 `UNACKED_MAX` 条（与运行期 deque 一样保留最新的窗口）。
        """
        if frames is None:
            return
        if not isinstance(frames, list):
            logger.warning("微信桥入站帧缓冲格式非法，按空处理：%s", self.state_path)
            return
        valid: dict[int, dict[str, Any]] = {}
        skipped = 0
        for item in frames:
            if not isinstance(item, dict):
                skipped += 1
                continue
            frame_seq = item.get("seq")
            frame = item.get("frame")
            if not isinstance(frame, dict):
                skipped += 1
                continue
            # bool 是 int 的子类，这里只认真正的正整数 seq。
            if type(frame_seq) is not int or frame_seq <= 0:
                skipped += 1
                continue
            if frame_seq <= acked or frame_seq > seq:
                skipped += 1
                continue
            inner = frame.get("seq")
            if inner is None:
                # 旧数据可能只在顶层记了 seq：回填进帧内，重投时与协议一致。
                frame["seq"] = frame_seq
            elif type(inner) is not int or inner != frame_seq:
                skipped += 1
                continue
            if frame_seq in valid:
                skipped += 1
                continue
            valid[frame_seq] = frame
        if skipped:
            # 一次汇总告警，避免坏文件把启动日志刷屏。
            logger.warning(
                "微信桥入站帧缓冲有 %d 条非法条目，已跳过：%s",
                skipped,
                self.state_path,
            )
        self._unacked.extend(sorted(valid.items())[-UNACKED_MAX:])

    def _save_inbound_state(self) -> None:
        """水位 + 未确认帧落盘：临时文件 + ``os.replace`` 原子替换；失败只记 debug。

        `state_path` 为 None（未显式开启持久化）时什么都不写。
        """
        path = self.state_path
        if path is None:
            return
        try:
            payload = {
                "seq": self._inbound_seq,
                "acked": self._inbound_acked,
                "gap_from": self._inbound_gap_from,
                # 已 ack 的帧不在缓冲里，这里只写窗口内的未确认帧（升序）。
                "frames": [
                    {"seq": seq, "frame": envelope}
                    for seq, envelope in self._unacked
                ],
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False),
                "utf-8",
            )
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001 - 落盘失败只影响重投
            logger.debug("写入微信桥入站水位失败：%s", exc)

    def _handle_ack(self, params: dict[str, Any], command_id: str) -> dict[str, Any]:
        """ack 指令：丢弃所有 seq <= 参数值的未确认帧并落盘，回 ok。"""
        seq = params.get("seq")
        if type(seq) is not int or seq < 0:
            return _receipt(command_id, False, "bad_params")
        seq = min(seq, self._inbound_seq)
        if seq > self._inbound_acked:
            # 单调取大：乱序/重复 ack 不回退水位，也不重复裁剪。
            self._inbound_acked = seq
            while self._unacked and self._unacked[0][0] <= seq:
                self._unacked.popleft()
            self._save_inbound_state()
        return _receipt(command_id, True, None)

    async def _handle_hello(
        self, params: dict[str, Any], connection: ServerConnection, command_id: str
    ) -> dict[str, Any]:
        """hello 指令：记录该连接高水位；首个 hello 立即重投 seq 更高的帧。"""
        last_seq = params.get("last_seq")
        if type(last_seq) is not int or last_seq < 0:
            return _receipt(command_id, False, "bad_params")
        known = self._conn_hello.get(connection)
        self._conn_hello[connection] = last_seq
        if known is not None:
            # 同一连接只回投一次；重复 hello 只更新高水位，不重复重投。
            return _receipt(command_id, True, None)
        await self._redeliver_inbound(connection, last_seq)
        return _receipt(command_id, True, None)

    async def _redeliver_inbound(
        self, connection: ServerConnection, last_seq: int
    ) -> None:
        """按 seq 升序给新连接重投 > last_seq 的未确认帧（带原 seq）。

        以客户端报的 ``last_seq`` 为准，**不看**该连接的已发历史：历史只说明
        「发过」，不说明「客户端处理过」；同一连接重连前的 socket 缓冲丢失时，
        hello 是唯一的补救。逐帧记入已发历史是为了让随后的帧泵 pending 广播
        因历史命中被跳过，同一帧对同一连接只出一遍。帧照旧留在缓冲里，
        ack 到位后才裁剪。没有 hello 的新连接不走这里（客户端没准备好时
        不重复投递）。
        """
        for seq, envelope in list(self._unacked):
            if seq <= last_seq:
                continue
            try:
                await self._send(connection, dict(envelope))
            except (TimeoutError, ConnectionClosed, OSError, RuntimeError) as exc:
                logger.debug("微信桥 hello 重投失败 seq=%s：%s", seq, exc)
                return
            hist = self._conn_seq_hist.get(connection)
            if hist is None:
                hist = deque(maxlen=_CONN_SEQ_MAX)
                self._conn_seq_hist[connection] = hist
            hist.append(seq)

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
        self._refresh_clients()
        try:
            snapshot = self._latest_health
            if snapshot is not None:
                await self._send(connection, snapshot)
            async for raw in connection:
                reply, close, stop = await self._handle_frame(raw, connection)
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
            self._conn_seq_hist.pop(connection, None)
            self._conn_hello.pop(connection, None)
            self._refresh_clients()

    async def _send(
        self, connection: ServerConnection, envelope: dict[str, Any]
    ) -> None:
        payload = json.dumps(envelope, ensure_ascii=False)
        await asyncio.wait_for(connection.send(payload), _AWAIT_TIMEOUT)

    # --------------------------------------------------------- 帧分派
    async def _handle_frame(
        self, raw: Any, connection: ServerConnection
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

        # 传输级指令：ack / hello 只操作入站水位，不走 command_handler 注入。
        if action == "ack":
            return self._handle_ack(params, command_id), False, False
        if action == "hello":
            reply = await self._handle_hello(params, connection, command_id)
            return reply, False, False

        return (
            await self._dispatch_command(action, params, command_id),
            False,
            False,
        )

    async def _dispatch_command(
        self, action: str, params: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        dedupe_key = params.get("dedupe_key")
        if isinstance(dedupe_key, str) and dedupe_key:
            chat_key = params.get("chat_id")
            cache_key = (
                action,
                chat_key if isinstance(chat_key, str) else "",
                dedupe_key,
            )
            entry = self._dedupe_entry(cache_key)
            if entry is None:
                entry = self._dedupe_start(cache_key, action, params, command_id)
            outcome = await asyncio.shield(entry.task)
            result, error = outcome
            if error is not None:
                # 超时/异常与直接执行同一套回执码；结局没有缓存。
                return _receipt(command_id, False, error)
            return _receipt_from_result(command_id, result)

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
        receipt = _receipt_from_result(command_id, result)
        return receipt

    # --------------------------------------------------------- 发送去重
    def _dedupe_entry(
        self, cache_key: tuple[str, str, str]
    ) -> _DedupeEntry | None:
        """取可复用的表项；过期缓存顺手淘汰。"""
        entry = self._dedupe.get(cache_key)
        if entry is None:
            return None
        if entry.outcome is None:
            return entry  # 在途：合流（通过 entry.task await）。
        if time.monotonic() - entry.done_at > DEDUPE_TTL_SECONDS:
            self._dedupe.pop(cache_key, None)
            return None
        return entry

    def _dedupe_start(
        self,
        cache_key: tuple[str, str, str],
        action: str,
        params: dict[str, Any],
        command_id: str,
    ) -> _DedupeEntry:
        """登记新执行：容量到顶淘汰最旧（不淘汰在途项）。"""
        while len(self._dedupe) >= DEDUPE_MAX_ENTRIES:
            stale = next(
                (key for key, old in self._dedupe.items() if old.outcome is not None),
                None,
            )
            if stale is None:
                break
            self._dedupe.pop(stale)
        entry = _DedupeEntry(
            asyncio.create_task(
                self._dedupe_run(cache_key, action, params, command_id),
                name="wechat-bridge-dedupe-send",
            )
        )
        self._dedupe[cache_key] = entry
        return entry

    async def _dedupe_run(
        self,
        cache_key: tuple[str, str, str],
        action: str,
        params: dict[str, Any],
        command_id: str,
    ) -> _DedupeOutcome:
        """共享执行的唯一实现：超时策略与直接执行完全一致。"""
        entry = self._dedupe.get(cache_key)
        timeout = self._send_timeout if action in _SEND_ACTIONS else _AWAIT_TIMEOUT
        try:
            handler = self.command_handler
            result: dict[str, Any] | None = None
            if handler is not None:
                result = await asyncio.wait_for(
                    handler(action, params, command_id), timeout
                )
        except asyncio.CancelledError:
            self._dedupe.pop(cache_key, None)
            raise
        except TimeoutError:
            self._dedupe.pop(cache_key, None)
            return (None, "timeout")
        except Exception:
            self._dedupe.pop(cache_key, None)
            logger.exception("微信桥指令处理异常 action=%s", action)
            return (None, "internal_error")
        if entry is not None:
            entry.outcome = (result, None)
            entry.done_at = time.monotonic()
        return (result, None)


class _FrameSourceFailure:
    """帧源迭代异常的中转信封：把生产任务里的异常原样抛回消费侧。"""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_FRAME_SOURCE_END = object()

#: 生产队列上限：投递停滞时生产者 `await queue.put` 背压顶住帧源，
#: 不会在无人接收时无限生产。
_QUEUE_MAX = 64
#: 失败/结束标记挤队上限：满员的队列最多等这么久，等不到就放弃——
#: 消费侧的空转护栏仍能停泵，不会因此挂死收摊。
_QUEUE_PUT_TIMEOUT = 1.0


class _ClientGate:
    """`wait_for_client` / `has_clients` 的鸭子类型闸门。

    只实现 broadcast 的替身服务器（旧测试）没有这两个接缝时，
    闸门全通：泵行为与旧版一致。真实 BridgeServer 两个接缝都在。
    """

    __slots__: tuple[str, ...] = ("_server", "active")

    def __init__(self, server: Any) -> None:
        wait = getattr(server, "wait_for_client", None)
        # has_clients 若是 property，getattr 即取值（总是 bool，不会是 None）；
        # 没有该接缝的替身 getattr 落到 None，闸门禁用。
        self._server: Any = server
        self.active: bool = callable(wait) and (
            getattr(server, "has_clients", None) is not None
        )

    def present(self) -> bool:
        """此刻是否有客户端连接（每次动态取值）。"""
        return bool(getattr(self._server, "has_clients", False))

    def closed(self) -> bool:
        """服务器是否已收摊（收摊且无人时等待方该停泵）。"""
        return bool(getattr(self._server, "closed", False))

    async def wait(self) -> bool:
        """等至少一个客户端接入；返回 False = 已收摊且无人会来，该停泵。

        先查后等：连接在检查与等待之间接入也不会漏（Event 已被置位）。
        """
        if not self.active:
            return True
        wait = getattr(self._server, "wait_for_client")
        while True:
            await wait()
            if self.present():
                return True
            if self.closed():
                return False
            # wait() 被瞬时加/撤连接唤醒但仍无人在线：继续等。


async def _put_marker(queue: asyncio.Queue[Any], item: Any) -> None:
    """失败/结束标记也要穿过满员队列：最多等一小会，等不到交给空转护栏。"""
    try:
        await asyncio.wait_for(queue.put(item), _QUEUE_PUT_TIMEOUT)
    except (TimeoutError, asyncio.CancelledError):
        # CancelledError 只可能是泵收摊取消；标记丢了没关系，
        # 消费侧的空转护栏照样能停泵。不可再吞生产数据帧。
        pass


async def pump_frames(
    server: BridgeServer,
    source: Iterable[dict[str, Any]] | AsyncIterable[dict[str, Any]],
    *,
    idle_timeout: float | None = None,
    stall_limit: int = 0,
) -> None:
    """把帧源产出的每一帧灌进 `server.broadcast`（PART B 的下发通路）。

    接受同步或异步迭代器。帧源由独立的生产者任务驱动，消费侧只等队列：
    `asyncio.wait_for` 超时会取消被等待的协程，直接取消 `__anext__` 会让
    异步生成器进入不可恢复的状态，进而静默停泵——经队列中转后超时只取消
    `Queue.get()`，帧源本身毫发无损。

    三个 loss-free 接缝：
    -   无客户端时暂停源：生产者拉下一帧前先过 `wait_for_client()` 闸门
        （服务器没这两个接缝的替身则全通），SessionAligner 不在没人收帧时
        推进；队列 `maxsize=_QUEUE_MAX` + `await queue.put` 给源头背压。
    -   投递失败不丢帧：`broadcast` 超时/无人收到时该帧被持有，客户端回来
        后先重投这一帧再继续队列积压帧——绝不乱序、绝不静默丢
        message/recall/resync。`broadcast` 返回 None 的旧替身按「已送达」算。
    -   广播超时不外泄：单次 broadcast 抛 TimeoutError 只按 delivered=0
        记警告继续，泵绝不因此退出。

    `idle_timeout` 是「帧源多久不出帧算一次空转」的显式接缝：默认 5s；
    轮询循环每轮至少一帧 health，帧间隔约等于 `WeChatConfig.poll_seconds`，
    所以 `__main__` 传 `max(5s, 2 * poll_seconds)`。

    `stall_limit` 决定连续空转几次才真正停泵：默认 0 = 第一次超时即返回；
    生产路径传 3——单次慢轮询只记警告并继续等待，连续 3 次才放弃。空转
    只对「有客户端在场但源无产出」成立：等客户端接入期间的挂起不算。
    真卡死时泵仍会退出，由 `__main__` 的完成回调请求停机、交给 supervisor
    重启，不会出现「进程活着但永不推帧」的假活。
    """
    timeout = _AWAIT_TIMEOUT if idle_timeout is None else float(idle_timeout)
    limit = max(0, int(stall_limit))
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_MAX)
    clients = _ClientGate(server)

    async def _produce() -> None:
        try:
            if hasattr(source, "__anext__"):
                # 不能用 `async for`：它会先把第一帧拉出来才进循环体，
                # 闸门就晚了一步。显式 next：有人能收帧才拉下一帧，
                # 帧源（及其推进的对齐状态）冻结在闸前。
                async_source = cast(AsyncIterator[dict[str, Any]], source)
                async_next: Callable[[], Awaitable[dict[str, Any]]] = (
                    async_source.__anext__
                )
                while True:
                    if not await clients.wait():
                        return
                    try:
                        frame = await async_next()
                    except StopAsyncIteration:
                        break
                    await queue.put(frame)
            else:
                sync_source = cast(Iterable[dict[str, Any]], source)
                iterator = iter(sync_source)
                while True:
                    if not await clients.wait():
                        return
                    try:
                        frame = next(iterator)
                    except StopIteration:
                        break
                    await queue.put(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 原样转交给消费侧抛出
            await _put_marker(queue, _FrameSourceFailure(exc))
        finally:
            await _put_marker(queue, _FRAME_SOURCE_END)

    async def _deliver(frame: dict[str, Any]) -> int:
        """推一帧；返回送达连接数（0 = 没人收到，该帧要留下来重试）。"""
        try:
            result: Any = await server.broadcast(frame)
        except TimeoutError:
            # 广播超时不外泄（事故根因）：按 delivered=0 留帧重试。
            logger.warning("微信桥帧广播超时，该帧留待客户端可用后重试")
            return 0
        # 返回 None 的替身 = 已送达现有客户端。
        return 1 if result is None else int(result or 0)

    producer = asyncio.create_task(_produce(), name="wechat-bridge-frame-source")
    stalls = 0
    pending: dict[str, Any] | None = None
    try:
        while True:
            # 待重试帧最先投：积压帧排在队列里，顺序不会被跳过。
            if pending is not None:
                if not await clients.wait():
                    return
                if await _deliver(pending):
                    pending = None
                continue
            # 无人收帧就不取帧：等客户端（不算帧源空转）。
            if not await clients.wait():
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout)
            except TimeoutError:
                # 有客户端在场才谈空转：等客户端接入的挂起不该累计 stall。
                if clients.active and not clients.present():
                    logger.debug("微信桥无客户端可收帧，暂停灌帧等待客户端")
                    continue
                stalls += 1
                if limit <= 0:
                    logger.warning("微信桥帧源超时无产出，停止灌帧")
                    return
                logger.warning(
                    "微信桥帧源 %.1fs 无产出（%d/%d），继续等待",
                    timeout,
                    stalls,
                    limit,
                )
                if stalls >= limit:
                    logger.error(
                        "微信桥帧源连续 %d 次无产出（%.1fs），停止灌帧",
                        stalls,
                        timeout,
                    )
                    return
                continue
            if item is _FRAME_SOURCE_END:
                return
            if isinstance(item, _FrameSourceFailure):
                raise item.exc
            stalls = 0
            if await _deliver(item) == 0:
                pending = item
    finally:
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await producer
