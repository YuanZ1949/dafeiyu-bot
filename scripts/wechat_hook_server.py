"""微信「非 UIA」出站 hook 服务（helper 原生桥接后端）。

dafeiyu 在 ``gateway.wechat.outbound: hook`` 时会把发送请求 POST 到这个进程，
契约与 ``src/dafeiyu/gateway/wechat_bridge/outbound.py`` 的 :class:`HookTransport`
一致::

    POST /send_text
    {"action": "send_text", "chatroom": "<wxid 或 xxx@chatroom>",
     "chat_name": "<群显示名>", "text": "<正文>"}

    -> {"ok": true, "message_id": "<可空>"}          # 已交给微信
    -> {"ok": false, "sent": false, "error_code": "..."}  # 明确没发出去
    -> {"ok": false, "error_code": "hook_unknown"}    # 结果不明（客户端不回落）

底层发送走 ``Prometheus-first/helper`` 的 ``native/text_bridge.dll``：DLL 按
Weixin.dll 的版本/大小/SHA-256 指纹校验后注入微信进程，直接调用微信自己的发送
队列。全程不点窗口、不模拟键鼠、不要求桌面解锁，所以锁屏/RDP 断开也能发。

用法::

    python scripts/wechat_hook_server.py --helper-root C:\\MyFile\\AICoding\\helper

首次启动会等微信登录就绪；日志默认写 ``logs/wechat-hook.log``。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_HELPER_ROOT = r"C:\MyFile\AICoding\helper"
DEFAULT_PORT = 30011
MAX_BODY = 1 << 20
_TARGET_SUFFIXES = ("@chatroom",)

log = logging.getLogger("wechat-hook")


# --------------------------------------------------------------------------- 后端


class HookBackend:
    """把 helper 的 NativeTextService 包成「可重连 + 线程安全」的后端。"""

    def __init__(self, helper_root: Path, *, account: str = "") -> None:
        self.helper_root = Path(helper_root)
        self.account = account or None
        self._service: Any | None = None
        self._calls = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.wechat_version = ""

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        self._bootstrap()
        self._thread = threading.Thread(target=self._supervise, name="helper-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        service = self._service
        self._service = None
        self._ready.clear()
        if service is not None:
            try:
                service.stop()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                log.warning("停止 helper 服务失败", exc_info=True)

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._service is not None

    def health(self) -> dict[str, Any]:
        service = self._service
        return {
            "ok": self.ready,
            "ready": self.ready,
            "self_wxid": getattr(service, "self_wxid", "") if service is not None else "",
            "wechat_version": self.wechat_version,
            "account": self.account or "",
            "last_error": self.last_error,
        }

    def _bootstrap(self) -> None:
        root = self.helper_root
        if not (root / "native" / "text_service.py").is_file():
            raise SystemExit(f"helper 目录不对（找不到 native/text_service.py）：{root}")
        for path in (root / "native", root):
            entry = str(path)
            if entry not in sys.path:
                sys.path.insert(0, entry)

    def _supervise(self) -> None:
        from text_service import NativeTextService  # 延迟导入：helper 依赖只在 Windows 存在

        delay = 2.0
        while not self._stop.is_set():
            try:
                service = NativeTextService(account=self.account)
                service.start()
                self.wechat_version = str(getattr(service, "wechat_version", "") or "")
                self._service = service
                self.last_error = ""
                self._ready.set()
                log.info("helper 原生桥就绪：wxid=%s 微信版本=%s",
                         getattr(service, "self_wxid", "?"), self.wechat_version or "?")
                while not self._stop.is_set() and self._service is service:
                    time.sleep(0.5)
                if self._service is service:
                    self._service = None
                self._ready.clear()
                try:
                    service.stop()
                except Exception:  # noqa: BLE001
                    log.warning("清理旧 helper 服务失败", exc_info=True)
            except Exception as exc:  # noqa: BLE001 - 微信没登录/正在升级都走重试
                self._ready.clear()
                self._service = None
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("helper 原生桥未就绪（%.0fs 后重试）：%s", delay, self.last_error)
                self._stop.wait(delay)
                delay = min(delay * 2, 60.0)
                continue
            delay = 2.0

    # ---------------------------------------------------------------- 发送
    def send_text(self, target: str, text: str) -> dict[str, Any]:
        error = _validate_target(target)
        if error:
            return _not_sent("invalid_argument", error)
        if not str(text or ""):
            return _not_sent("invalid_argument", "正文为空")

        service = self._service
        if service is None or not self._ready.is_set():
            return _not_sent("hook_not_ready", "helper 原生桥尚未就绪")

        try:
            with self._calls:
                ok = bool(service.send_text(target, text))
        except Exception as exc:  # noqa: BLE001 - DLL 侧异常：不保证没发出去
            log.exception("原生发送失败")
            self._mark_broken()
            return {"ok": False, "error_code": "hook_unknown",
                    "detail": f"{type(exc).__name__}: {exc}"}
        if not ok:
            # DLL 明确回 success=false，视为没发出去。
            return _not_sent("hook_rejected", "原生桥返回 success=false")
        return {"ok": True, "message_id": _message_id(service)}

    def send_at(self, target: str, text: str, at: list[str]) -> dict[str, Any]:
        error = _validate_target(target)
        if error:
            return _not_sent("invalid_argument", error)
        if not target.endswith("@chatroom"):
            return _not_sent("invalid_argument", "艾特消息只能发到群")
        if not at:
            return self.send_text(target, text)
        service = self._service
        if service is None or not self._ready.is_set():
            return _not_sent("hook_not_ready", "helper 原生桥尚未就绪")
        try:
            with self._calls:
                members, reason = _resolve_mentions(service, target, at)
                if members is None:
                    return _not_sent("at_unresolved", reason)
                ok = bool(service.send_at_text(target, text, members))
        except Exception as exc:  # noqa: BLE001
            log.exception("原生 @ 发送失败")
            self._mark_broken()
            return {"ok": False, "error_code": "hook_unknown",
                    "detail": f"{type(exc).__name__}: {exc}"}
        if not ok:
            return _not_sent("hook_rejected", "原生桥返回 success=false")
        return {"ok": True, "message_id": _message_id(service)}

    def send_media(self, target: str, kind: str, path: str) -> dict[str, Any]:
        error = _validate_target(target)
        if error:
            return _not_sent("invalid_argument", error)
        service = self._service
        if service is None or not self._ready.is_set():
            return _not_sent("hook_not_ready", "helper 原生桥尚未就绪")
        if not Path(str(path or "")).is_file():
            return _not_sent("invalid_argument", f"文件不存在：{path}")
        senders = {
            "image": "send_image",
            "file": "send_file",
            "voice": "send_voice",
            "sticker": "send_emoticon",
        }
        name = senders.get(str(kind or "").lower())
        if name is None:
            return _not_sent("unsupported_media", f"不支持的类型：{kind}")
        try:
            with self._calls:
                ok = bool(getattr(service, name)(target, str(path)))
        except Exception as exc:  # noqa: BLE001
            log.exception("原生媒体发送失败")
            self._mark_broken()
            return {"ok": False, "error_code": "hook_unknown",
                    "detail": f"{type(exc).__name__}: {exc}"}
        if not ok:
            return _not_sent("hook_rejected", "原生桥返回 success=false")
        return {"ok": True, "message_id": _message_id(service)}

    def _mark_broken(self) -> None:
        """DLL 侧异常的下一步：把当前服务判死，交给监督线程重连。"""
        self._ready.clear()
        self._service = None


# --------------------------------------------------------------------------- HTTP


class _Handler(BaseHTTPRequestHandler):
    server_version = "dafeiyu-wechat-hook/1"
    protocol_version = "HTTP/1.1"

    # 由 create_server 注入
    backend: HookBackend
    token: str = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path.rstrip("/") in ("/health", "/probe"):
            self._reply(200, self.backend.health())
            return
        self._reply(404, {"ok": False, "error_code": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/probe":
            self._reply(200, {"ok": True, **self.backend.health()})
            return
        if path not in ("/send_text", "/send_at", "/send_media"):
            self._reply(404, {"ok": False, "sent": False, "error_code": "not_found"})
            return
        if not self._authorized():
            self._reply(401, {"ok": False, "sent": False, "error_code": "unauthorized"})
            return
        body = self._read_json()
        if body is None:
            return
        if not isinstance(body, dict):
            self._reply(400, {"ok": False, "sent": False, "error_code": "invalid_argument"})
            return

        target = str(body.get("chatroom") or "").strip()
        if path == "/send_text":
            result = self.backend.send_text(target, str(body.get("text") or ""))
        elif path == "/send_at":
            raw = body.get("at") or []
            at = [str(item) for item in raw] if isinstance(raw, list) else []
            result = self.backend.send_at(target, str(body.get("text") or ""), at)
        else:
            result = self.backend.send_media(
                target, str(body.get("kind") or ""), str(body.get("path") or "")
            )
        self._reply(200, result)

    # ---------------------------------------------------------------- 工具
    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.token}"

    def _read_json(self) -> Any | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reply(400, {"ok": False, "sent": False, "error_code": "bad_length"})
            return None
        if length <= 0 or length > MAX_BODY:
            self._reply(413 if length > MAX_BODY else 400,
                        {"ok": False, "sent": False, "error_code": "bad_length"})
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._reply(400, {"ok": False, "sent": False, "error_code": "invalid_json"})
            return None

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # 只走 logging，不污染 stderr
        log.debug("%s - %s", self.address_string(), fmt % args)


def create_server(backend: HookBackend, host: str, port: int, *, token: str = "") -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"backend": backend, "token": token})
    return ThreadingHTTPServer((host, port), handler)


# --------------------------------------------------------------------------- 工具函数


def _resolve_mentions(service: Any, room: str, names: list[str]) -> tuple[list[str] | None, str]:
    """显示名 → wxid；同名/查不到时 fail-closed（返回 ``None`` + 原因）。

    dafeiyu 传下来的是「显示名」，而 helper 的 ``send_at_text`` 只收 wxid。
    能唯一匹配「群昵称 / 备注名 / 微信名」才算成功，否则明确失败，让调用方
    退回普通文本——绝不猜一个人来 @。
    """
    rows: Any = ()
    resolved: list[str] = []
    for item in names:
        value = str(item or "").strip()
        if not value:
            continue
        if value.startswith("wxid_"):
            resolved.append(value)
            continue
        if not rows:
            try:
                rows = service.group_members(room) or ()
            except Exception as exc:  # noqa: BLE001
                return None, f"群成员查询失败：{type(exc).__name__}: {exc}"
        hits = sorted(
            {
                str(row.get("wxid") or "")
                for row in rows
                if value
                in {
                    str(row.get("group_nickname") or "").strip(),
                    str(row.get("nickname") or "").strip(),
                    str(row.get("wechat_name") or "").strip(),
                }
            }
        )
        hits = [hit for hit in hits if hit]
        if len(hits) != 1:
            return None, f"群成员「{value}」无法唯一解析（命中 {len(hits)} 个）"
        resolved.append(hits[0])
    if not resolved:
        return None, "at 为空"
    return resolved, ""


def _validate_target(target: str) -> str:
    value = str(target or "").strip()
    if not value:
        return "chatroom 为空"
    if len(value) > 127:
        return "chatroom 过长"
    if not all(ch.isascii() and (ch.isalnum() or ch in "_.@-") for ch in value):
        return "chatroom 含非法字符"
    return ""


def _message_id(service: Any) -> str:
    result = getattr(service, "last_result", None) or {}
    report = result.get("report") if isinstance(result, dict) else None
    if isinstance(report, dict):
        for key in ("local_id", "message_id", "msg_id", "sequence"):
            value = report.get(key)
            if value:
                return str(value)
    return ""


def _not_sent(code: str, detail: str = "") -> dict[str, Any]:
    return {"ok": False, "sent": False, "error_code": code, "detail": detail}


# --------------------------------------------------------------------------- 入口


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="微信非 UIA 出站 hook 服务")
    parser.add_argument("--helper-root", default=os.environ.get("WECHAT_HELPER_ROOT", DEFAULT_HELPER_ROOT))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WECHAT_HOOK_PORT", DEFAULT_PORT)))
    parser.add_argument("--account", default=os.environ.get("WECHAT_ACCOUNT", ""),
                        help="绑定的 bot 账号 wxid；留空 = 用当前登录的账号")
    parser.add_argument("--token", default=os.environ.get("WECHAT_HOOK_TOKEN", ""),
                        help="可选 Bearer token；桥访问非回环地址时建议设置")
    parser.add_argument("--log-file", default="")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        path = Path(args.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=str(args.log_level).upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )

    backend = HookBackend(Path(args.helper_root), account=args.account)
    try:
        backend.start()
    except SystemExit:
        raise
    server = create_server(backend, args.host, args.port, token=args.token)
    log.info("hook 服务监听 http://%s:%s（helper=%s）", args.host, args.port, args.helper_root)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，准备退出")
    finally:
        server.shutdown()
        server.server_close()
        backend.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
