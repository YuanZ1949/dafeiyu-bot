"""服务端链接 HTTP 抓取层（chat-driven-behaviour-fixes T1）。

职责：给定 URL，用浏览器级请求头抓一次 HTML，抽出标题与正文要点，
并按 URL 的 sha256 落本机 JSON 缓存。失败**不抛异常、不重试**——
403/412 是站点的风控结论（实测知乎整站 403、B 站默认 UA 412），
再撞只会更糟；换路子（T2 浏览器渲染 / 搜索兜底）是后续层的事。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from dafeiyu.config import LinkConfig
from dafeiyu.netssl import default_ssl_context

__all__ = [
    "LINK_BLOCK_HEADER",
    "LINK_UNREADABLE",
    "LinkBlock",
    "LinkPromptResolver",
    "LinkResolution",
    "LinkResolver",
    "extract_title",
    "extract_urls",
    "html_to_text",
    "render_link_block",
    "resolve_for_prompt",
    "resolve_url",
]

#: 为什么必须浏览器级头：本机实测（.omo/evidence/chat-audit-2026-11-14/
#: _probe403.py.txt）B 站对 httpx 默认 UA 回 412，换真实 Edge UA 即 200；
#: 知乎对非浏览器指纹整站 403。默认 UA 是 UA 里出现概率最高的指纹之一。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

#: 缓存条目文件名形态：URL 的 sha256 + .json。淘汰只碰这种文件。
_CACHE_ENTRY_RE = re.compile(r"^[0-9a-f]{64}\.json$")

#: 挑战页特征（Cloudflare / 站点风控）。命中且正文极短时按 challenge 处理。
_CHALLENGE_MARKERS = ("just a moment", "cf-challenge", "人机验证", "安全验证")
#: 「极短」的阈值：真页面的正文几乎不可能少于这么多字。
_CHALLENGE_MAX_CHARS = 300

#: 产生段落级换行的块级元素。
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
        "tr", "table", "section", "article", "blockquote", "header", "footer",
        "figure", "figcaption",
    }
)
#: 连内容一起剥掉的标签：脚本与样式绝不能漏进正文；<title> 是元数据，
#: 标题由 extract_title 单独抽，留在正文里只会和 title 字段重复。
_DROP_TAGS = frozenset({"script", "style", "title"})


@dataclass(frozen=True)
class LinkResolution:
    """一次 URL 解析的结果；失败时 ok=False 且 reason 说明原因。"""

    ok: bool
    url: str
    status: int = 0
    title: str = ""
    text: str = ""
    reason: str = ""
    fetched_at: float = 0.0


#: 【链接内容】块头。模型侧协议（persona「六、什么时候才查」）以它为锚点：
#: 看到这一块就以块内内容为准，不再自己抓同一个链接。
LINK_BLOCK_HEADER = "【链接内容】"
#: 服务端读不到时的统一措辞。整块里只出现一次（每个失败 URL 一行），
#: 后面跟一次给模型的回退指令——绝不再让模型「再抓一次」。
LINK_UNREADABLE = "服务器读不到"

#: URL 提取：到空白/引号/中文标点为止（ASCII 尾部标点另有清理）。
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；：！？、（）【】《》]+", re.IGNORECASE)
#: 句子末尾常粘上这些字符，它们不属于 URL。
_URL_TRAILING = ".,;:!?'\">"


@dataclass(frozen=True)
class LinkBlock:
    """一个 URL 进入提示词时的最终形态（服务端两级降级的结论）。

    ``failure_layer`` 是给 T8「失败知识复用」读的：``http`` / ``browser``
    表示结论来自哪一层，``none`` 表示成功（或根本没来得及发起请求）。
    ``reason`` 复用既有词汇（``http_403`` / ``browser_timeout`` /
    ``budget_exhausted`` …），T8 按 host 沉淀结论时直接照抄。
    """

    url: str
    ok: bool
    title: str = ""
    text: str = ""
    failure_layer: str = "none"
    reason: str = ""


class _TextExtractor(HTMLParser):
    """剥 script/style、收集块级换行的正文抽取器。

    convert_charrefs=True 让 handle_data 拿到的就是反转义后的文本
    （等效 html.unescape）；script/style 的内容由 HTMLParser 以 CDATA
    模式原样送进 handle_data，靠 _skip_depth 丢弃。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> 之类自闭合标签只算一次换行，不能 start+end 双份。
        if tag in _DROP_TAGS:
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    """HTML → 可读正文：剥 script/style、反转义实体、压行内空白、留段落换行。"""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines: list[str] = []
    for line in extractor.text().split("\n"):
        # 行内空白（空格/制表符/回车）压成单个空格，段落边界保留为换行。
        collapsed = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines).strip()


class _TitleExtractor(HTMLParser):
    """og:title 优先、<title> 兜底的标题抽取器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_title = ""
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            values = dict(attrs)
            if values.get("property") == "og:title":
                self.og_title = values.get("content") or ""
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def extract_title(html: str) -> str:
    """取页面标题：og:title content 优先，否则 <title>；去空白、反转义。"""
    extractor = _TitleExtractor()
    extractor.feed(html)
    extractor.close()
    return _squash_whitespace(extractor.og_title or extractor.title)


def _squash_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _browser_headers(url: str, user_agent: str) -> dict[str, str]:
    """浏览器级请求头；Referer 指向同源首页（不少站点对无 Referer 的直链风控）。"""
    parts = urlsplit(url)
    host = parts.hostname or ""
    default_port = (parts.scheme == "https" and parts.port == 443) or (
        parts.scheme == "http" and parts.port == 80
    )
    netloc = host if parts.port is None or default_port else f"{host}:{parts.port}"
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"{parts.scheme}://{netloc}/",
    }


def _decode_body(raw: bytes, charset: str | None) -> str:
    """字节 → 文本；截断边界可能切在多字节字符中间，用 replace 兜住不炸。"""
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


class LinkResolver:
    """可复用的链接解析器：HTTP 抓取 + URL 哈希缓存 + 容量淘汰。

    ``transport`` / ``clock`` 是测试注入点，生产恒传 None
    （分别用真实网络与 time.time()——TTL 要跨进程重启有效，必须用墙钟）。
    """

    def __init__(
        self,
        config: LinkConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._clock: Callable[[], float] = clock or time.time
        # 同一实例会被后续任务复用：写缓存与淘汰必须串行，不让两个协程同时淘汰。
        self._lock = asyncio.Lock()

    async def resolve(self, url: str) -> LinkResolution:
        cached = self._cache_read(url)
        if cached is not None:
            return cached
        resolution = await self._fetch(url)
        # 只缓存成功结果：失败知识（403 等）由 T8 的 host-failures.json 按
        # host 沉淀，落在同一个 cache_dir 里——所以淘汰只能碰 <sha256>.json。
        if resolution.ok:
            await self._cache_write(url, resolution)
        return resolution

    # ---- HTTP --------------------------------------------------------------

    async def _fetch(self, url: str) -> LinkResolution:
        headers = _browser_headers(url, self._config.user_agent or DEFAULT_USER_AGENT)
        try:
            async with httpx.AsyncClient(
                timeout=self._config.timeout_seconds,
                follow_redirects=True,
                verify=default_ssl_context(),
                transport=self._transport,
            ) as client, client.stream("GET", url, headers=headers) as resp:
                status = resp.status_code
                # 非 2xx 不抛异常、不重试：风控结论，硬撞只会升级封锁。
                if not 200 <= status < 300:
                    return self._failure(url, f"http_{status}", status=status)
                chunks: list[bytes] = []
                total = 0
                too_large = False
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > self._config.max_bytes:
                        too_large = True
                        break
                    chunks.append(chunk)
                if too_large:
                    return self._failure(url, "too_large", status=status)
                page = _decode_body(b"".join(chunks), resp.charset_encoding)
        except httpx.TimeoutException:
            return self._failure(url, "timeout")
        except httpx.HTTPError:
            return self._failure(url, "network")

        text = html_to_text(page)
        lowered = text.lower()
        if len(text) <= _CHALLENGE_MAX_CHARS and any(
            marker in lowered for marker in _CHALLENGE_MARKERS
        ):
            return self._failure(url, "challenge", status=status)
        if not text:
            return self._failure(url, "empty", status=status)
        # 先按字符截断再返回：str 切片按码点走，不会切坏 UTF-8（字节层截断
        # 可能切在多字节字符中间，_decode_body 已用 errors=replace 兜住）。
        return LinkResolution(
            ok=True,
            url=url,
            status=status,
            title=_squash_whitespace(extract_title(page))[: self._config.max_chars],
            text=text[: self._config.max_chars],
            reason="ok",
            fetched_at=self._clock(),
        )

    def _failure(self, url: str, reason: str, status: int = 0) -> LinkResolution:
        return LinkResolution(
            ok=False, url=url, status=status, reason=reason, fetched_at=self._clock()
        )

    # ---- 缓存 ---------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return Path(self._config.cache_dir) / f"{digest}.json"

    def _cache_read(self, url: str) -> LinkResolution | None:
        path = self._cache_path(url)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            fetched_at = float(data["fetched_at"])
            if fetched_at + self._config.cache_ttl_seconds <= self._clock():
                return None  # 过期视为未命中；文件留给淘汰逻辑清理
            return LinkResolution(
                ok=True,
                url=url,
                status=int(data["status"]),
                title=str(data["title"]),
                text=str(data["text"]),
                reason="ok",
                fetched_at=fetched_at,
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def _cache_write(self, url: str, resolution: LinkResolution) -> None:
        payload = json.dumps(
            {
                "url": url,
                "status": resolution.status,
                "title": resolution.title,
                "text": resolution.text,
                "fetched_at": resolution.fetched_at,
            },
            ensure_ascii=False,
        )
        path = self._cache_path(url)
        tmp = path.with_name(path.name + ".tmp")
        async with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(payload)
                # 先写 *.tmp 再 replace：进程中途崩溃不会留下半截 JSON。
                os.replace(tmp, path)
            except OSError:
                # 缓存写失败不影响解析结果本身。
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            self._evict()

    def _evict(self) -> None:
        """超过条目数 / 总字节上限时按最旧淘汰。

        只碰 ^[0-9a-f]{64}\\.json$ 形态的缓存条目：同目录的
        host-failures.json 是 T8 要写的「按 host 的失败结论」，那不是
        URL 缓存条目，误删等于把「这个站读不到、下次直接换路子」的
        知识丢掉，机器人会重蹈覆辙。
        """
        directory = Path(self._config.cache_dir)
        if not directory.is_dir():
            return
        entries: list[tuple[float, int, Path]] = []
        for candidate in directory.iterdir():
            if not _CACHE_ENTRY_RE.fullmatch(candidate.name):
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            entries.append((self._entry_time(candidate, stat), stat.st_size, candidate))
        entries.sort()  # fetched_at 最旧在前（mtime 兜底保证确定性排序）
        total = sum(size for _, size, _ in entries)
        while (
            len(entries) > self._config.cache_max_entries
            or total > self._config.cache_max_bytes
        ):
            if not entries:
                break
            _, size, oldest = entries.pop(0)
            try:
                oldest.unlink()
            except OSError:
                break
            total -= size

    def _entry_time(self, path: Path, stat: os.stat_result) -> float:
        """排序键：优先条目自身的 fetched_at，读不到再退回文件 mtime。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return float(data["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return stat.st_mtime


async def resolve_url(
    url: str, config: LinkConfig, **kwargs: Any
) -> LinkResolution:
    """便捷入口：内部建 resolver，供后续任务（T6 注入、T8 失败沉淀）调用。"""
    return await LinkResolver(config, **kwargs).resolve(url)


# ---- URL 提取与提示词块（T6） ---------------------------------------------


def _trim_url(url: str) -> str:
    """剪掉句子尾巴上的 ASCII 标点；配对不上的右括号也不属于 URL。"""
    trimmed = url.strip().rstrip(_URL_TRAILING)
    while trimmed.endswith(")") and trimmed.count("(") < trimmed.count(")"):
        trimmed = trimmed[:-1].rstrip(_URL_TRAILING)
    return trimmed


def extract_urls(text: str) -> list[str]:
    """从一段消息正文里抽 http(s) URL：去重、保序、剪掉尾随标点。"""
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text or ""):
        url = _trim_url(match.group(0))
        if url and url not in seen:
            seen.add(url)
            found.append(url)
    return found


#: 失败块的唯一一条模型侧指令。措辞刻意不含「再抓 / 再来一次 / 继续抓」：
#: 服务端只声明读不到，重试由模型改用 websearch，最后一步是请对方贴内容。
_LINK_FALLBACK_INSTRUCTION = (
    "未能解析的链接：请用 websearch 搜这个链接的主题（站点名 + 标题线索 + 链接里的 id），"
    "不要重新打开同一个链接；如果还是搜不到，就用一句话回退："
    "「这链接本机读不到，麻烦给个标题或关键几句」，不要反复尝试。"
)


def _unreadable(url: str, layer: str, reason: str) -> LinkBlock:
    return LinkBlock(url=url, ok=False, failure_layer=layer, reason=reason)


def render_link_block(blocks: Sequence[LinkBlock]) -> str:
    """把解析结果渲染成【链接内容】块：成功给标题与正文，失败标「服务器读不到」。

    失败块整段只出现一次，且只在有失败 URL 时追加一条模型侧回退指令——
    多个失败 URL 共用同一句，避免提示词里堆叠重复指令。
    """
    blocks = [block for block in blocks if block.url]
    if not blocks:
        return ""
    lines = [
        f"{LINK_BLOCK_HEADER}下面是服务器替你读过的链接内容，以这里的为准"
        "（原文链接仍在本批消息里）："
    ]
    unreadable: list[LinkBlock] = []
    for block in blocks:
        if block.ok:
            title = f"（标题：{block.title}）" if block.title else ""
            lines.append(f"- {block.url}{title}")
            if block.text:
                lines.append(f"  正文：{block.text}")
        else:
            unreadable.append(block)
            suffix = f"（{block.reason}）" if block.reason else ""
            lines.append(f"- {block.url}：{LINK_UNREADABLE}{suffix}")
    if unreadable:
        lines.append(_LINK_FALLBACK_INSTRUCTION)
    return "\n".join(lines)


class _BudgetExhausted(Exception):
    """本批总预算用完：这一层没跑完，后续 URL 也不再发起请求。"""

    def __init__(self, layer: str) -> None:
        super().__init__(layer)
        self.layer = layer


class LinkPromptResolver:
    """一批 URL 的两级降级解析（HTTP → 浏览器），产出提示词用的 LinkBlock。

    服务端只有两层：T1 的 HTTP 抓取，失败（403/超时/挑战页/空白）才进 T2
    的有头最小化浏览器渲染。「仍读不到」是模型侧的行为（websearch / 请对方
    贴一段），服务端的职责到此为止。

    ``resolver`` / ``renderer`` / ``clock`` 是测试注入点：分别需要
    ``resolve(url) -> LinkResolution`` 与 ``render(url) -> BrowserResult``；
    生产恒传 None（懒建真实实现，浏览器进程只在第一次需要时才起）。
    """

    def __init__(
        self,
        config: LinkConfig,
        *,
        resolver: Any = None,
        renderer: Any = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._resolver: Any = resolver if resolver is not None else LinkResolver(config)
        self._renderer: Any = renderer
        self._renderer_owned = False
        self._clock: Callable[[], float] = clock or time.time

    async def aclose(self) -> None:
        """只关自己起的浏览器进程；注入进来的实现由调用方负责。"""
        renderer = self._renderer
        self._renderer = None
        if self._renderer_owned and renderer is not None:
            await renderer.aclose()

    def _browser(self) -> Any:
        if self._renderer is None:
            # 运行时 import：links_browser 反向依赖本模块的抽取函数，写成静态 import
            # 会构成模块环（basedpyright reportImportCycles），这里让依赖只在真要用
            # 浏览器层时才解析。
            module = importlib.import_module("dafeiyu.links_browser")
            self._renderer = module.BrowserRenderer(self._config.browser)
            self._renderer_owned = True
        return self._renderer

    async def resolve_for_prompt(self, urls: Sequence[str]) -> list[LinkBlock]:
        """解析一批 URL：同一 URL 只解析一次，整批共用一个总预算。"""
        if not self._config.enabled:
            return []
        unique = list(dict.fromkeys(_trim_url(url) for url in urls if url))
        unique = [url for url in unique if url]
        if not unique:
            return []
        budget = max(0.0, self._config.total_budget_seconds)
        deadline = self._clock() + budget
        blocks: list[LinkBlock] = []
        exhausted = False
        for url in unique:
            if exhausted:
                # 预算已用完：不发起任何新请求，直接按读不到处理。
                blocks.append(_unreadable(url, "none", "budget_exhausted"))
                continue
            try:
                blocks.append(await self._resolve_one(url, deadline))
            except _BudgetExhausted as exc:
                blocks.append(_unreadable(url, exc.layer, "budget_exhausted"))
                exhausted = True
        return blocks

    async def _resolve_one(self, url: str, deadline: float) -> LinkBlock:
        layer = "http"
        try:
            http = await self._within(self._resolver.resolve(url), deadline, layer)
            if http.ok:
                return LinkBlock(
                    url=url, ok=True, title=http.title, text=http.text, reason="ok"
                )
            reason = http.reason or "http_error"
            if not self._config.browser.enabled:
                return _unreadable(url, "http", reason)
            layer = "browser"
            browser = await self._within(self._browser().render(url), deadline, layer)
            if browser.ok:
                return LinkBlock(
                    url=url,
                    ok=True,
                    title=browser.title,
                    text=browser.text,
                    reason="ok",
                )
            return _unreadable(url, "browser", browser.reason or "browser_error")
        except _BudgetExhausted:
            raise
        except Exception:  # noqa: BLE001 - 解析失败只降级，绝不挡回复主链路
            return _unreadable(url, layer, "error")

    async def _within(
        self, coro: Awaitable[Any], deadline: float, layer: str
    ) -> Any:
        """等一层的结果，最多等到本批 deadline；超时归因为该层预算耗尽。"""
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _BudgetExhausted(layer)
        try:
            return await asyncio.wait_for(coro, timeout=remaining)
        except TimeoutError as exc:
            raise _BudgetExhausted(layer) from exc


async def resolve_for_prompt(
    urls: Sequence[str],
    config: LinkConfig | None = None,
    *,
    resolver: Any = None,
    renderer: Any = None,
    clock: Callable[[], float] | None = None,
) -> list[LinkBlock]:
    """便捷入口：建一个临时 LinkPromptResolver 并在结束时回收它起的浏览器。

    需要跨批复用时（service 就是这样）请自己持有 ``LinkPromptResolver``
    实例，否则每批都会重新起步。
    """
    engine = LinkPromptResolver(
        config or LinkConfig(), resolver=resolver, renderer=renderer, clock=clock
    )
    try:
        return await engine.resolve_for_prompt(urls)
    finally:
        await engine.aclose()
