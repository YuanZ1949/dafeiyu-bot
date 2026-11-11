"""QQ ↔ 微信群双向镜像的核心：快照、每方向队列、渲染、回声指纹与落库。

入站只在 `Service.handle` 的汇点旁路一次：`observe_inbound` 同步入队，
绝不 await/下载/抛异常；真正的发送与落库由每方向一个 worker 串行完成。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, Sequence

from . import images, media
from .config import Settings
from .memory.store import Store
from .models import Attachment, ChatType, Message, Segment
from .reply_media import build_onebot_segments
from .sender import MAX_CHUNK, _split

log = logging.getLogger(__name__)

#: 微信 ``.dat`` 解码器：与 ``Service.handle`` 注入的是同一个函数。
DecodeFn = Callable[[bytes], Awaitable[bytes | None]]
#: 下载函数：签名与 ``media.FetchFn`` / ``images.FetchFn`` 一致，可注入。
FetchFn = Callable[[str, float, int], Awaitable[bytes]]

#: 每方向队列容量：突发 30 条远达不到，溢出只可能是长期卡死。
QUEUE_MAXSIZE = 1000
#: 入站内存幂等窗口（秒）：覆盖桥重投，重启后的重投由 message_exists 兜住。
IDEMPOTENCY_TTL = 600.0
#: 回显指纹 TTL（秒）：只认 10 秒内本账号刚发出的内容。
FINGERPRINT_TTL = 10.0
#: 出队时超过这个年龄的条目直接丢弃，避免断桥恢复后补发一批过期消息。
ITEM_MAX_AGE = 300.0
#: 单条发送超时（秒）：超时视为结果未知，不重试，避免重复。
SEND_TIMEOUT = 30.0
#: 只对 not_connected 重试；这是三次重试的退避秒数（总尝试次数 = 1 + 3）。
RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)

_COUNTER_NAMES = ("sent", "degraded", "dropped", "expired", "deduped", "retries")

#: QQ 段类型 → 归一化媒体类型；`record` 统一成 voice。
_SEGMENT_MEDIA = {
    "image": "image",
    "record": "voice",
    "voice": "voice",
    "video": "video",
    "file": "file",
    "face": "sticker",
    "mface": "sticker",
    "node": "forward",
    "forward": "forward",
}

_PLACEHOLDERS = {
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "sticker": "[表情]",
}


class _Stop:
    """队列停止哨兵：cancel 在 Windows 事件循环上偶发不唤醒时兜底。"""


_STOP = _Stop()


@dataclass(frozen=True)
class MirrorItem:
    """入队时的不可变快照：绝不持有活 `Message`。"""

    source_chat_id: str
    target_chat_id: str
    source_message_id: str
    nickname: str
    is_bot: bool
    text_lines: tuple[str, ...]
    attachments: tuple[Attachment, ...]
    segments: tuple[Segment, ...]
    source_channel: str
    created_at: float
    # QQ 的 kind 恒为 text；微信才用它区分媒体，故随快照一起保存。
    kind: str = "text"


def _normalize(text: str) -> str:
    """指纹文本归一化：只 strip + 折叠连续空白，不改写正文。"""
    return " ".join(text.split())


def _bare_id(value: str) -> str:
    """去掉通道前缀（``qq:`` / ``wxg:``）后的裸 id，用于跨形态比较。"""
    text = str(value or "")
    _prefix, sep, rest = text.partition(":")
    return rest if sep else text


def _copy_segments(segments: Sequence[Segment]) -> tuple[Segment, ...]:
    return tuple(Segment(type=seg.type, data=dict(seg.data)) for seg in segments)


def _media_kind(kind: str) -> str:
    normalized = _SEGMENT_MEDIA.get(str(kind or "").strip().lower(), "")
    return normalized or str(kind or "").strip().lower()


#: 只为内层真实存在的方法生成包装，能力探测语义才不会被代理伪造。
_PROXIED_METHODS = (
    "send",
    "send_segments",
    "send_media",
    "upload_file",
    "send_forward",
    "send_at",
)

#: OneBot 段类型里需要转成附件的媒体集合。
_ATTACHMENT_SEGMENTS = frozenset(
    {"image", "file", "record", "voice", "video", "sticker"}
)
#: 纯表情段：没有可发送的媒体，只在正文补占位。
_STICKER_SEGMENTS = frozenset({"face", "mface"})


class OutboundObserver(Protocol):
    """`MirrorRouter` 的最小出站观察接口：测试可注入轻量替身。"""

    def observe_outbound(
        self,
        *,
        target_chat_id: str,
        text: str = "",
        message_id: str | None = None,
        attachments: Sequence[Attachment] = (),
        segments: Sequence[Segment] = (),
        kind: str = "text",
        nickname: str = "",
    ) -> None: ...


class MirroredAdapter:
    """出站代理：把内层 adapter 的每次成功发送旁路给 `MirrorRouter`。

    只为内层真实存在的方法在实例上绑定包装，因此
    `callable(getattr(proxy, "send_media", None))` 与内层保持一致；
    其余属性（含生命周期/事件注册）一律 `__getattr__` 委托。
    """

    _inner: Any
    _router: OutboundObserver | None

    def __init__(self, inner: object, router: OutboundObserver | None) -> None:
        self._inner = inner
        self._router = router
        for name in _PROXIED_METHODS:
            if callable(getattr(inner, name, None)):
                object.__setattr__(self, name, getattr(self, f"_proxy_{name}"))

    def __getattr__(self, name: str) -> Any:
        try:
            inner = object.__getattribute__(self, "_inner")
        except AttributeError:
            raise AttributeError(name) from None
        return getattr(inner, name)

    # ------------------------------------------------------------ 观察旁路
    def _observe(self, **payload: Any) -> None:
        router = self._router
        if router is None:
            return
        router.observe_outbound(**payload)

    # ------------------------------------------------------------ 文本
    async def _proxy_send(
        self,
        chat_type: ChatType,
        chat_id: str,
        text: str,
        **kwargs: Any,
    ) -> Any:
        result = await self._inner.send(chat_type, chat_id, text, **kwargs)
        self._observe(
            target_chat_id=chat_id,
            text=text,
            message_id=result if isinstance(result, str) else None,
        )
        return result

    # ------------------------------------------------------------ 消息段
    async def _proxy_send_segments(
        self,
        chat_type: ChatType,
        chat_id: str,
        segments: Sequence[Mapping[str, Any]],
        **kwargs: Any,
    ) -> Any:
        result = await self._inner.send_segments(chat_type, chat_id, segments, **kwargs)
        body, attachments = self._rebuild_segments(segments)
        # T3 的 QQ 分支：有附件且无 text 段时正文为空，所以正文必须显式回传。
        synthetic = (
            (Segment(type="text", data={"text": body}),) if body else ()
        )
        self._observe(
            target_chat_id=chat_id,
            text=body,
            message_id=result if isinstance(result, str) else None,
            attachments=attachments,
            segments=synthetic,
            kind="text",
        )
        return result

    @staticmethod
    def _rebuild_segments(
        segments: Sequence[Mapping[str, Any]],
    ) -> tuple[str, tuple[Attachment, ...]]:
        body: list[str] = []
        attachments: list[Attachment] = []
        for raw in segments:
            if not isinstance(raw, dict):
                continue
            seg_type = str(raw.get("type") or "")
            data = raw.get("data")
            payload: Mapping[str, Any] = data if isinstance(data, dict) else {}
            if seg_type == "text":
                body.append(str(payload.get("text") or ""))
            elif seg_type in _STICKER_SEGMENTS:
                body.append("[表情]")
            elif seg_type in _ATTACHMENT_SEGMENTS:
                attachments.append(
                    Attachment(
                        kind=seg_type,
                        source=str(payload.get("url") or ""),
                        path=str(payload.get("file") or payload.get("path") or ""),
                        name=str(payload.get("name") or ""),
                    )
                )
            # at/reply 及未知段：跨平台不搬运，直接丢弃。
        return "".join(body), tuple(attachments)

    # ------------------------------------------------------------ 媒体
    async def _proxy_send_media(
        self,
        chat_type: ChatType,
        chat_id: str,
        kind: str,
        path: str,
        name: str = "",
        **kwargs: Any,
    ) -> Any:
        result = await self._inner.send_media(
            chat_type, chat_id, kind, path, name=name, **kwargs
        )
        if result is True:
            self._observe(
                target_chat_id=chat_id,
                attachments=[Attachment(kind=kind, path=path, name=name)],
                kind=kind,
            )
        return result

    async def _proxy_upload_file(
        self,
        chat_type: ChatType,
        chat_id: str,
        path: str,
        name: str = "",
        **kwargs: Any,
    ) -> Any:
        inner = self._inner
        if callable(getattr(inner, "send_media", None)):
            # 微信 upload_file 在 unsupported_media 时静默返回 None，无法判定；
            # 直接走 send_media 拿到布尔结果，仅 True 才入队。
            result = await inner.send_media(chat_type, chat_id, "file", path, name=name)
            if result is True:
                self._observe(
                    target_chat_id=chat_id,
                    attachments=[Attachment(kind="file", path=path, name=name)],
                    kind="file",
                )
            return result
        result = await inner.upload_file(
            chat_type, chat_id, path, name=name, **kwargs
        )
        self._observe(
            target_chat_id=chat_id,
            attachments=[Attachment(kind="file", path=path, name=name)],
            kind="file",
        )
        return result

    # ------------------------------------------------------------ 转发/@
    async def _proxy_send_forward(
        self,
        chat_type: ChatType,
        chat_id: str,
        nodes: Sequence[Mapping[str, Any]],
        **kwargs: Any,
    ) -> Any:
        result = await self._inner.send_forward(
            chat_type, chat_id, nodes, **kwargs
        )
        self._observe(
            target_chat_id=chat_id,
            text=f"[合并转发 {max(1, len(nodes))} 条]",
            kind="forward",
        )
        return result

    async def _proxy_send_at(
        self,
        chat_type: ChatType,
        chat_id: str,
        text: str,
        at: Sequence[str],
        **kwargs: Any,
    ) -> Any:
        result = await self._inner.send_at(
            chat_type, chat_id, text, at, **kwargs
        )
        # @ 关系不跨平台，按纯文本镜像。
        self._observe(target_chat_id=chat_id, text=text)
        return result


class MirrorRouter:
    """多对群的双向镜像：每个方向一条共用队列 + 一个 worker。

    多对时按群精确匹配路由，每个群只属于一对、各对之间不交叉。同一方向
    的多对共用一条队列是有意为之（保持队列/worker 结构与既有测试不变）：
    发送按条目自带的 ``target_chat_id`` 分流，代价是同一方向上不同对串行。

    ``decode`` / ``fetch`` 是 worker 内媒体物化的可选协作钩子：

    - ``decode`` 用于微信 ``.dat`` 解码，与 ``Service.handle`` 必须同一个实现；
      T5 在 ``Service`` 构造完成后设置 ``router.decode = service._decode_wechat_dat``；
    - ``fetch`` 可注入下载函数（测试或替换 httpx 默认实现）；``None`` 时由
      ``images`` / ``media`` 模块使用各自默认实现。
    """

    def __init__(
        self,
        settings: Settings,
        store: Store,
        adapters: Mapping[str, Any],
        *,
        decode: DecodeFn | None = None,
        fetch: FetchFn | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        #: 只持有内层 adapter：镜像自身的发送永远不经过代理，闭环防回环。
        self.adapters: Mapping[str, Any] = dict(adapters)
        #: 微信 .dat 解码器与下载函数（见类 docstring）；T5 会覆写 decode。
        self.decode: DecodeFn | None = decode
        self.fetch: FetchFn | None = fetch
        pairs = tuple(settings.mirror.pairs) if settings.mirror.enabled else ()
        #: chat_id → (来源通道, 目标群)；多对时每对独立匹配，群之间不交叉。
        self._routes: dict[str, tuple[str, str]] = {}
        for pair in pairs:
            self._routes[pair.qq] = ("qq", pair.wechat)
            self._routes[pair.wechat] = ("wechat", pair.qq)
        self._queues: dict[str, asyncio.Queue[MirrorItem | _Stop]] = {
            "qq": asyncio.Queue(maxsize=QUEUE_MAXSIZE),
            "wechat": asyncio.Queue(maxsize=QUEUE_MAXSIZE),
        }
        self._tasks: list[asyncio.Task[None]] = []
        #: (source_chat_id, source_message_id) → 首次观察时间。
        self._seen: dict[tuple[str, str], float] = {}
        #: (chat_id, 归一化文本) → 过期时间；一次性、10s。
        self._text_echo: dict[tuple[str, str], float] = {}
        #: (chat_id, kind, name, size) → 过期时间；一次性、10s。
        self._media_echo: dict[tuple[str, str, str, int], float] = {}
        self._counters: dict[str, int] = {name: 0 for name in _COUNTER_NAMES}
        self._last_error = ""

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """创建两个方向的 worker；未配置配对时不启动任何任务。"""
        if not self._routes:
            return
        if self._tasks:
            return
        for channel in ("qq", "wechat"):
            task = asyncio.create_task(
                self._worker(channel), name=f"mirror-{channel}"
            )
            task.add_done_callback(self._on_worker_done)
            self._tasks.append(task)

    def _on_worker_done(self, task: "asyncio.Task[None]") -> None:
        """异常退出要让统计与日志看得见；正常结束/cancel 保持安静。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        self._last_error = f"worker_exit:{type(exc).__name__}:{exc}"
        log.error("镜像 worker 异常退出：%s", task.get_name(), exc_info=exc)

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        # 兜底：cancel 偶发不唤醒等待 queue.get() 的任务时，用哨兵把它叫醒。
        for queue in self._queues.values():
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(_STOP)
        for task in tasks:
            # 已崩溃的 worker 由 done-callback 记录；这里不能让 await 把异常
            # 再抛给 Service.stop，否则停机流程会被一个 worker 故障打断。
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def stats(self) -> dict[str, int | str]:
        result: dict[str, int | str] = dict(self._counters)
        result["last_error"] = self._last_error
        return result

    # ------------------------------------------------------------ 入站观察
    def observe_inbound(self, msg: Message) -> None:
        """同步、O(1)、绝不 await/下载/抛异常。"""
        try:
            self._observe_inbound(msg)
        except Exception:  # noqa: BLE001 - 镜像异常不得影响原消息处理
            log.exception("镜像入站观察失败（已忽略）msg=%s", getattr(msg, "message_id", ""))

    def _observe_inbound(self, msg: Message) -> None:
        if msg.chat_type is not ChatType.GROUP:
            return
        direction = self._direction(msg.chat_id)
        if direction is None:
            return
        if msg.is_bot:
            return
        if not msg.message_id:
            return
        # 进程重启后桥重投：内存幂等已清空，只能靠 UNIQUE 索引判断。
        if self.store.message_exists(msg.chat_type, msg.chat_id, msg.message_id):
            self._counters["deduped"] += 1
            return
        now = time.time()
        if self._seen_before((msg.chat_id, msg.message_id), now):
            self._counters["deduped"] += 1
            return
        source_channel, target_chat_id = direction
        if self._consume_echo(msg, source_channel, now):
            self._counters["deduped"] += 1
            return
        item = self._snapshot(msg, target_chat_id, source_channel)
        self._enqueue(source_channel, item)

    # ------------------------------------------------------------ 出站观察
    def observe_outbound(
        self,
        *,
        target_chat_id: str,
        text: str = "",
        message_id: str | None = None,
        attachments: Sequence[Attachment] = (),
        segments: Sequence[Segment] = (),
        kind: str = "text",
        nickname: str = "",
    ) -> None:
        """T4 在「调用未抛异常」后调用；id 可以是 None。

        只有目标群属于配对时才镜像到另一侧；镜像自身的发送绕过代理。
        """
        try:
            self._observe_outbound(
                target_chat_id=target_chat_id,
                text=text,
                message_id=message_id,
                attachments=attachments,
                segments=segments,
                kind=kind,
                nickname=nickname,
            )
        except Exception:  # noqa: BLE001 - 出站观察同样不得影响原发送
            log.exception("镜像出站观察失败（已忽略）target=%s", target_chat_id)

    def _observe_outbound(
        self,
        *,
        target_chat_id: str,
        text: str,
        message_id: str | None,
        attachments: Sequence[Attachment],
        segments: Sequence[Segment],
        kind: str,
        nickname: str,
    ) -> None:
        direction = self._direction(target_chat_id)
        if direction is None:
            return
        source_channel, target = direction
        item = MirrorItem(
            source_chat_id=target_chat_id,
            target_chat_id=target,
            source_message_id=message_id or "",
            nickname=nickname or self.settings.persona.name,
            is_bot=True,
            text_lines=tuple(text.split("\n")) if text else (),
            attachments=tuple(attachments),
            segments=_copy_segments(segments),
            source_channel=source_channel,
            created_at=time.time(),
            kind=kind,
        )
        # 先把「本账号刚发的内容」记成一次性指纹，回显帧 10s 内命中即丢弃。
        self._register_fingerprints(target_chat_id, text, attachments)
        self._enqueue(source_channel, item)

    # ------------------------------------------------------------ worker
    async def _worker(self, source_channel: str) -> None:
        queue = self._queues[source_channel]
        while True:
            try:
                item = await queue.get()
            except asyncio.CancelledError:
                raise
            if isinstance(item, _Stop):
                queue.task_done()
                return
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker 必须永久存活
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("镜像 worker 处理失败（继续下一条）msg=%s", item.source_message_id)
            finally:
                queue.task_done()

    async def _deliver(self, item: MirrorItem) -> None:
        if time.time() - item.created_at > ITEM_MAX_AGE:
            self._counters["expired"] += 1
            log.info("镜像条目已过期，丢弃：%s msg=%s", item.source_chat_id, item.source_message_id)
            return
        target_channel = "wechat" if item.source_channel == "qq" else "qq"
        adapter = self.adapters.get(target_channel)
        if adapter is None:
            self._counters["degraded"] += 1
            self._last_error = f"no_{target_channel}_adapter"
            log.warning("镜像目标通道没有 adapter，降级跳过：%s", target_channel)
            return

        # 物化只发生在 worker：observe_inbound 仍是同步快照。
        attachments = await self._materialized_attachments(item)
        lines = self._render(item)
        if target_channel == "qq":
            await self._deliver_qq(item, adapter, lines, attachments)
        else:
            await self._deliver_wechat(item, adapter, lines, attachments)

    # ------------------------------------------------------------ 物化
    async def _materialized_attachments(self, item: MirrorItem) -> list[Attachment]:
        """按来源通道物化媒体，返回可直接交给目标通道的附件列表。

        任何失败都只记日志并保留原附件（占位路径照常发送），绝不抛异常。
        """
        raw = list(item.attachments)
        try:
            if item.source_channel == "wechat":
                message = self._relay_message(item, raw, item.segments)
                materialized = await media.materialize_relay_attachments(
                    message,
                    self.settings,
                    fetch=self.fetch,
                    decode=self.decode,
                )
                return list(materialized.attachments)
            return await self._materialize_qq(item, raw)
        except Exception:  # noqa: BLE001 - 物化失败不得影响占位发送
            log.warning(
                "镜像媒体物化失败，按占位发送：msg=%s",
                item.source_message_id,
                exc_info=True,
            )
            return raw

    async def _materialize_qq(
        self, item: MirrorItem, raw: list[Attachment]
    ) -> list[Attachment]:
        """QQ 源：只补缺失本地文件，已有可读路径原样复用。"""
        readable = [
            media.resolve_media_path(self.settings, attachment) is not None
            for attachment in raw
        ]
        merged = list(raw)

        image_positions = [
            index
            for index, attachment in enumerate(raw)
            if _media_kind(attachment.kind) == "image"
        ]
        missing_images = [
            index for index in image_positions if not readable[index]
        ]
        if missing_images:
            # 图片段与 image 附件同序；只把缺失本地文件的段交给 relay。
            image_segments = [seg for seg in item.segments if seg.type == "image"]
            selected = [
                image_segments[position]
                for position, index in enumerate(image_positions)
                if not readable[index] and position < len(image_segments)
            ]
            if selected:
                message = self._relay_message(item, [], selected)
                produced = await images.materialize_relay(
                    message, self.settings, fetch=self.fetch
                )
                for index, attachment in zip(missing_images, produced):
                    merged[index] = attachment

        missing_others = [
            (index, attachment)
            for index, attachment in enumerate(raw)
            if not readable[index] and _media_kind(attachment.kind) != "image"
        ]
        if missing_others:
            message = self._relay_message(
                item, [attachment for _, attachment in missing_others], ()
            )
            materialized = await media.materialize_relay_attachments(
                message, self.settings, fetch=self.fetch, decode=None
            )
            # 语音/视频会换成本地名；按 (kind, source) 回填，顺序保持不变。
            buckets: dict[tuple[str, str], list[Attachment]] = {}
            for attachment in materialized.attachments:
                key = (_media_kind(attachment.kind), attachment.source)
                buckets.setdefault(key, []).append(attachment)
            for index, attachment in missing_others:
                key = (_media_kind(attachment.kind), attachment.source)
                bucket = buckets.get(key)
                if bucket:
                    merged[index] = bucket.pop(0)
        return merged

    @staticmethod
    def _relay_message(
        item: MirrorItem,
        attachments: Sequence[Attachment],
        segments: Sequence[Segment],
    ) -> Message:
        """构造只给物化 helper 用的轻量消息：不共享调用方的 content/附件列表。"""
        return Message(
            chat_type=ChatType.GROUP,
            chat_id=item.source_chat_id,
            user_id="",
            nickname=item.nickname,
            content="\n".join(item.text_lines),
            message_id=item.source_message_id,
            segments=[
                Segment(type=segment.type, data=dict(segment.data))
                for segment in segments
            ],
            attachments=list(attachments),
            is_bot=item.is_bot,
            kind=item.kind,
        )

    # ------------------------------------------------------------ 目标 QQ
    async def _deliver_qq(
        self,
        item: MirrorItem,
        adapter: Any,
        lines: list[str],
        attachments: Sequence[Attachment],
    ) -> None:
        segment_attachments: list[Attachment] = []
        files: list[Attachment] = []
        for attachment in attachments:
            kind = _media_kind(attachment.kind)
            if kind not in ("image", "voice", "video", "file", "sticker"):
                continue
            resolved = media.resolve_media_path(self.settings, attachment)
            if resolved is None:
                continue
            prepared = replace(attachment, path=str(resolved))
            if kind == "file":
                files.append(prepared)
            else:
                segment_attachments.append(prepared)

        if segment_attachments and callable(
            getattr(adapter, "send_segments", None)
        ):
            explanation = "".join(lines)
            segments = build_onebot_segments(
                explanation, attachments=segment_attachments
            )
            ok, result = await self._send_with_retry(
                adapter,
                lambda: adapter.send_segments(
                    ChatType.GROUP, item.target_chat_id, segments
                ),
                "消息段发送",
                item.target_chat_id,
            )
            if not ok:
                self._counters["dropped"] += 1
                return
            self._counters["sent"] += 1
            message_id = result if isinstance(result, str) else None
            self._register_fingerprints(item.target_chat_id, explanation, ())
            try:
                self._store_line(item, adapter, explanation, message_id, 0)
            except Exception:  # noqa: BLE001 - 落库失败不阻断文件上传
                log.exception("镜像消息落库失败：%s", item.target_chat_id)
        elif not await self._send_lines(item, adapter, lines):
            return

        for attachment in files:
            await self._send_with_retry(
                adapter,
                lambda a=attachment: adapter.upload_file(
                    ChatType.GROUP,
                    item.target_chat_id,
                    a.path,
                    name=a.name,
                ),
                "文件上传",
                item.target_chat_id,
            )

    # ------------------------------------------------------------ 目标微信
    async def _deliver_wechat(
        self,
        item: MirrorItem,
        adapter: Any,
        lines: list[str],
        attachments: Sequence[Attachment],
    ) -> None:
        if not lines:
            return
        if not await self._send_lines(item, adapter, lines):
            return

        for attachment in attachments:
            kind = _media_kind(attachment.kind)
            if kind not in ("image", "voice", "file", "sticker"):
                # video / forward：hook 不支持，说明行里的占位就是全部。
                continue
            resolved = media.resolve_media_path(self.settings, attachment)
            if resolved is None:
                continue
            path = str(resolved)
            name = attachment.name or ""
            if kind == "sticker":
                ok = await self._send_media(
                    adapter, item.target_chat_id, "sticker", path, name
                )
                if not ok:
                    # 微信 hook 的 sticker 可能不被接受：降级当图片再试一次。
                    await self._send_media(
                        adapter, item.target_chat_id, "image", path, name
                    )
                continue
            await self._send_media(
                adapter, item.target_chat_id, kind, path, name
            )

    async def _send_lines(
        self, item: MirrorItem, adapter: Any, lines: Sequence[str]
    ) -> bool:
        """逐片发送说明行；全部成功才返回 True（失败计入 dropped 并跳过媒体）。"""
        all_ok = True
        for index, text in enumerate(lines):
            ok, message_id = await self._send_text(
                adapter, item.target_chat_id, text
            )
            if not ok:
                self._counters["dropped"] += 1
                all_ok = False
                continue
            self._counters["sent"] += 1
            self._register_fingerprints(item.target_chat_id, text, ())
            try:
                self._store_line(item, adapter, text, message_id, index)
            except Exception:  # noqa: BLE001 - 落库失败不阻断后续分片
                log.exception("镜像消息落库失败：%s", item.target_chat_id)
        return all_ok

    async def _send_media(
        self,
        adapter: Any,
        chat_id: str,
        kind: str,
        path: str,
        name: str,
    ) -> bool:
        if not callable(getattr(adapter, "send_media", None)):
            return False
        ok, result = await self._send_with_retry(
            adapter,
            lambda: adapter.send_media(
                ChatType.GROUP, chat_id, kind, path, name=name
            ),
            "媒体发送",
            chat_id,
        )
        if not ok:
            return False
        if result is False:
            # 桥明确说不支持：不重试，也不补第二条占位。
            log.info("镜像媒体被目标通道拒绝（不重试，保留占位）：%s kind=%s", chat_id, kind)
            return False
        return True

    async def _send_text(
        self, adapter: Any, chat_id: str, text: str
    ) -> tuple[bool, str | None]:
        ok, result = await self._send_with_retry(
            adapter,
            lambda: adapter.send(ChatType.GROUP, chat_id, text),
            "发送",
            chat_id,
        )
        if not ok:
            return False, None
        return True, result if isinstance(result, str) else None

    async def _send_with_retry(
        self,
        adapter: Any,
        action: Callable[[], Awaitable[Any]],
        label: str,
        chat_id: str,
    ) -> tuple[bool, Any]:
        """所有出站动作共用的超时/重试/终态分类（与 T3 文本发送一致）。"""
        attempt = 0
        while True:
            try:
                result = await asyncio.wait_for(action(), SEND_TIMEOUT)
                return True, result
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._counters["degraded"] += 1
                self._last_error = "timeout"
                log.warning("镜像%s超时（结果未知，不重试）：%s", label, chat_id)
                return False, None
            except Exception as exc:  # noqa: BLE001 - 按错误码分类
                code = self._error_code(exc, adapter)
                if code == "unsupported_media":
                    log.info(
                        "镜像目标不支持该媒体（不重试）：%s %s", chat_id, exc
                    )
                    return False, None
                if code == "not_connected" and attempt < len(RETRY_BACKOFF_SECONDS):
                    delay = RETRY_BACKOFF_SECONDS[attempt]
                    attempt += 1
                    self._counters["retries"] += 1
                    log.info(
                        "镜像目标未连接，%.0fs 后第 %d 次重试：%s",
                        delay,
                        attempt,
                        chat_id,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._counters["degraded"] += 1
                self._last_error = code or str(exc)
                if code == "rate_limited":
                    log.warning(
                        "镜像发送被限速：微信桥仍在限速，修改配置后需同时重启 bot 与桥"
                    )
                log.warning(
                    "镜像%s失败（终态 %s）：%s %s",
                    label,
                    code or "-",
                    chat_id,
                    exc,
                )
                return False, None

    @staticmethod
    def _error_code(exc: Exception, adapter: Any) -> str:
        code = str(getattr(exc, "error_code", "") or "")
        if code:
            return code
        # QQ 没有错误码：断开就是 not_connected，其余一律终态。
        if getattr(adapter, "connected", True) is False:
            return "not_connected"
        return ""

    # ------------------------------------------------------------ 渲染
    def _render(self, item: MirrorItem) -> list[str]:
        placeholders = [self._placeholder(entry) for entry in self._media_entries(item)]
        text = self._plain_text(item)
        if not text:
            if not placeholders:
                return []
            body = " ".join(placeholders)
        elif placeholders:
            body = f"{text} {' '.join(placeholders)}"
        else:
            body = text
        prefix = "" if item.is_bot else f"{item.nickname}："
        full = f"{prefix}{body}" if prefix else body
        return _split(full, MAX_CHUNK) if full.strip() else []

    def _plain_text(self, item: MirrorItem) -> str:
        if item.source_channel == "qq":
            texts = [
                str(seg.data.get("text") or "")
                for seg in item.segments
                if seg.type == "text"
            ]
            if texts:
                return "".join(texts).strip()
            # 纯媒体：正文里的 [图片] 只是占位，媒体由 attachments/segments 渲染。
            if item.attachments:
                return ""
            return "\n".join(item.text_lines).strip()
        # 微信：kind 非 text 时 content 常是 [图片] 占位，不能当正文重复。
        if item.kind != "text":
            return ""
        return "\n".join(item.text_lines).strip()

    def _media_entries(self, item: MirrorItem) -> list[tuple[str, str, int]]:
        if item.source_channel == "wechat":
            if item.attachments:
                return [
                    (_media_kind(att.kind), att.name, 0) for att in item.attachments
                ]
            if item.kind != "text":
                return [(_media_kind(item.kind), "", 0)]
            return []

        entries: list[tuple[str, str, int]] = [
            (_media_kind(att.kind), att.name, 0) for att in item.attachments
        ]
        if entries:
            # 合并转发的条数只在段里；按出现顺序回填到 forward 条目。
            counts = [
                self._forward_count(seg)
                for seg in item.segments
                if _media_kind(seg.type) == "forward"
            ]
            if counts:
                filled: list[tuple[str, str, int]] = []
                cursor = 0
                for kind, name, count in entries:
                    if kind == "forward" and cursor < len(counts):
                        count = counts[cursor]
                        cursor += 1
                    filled.append((kind, name, count))
                entries = filled
            return entries
        for seg in item.segments:
            kind = _media_kind(seg.type)
            if not kind or kind == "text":
                continue
            entries.append((kind, self._segment_name(seg), self._forward_count(seg)))
        return entries

    @staticmethod
    def _forward_count(seg: Segment) -> int:
        if _media_kind(seg.type) != "forward":
            return 0
        content = seg.data.get("content")
        if isinstance(content, list):
            return max(1, len(content))
        messages = seg.data.get("messages")
        if isinstance(messages, list):
            return max(1, len(messages))
        return 1

    @staticmethod
    def _segment_name(seg: Segment) -> str:
        for key in ("name", "file", "summary"):
            value = seg.data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _placeholder(entry: tuple[str, str, int]) -> str:
        kind, name, count = entry
        if kind == "file":
            return f"[文件] {name}".rstrip()
        if kind == "forward":
            return f"[合并转发 {max(1, count)} 条]"
        return _PLACEHOLDERS.get(kind, f"[{kind}]")

    # ------------------------------------------------------------ 落库
    def _store_line(
        self,
        item: MirrorItem,
        adapter: Any,
        text: str,
        message_id: str | None,
        index: int,
    ) -> None:
        source_id = item.source_message_id or "mirror"
        mid = message_id or f"{source_id}:{uuid.uuid4().hex}:{index}"
        self.store.add_message(
            Message(
                chat_type=ChatType.GROUP,
                chat_id=item.target_chat_id,
                user_id=str(getattr(adapter, "bot_id", "") or ""),
                nickname=self.settings.persona.name,
                content=text,
                message_id=mid,
                is_bot=True,
                mirror=True,
                timestamp=int(time.time()),
                at_bot=False,
                reply_to_bot=False,
            )
        )

    # ------------------------------------------------------------ 去重/指纹
    def _direction(self, chat_id: str) -> tuple[str, str] | None:
        return self._routes.get(chat_id)

    def _seen_before(self, key: tuple[str, str], now: float) -> bool:
        if key in self._seen:
            return True
        self._seen[key] = now
        if len(self._seen) > 4096:
            cutoff = now - IDEMPOTENCY_TTL
            stale = [item for item, ts in self._seen.items() if ts < cutoff]
            for item in stale:
                self._seen.pop(item, None)
        return False

    def _consume_echo(self, msg: Message, source_channel: str, now: float) -> bool:
        hits: list[tuple[Any, Any]] = []
        if msg.content.strip():
            key = (msg.chat_id, _normalize(msg.content))
            if self._fingerprint_hit(self._text_echo, key, now):
                hits.append((self._text_echo, key))
        for att in msg.attachments:
            key = (msg.chat_id, _media_kind(att.kind), att.name, int(att.size))
            if self._fingerprint_hit(self._media_echo, key, now):
                hits.append((self._media_echo, key))
        if not hits:
            return False
        adapter = self.adapters.get(source_channel)
        if not self._is_own_echo(msg.user_id, adapter):
            # 人类消息绝不因同文被吞掉：只记日志，照常镜像。
            log.info(
                "人类消息命中镜像回显指纹，但发送者不是本通道账号，照常镜像：%s msg=%s",
                msg.chat_id,
                msg.message_id,
            )
            return False
        for registry, key in hits:
            registry.pop(key, None)
        return True

    @staticmethod
    def _fingerprint_hit(registry: dict[Any, float], key: Any, now: float) -> bool:
        expiry = registry.get(key)
        if expiry is None:
            return False
        if expiry < now:
            registry.pop(key, None)
            return False
        return True

    @staticmethod
    def _is_own_echo(user_id: str, adapter: Any) -> bool:
        bot_id = str(getattr(adapter, "bot_id", "") or "")
        return bool(bot_id) and _bare_id(bot_id) == _bare_id(user_id)

    def _register_fingerprints(
        self,
        chat_id: str,
        text: str,
        attachments: Sequence[Attachment],
    ) -> None:
        expiry = time.time() + FINGERPRINT_TTL
        if text.strip():
            self._text_echo[(chat_id, _normalize(text))] = expiry
        for att in attachments:
            self._media_echo[
                (chat_id, _media_kind(att.kind), att.name, int(att.size))
            ] = expiry

    def _snapshot(
        self, msg: Message, target_chat_id: str, source_channel: str
    ) -> MirrorItem:
        return MirrorItem(
            source_chat_id=msg.chat_id,
            target_chat_id=target_chat_id,
            source_message_id=msg.message_id,
            nickname=msg.nickname,
            is_bot=msg.is_bot,
            text_lines=tuple(msg.content.split("\n")) if msg.content else (),
            attachments=tuple(msg.attachments),
            segments=_copy_segments(msg.segments),
            source_channel=source_channel,
            created_at=time.time(),
            kind=msg.kind,
        )

    def _enqueue(self, source_channel: str, item: MirrorItem) -> None:
        queue = self._queues[source_channel]
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            self._counters["dropped"] += 1
            log.warning(
                "镜像队列已满，丢弃最新一条：%s -> %s msg=%s",
                item.source_chat_id,
                item.target_chat_id,
                item.source_message_id,
            )
