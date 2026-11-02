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
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

import httpx

from ..memory.store import Store
from ..models import ChatType
from ..netssl import default_ssl_context
from .client import BrainError, BrainReply, parse_brain_reply
from .tokens import resolve_usage

log = logging.getLogger(__name__)

# OpenCode server 的 Basic 认证用户名固定，密码由 OPENCODE_SERVER_PASSWORD 决定。
AUTH_USERNAME = "opencode"
POLL_INTERVAL_SECONDS = 0.5
# /wait 会一直挂到会话空闲才回 204；切成小片才能在中途看到工具 part。
WAIT_SLICE_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 15.0
# 服务端 time.created 是毫秒（spike S2b）；本轮窗口只用它，禁止与 time.time() 混比。
WINDOW_TOLERANCE_MS = 1000.0
# 工具 part 只在活跃状态提示；error 不提示，用户不该等一个已失败的抓取。
TOOL_ACTIVE_STATES = {"pending", "running", "completed"}
# 一轮正常对话只会出现这三类消息（spike S3：每轮都有 idle，漏掉它会每轮误判）。
ALLOWED_MESSAGE_TYPES = {"user", "assistant", "idle"}
# 工作区关闭时的会话级兜底：不改 agent 文件重启 opencode 也能收回全部工具。
DENY_ALL_PERMISSIONS: list[dict[str, Any]] = [
    {"action": "*", "resource": "*", "effect": "deny"}
]
# 旁路（摘要/印象）用的 agent：只有一条指令、全工具禁用，不与群聊 agent 共用。
DEFAULT_MEMORY_AGENT = "dafeiyu-memory"


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


def _error_status(error: dict[str, Any]) -> int | None:
    """provider 错误里的 HTTP 状态码：字段名随 SDK 版本变，能认几个认几个。"""
    status = _status_field(error)
    if status is not None:
        return status
    # 有的 provider 把细节包在 data 里（NamedError 风格）：再看一层。
    nested = error.get("data")
    if isinstance(nested, dict):
        return _status_field(nested)
    return None


def _status_field(error: dict[str, Any]) -> int | None:
    for field in ("statusCode", "status_code", "status", "code"):
        raw = error.get(field)
        if isinstance(raw, bool):
            continue
        value: int | None = None
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            value = int(raw.strip())
        if value is not None and 400 <= value <= 599:
            return value
    return None


@dataclass(frozen=True)
class RoundResult:
    """一轮对话的结构化结果：最终文本、token 与时间窗（服务端毫秒）。"""

    text: str
    prompt_tokens: int
    completion_tokens: int
    since: float
    until: float


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class OpenCodeHttpBrain:
    """把 OpenCode session 当成聊天模型使用（HTTP 版）。

    每个 (chat_type, chat_id) 对应一个 OpenCode session；发消息走
    ``POST /api/session/{id}/prompt``，再轮询 ``GET /api/session/{id}/message``
    直到拿到本轮完成的 assistant 文本。
    """

    # agent 文件里有 read/edit/webfetch；工作区关掉时 factory 会下推全量 deny。
    supports_tools = True

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
        memory_agent: str = DEFAULT_MEMORY_AGENT,
        permissions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url:
            raise BrainError("未配置 opencode.server_url")
        self.store = store
        self.agent = agent.strip() or "deepseek-qq"
        self.memory_agent = memory_agent.strip() or DEFAULT_MEMORY_AGENT
        # None = 用 agent 文件里的权限；非 None 时下推到每个新 session。
        self.permissions = permissions
        self.project_dir = project_dir.strip()
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise BrainError("opencode.timeout_seconds 必须大于 0")

        headers: dict[str, str] = {}
        if password:
            raw = f"{AUTH_USERNAME}:{password}".encode()
            headers["Authorization"] = (
                "Basic " + base64.b64encode(raw).decode("ascii")
            )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
            verify=default_ssl_context(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def forget(self, chat_type: ChatType, chat_id: str) -> None:
        """删掉远端 session 再清映射：/forget 之后不该留下可被翻出来的历史。

        映射一定清：远端删失败（网络、服务端异常）时若留着映射，下一轮会把
        本该被忘掉的 session 接回来，等于 /forget 失效。
        """
        session_id = self.store.get_opencode_session(chat_type, chat_id)
        if session_id:
            try:
                await self._request("DELETE", f"/api/session/{session_id}")
            except _UnknownSession:
                pass  # 远端已经没有了，映射照样清
            except BrainError as exc:
                log.warning("删除 opencode 会话失败，仍清空映射：%s", exc)
        self.store.clear_opencode_session(chat_type, chat_id)

    def reset_session(self, chat_type: ChatType, chat_id: str) -> None:
        """只断开映射；远端 session 由轮换流程决定何时废弃。"""
        self.store.clear_opencode_session(chat_type, chat_id)

    async def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 400,
        temperature: float = 0.2,
    ) -> str:
        text, _, _ = await self.complete_detailed(
            system, messages, max_tokens=max_tokens, temperature=temperature
        )
        return text

    async def complete_detailed(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 400,
        temperature: float = 0.2,
    ) -> tuple[str, int, int]:
        """一次性补全：走临时 session，用完即删，不污染任何聊天。

        ``max_tokens``/``temperature`` 只为兼容旧接口：prompt 只收文本，
        无法在客户端侧生效。
        """
        prompt = f"{system}\n\n" + "\n".join(
            f"{m['role']}: {m['content']}" for m in messages
        )
        return await self._temp_session(prompt, purpose="complete")

    async def _temp_session(
        self, text: str, *, purpose: str = "memory"
    ) -> tuple[str, int, int]:
        session_id = await self._create_session(
            agent=self.memory_agent, title=f"mem:{purpose}"
        )
        try:
            result = await self._ask(session_id, text)
            return (result.text, result.prompt_tokens, result.completion_tokens)
        finally:
            try:
                await self._request("DELETE", f"/api/session/{session_id}")
            except Exception:
                log.debug("临时 session 删除失败：%s", session_id, exc_info=True)

    async def _audit_round(
        self, session_id: str, since: float, until: float
    ) -> list[str]:
        """找出本轮窗口里不该出现的消息（外部注入 / 别的进程写的）。

        正常一轮只有 user/assistant/idle，且本轮只有 bot 发的那一条 user。
        只看窗口内：窗口之前的消息属于之前的轮次，是历史，不是注入——
        把它们当外来消息会让每两轮就重置一次 session，上下文全丢。
        """
        messages = await self._messages(session_id, limit=50)
        # 服务端默认倒序；按时间排好，第一条 user 才是本轮 bot 发的那条。
        messages.sort(key=_created)
        hits: list[str] = []
        users: list[dict[str, Any]] = []
        for message in messages:
            created = _created(message)
            if created < since or created > until:
                continue
            kind = message.get("type")
            if kind not in ALLOWED_MESSAGE_TYPES:
                hits.append(str(message.get("id") or ""))
            elif kind == "user":
                users.append(message)
        # 一轮只应有 bot 自己发的那一条 user；窗口里多出来的才是外部写入。
        if len(users) > 1:
            hits.extend(str(m.get("id") or "") for m in users[1:])
        return hits

    async def reply(
        self,
        chat_type: ChatType,
        chat_id: str,
        text: str,
        *,
        on_tool: Callable[[str], Awaitable[None]] | None = None,
    ) -> BrainReply:
        prompt = text.strip()
        if not prompt:
            raise BrainError("用户消息为空")

        session_id = self.store.get_opencode_session(chat_type, chat_id)
        for attempt in (0, 1):
            try:
                sid = session_id or await self._create_session()
                result = await self._ask(sid, prompt, on_tool=on_tool)
            except _UnknownSession:
                # session 被删或数据库重置：清映射，新开一个再试一次。
                self.store.clear_opencode_session(chat_type, chat_id)
                session_id = None
                if attempt:
                    raise BrainError("OpenCode session 已失效，重建后仍失败") from None
                continue
            self.store.set_opencode_session(chat_type, chat_id, sid)
            if not result.text.strip():
                raise BrainError("OpenCode 没有返回文本")
            foreign = await self._audit_round(sid, result.since, result.until)
            if foreign:
                # 不 raise：这一轮的回复已经拿到了，先把答案给用户，
                # 再把这个被污染的 session 丢掉，下一轮重新开一个。
                log.warning(
                    "OpenCode session %s 出现外来消息 %s，已重置该会话",
                    sid,
                    foreign,
                )
                self.store.clear_opencode_session(chat_type, chat_id)
            return self._reply_from(prompt, result)

        raise BrainError("OpenCode 调用失败：session 重建失败")

    async def reply_fresh(
        self,
        chat_type: ChatType,
        chat_id: str,
        text: str,
        *,
        system: str = "",
        history: list[dict[str, str]] | None = None,
        on_tool: Callable[[str], Awaitable[None]] | None = None,
    ) -> BrainReply:
        """丢掉旧会话，用「系统提示 + 本地历史 + 本条」开新会话再试一次。

        额度/限流类失败之后，旧 session 里往往已经留下半截上下文；服务端配额
        不会因为重试同一会话就好转。这里按无状态大脑的拼法把本地窗口重放一遍，
        等价于「换会话继续聊」，下一轮起又回到正常的会话型流程。
        """
        prompt = text.strip()
        if not prompt:
            raise BrainError("用户消息为空")
        blocks: list[str] = []
        if system.strip():
            blocks.append(system.strip())
        for item in history or []:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            role = str(item.get("role") or "user")
            blocks.append(f"{role}: {content}")
        blocks.append(prompt)
        body = "\n\n".join(blocks)

        self.store.clear_opencode_session(chat_type, chat_id)
        sid = await self._create_session()
        result = await self._ask(sid, body, on_tool=on_tool)
        self.store.set_opencode_session(chat_type, chat_id, sid)
        if not result.text.strip():
            raise BrainError("OpenCode 没有返回文本")
        return self._reply_from(prompt, result)

    @staticmethod
    def _reply_from(prompt: str, result: RoundResult) -> BrainReply:
        prompt_tokens, completion_tokens, estimated = resolve_usage(
            prompt,
            result.text,
            result.prompt_tokens,
            result.completion_tokens,
        )
        reply = parse_brain_reply(
            result.text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return replace(reply, estimated=True) if estimated else reply

    async def _create_session(
        self,
        *,
        agent: str | None = None,
        title: str = "dafeiyu",
        permissions: list[dict[str, Any]] | None = None,
    ) -> str:
        body: dict[str, Any] = {"title": title}
        agent_name = self.agent if agent is None else agent
        if agent_name:
            body["agent"] = agent_name
        effective = permissions if permissions is not None else self.permissions
        if effective is not None:
            body["permissions"] = effective
        if self.project_dir:
            body["location"] = {"directory": self.project_dir}
        payload = await self._request("POST", "/api/session", body=body)
        data = payload.get("data") or {}
        sid = data.get("id") if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid:
            raise BrainError("OpenCode 没有返回 session id")
        return sid

    async def _ask(
        self,
        session_id: str,
        prompt: str,
        on_tool: Callable[[str], Awaitable[None]] | None = None,
    ) -> RoundResult:
        baseline = await self._latest_created(session_id)
        payload = await self._request(
            "POST",
            f"/api/session/{session_id}/prompt",
            body={"text": prompt},
        )
        data = payload.get("data") or {}
        prompt_time = _created(data) if isinstance(data, dict) else 0.0
        since = max(baseline, prompt_time - WINDOW_TOLERANCE_MS)
        deadline = time.monotonic() + self.timeout
        tool_fired = False

        while True:
            messages = await self._round_messages(session_id, since)
            if on_tool is not None and not tool_fired:
                tool = self._first_tool(messages)
                if tool is not None:
                    tool_fired = True
                    await on_tool(tool)
            idle = False
            sliced = False
            try:
                idle = await asyncio.wait_for(
                    self._wait(session_id), timeout=WAIT_SLICE_SECONDS
                )
            except TimeoutError:
                # 服务端挂到会话空闲才回 204：切片超时说明本轮还在跑，
                # 直接回到循环顶部重新读消息，工具 part 才能被看到。
                sliced = True
            if idle:
                messages = await self._round_messages(session_id, since)
                failure = self._round_error(messages)
                if failure is not None:
                    raise failure
                if self._round_finished(messages):
                    prompt_tokens = 0
                    completion_tokens = 0
                    for message in messages:
                        if message.get("type") != "assistant":
                            continue
                        used_in, used_out = self._usage(message)
                        prompt_tokens += used_in
                        completion_tokens += used_out
                    return RoundResult(
                        text=self._answer(messages),
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        since=since,
                        until=max((_created(m) for m in messages), default=since),
                    )
            if time.monotonic() >= deadline:
                raise BrainError(
                    f"OpenCode 超时（{self.timeout:.0f} 秒），本条放弃"
                )
            if not sliced:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _round_messages(
        self, session_id: str, since: float
    ) -> list[dict[str, Any]]:
        """本轮消息，按 created 升序——服务端默认倒序，顺序不可信。"""
        messages = await self._messages(session_id)
        current = [m for m in messages if _created(m) > since]
        current.sort(key=_created)
        return current

    def _round_finished(self, messages: list[dict[str, Any]]) -> bool:
        return any(
            m.get("type") == "assistant" and _join_text(m).strip()
            for m in messages
        )

    @staticmethod
    def _round_error(messages: list[dict[str, Any]]) -> BrainError | None:
        """本轮最后一条 assistant 以 error 收尾时，返回可直接抛出的错误。

        模型侧失败（配额、限流、鉴权）在 opencode 里表现为 ``finish: "error"``
        且 content 为空；不认这个标记的话只能等到超时，真实原因会被吞掉。
        provider 若在 error 里给了状态码就一并带出去：限流判定不该只靠文案。
        """
        for message in reversed(messages):
            if message.get("type") != "assistant":
                continue
            if message.get("finish") != "error":
                return None
            error = message.get("error") or {}
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            kind = str(error.get("type") or error.get("name") or "unknown")
            detail = str(error.get("message") or data.get("message") or "").strip()
            text = (
                f"OpenCode 调用失败（{kind}）：{detail}"
                if detail
                else f"OpenCode 调用失败（{kind}）"
            )
            return BrainError(text, status=_error_status(error))
        return None

    @staticmethod
    def _answer(messages: list[dict[str, Any]]) -> str:
        answer = ""
        for message in messages:
            if message.get("type") != "assistant":
                continue
            text = _join_text(message)
            if text.strip():
                answer = text
        return answer

    @staticmethod
    def _first_tool(messages: list[dict[str, Any]]) -> str | None:
        for message in messages:
            if message.get("type") != "assistant":
                continue
            for part in message.get("content") or []:
                if not isinstance(part, dict) or part.get("type") != "tool":
                    continue
                if part.get("name") != "webfetch":
                    continue
                state = part.get("state") or {}
                if state.get("status") in TOOL_ACTIVE_STATES:
                    return "webfetch"
        return None

    @staticmethod
    def _usage(message: dict[str, Any]) -> tuple[int, int]:
        tokens = message.get("tokens")
        if not isinstance(tokens, dict):
            return (0, 0)
        return (_as_int(tokens.get("input")), _as_int(tokens.get("output")))

    async def _wait(self, session_id: str) -> bool:
        """等本轮结束：204 表示会话空闲。等待自身超时按「未空闲」继续轮询。"""
        try:
            response = await self._client.post(
                f"/api/experimental/session/{session_id}/wait"
            )
        except httpx.TimeoutException:
            return False
        except httpx.HTTPError as exc:
            raise BrainError(
                f"OpenCode 服务不可达：{exc.__class__.__name__}"
            ) from exc
        self._check_response(response)
        return response.status_code == 204

    async def _latest_created(self, session_id: str) -> float:
        messages = await self._messages(session_id)
        return max((_created(m) for m in messages), default=0.0)

    async def _messages(
        self, session_id: str, *, limit: int = 30
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/api/session/{session_id}/message",
            params={"limit": limit},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise BrainError(
                "OpenCode 服务拒绝认证：检查 opencode.server_password"
                " 或环境变量 OPENCODE_SERVER_PASSWORD",
                status=response.status_code,
            )
        if response.status_code == 404:
            raise _UnknownSession()
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:200]
            raise BrainError(
                f"OpenCode 返回 {response.status_code}：{detail}",
                status=response.status_code,
            )

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
        self._check_response(response)
        # 204 / 空 body 是正常响应（DELETE /api/session 就是 204）：没有正文
        # 不代表出错，按空对象处理，否则「彻底删除会话」永远失败。
        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrainError("OpenCode 返回的不是 JSON") from exc
        return payload if isinstance(payload, dict) else {}
