"""出站传输后端：UIA（默认）/ HTTP hook / auto 回落。

设计约束（与协议 §7.6 对齐）：

- **默认 uia**：行为与历史完全一致，由 `sender.UiaTransport` 承担；
- **hook**：只向 ``hook_url`` POST 一个 JSON，成功也只回 ``typed``。后端自己的
  成功不可信（社区 hook 常见「永远 ret:0」），真实送达由本地库回读校验
  （见 :mod:`receipt`）升级成 ``observed``；
- **auto**：先 hook，只有**可证明没发出去**时才回落 UIA（群显示名解析失败、
  连接被拒、后端明确 ``sent:false``）。HTTP 超时 / 5xx / 响应损坏属于结果不明，
  **绝不**回落——那正是重复发送的根源。

hook 请求/响应契约（本项目自定义，接具体 hook 时按它适配）::

    POST <hook_url>   Content-Type: application/json
    {"action": "send_text", "chatroom": "<xxx@chatroom>",
     "chat_name": "<群显示名>", "text": "<正文>"}

    -> {"ok": true, "message_id": "..."}
    -> {"ok": false, "sent": false, "error_code": "..."}   # 明确未发送
    -> 其它（非 2xx / 非 JSON / 无明确 ok/sent）视为结果不明

群 username 从本地库解析（``LocalDbSource.resolve_username``）：hook 模式下
本地库不可用就返回 ``chat_name_unresolved``，绝不猜 id。
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .sender import Prepared, SendResult

log = logging.getLogger(__name__)

__all__ = ["AutoTransport", "HookTransport", "build_transport"]


class HookTransport:
    """HTTP 注入后端：不碰 UIA，锁屏也能发（前提是 hook 服务可用）。"""

    name = "hook"
    requires_desktop = False
    may_use_desktop = False

    def __init__(
        self,
        url: str,
        *,
        resolver: Callable[[str], Any] | None = None,
        timeout: float = 5.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._url = str(url or "").strip()
        self._resolver = resolver
        self._timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen

    # ------------------------------------------------------------- 准备
    def prepare(self, chat_name: str) -> Prepared | SendResult:
        name = str(chat_name or "").strip()
        if not name:
            return SendResult(
                False, "invalid_argument", "failed", detail="会话显示名为空"
            )
        target = self._resolve(name)
        if not target:
            return SendResult(
                False,
                "chat_name_unresolved",
                "failed",
                chat_name=name,
                detail="hook 需要本地库把群显示名解析成 xxx@chatroom",
                not_sent=True,
            )
        return Prepared(chat_name=name, target=target)

    def _resolve(self, name: str) -> str:
        if self._resolver is None:
            return ""
        try:
            return str(self._resolver(name) or "").strip()
        except Exception:
            log.warning("hook 解析群 username 失败：%s", name, exc_info=True)
            return ""

    # ------------------------------------------------------------- 发送
    def send(self, prepared: Prepared, text: str, delay: float) -> SendResult:
        # `delay` 是 UIA 的打字节奏；hook 不用，保留统一签名。
        del delay
        chat_name = prepared.chat_name
        if not self._url:
            return SendResult(
                False,
                "hook_unavailable",
                "failed",
                chat_name=chat_name,
                detail="hook_url 未配置",
                not_sent=True,
            )

        payload = {
            "action": "send_text",
            "chatroom": prepared.target,
            "chat_name": chat_name,
            "text": text,
        }
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # 请求已到服务器，发没发出去未知：不回落。
            return SendResult(
                False,
                "hook_unknown",
                "failed",
                chat_name=chat_name,
                detail=f"HTTP {exc.code}",
            )
        except TimeoutError as exc:
            return SendResult(
                False,
                "hook_unknown",
                "failed",
                chat_name=chat_name,
                detail=f"timeout: {exc}",
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return SendResult(
                    False,
                    "hook_unknown",
                    "failed",
                    chat_name=chat_name,
                    detail=f"timeout: {reason}",
                )
            return SendResult(
                False,
                "hook_unavailable",
                "failed",
                chat_name=chat_name,
                detail=f"{type(reason).__name__}: {reason}",
                not_sent=True,
            )
        except OSError as exc:
            # 连接根本没建立（拒绝 / DNS 失败）可以证明没发；其它 OSError
            # （比如写到一半连接断了）结果不明，不能回落。
            never_connected = isinstance(
                exc, (ConnectionRefusedError, socket.gaierror)
            )
            return SendResult(
                False,
                "hook_unavailable",
                "failed",
                chat_name=chat_name,
                detail=f"{type(exc).__name__}: {exc}",
                not_sent=never_connected,
            )
        except Exception as exc:  # noqa: BLE001 - opener 可注入，保底不崩 worker
            return SendResult(
                False,
                "hook_unknown",
                "failed",
                chat_name=chat_name,
                detail=f"{type(exc).__name__}: {exc}",
            )

        if not 200 <= status < 300:
            return SendResult(
                False,
                "hook_unknown",
                "failed",
                chat_name=chat_name,
                detail=f"HTTP {status}",
            )
        return self._parse_response(chat_name, raw)

    def _parse_response(self, chat_name: str, raw: Any) -> SendResult:
        try:
            body = json.loads(bytes(raw).decode("utf-8") or "{}")
        except (TypeError, UnicodeDecodeError, ValueError):
            return SendResult(
                False,
                "hook_unknown",
                "failed",
                chat_name=chat_name,
                detail="hook 响应不是 JSON",
            )
        if not isinstance(body, dict):
            return SendResult(
                False,
                "hook_unknown",
                "failed",
                chat_name=chat_name,
                detail="hook 响应不是 JSON 对象",
            )
        if body.get("ok") is True:
            message_id = body.get("message_id")
            return SendResult(
                True,
                None,
                "typed",
                str(message_id) if message_id is not None else None,
                chat_name,
            )
        if body.get("sent") is False:
            code = str(body.get("error_code") or "hook_rejected")
            return SendResult(
                False,
                code,
                "failed",
                chat_name=chat_name,
                detail="hook 明确返回 sent:false",
                not_sent=True,
            )
        return SendResult(
            False,
            "hook_unknown",
            "failed",
            chat_name=chat_name,
            detail="hook 响应缺少明确的 ok / sent:false",
        )


class AutoTransport:
    """先 hook、必要时回落 UIA；只在「确定没发」时回落。"""

    name = "auto"
    requires_desktop = False
    may_use_desktop = True

    def __init__(
        self,
        hook: HookTransport,
        uia_factory: Callable[[], Any],
    ) -> None:
        self._hook = hook
        self._uia_factory = uia_factory
        self._uia: Any | None = None

    # ------------------------------------------------------------- 准备
    def prepare(self, chat_name: str) -> Prepared | SendResult:
        name = str(chat_name or "").strip()
        if not name:
            return SendResult(
                False, "invalid_argument", "failed", detail="会话显示名为空"
            )
        # 真正的准备（hook 解析 / UIA 开窗）推迟到 send：先给 hook 一次机会。
        return Prepared(chat_name=name)

    # ------------------------------------------------------------- 发送
    def send(self, prepared: Prepared, text: str, delay: float) -> SendResult:
        hook_prepared = self._hook.prepare(prepared.chat_name)
        if isinstance(hook_prepared, SendResult):
            if hook_prepared.not_sent:
                log.info(
                    "hook 侧无法发送（%s），回落 UIA：%s",
                    hook_prepared.error_code,
                    prepared.chat_name,
                )
                return self._fallback(prepared, text, delay)
            return hook_prepared

        result = self._hook.send(hook_prepared, text, delay)
        if result.ok:
            return result
        if result.not_sent:
            log.warning(
                "hook 明确未发送（%s），回落 UIA：%s",
                result.error_code,
                prepared.chat_name,
            )
            return self._fallback(prepared, text, delay)
        # 结果不明（超时 / 5xx / 响应损坏）：绝不回落，避免重复发送。
        log.error(
            "hook 结果不明（%s），不回落 UIA：%s",
            result.error_code,
            prepared.chat_name,
        )
        return result

    def _fallback(self, prepared: Prepared, text: str, delay: float) -> SendResult:
        uia = self._uia
        if uia is None:
            uia = self._uia_factory()
            self._uia = uia
        uia_prepared = uia.prepare(prepared.chat_name)
        if isinstance(uia_prepared, SendResult):
            return uia_prepared
        return uia.send(uia_prepared, text, delay)


def build_transport(
    wechat: Any,
    *,
    uia_driver: Any,
    poller: Any = None,
    uia_factory: Callable[[], Any] | None = None,
) -> Any | None:
    """按 ``gateway.wechat.outbound`` 构造传输；``uia`` 返回 None 表示用默认。

    ``uia_driver`` 必须是 UIA 线程代理（生产路径是 `_UiThreadDriver`）。
    """
    mode = str(getattr(wechat, "outbound", "uia") or "uia").strip().lower()
    if mode == "uia":
        return None

    from .sender import UiaTransport

    def make_uia() -> Any:
        if uia_factory is not None:
            return uia_factory()
        return UiaTransport(wechat, driver=uia_driver)

    hook = HookTransport(
        str(getattr(wechat, "hook_url", "") or ""),
        resolver=_localdb_username_resolver(poller),
        timeout=float(getattr(wechat, "hook_timeout_seconds", 5.0) or 5.0),
    )
    if mode == "hook":
        return hook
    if mode == "auto":
        return AutoTransport(hook, make_uia)
    # 配置层已校验；这里保底按默认 UIA 处理。
    log.warning("未知 outbound=%r，按 UIA 处理", mode)
    return None


def _localdb_username_resolver(poller: Any) -> Callable[[str], Any] | None:
    source = getattr(poller, "localdb", None)
    if source is None:
        return None
    resolve = getattr(source, "resolve_username", None)
    return resolve if callable(resolve) else None
