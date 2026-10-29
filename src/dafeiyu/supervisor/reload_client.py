"""bot → supervisor：切档后让 opencode 真的重启。

面板和 bot 是两个进程：配置由 bot 落盘，进程树归 supervisor 管。切档写完配置
却不重启 opencode，运行中的实例仍用旧模型——界面显示「已切换」，实际没切。
这里用共享口令调 supervisor 的 ``/api/opencode/reload``，拿到显式 ack 才算成功。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .control_token import TOKEN_PATH

log = logging.getLogger(__name__)

# supervised 重启要等 opencode 重新就绪（start 内部轮询 ready_url），别用默认 5s。
DEFAULT_TIMEOUT = 120.0


async def reload_opencode(
    port: int,
    *,
    token_path: Path = TOKEN_PATH,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    """请求 supervisor 重启 opencode；失败也返回 ``{"ok": False, ...}``。

    不抛异常：调用方只按 ok 决定回执，抛出去会让「配置已落盘、进程没重载」
    这个半成功状态没人收尾。
    """
    try:
        token = Path(token_path).read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        return {"ok": False, "error": "缺少 supervisor 控制口令"}
    url = f"http://127.0.0.1:{port}/api/opencode/reload"
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(url, headers={"X-Control-Token": token})
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"supervisor 不可达：{exc.__class__.__name__}"}
    try:
        payload = response.json()
    except ValueError:
        return {"ok": False, "error": f"supervisor 返回 HTTP {response.status_code}"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "supervisor 返回了非对象响应"}
    if payload.get("ok") is not True:
        log.warning("opencode 重载未成功：%s", payload.get("error") or payload)
    return payload


__all__ = ["DEFAULT_TIMEOUT", "reload_opencode"]
