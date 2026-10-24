"""OpenCode 的 HTTP 传输：直接读结构化消息，不解析 CLI stdout。

CLI 的 ``--format json`` 事件流走 Windows 管道时会偶发多字节字符损坏
（U+FFFD，或一个裸引号让 reply 字符串提前闭合），解析层只能降级成兜底
话术，且日志里毫无痕迹。HTTP 接口返回的是服务端序列化好的 JSON，
``content[].text`` 天然完整，还能直接拿到 session id 与完成状态，不必
猜事件流的语义。
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx

from ..memory.store import Store
from ..models import ChatType
from .client import BrainError, BrainReply, parse_brain_reply

# OpenCode server 的 Basic 认证用户名固定，密码由 OPENCODE_SERVER_PASSWORD 决定。
AUTH_USERNAME = "opencode"
POLL_INTERVAL_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 15.0


class _UnknownSession(Exception):
    """服务端说这个 session 不存在（被删或换了库）。"""


def _join_text(message: dict[str, Any]) -> str:
    """拼出 assistant 消息里所有 text part；tool/reasoning 不算回复。"""
    parts = message.get("content") or []
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        value = part.get("text")
        if isinstance(value, str) and value.strip():
            texts.append(value)
    return "\n".join(texts)


def _created(message: dict[str, Any]) -> float:
    value = (message.get("time") or {}).get("created")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _completed(message: dict[str, Any]) -> bool:
    return "completed" in (message.get("time") or {})


class OpenCodeHttpBrain:
    """把 OpenCode session 当成聊天模型使用（HTTP 版）。

    每个 (chat_type, chat_id) 对应一个 OpenCode session；发消息走
    ``POST /api/session/{id}/prompt``，再轮询 ``GET /api/session/{id}/message``
    直到拿到本轮完成的 assistant 文本。
    """

    def __init__(
        self,
        *,
        base_url: str,
        store: Store,
        password: str = "",
        agent: str = "deepseek-qq",
        project_dir: str = "",
        timeout: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url:
            raise BrainError("未配置 opencode.server_url")
        self.store = store
        self.agent = agent.strip() or "deepseek-qq"
        self.project_dir = project_dir.strip()
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise BrainError("opencode.timeout_seconds 必须大于 0")

        headers: dict[str, str] = {}
        if password:
            raw = f"{AUTH_USERNAME}:{password}".encode("utf-8")
            headers["Authorization"] = (
                "Basic " + base64.b64encode(raw).decode("ascii")
            )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def forget(self, chat_type: ChatType, chat_id: int) -> None:
        self.store.clear_opencode_session(chat_type, chat_id)

    async def reply(
        self,
        chat_type: ChatType,
        chat_id: int,
        text: str,
    ) -> BrainReply:
        prompt = text.strip()
        if not prompt:
            raise BrainError("用户消息为空")

        session_id = self.store.get_opencode_session(chat_type, chat_id)
        for attempt in (0, 1):
            try:
                sid = session_id or await self._create_session()
                answer = await self._ask(sid, prompt)
            except _UnknownSession:
                # session 被删或数据库重置：清映射，新开一个再试一次。
                self.store.clear_opencode_session(chat_type, chat_id)
                session_id = None
                if attempt:
                    raise BrainError("OpenCode session 已失效，重建后仍失败") from None
                continue
            self.store.set_opencode_session(chat_type, chat_id, sid)
            if not answer.strip():
                raise BrainError("OpenCode 没有返回文本")
            return parse_brain_reply(answer, prompt_tokens=0, completion_tokens=0)

        raise BrainError("OpenCode 调用失败：session 重建失败")

    async def _create_session(self) -> str:
        body: dict[str, Any] = {"title": "dafeiyu"}
        if self.agent:
            body["agent"] = self.agent
        if self.project_dir:
            body["location"] = {"directory": self.project_dir}
        payload = await self._request("POST", "/api/session", body=body)
        data = payload.get("data") or {}
        sid = data.get("id") if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid:
            raise BrainError("OpenCode 没有返回 session id")
        return sid

    async def _ask(self, session_id: str, prompt: str) -> str:
        baseline = await self._latest_created(session_id)
        payload = await self._request(
            "POST",
            f"/api/session/{session_id}/prompt",
            body={"text": prompt},
        )
        data = payload.get("data") or {}
        prompt_time = _created(data) if isinstance(data, dict) else 0.0
        since = max(baseline, prompt_time - 1.0)
        deadline = time.monotonic() + self.timeout

        while True:
            for message in await self._messages(session_id):
                if message.get("type") != "assistant":
                    continue
                if not _completed(message) or _created(message) < since:
                    continue
                answer = _join_text(message)
                if answer.strip():
                    return answer
            if time.monotonic() >= deadline:
                raise BrainError(
                    f"OpenCode 超时（{self.timeout:.0f} 秒），本条放弃"
                )
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _latest_created(self, session_id: str) -> float:
        messages = await self._messages(session_id)
        return max((_created(m) for m in messages), default=0.0)

    async def _messages(self, session_id: str) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/api/session/{session_id}/message",
            params={"limit": 30},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, path, json=body, params=params
            )
        except httpx.HTTPError as exc:
            raise BrainError(
                f"OpenCode 服务不可达：{exc.__class__.__name__}"
            ) from exc
        if response.status_code in (401, 403):
            raise BrainError(
                "OpenCode 服务拒绝认证：检查 opencode.server_password"
                " 或环境变量 OPENCODE_SERVER_PASSWORD"
            )
        if response.status_code == 404:
            raise _UnknownSession()
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:200]
            raise BrainError(f"OpenCode 返回 {response.status_code}：{detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrainError("OpenCode 返回的不是 JSON") from exc
        return payload if isinstance(payload, dict) else {}
