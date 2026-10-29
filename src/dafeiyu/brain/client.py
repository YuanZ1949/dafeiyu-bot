from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace

import httpx

from ..netssl import default_ssl_context

from .tokens import resolve_usage

log = logging.getLogger(__name__)

# UTF-8 解码失败的残渣。丢了原字，但至少别把替换符发到群里。
_REPLACEMENT_RE = re.compile(r"\ufffd+\??")
# 抢救用：贪到最后一个引号，能吞掉字符串里插进来的裸引号。
_REPLY_RE = re.compile(r'"reply"\s*:\s*"(.*)"', re.S)
_SPEAK_FALSE_RE = re.compile(
    r'"speak"\s*:\s*(?:false|"false"|0|"no"|"off")', re.IGNORECASE
)

OUTPUT_INSTRUCTION = """\
只输出一个 JSON 对象，不要输出解释、前后缀或代码围栏。格式：
{"speak": true 或 false, "reply": "要对群里说的话", "reason": "一句话理由"}
speak=false 时 reply 留空。"""

FALLBACK_REPLY = "本鱼刚刚游神了…你再说一遍？🐋"


class BrainError(Exception):
    """模型调用失败，或返回内容无法处理。

    ``status`` 是 HTTP 状态码（拿不到时 None）：配额/限流判定不该只靠文案，
    provider 换个措辞就会让熔断失灵。
    """

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _Retryable(Exception):
    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BrainReply:
    speak: bool
    reply: str
    reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False


def _strip_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    lines = lines[1:] if len(lines) > 1 else []
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _clean_replacements(text: str) -> str:
    return _REPLACEMENT_RE.sub("", text)


def _reply_from_dict(data: dict) -> tuple[bool, str] | None:
    raw_speak = data.get("speak", True)
    if isinstance(raw_speak, str):
        # 模型偶尔把布尔值写成字符串，bool("false") 会是 True，必须显式归一。
        speak = raw_speak.strip().casefold() not in ("false", "no", "0", "off", "")
    else:
        speak = bool(raw_speak)
    reply = str(data.get("reply") or "").strip()
    return (speak, reply) if reply else None


def salvage_reply(text: str) -> tuple[bool, str] | None:
    """从损坏的模型输出里抢救出 (speak, reply)。

    真实见过的三种损坏：

    * 字符串里插进一个裸引号，让 reply 提前闭合 —— json 报 Extra data；
    * 多字节字符被拆坏，出现 U+FFFD；
    * 两段 JSON 首尾相接 —— json 报 Extra data，取第一个对象即可。
    """
    cleaned = _clean_replacements(text).strip()
    if not cleaned:
        return None

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(cleaned[index:])
        except ValueError:
            # 第一个 { 就解析不出，后面的 { 只可能是嵌套，不必再试。
            break
        if isinstance(data, dict):
            found = _reply_from_dict(data)
            if found is not None:
                return found
        break

    match = _REPLY_RE.search(cleaned)
    if not match:
        return None
    reply = match.group(1).replace('\\"', '"').replace("\\n", "\n").strip()
    if not reply:
        return None
    return (not _SPEAK_FALSE_RE.search(cleaned)), reply


def parse_brain_reply(
    content: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    finish_reason: str = "",
) -> BrainReply:
    """把模型正文解析成 BrainReply。

    OpenCode 和 DeepSeek 共用同一套输出协议，避免两条链路对 JSON 残缺、
    speak 字符串等边界行为不一致。
    """
    text = _strip_fence(content)
    looks_like_json = text.lstrip()[:1] in ("{", "[")
    try:
        data = json.loads(text)
    except ValueError:
        salvaged = salvage_reply(text)
        if salvaged is not None:
            speak, reply = salvaged
            log.warning(
                "模型输出 JSON 损坏，已抢救出回复（finish_reason=%s，片段=%r）",
                finish_reason,
                text[:160],
            )
            return BrainReply(
                speak=speak,
                reply=reply,
                reason="模型输出 JSON 损坏，已抢救",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        # 被 max_tokens 截断、或明显是残缺 JSON 时，不要把协议原文丢到群里。
        if finish_reason == "length" or looks_like_json:
            log.warning(
                "模型输出无法修复，降级为兜底话术（finish_reason=%s，片段=%r）",
                finish_reason,
                text[:160],
            )
            return BrainReply(
                speak=True,
                reply=FALLBACK_REPLY,
                reason="模型输出被截断或 JSON 残缺，已降级",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        reply = content.strip()
        if not reply:
            log.warning(
                "模型返回空内容，降级为兜底话术（finish_reason=%s）", finish_reason
            )
        return BrainReply(
            speak=True,
            reply=reply or FALLBACK_REPLY,
            reason="非 JSON 返回，按原文处理",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    if not isinstance(data, dict):
        return BrainReply(
            speak=True,
            reply=content.strip() or FALLBACK_REPLY,
            reason="返回不是对象，按原文处理",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    raw_speak = data.get("speak", True)
    if isinstance(raw_speak, str):
        # 模型偶尔把布尔值写成字符串，bool("false") 会是 True，必须显式归一。
        speak = raw_speak.strip().casefold() not in ("false", "no", "0", "off", "")
    else:
        speak = bool(raw_speak)
    reply = str(data.get("reply") or "").strip()
    if "\ufffd" in reply:
        # JSON 合法但字符串里带着解码残渣（管道损坏的残留），清掉再发。
        log.warning("回复含 UTF-8 替换符，已清理（片段=%r）", reply[:160])
        reply = _clean_replacements(reply).strip()
    if speak and not reply:
        log.warning("模型返回 speak=true 但 reply 为空，降级为兜底话术")
        reply = FALLBACK_REPLY
    return BrainReply(
        speak=speak,
        reply=reply,
        reason=str(data.get("reason") or ""),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


class BrainClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 15.0,
        sleep=None,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = True,
        reasoning_effort: str | None = None,
        max_tokens: int = 400,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.reasoning_effort = (reasoning_effort or "").strip() or None
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            trust_env=trust_env,
            verify=default_ssl_context(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def decide(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int | None = None,
    ) -> BrainReply:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"{system}\n\n{OUTPUT_INSTRUCTION}"},
                *messages,
            ],
            "temperature": 1.0,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        content, prompt_tokens, completion_tokens, finish_reason = await self._call(payload)
        prompt_text = system + "\n" + "\n".join(
            str(message.get("content") or "") for message in messages
        )
        prompt_tokens, completion_tokens, estimated = resolve_usage(
            prompt_text, content, prompt_tokens, completion_tokens
        )
        reply = self._parse_content(
            content, prompt_tokens, completion_tokens, finish_reason
        )
        return replace(reply, estimated=True) if estimated else reply

    async def reply(
        self,
        chat_type: object,
        chat_id: int,
        prompt: str,
        *,
        system: str = "",
        history: list[dict] | None = None,
        on_tool: object = None,
    ) -> BrainReply:
        """与 OpenCode 对齐的入口：无状态端点靠显式历史获得连续性。

        chat_type / chat_id / on_tool 是为接口一致性保留的；本地端点没有
        session，也接不了工具回调，历史由 service 按 window_size 拼好传进来。
        """
        messages = [*(history or []), {"role": "user", "content": prompt}]
        return await self.decide(system, messages)

    # service 只在为 True 时才传 system/history，避免改坏只认旧签名的 brain。
    accepts_history = True
    # 纯聊天端点没有任何工具（read/edit/webfetch 都没有）。
    supports_tools = False

    async def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 400,
        temperature: float = 0.2,
    ) -> str:
        """原始补全：不注入 OUTPUT_INSTRUCTION，返回去掉围栏的正文。"""
        content, _, _ = await self.complete_detailed(
            system, messages, max_tokens=max_tokens, temperature=temperature
        )
        return content

    async def complete_detailed(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 400,
        temperature: float = 0.2,
    ) -> tuple[str, int, int]:
        """原始补全，同时返回 token 用量，供旁路任务记账。"""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        content, prompt_tokens, completion_tokens, _ = await self._call(payload)
        return _strip_fence(content), prompt_tokens, completion_tokens

    async def _call(self, payload: dict) -> tuple[str, int, int, str]:
        if self.reasoning_effort:
            payload = {**payload, "reasoning_effort": self.reasoning_effort}
        for attempt in range(2):
            try:
                return await self._request_raw(payload)
            except _Retryable as exc:
                if attempt == 0:
                    await self._sleep(2)
                    continue
                raise BrainError(
                    f"DeepSeek 请求失败：{exc}", status=getattr(exc, "status", None)
                ) from exc

        raise BrainError("DeepSeek 请求失败：未知原因")

    async def _request_raw(self, payload: dict) -> tuple[str, int, int, str]:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = await self._client.post(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise _Retryable(exc.__class__.__name__) from exc

        if resp.status_code == 429:
            raise BrainError("DeepSeek 限流（429），本条放弃", status=429)
        if resp.status_code >= 500:
            raise _Retryable(f"HTTP {resp.status_code}", status=resp.status_code)
        if resp.status_code >= 400:
            raise BrainError(
                f"DeepSeek 返回 HTTP {resp.status_code}", status=resp.status_code
            )

        try:
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
            finish_reason = str(choice.get("finish_reason") or "")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise BrainError("DeepSeek 返回格式异常") from exc

        usage = data.get("usage") or {}
        return (
            str(content),
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            finish_reason,
        )

    @staticmethod
    def _parse_content(
        content: str,
        prompt_tokens: int,
        completion_tokens: int,
        finish_reason: str = "",
    ) -> BrainReply:
        return parse_brain_reply(
            content, prompt_tokens, completion_tokens, finish_reason
        )
