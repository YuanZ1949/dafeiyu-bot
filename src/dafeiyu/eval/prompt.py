"""从 case 构造 prompt：复用真实批次构建器，注入段由 context 确定性拼出。

链接解析（chat-driven-behaviour-fixes T6）复用真实的两级链
``LinkPromptResolver`` + ``render_link_block``，但 HTTP / 浏览器两层都由
case 里 ``links:`` 声明的**离线桩**顶上：评测不联网，403 也不会去撞真实站点。
未在 ``links:`` 里声明的 URL 一律按读不到处理，同样不发起网络请求。
"""

from __future__ import annotations

from collections.abc import Mapping

from dafeiyu.config import LinkBrowserConfig, LinkConfig
from dafeiyu.links import (
    LinkResolution,
    extract_urls,
    render_link_block,
    resolve_for_prompt,
)
from dafeiyu.links_browser import BrowserResult
from dafeiyu.memory.context import (
    INJECTION_IMPRESSION_HEADER,
    INJECTION_SUMMARY_HEADER,
)
from dafeiyu.memory.timeline import build_batch_prompt
from dafeiyu.models import ChatType, Message

from .models import EvalCase, EvalContext, EvalLink

#: 未声明 URL 的离线失败原因：名字带 offline，一眼能看出没联网。
OFFLINE_HTTP_REASON = "http_offline_stub"
OFFLINE_BROWSER_REASON = "browser_offline_stub"


def build_injection(context: EvalContext) -> str:
    """把 case 的摘要/印象按固定顺序拼成注入段。

    顺序与真实注入一致：群友印象在前、昨日摘要在后；空段整段省略。
    """
    parts: list[str] = []
    if context.impressions:
        parts.append(
            f"{INJECTION_IMPRESSION_HEADER}\n" + "\n".join(context.impressions)
        )
    if context.summary:
        parts.append(f"{INJECTION_SUMMARY_HEADER}\n{context.summary}")
    return "\n\n".join(parts)


def to_messages(case: EvalCase) -> list[Message]:
    chat_type = ChatType(case.chat_type)
    return [
        Message(
            chat_type=chat_type,
            chat_id=case.chat_id,
            user_id=item.user_id,
            nickname=item.nickname,
            content=item.content,
            message_id=f"{case.id}:{index}",
            at_bot=item.at_bot,
            reply_to_bot=item.reply_to_bot,
            timestamp=item.timestamp,
        )
        for index, item in enumerate(case.messages)
    ]


class _StubResolver:
    """HTTP 层离线桩：按 case 的 links 声明返回成功或失败。"""

    def __init__(self, links: Mapping[str, EvalLink]) -> None:
        self._links = links

    async def resolve(self, url: str) -> LinkResolution:
        link = self._links.get(url)
        if link is None:
            return LinkResolution(ok=False, url=url, reason=OFFLINE_HTTP_REASON)
        if link.outcome == "http_ok":
            return LinkResolution(
                ok=True,
                url=url,
                status=link.status,
                title=link.title,
                text=link.text,
                reason="ok",
            )
        # browser_ok / unreadable 都先让 HTTP 层失败，才轮得到浏览器层。
        return LinkResolution(
            ok=False, url=url, status=link.status, reason=f"http_{link.status}"
        )


class _StubRenderer:
    """浏览器层离线桩：只有 browser_ok 成功，其余失败。"""

    def __init__(self, links: Mapping[str, EvalLink]) -> None:
        self._links = links

    async def render(self, url: str) -> BrowserResult:
        link = self._links.get(url)
        if link is None:
            return BrowserResult(ok=False, url=url, reason=OFFLINE_BROWSER_REASON)
        if link.outcome == "browser_ok":
            return BrowserResult(
                ok=True, url=url, title=link.title, text=link.text, reason="ok"
            )
        return BrowserResult(ok=False, url=url, reason="browser_timeout")


def case_urls(case: EvalCase) -> list[str]:
    """本 case 消息里出现的 URL（去重、保序）；与 service 一样只认 http(s)。"""
    urls: list[str] = []
    for message in case.messages:
        for url in extract_urls(message.content):
            if url not in urls:
                urls.append(url)
    return urls


async def build_link_block(case: EvalCase) -> str:
    """把本 case 的链接解析结果渲染成【链接内容】块；没有链接时返回空串。"""
    if not case.links:
        return ""
    declared = {link.url: link for link in case.links}
    config = LinkConfig(enabled=True, browser=LinkBrowserConfig(enabled=True))
    blocks = await resolve_for_prompt(
        case_urls(case),
        config,
        resolver=_StubResolver(declared),
        renderer=_StubRenderer(declared),
    )
    return render_link_block(blocks)


def compose_prompt(case: EvalCase, link_block: str) -> str:
    """注入段 = 【链接内容】 → 群友印象 → 近期摘要，再交给批次构建器。"""
    injection = build_injection(case.context)
    if link_block:
        injection = f"{link_block}\n\n{injection}" if injection else link_block
    return build_batch_prompt(
        to_messages(case),
        injection,
        continuation=case.context.continuation,
    )


def build_prompt(case: EvalCase) -> str:
    """同步入口：**不解析链接**；runner 用 :func:`build_prompt_async`。"""
    return compose_prompt(case, "")


async def build_prompt_async(case: EvalCase) -> str:
    """runner 的入口：链接用离线桩解析后注入，全程不联网。"""
    return compose_prompt(case, await build_link_block(case))
