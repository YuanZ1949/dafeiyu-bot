"""chat-driven-behaviour-fixes T8：失败知识复用（按 host 的失败账本）。

覆盖计划验收：HTTP 层失败 1 次即记 ``needs_browser``；第 2 次同 host **不发
HTTP 请求**、直接进浏览器层；浏览器层也失败则**同一条**记录升级为
``search_only``；第 3 次**不发任何请求**、直接产出降级指令块；TTL 过期后
重新尝试一次并刷新结论；重复写同一 host+形态不产生第二条记录；坏文件
不让解析链路抛异常；账本落在 ``<cache_dir>/host-failures.json``。

全部离线：HTTP 层、浏览器层、时钟、文件路径都是注入的假实现。
"""

from __future__ import annotations

import json
from pathlib import Path

from dafeiyu.config import LinkBrowserConfig, LinkConfig
from dafeiyu.links import (
    LINK_UNREADABLE,
    LinkPromptResolver,
    LinkResolution,
    render_link_block,
)
from dafeiyu.links_browser import BrowserResult
from dafeiyu.links_failures import (
    FAILURE_FILE_NAME,
    NEEDS_BROWSER,
    SEARCH_ONLY,
    HostFailureStore,
    HostVerdict,
    failure_shape,
    url_host,
)

HOST = "blocked.test"
URL = f"https://{HOST}/page"
#: 整段提示词里绝不能再出现「再抓一次」类字样（IS-1）。
REFETCH_WORDING = ("再抓", "再来一次", "继续抓")


class FakeClock:
    """可手动推进的时钟：测 TTL 与 updated_at 刷新，避免真实 mtime 粒度。"""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeHttpResolver:
    """HTTP 层假实现：返回预设结果并记录每一次真实发起的请求。"""

    def __init__(self, result: LinkResolution) -> None:
        self.result = result
        self.calls: list[str] = []

    async def resolve(self, url: str) -> LinkResolution:
        self.calls.append(url)
        return self.result


class FakeBrowserRenderer:
    """浏览器层假实现：结果可在两次解析之间改（模拟「这次也挂了」）。"""

    def __init__(self, result: BrowserResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def render(self, url: str) -> BrowserResult:
        self.calls.append(url)
        return self.result

    async def aclose(self) -> None:
        return None


def http_403() -> LinkResolution:
    return LinkResolution(ok=False, url=URL, status=403, reason="http_403")


def ok_resolution() -> LinkResolution:
    return LinkResolution(
        ok=True, url=URL, status=200, title="标题", text="正文片段", reason="ok"
    )


def build(
    tmp_path: Path,
    *,
    http_result: LinkResolution | None = None,
    browser_result: BrowserResult | None = None,
    clock: FakeClock | None = None,
    browser_enabled: bool = True,
) -> tuple[LinkPromptResolver, HostFailureStore, FakeHttpResolver, FakeBrowserRenderer, LinkConfig, FakeClock]:
    config = LinkConfig(
        cache_dir=str(tmp_path / "link-cache"),
        browser=LinkBrowserConfig(enabled=browser_enabled),
    )
    clock = clock or FakeClock()
    store = HostFailureStore(config, clock=clock)
    http = FakeHttpResolver(http_result or http_403())
    browser = FakeBrowserRenderer(
        browser_result or BrowserResult(ok=True, url=URL, title="渲染标题", text="渲染正文")
    )
    engine = LinkPromptResolver(
        config, resolver=http, renderer=browser, clock=clock, failures=store
    )
    return engine, store, http, browser, config, clock


def read_ledger_hosts(config: LinkConfig) -> dict[str, dict[str, object]]:
    path = Path(config.cache_dir) / FAILURE_FILE_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    hosts = data["hosts"]
    assert isinstance(hosts, dict)
    return hosts


def require(store: HostFailureStore, host: str = HOST) -> HostVerdict:
    verdict = store.get(host)
    assert verdict is not None
    return verdict


# ---- 计划 QA 主线：三次解析 --------------------------------------------------


async def test_first_http_failure_records_needs_browser(tmp_path: Path) -> None:
    engine, store, http, browser, config, _ = build(tmp_path)

    blocks = await engine.resolve_for_prompt([URL])

    # HTTP 只撞一次；浏览器兜住了这次回复。
    assert http.calls == [URL]
    assert browser.calls == [URL]
    assert blocks[0].ok is True
    assert blocks[0].title == "渲染标题"
    assert blocks[0].failure_layer == "none"

    verdict = store.get(HOST)
    assert verdict is not None
    assert verdict.verdict == NEEDS_BROWSER
    assert verdict.shape == "403"
    assert verdict.reason == "http_403"
    assert verdict.layer == "http"

    ledger = read_ledger_hosts(config)
    assert list(ledger) == [HOST]


async def test_second_attempt_skips_http_then_upgrades_to_search_only(
    tmp_path: Path,
) -> None:
    engine, store, http, browser, config, clock = build(tmp_path)
    await engine.resolve_for_prompt([URL])
    first_seen = store.get(HOST)
    assert first_seen is not None

    # 第二次：HTTP 层必须被完全跳过，直接走浏览器；这次浏览器也挂了。
    clock.advance(60.0)
    browser.result = BrowserResult(ok=False, url=URL, reason="browser_timeout")

    blocks = await engine.resolve_for_prompt([URL])

    assert http.calls == [URL], "命中 needs_browser 后不得再发起 HTTP 请求"
    assert browser.calls == [URL, URL], "应直接从浏览器层重试一次"
    assert blocks[0].ok is False
    assert blocks[0].failure_layer == "browser"
    assert blocks[0].reason == "browser_timeout"

    verdict = require(store)
    assert verdict.verdict == SEARCH_ONLY
    assert verdict.layer == "browser"
    assert verdict.shape == "timeout"
    assert verdict.first_seen_at == first_seen.first_seen_at, "升级复用同一条记录"
    assert verdict.updated_at == clock.now

    # 覆盖升级：该 host 仍然只有一条记录（文件里也只有一条）。
    assert list(store.snapshot()) == [HOST]
    assert len(read_ledger_hosts(config)) == 1


async def test_third_attempt_sends_no_request_and_emits_fallback(
    tmp_path: Path,
) -> None:
    engine, _, http, browser, _, clock = build(tmp_path)
    await engine.resolve_for_prompt([URL])
    browser.result = BrowserResult(ok=False, url=URL, reason="browser_timeout")
    await engine.resolve_for_prompt([URL])

    http_before, browser_before = len(http.calls), len(browser.calls)
    clock.advance(60.0)

    blocks = await engine.resolve_for_prompt([URL])

    assert len(http.calls) == http_before, "search_only 的 host 不得再发 HTTP 请求"
    assert len(browser.calls) == browser_before, "search_only 的 host 不得再渲染"
    assert blocks[0].ok is False
    assert blocks[0].failure_layer == "none"  # 一次请求都没发起
    assert blocks[0].reason == SEARCH_ONLY

    prompt = render_link_block(blocks)
    assert prompt.count(LINK_UNREADABLE) == 1
    assert prompt.count("未能解析的链接") == 1  # 降级块只有一个
    assert "websearch" in prompt
    for word in REFETCH_WORDING:
        assert word not in prompt


async def test_needs_browser_with_browser_disabled_sends_no_request(
    tmp_path: Path,
) -> None:
    """浏览器关掉时 needs_browser 无路可走：不发 HTTP（已知它不行），直接标读不到。"""
    engine, store, http, browser, _, _ = build(tmp_path, browser_enabled=False)
    store.record_http_failure(HOST, "http_403")

    blocks = await engine.resolve_for_prompt([URL])

    assert http.calls == []
    assert browser.calls == []
    assert blocks[0].ok is False
    assert blocks[0].failure_layer == "http"
    assert blocks[0].reason == NEEDS_BROWSER


# ---- TTL：过期后允许重试并刷新结论 ------------------------------------------


async def test_expired_search_only_gets_one_real_retry_and_refreshes(
    tmp_path: Path,
) -> None:
    engine, store, http, browser, _, clock = build(tmp_path)
    await engine.resolve_for_prompt([URL])
    browser.result = BrowserResult(ok=False, url=URL, reason="browser_timeout")
    await engine.resolve_for_prompt([URL])
    assert require(store).verdict == SEARCH_ONLY
    assert len(http.calls) == 1

    # 7 天后：结论过期，必须真的再试一次（HTTP 层重新参与），并刷新时间戳。
    clock.advance(LinkConfig().host_failure_ttl_seconds)
    browser.result = BrowserResult(ok=True, url=URL, title="又好了", text="正文")
    blocks = await engine.resolve_for_prompt([URL])

    assert http.calls == [URL, URL], "TTL 过期后应重新发起一次 HTTP 请求"
    assert blocks[0].ok is True
    # 浏览器仍兜住了回复，但 HTTP 失败这一条知识被刷新成新时间戳。
    verdict = store.get(HOST)
    assert verdict is not None
    assert verdict.verdict == NEEDS_BROWSER
    assert verdict.updated_at == clock.now


async def test_expired_verdict_is_dropped_when_http_succeeds(tmp_path: Path) -> None:
    engine, store, http, browser, _, clock = build(tmp_path)
    await engine.resolve_for_prompt([URL])
    assert store.get(HOST) is not None

    clock.advance(LinkConfig().host_failure_ttl_seconds)
    http.result = ok_resolution()

    blocks = await engine.resolve_for_prompt([URL])

    assert http.calls == [URL, URL]
    assert browser.calls == [URL]  # 成功就不进浏览器
    assert blocks[0].ok is True and blocks[0].title == "标题"
    assert store.get(HOST) is None, "成功的真实抓取应销掉过期旧账"
    assert store.snapshot() == {}


# ---- 账本本身：去重、持久化、坏文件容错 ------------------------------------


def test_duplicate_writes_keep_one_record(tmp_path: Path) -> None:
    config = LinkConfig(cache_dir=str(tmp_path / "link-cache"))
    clock = FakeClock()
    store = HostFailureStore(config, clock=clock)

    for _ in range(3):
        store.record_http_failure(HOST, "http_403")

    assert list(store.snapshot()) == [HOST]
    assert len(read_ledger_hosts(config)) == 1

    # 换一个实例读同一个文件：仍然是同一条（不是追加的第二条）。
    again = HostFailureStore(config, clock=clock)
    assert require(again).shape == "403"
    assert len(again.snapshot()) == 1


def test_different_shapes_for_same_host_still_one_record(tmp_path: Path) -> None:
    config = LinkConfig(cache_dir=str(tmp_path / "link-cache"))
    store = HostFailureStore(config, clock=FakeClock())

    store.record_http_failure(HOST, "http_403")
    store.record_http_failure(HOST, "timeout")

    assert len(store.snapshot()) == 1
    assert require(store).shape == "timeout"


def test_corrupt_or_missing_file_never_breaks_resolution(tmp_path: Path) -> None:
    config = LinkConfig(cache_dir=str(tmp_path / "link-cache"))
    path = Path(config.cache_dir) / FAILURE_FILE_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json", encoding="utf-8")

    store = HostFailureStore(config, clock=FakeClock())
    assert store.get(HOST) is None
    assert store.snapshot() == {}

    # 坏文件之后仍能正常写：覆盖成合法 JSON。
    store.record_http_failure(HOST, "http_403")
    assert store.get(HOST) is not None
    assert list(read_ledger_hosts(config)) == [HOST]

    # 目录/文件都不存在时也能按需建。
    other = HostFailureStore(config, path=tmp_path / "deep" / "nested" / FAILURE_FILE_NAME)
    other.record_http_failure("new.test", "http_403")
    assert other.get("new.test") is not None


def test_ledger_sits_next_to_url_cache(tmp_path: Path) -> None:
    config = LinkConfig(cache_dir=str(tmp_path / "link-cache"))
    store = HostFailureStore(config)
    assert store.path == Path(config.cache_dir) / "host-failures.json"
    store.record_http_failure(HOST, "http_403")
    assert store.path.exists()
    assert not list(store.path.parent.glob("*.tmp")), "临时文件必须被清理"


def test_empty_host_failure_is_never_recorded(tmp_path: Path) -> None:
    config = LinkConfig(cache_dir=str(tmp_path / "link-cache"))
    store = HostFailureStore(config, path=tmp_path / "host-failures.json")

    assert url_host("https://WWW.Example.Test:443/a?b=1") == "www.example.test"
    assert url_host("不是链接") == ""
    assert store.record_http_failure("", "http_403") is None
    assert store.get("") is None
    assert not store.path.exists(), "没有 host 就不该建文件"


def test_failure_shape_normalizes_reasons() -> None:
    assert failure_shape("http_403") == "403"
    assert failure_shape("http_412") == "412"
    assert failure_shape("browser_timeout") == "timeout"
    assert failure_shape("timeout") == "timeout"
    assert failure_shape("challenge") == "challenge"
    assert failure_shape("empty") == "empty"
    assert failure_shape("") == "unknown"
