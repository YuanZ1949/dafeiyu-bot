"""chat-driven-behaviour-fixes T6：链接降级链与提示词注入。

覆盖：两级降级（HTTP → 浏览器，浏览器只在 HTTP 失败后尝试）、同一 URL 每批
只解析一次、总预算耗尽后不再发起新请求且不抛异常、机器人自己的消息不解析、
私聊不解析、【链接内容】块位置（【本批】之后、注入块之前）、失败块只出现
一次且不含任何「再抓一次」式指令、failure_layer 取值为 http / browser / none。

全部离线：HTTP/浏览器两层都注入假实现，不碰网络。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from dafeiyu.brain.client import BrainReply
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    InjectionConfig,
    LinkBrowserConfig,
    LinkConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.links import (
    LINK_BLOCK_HEADER,
    LINK_UNREADABLE,
    LinkBlock,
    LinkPromptResolver,
    LinkResolution,
    extract_urls,
    render_link_block,
    resolve_for_prompt,
)
from dafeiyu.links_browser import BrowserResult
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service

SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI)
CHAT = "qq:1001"
URL_OK = "https://example.test/article?id=1"
URL_403 = "https://blocked.test/page"

#: 「再抓一次」类字样在整段提示词里一个都不许出现（IS-1）。
REFETCH_WORDING = ("再抓", "再来一次", "继续抓")


# ---- 假实现（与 T1/T2 的注入点同风格：只实现被调到的方法） -------------------


class FakeHttpResolver:
    """HTTP 层假实现：按 URL 返回预设结果（异常也可预设），并记录调用顺序。"""

    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def resolve(self, url: str) -> LinkResolution:
        self.calls.append(url)
        result = self.results.get(url)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, LinkResolution):
            return result
        return LinkResolution(ok=False, url=url, reason="http_404")


class FakeBrowserRenderer:
    """浏览器层假实现；``explode=True`` 时一被调用就失败（断言不该走这层）。"""

    def __init__(self, results: dict[str, BrowserResult] | None = None, *, explode=False):
        self.results = results or {}
        self.explode = explode
        self.calls: list[str] = []
        self.closed = False

    async def render(self, url: str) -> BrowserResult:
        self.calls.append(url)
        assert not self.explode, f"不该进浏览器层：{url}"
        return self.results.get(url, BrowserResult(ok=False, url=url, reason="browser_error"))

    async def aclose(self) -> None:
        self.closed = True


class HangingResolver:
    """永不返回的 HTTP 层：用来耗尽总预算（被 wait_for 取消）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, url: str) -> LinkResolution:
        self.calls.append(url)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakePromptResolver:
    """service 层的假解析器：直接给出一批 LinkBlock，记录收到的 URL。"""

    def __init__(self, blocks: Sequence[LinkBlock]) -> None:
        self.blocks = list(blocks)
        self.calls: list[list[str]] = []

    async def resolve_for_prompt(self, urls: Sequence[str]) -> list[LinkBlock]:
        self.calls.append(list(urls))
        return list(self.blocks)


def ok_resolution(url: str, title: str = "标题", text: str = "正文片段") -> LinkResolution:
    return LinkResolution(ok=True, url=url, status=200, title=title, text=text, reason="ok")


# ---- links 层：两级降级 -----------------------------------------------------


async def test_http_success_never_touches_browser() -> None:
    http = FakeHttpResolver({URL_OK: ok_resolution(URL_OK)})
    browser = FakeBrowserRenderer(explode=True)

    blocks = await resolve_for_prompt(
        [URL_OK], LinkConfig(), resolver=http, renderer=browser
    )

    assert [b.ok for b in blocks] == [True]
    assert blocks[0].title == "标题"
    assert blocks[0].text == "正文片段"
    assert blocks[0].failure_layer == "none"
    assert blocks[0].reason == "ok"
    assert http.calls == [URL_OK]
    assert browser.calls == []


async def test_403_falls_back_to_browser_and_succeeds() -> None:
    http = FakeHttpResolver(
        {URL_403: LinkResolution(ok=False, url=URL_403, status=403, reason="http_403")}
    )
    browser = FakeBrowserRenderer(
        {URL_403: BrowserResult(ok=True, url=URL_403, title="渲染标题", text="渲染正文")}
    )

    blocks = await resolve_for_prompt(
        [URL_403], LinkConfig(), resolver=http, renderer=browser
    )

    assert blocks[0].ok is True
    assert blocks[0].title == "渲染标题"
    assert blocks[0].failure_layer == "none"
    assert http.calls == [URL_403]
    assert browser.calls == [URL_403]


async def test_403_marks_http_layer_when_browser_disabled() -> None:
    http = FakeHttpResolver(
        {URL_403: LinkResolution(ok=False, url=URL_403, status=403, reason="http_403")}
    )
    browser = FakeBrowserRenderer(explode=True)
    config = LinkConfig(browser=LinkBrowserConfig(enabled=False))

    blocks = await resolve_for_prompt([URL_403], config, resolver=http, renderer=browser)

    assert blocks[0].ok is False
    assert blocks[0].failure_layer == "http"
    assert blocks[0].reason == "http_403"
    assert browser.calls == []


async def test_browser_timeout_marks_browser_layer() -> None:
    http = FakeHttpResolver(
        {URL_403: LinkResolution(ok=False, url=URL_403, status=403, reason="http_403")}
    )
    browser = FakeBrowserRenderer(
        {URL_403: BrowserResult(ok=False, url=URL_403, reason="browser_timeout")}
    )

    blocks = await resolve_for_prompt(
        [URL_403], LinkConfig(), resolver=http, renderer=browser
    )

    assert blocks[0].ok is False
    assert blocks[0].failure_layer == "browser"
    assert blocks[0].reason == "browser_timeout"
    # 浏览器只在 HTTP 层失败后才试。
    assert http.calls == [URL_403]
    assert browser.calls == [URL_403]


async def test_duplicate_urls_resolved_once() -> None:
    http = FakeHttpResolver({URL_OK: ok_resolution(URL_OK)})

    blocks = await resolve_for_prompt(
        [URL_OK, URL_OK], LinkConfig(), resolver=http, renderer=FakeBrowserRenderer()
    )

    assert http.calls == [URL_OK]
    assert len(blocks) == 1


async def test_budget_exhaustion_marks_unreadable_and_stops_fetching() -> None:
    http = HangingResolver()
    config = LinkConfig(total_budget_seconds=0.05)
    engine = LinkPromptResolver(config, resolver=http, renderer=FakeBrowserRenderer())

    started = time.monotonic()
    blocks = await engine.resolve_for_prompt(["https://slow.test/1", "https://slow.test/2"])
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, "总预算必须真的截断，不能拖住主链路"
    assert http.calls == ["https://slow.test/1"], "预算耗尽后不得再发起新请求"
    assert [b.ok for b in blocks] == [False, False]
    assert [b.failure_layer for b in blocks] == ["http", "none"]
    assert {b.reason for b in blocks} == {"budget_exhausted"}


class _BrokenResolver:
    async def resolve(self, url: str) -> LinkResolution:
        raise RuntimeError("boom")


async def test_unexpected_error_degrades_instead_of_raising() -> None:
    blocks = await resolve_for_prompt(
        [URL_OK], LinkConfig(), resolver=_BrokenResolver(), renderer=FakeBrowserRenderer()
    )

    assert blocks[0].ok is False
    assert blocks[0].failure_layer == "http"
    assert blocks[0].reason == "error"


# ---- 提示词块渲染 -----------------------------------------------------------


def test_extract_urls_trims_punctuation_and_dedupes() -> None:
    text = f"看 {URL_OK}，还有 {URL_OK}。以及 https://b.test/y?z=(1)，和 https://c.test/z)"
    assert extract_urls(text) == [URL_OK, "https://b.test/y?z=(1)", "https://c.test/z"]


def test_failed_block_appears_once_and_never_asks_to_refetch() -> None:
    text = render_link_block(
        [
            LinkBlock(url=URL_403, ok=False, failure_layer="http", reason="http_403"),
            LinkBlock(url="https://blocked.test/two", ok=False, failure_layer="browser", reason="browser_timeout"),
        ]
    )

    assert text.count(LINK_BLOCK_HEADER) == 1
    assert text.count(LINK_UNREADABLE) == 2  # 每个失败 URL 一行，块还是同一块
    assert text.count("未能解析的链接") == 1  # 模型侧指令只出现一次
    assert "websearch" in text
    for word in REFETCH_WORDING:
        assert word not in text


def test_mixed_block_keeps_success_text_and_marks_failure() -> None:
    text = render_link_block(
        [
            LinkBlock(url=URL_OK, ok=True, title="标题甲", text="正文甲", reason="ok"),
            LinkBlock(url=URL_403, ok=False, failure_layer="http", reason="http_403"),
        ]
    )

    assert LINK_BLOCK_HEADER in text
    assert "标题甲" in text and "正文甲" in text
    assert f"{URL_403}：{LINK_UNREADABLE}（http_403）" in text
    assert URL_OK in text


# ---- service 层：注入位置与来源过滤 ----------------------------------------


def make_settings() -> Settings:
    return Settings(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        bot_qq=999,
        ws_host="127.0.0.1",
        ws_port=8080,
        ws_token="",
        db_path=":memory:",
        persona=PersonaConfig("溟月", ("大肥鱼",), 120, 300, 0.05),
        triggers=TriggerConfig(
            60, 0.0, ("01:00", "07:00"), ("怎么",), mode="model", batch_seconds=0.0
        ),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []

    def on_message(self, handler) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"


class FakeBrain:
    """只实现 reply 路径：prompt 被原样记录下来供断言。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    @property
    def last_prompt(self) -> str:
        return self.prompts[-1]

    async def reply(self, chat_type, chat_id, text: str, *, on_tool=None) -> BrainReply:
        self.prompts.append(text)
        return BrainReply(True, "在呢", "test", 100, 50)

    async def aclose(self) -> None:
        pass


def make_msg(
    content: str,
    *,
    message_id: str,
    at_bot: bool = False,
    is_bot: bool = False,
    chat_type: ChatType = ChatType.GROUP,
) -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=CHAT,
        user_id="qq:999" if is_bot else "qq:2002",
        nickname="溟月" if is_bot else "阿鱼",
        content=content,
        message_id=message_id,
        segments=[],
        is_bot=is_bot,
        at_bot=at_bot,
        timestamp=0,
    )


def build(tmp_path) -> tuple[Service, Store, FakeBrain]:
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    brain = FakeBrain()
    router = Router(settings, store, clock=lambda: NOON)
    service = Service(
        settings,
        store,
        brain,
        {"qq": FakeAdapter()},
        router,
        "你是溟月",
        injection=InjectionConfig(),
    )
    # 钉一条上线前的旧消息当游标下限，让下面两条一起进批次。
    store.add_message(make_msg("上线前的旧消息", message_id="seed"))
    store.mark_delivered(ChatType.GROUP, CHAT, "seed")
    store.add_message(make_msg(f"@溟月 看这个 {URL_OK}", message_id="m1", at_bot=True))
    store.add_message(make_msg("我也看看", message_id="m2"))
    return service, store, brain


async def test_prompt_has_link_block_after_batch_header(tmp_path) -> None:
    service, _store, brain = build(tmp_path)
    service.link_resolver = FakePromptResolver(
        [LinkBlock(url=URL_OK, ok=True, title="测试标题", text="测试正文", reason="ok")]
    )

    await service._run_batch_locked(ChatType.GROUP, CHAT)

    prompt = brain.last_prompt
    assert LINK_BLOCK_HEADER in prompt
    assert "测试标题" in prompt and "测试正文" in prompt
    # 原始 URL 不能被块吞掉：模型仍要知道链接在哪儿。
    assert URL_OK in prompt
    # 位置：【本批】之后、其它注入块（【此刻】）之前。
    assert prompt.index("【本批】") < prompt.index(LINK_BLOCK_HEADER)
    assert prompt.index(LINK_BLOCK_HEADER) < prompt.index("【此刻】")
    assert service.link_resolver.calls == [[URL_OK]]


async def test_failed_link_prompt_has_single_unreadable_block(tmp_path) -> None:
    service, _store, brain = build(tmp_path)
    service.link_resolver = FakePromptResolver(
        [LinkBlock(url=URL_OK, ok=False, failure_layer="http", reason="http_403")]
    )

    await service._run_batch_locked(ChatType.GROUP, CHAT)

    prompt = brain.last_prompt
    assert prompt.count(LINK_UNREADABLE) == 1
    assert prompt.count(LINK_BLOCK_HEADER) == 1
    assert "http_403" in prompt
    assert "websearch" in prompt
    for word in REFETCH_WORDING:
        assert word not in prompt
    # 链接本体与消息原文都还在。
    assert URL_OK in prompt


async def test_budget_exhaustion_still_produces_a_prompt(tmp_path) -> None:
    service, _store, brain = build(tmp_path)
    hanging = HangingResolver()
    service.link_resolver = LinkPromptResolver(
        LinkConfig(total_budget_seconds=0.05), resolver=hanging
    )

    await service._run_batch_locked(ChatType.GROUP, CHAT)

    assert brain.prompts, "预算耗尽也必须照常生成提示词、照常调模型"
    assert LINK_UNREADABLE in brain.last_prompt
    assert hanging.calls == [URL_OK]


async def test_same_url_in_two_messages_resolved_once(tmp_path) -> None:
    service, store, _brain = build(tmp_path)
    store.add_message(make_msg("还是这个 https://example.test/second", message_id="m3"))
    fake = FakePromptResolver(
        [
            LinkBlock(url=URL_OK, ok=True, title="标题", text="正文", reason="ok"),
            LinkBlock(url="https://example.test/second", ok=True, title="乙", reason="ok"),
        ]
    )
    service.link_resolver = fake

    await service._run_batch_locked(ChatType.GROUP, CHAT)

    assert fake.calls, "批次里有 URL，解析器必须被调用"
    assert len(fake.calls[0]) == len(set(fake.calls[0])), "同一 URL 每批只交出去一次"


async def test_bot_own_links_and_private_chat_are_not_resolved(tmp_path) -> None:
    service, _store, _brain = build(tmp_path)
    fake = FakePromptResolver([])
    service.link_resolver = fake

    bot_message = make_msg(f"我发的链接 {URL_403}", message_id="b1", is_bot=True)
    peer_message = make_msg("群友的链接", message_id="p1")

    group_block = await service._link_block_for_prompt(
        ChatType.GROUP, [bot_message, peer_message]
    )
    private_block = await service._link_block_for_prompt(
        ChatType.PRIVATE, [make_msg(f"私聊链接 {URL_403}", message_id="p2", chat_type=ChatType.PRIVATE)]
    )

    assert group_block is None
    assert private_block is None
    assert fake.calls == []


async def test_resolver_output_is_rendered_into_prompt(tmp_path) -> None:
    service, _store, _brain = build(tmp_path)
    service.link_resolver = FakePromptResolver(
        [
            LinkBlock(url=URL_OK, ok=True, title="甲", text="乙", reason="ok"),
            LinkBlock(url="https://example.test/two", ok=False, failure_layer="browser", reason="browser_timeout"),
        ]
    )

    block = await service._link_block_for_prompt(
        ChatType.GROUP, [make_msg(f"两个 {URL_OK} 和 https://example.test/two", message_id="x1")]
    )

    assert block is not None
    assert block.count(LINK_BLOCK_HEADER) == 1
    assert f"https://example.test/two：{LINK_UNREADABLE}（browser_timeout）" in block
