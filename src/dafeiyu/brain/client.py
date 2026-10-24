from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx

OUTPUT_INSTRUCTION = """\
只输出一个 JSON 对象，不要输出解释、前后缀或代码围栏。格式：
{"speak": true 或 false, "reply": "要对群里说的话", "reason": "一句话理由"}
speak=false 时 reply 留空。"""

FALLBACK_REPLY = "本鱼刚刚游神了…你再说一遍？🐋"


class BrainError(Exception):
    """模型调用失败，或返回内容无法处理。"""


class _Retryable(Exception):
    pass


@dataclass(frozen=True)
class BrainReply:
    speak: bool
    reply: str
    reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _strip_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    lines = lines[1:] if len(lines) > 1 else []
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


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
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.reasoning_effort = (reasoning_effort or "").strip() or None
        self.timeout = timeout
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._client = httpx.AsyncClient(
            timeout=timeout, transport=transport, trust_env=trust_env
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def decide(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 400,
    ) -> BrainReply:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"{system}\n\n{OUTPUT_INSTRUCTION}"},
                *messages,
            ],
            "temperature": 1.0,
            "max_tokens": max_tokens,
        }
        content, prompt_tokens, completion_tokens, finish_reason = await self._call(payload)
        return self._parse_content(
            content, prompt_tokens, completion_tokens, finish_reason
        )

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
                raise BrainError(f"DeepSeek 请求失败：{exc}") from exc

        raise BrainError("DeepSeek 请求失败：未知原因")

    async def _request_raw(self, payload: dict) -> tuple[str, int, int, str]:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = await self._client.post(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise _Retryable(exc.__class__.__name__) from exc

        if resp.status_code == 429:
            raise BrainError("DeepSeek 限流（429），本条放弃")
        if resp.status_code >= 500:
            raise _Retryable(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise BrainError(f"DeepSeek 返回 HTTP {resp.status_code}")

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
        text = _strip_fence(content)
        looks_like_json = text.lstrip()[:1] in ("{", "[")
        try:
            data = json.loads(text)
        except ValueError:
            # 被 max_tokens 截断、或明显是残缺 JSON 时，不要把协议原文丢到群里。
            if finish_reason == "length" or looks_like_json:
                return BrainReply(
                    speak=True,
                    reply=FALLBACK_REPLY,
                    reason="模型输出被截断或 JSON 残缺，已降级",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            reply = content.strip()
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
        if speak and not reply:
            reply = FALLBACK_REPLY
        return BrainReply(
            speak=speak,
            reply=reply,
            reason=str(data.get("reason") or ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
