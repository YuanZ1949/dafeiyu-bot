from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any, cast

import httpx

from ..models import Attachment
from ..netssl import default_ssl_context
from .tokens import resolve_usage

log = logging.getLogger(__name__)

# UTF-8 解码失败的残渣。丢了原字，但至少别把替换符发到群里。
_REPLACEMENT_RE = re.compile(r"\ufffd+\??")
# 抢救用：贪到最后一个引号，能吞掉字符串里插进来的裸引号。
_REPLY_RE = re.compile(r'"reply"\s*:\s*"(.*)"', re.DOTALL)
_SPEAK_FALSE_RE = re.compile(
    r'"speak"\s*:\s*(?:false|"false"|0|"no"|"off")', re.IGNORECASE
)

OUTPUT_INSTRUCTION = """\
只输出一个 JSON 对象，不要输出解释、前后缀或代码围栏。格式：
{"read": "这轮大家在聊什么、情绪如何（≤40 字）", "think": "你打算怎么接、为什么（≤40 字）", "speak": true 或 false, "reply": "要对群里说的话", "reason": "一句话理由"}
speak=false 时 reply 留空。read/think 写给自己看，不会发到群里；旧格式只有 speak/reply/reason 也认。
"reply" 可以是字符串，也可以是字符串数组：想分几条发就把每条放进数组，数组元素就是发送边界；不想分条就用字符串。单条尽量 ≤500 字，整轮 ≤2000 字，别为凑长度硬撑。
可选字段："to": "me"|"other"|"topic"|"unknown" 表示这句话在跟谁说（第一版只记日志，不强制）；"at": ["昵称"] 表示这条回复要 @ 谁；"attachments": [{"type": "image"|"file"|"voice"|"video", "path": "replies/xxx.png", "name": "可选"}] 只在确实产出文件时填写，path 必须相对 workspace，拿不准就省略；想发表情时用 {"type": "sticker", "name": "表情 id 或表情名"}，name 填纯数字表示 QQ 内置表情。；需要转发聊天记录时用 {"type": "forward", "nodes": [{"name": "谁", "content": "一句原话"}]}（最多 5 条）"""

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
    # 新版输出协议的「读」和「想」：只进日志，不进群，也不落库。
    # 追加在末尾，避免打乱既有按位置构造的位置参数。
    read: str = ""
    think: str = ""
    # 协议 v2 的可选字段：同样追加在末尾，旧的位置参数构造保持可用。
    attachments: tuple[Attachment, ...] = ()
    at: tuple[str, ...] = ()
    # 模型显式分条：每个元素就是一条待发消息；reply 仍是全文（旧字段兼容）。
    parts: tuple[str, ...] = ()
    # 可选意图：这条话在跟谁说（me/other/topic/unknown）。第一版只记日志。
    target: str = ""


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


READ_THINK_LIMIT = 40


def _short_field(value: object, limit: int = READ_THINK_LIMIT) -> str:
    """read/think 只用来记日志：单行、限长，超出直接截断。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


MAX_REPLY_ATTACHMENTS = 2
MAX_REPLY_AT = 3
MAX_ATTACHMENT_PATH = 200
MAX_ATTACHMENT_NAME = 60
REPLY_ATTACHMENT_KINDS = frozenset(
    {"image", "file", "voice", "video", "sticker", "forward"}
)
#: 合并转发的节点上限与单节点正文长度：模型输出不可信，先在这里收口。
MAX_FORWARD_NODES = 5
MAX_FORWARD_TEXT = 200


def _int_field(value: object) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str | float):
        return 0
    try:
        return int(value)
    except (ValueError, OverflowError):
        return 0


def _parse_forward_nodes(raw: object) -> tuple[dict[str, Any], ...]:
    """解析合并转发节点：只保留 name/content 两个字符串字段并限长。

    模型可能给出任意结构；空正文的节点直接丢弃，整体最多 ``MAX_FORWARD_NODES``。
    """
    if not isinstance(raw, list):
        return ()
    nodes: list[dict[str, Any]] = []
    for item in cast(list[object], raw):
        if len(nodes) >= MAX_FORWARD_NODES:
            break
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, object], item)
        name = str(entry.get("name") or "").strip()
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        nodes.append(
            {
                "name": name[:MAX_ATTACHMENT_NAME],
                "content": content[:MAX_FORWARD_TEXT],
            }
        )
    return tuple(nodes)


def parse_reply_attachments(raw: object) -> tuple[Attachment, ...]:
    """解析模型给的 attachments 字段。

    模型输出不可信：只做结构上的过滤与限长，不读文件、不校验路径存在
    （真正的白名单校验在发送前由 reply_media 负责）。非法项静默丢弃。
    """
    if not isinstance(raw, list):
        return ()
    parsed: list[Attachment] = []
    for item in cast(list[object], raw):
        if len(parsed) >= MAX_REPLY_ATTACHMENTS:
            break
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, object], item)
        kind = str(entry.get("type") or "").strip()
        if kind not in REPLY_ATTACHMENT_KINDS:
            continue
        name = str(entry.get("name") or "").strip()
        if kind == "sticker":
            # 表情不读文件：name 是 face id 或 mface 摘要，path 直接忽略。
            if not name:
                continue
            parsed.append(Attachment(kind="sticker", name=name[:MAX_ATTACHMENT_NAME]))
            continue
        if kind == "forward":
            nodes = _parse_forward_nodes(entry.get("nodes"))
            if not nodes:
                continue
            parsed.append(Attachment(kind="forward", nodes=nodes))
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        path = path.strip()
        if not path or len(path) > MAX_ATTACHMENT_PATH:
            continue
        parsed.append(
            Attachment(
                kind=kind,
                path=path,
                name=name[:MAX_ATTACHMENT_NAME],
                size=_int_field(entry.get("size")),
                duration=_int_field(entry.get("duration")),
            )
        )
    return tuple(parsed)


def parse_at(raw: object) -> tuple[str, ...]:
    """解析 @ 昵称列表：只认字符串，去空、去重、限 3 个。"""
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for item in cast(list[object], raw):
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in names:
            continue
        names.append(name)
        if len(names) >= MAX_REPLY_AT:
            break
    return tuple(names)


_TARGETS = frozenset({"me", "other", "topic", "unknown"})
MAX_REPLY_PARTS = 50


def _reply_parts(raw: object) -> tuple[str, tuple[str, ...]]:
    """解析 reply：字符串原样返回；数组返回 (全文, 分条)。

    数组里非字符串/空白的元素直接丢弃；全文用换行拼接，供旧路径与日志使用。
    """
    if isinstance(raw, list):
        parts: list[str] = []
        for item in cast(list[object], raw):
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text:
                parts.append(text)
            if len(parts) >= MAX_REPLY_PARTS:
                break
        return "\n".join(parts), tuple(parts)
    return str(raw or "").strip(), ()


def _target_field(raw: object) -> str:
    text = str(raw or "").strip().casefold()
    return text if text in _TARGETS else ""


def _reply_from_dict(data: dict) -> tuple[bool, str] | None:
    raw_speak = data.get("speak", True)
    if isinstance(raw_speak, str):
        # 模型偶尔把布尔值写成字符串，bool("false") 会是 True，必须显式归一。
        speak = raw_speak.strip().casefold() not in ("false", "no", "0", "off", "")
    else:
        speak = bool(raw_speak)
    reply, _parts = _reply_parts(data.get("reply"))
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
    reply, parts = _reply_parts(data.get("reply"))
    if "\ufffd" in reply:
        # JSON 合法但字符串里带着解码残渣（管道损坏的残留），清掉再发。
        log.warning("回复含 UTF-8 替换符，已清理（片段=%r）", reply[:160])
        reply = _clean_replacements(reply).strip()
        parts = tuple(
            cleaned
            for cleaned in (_clean_replacements(part).strip() for part in parts)
            if cleaned
        )
    if speak and not reply:
        log.warning("模型返回 speak=true 但 reply 为空，降级为兜底话术")
        reply = FALLBACK_REPLY
        parts = ()
    return BrainReply(
        speak=speak,
        reply=reply,
        reason=str(data.get("reason") or ""),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        read=_short_field(data.get("read")),
        think=_short_field(data.get("think")),
        attachments=parse_reply_attachments(data.get("attachments")),
        at=parse_at(data.get("at")),
        parts=parts,
        target=_target_field(data.get("to")),
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
        chat_id: str,
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
