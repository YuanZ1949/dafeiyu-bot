"""chat-driven-behaviour-fixes T1：服务端链接 HTTP 抓取层。

覆盖：浏览器级请求头、403/412 不重试不抛异常、超时/网络错误归因、
缓存命中不发请求、TTL 过期视为未命中、html_to_text 剥 script/style
并保留段落换行、缓存淘汰最旧先走且不碰 host-failures.json、
录制的 B 站 fixture 离线解析（不联网）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from dafeiyu.config import LinkConfig
from dafeiyu.links import (
    LinkResolution,
    LinkResolver,
    extract_title,
    html_to_text,
    resolve_url,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class FakeClock:
    """可手动推进的时钟：测 TTL 过期与淘汰顺序（避免依赖真实 mtime 粒度）。"""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class CountingTransport:
    """MockTransport 的计数包装：断言「不重试」「缓存命中不发请求」。"""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.calls = 0
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(request)
        return self._responder(request)


def make_resolver(
    tmp_path: Path,
    responder: Callable[[httpx.Request], httpx.Response],
    clock: FakeClock | None = None,
    **overrides: object,
) -> tuple[LinkResolver, CountingTransport]:
    cfg = LinkConfig(cache_dir=str(tmp_path / "link-cache"), **overrides)  # type: ignore[arg-type]
    counter = CountingTransport(responder)
    resolver = LinkResolver(cfg, transport=httpx.MockTransport(counter), clock=clock)
    return resolver, counter


_SIMPLE_PAGE = "<html><head><title>标题</title></head><body><p>正文</p></body></html>"


# ---- 请求头：必须浏览器级 ----------------------------------------------------


async def test_headers_are_browser_grade(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=_SIMPLE_PAGE)

    resolver, counter = make_resolver(tmp_path, responder)
    result = await resolver.resolve("http://example.test/page")

    assert result.ok is True
    assert counter.calls == 1
    request = captured[0]
    ua = request.headers["user-agent"]
    # 实测 B 站对 httpx 默认 UA 回 412，只有浏览器级 UA 能拿 200。
    assert ua.startswith("Mozilla/5.0")
    assert "Edg/" in ua
    assert request.headers["accept-language"] == "zh-CN,zh;q=0.9,en;q=0.8"
    assert "text/html" in request.headers["accept"]
    assert request.headers["referer"] == "http://example.test/"


# ---- 非 2xx：不抛异常、不重试 ------------------------------------------------


@pytest.mark.parametrize("status", [403, 412])
async def test_http_error_no_retry_no_raise(tmp_path: Path, status: int) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="<html><body>denied</body></html>")

    resolver, counter = make_resolver(tmp_path, responder)
    result = await resolver.resolve("http://example.test/denied")

    assert result.ok is False
    assert result.status == status
    assert result.reason == f"http_{status}"
    # 风控结论（知乎整站 403、B 站默认 UA 412）再撞只会更糟：只调一次。
    assert counter.calls == 1


async def test_timeout_maps_to_timeout_reason(tmp_path: Path) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    resolver, counter = make_resolver(tmp_path, responder)
    result = await resolver.resolve("http://example.test/slow")

    assert result.ok is False
    assert result.reason == "timeout"
    assert counter.calls == 1


async def test_connect_error_maps_to_network_reason(tmp_path: Path) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    resolver, counter = make_resolver(tmp_path, responder)
    result = await resolver.resolve("http://example.test/refused")

    assert result.ok is False
    assert result.reason == "network"
    assert counter.calls == 1


# ---- 内容侧失败归因 ----------------------------------------------------------


async def test_too_large_body(tmp_path: Path) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 200)

    resolver, _ = make_resolver(tmp_path, responder, max_bytes=50)
    result = await resolver.resolve("http://example.test/big")

    assert result.ok is False
    assert result.status == 200
    assert result.reason == "too_large"


async def test_empty_body(tmp_path: Path) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><head></head><body>   </body></html>")

    resolver, _ = make_resolver(tmp_path, responder)
    result = await resolver.resolve("http://example.test/blank")

    assert result.ok is False
    assert result.status == 200
    assert result.reason == "empty"


async def test_challenge_page(tmp_path: Path) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Just a moment...</body></html>")

    resolver, _ = make_resolver(tmp_path, responder)
    result = await resolver.resolve("http://example.test/challenge")

    assert result.ok is False
    assert result.reason == "challenge"


# ---- 缓存：命中不发请求、TTL 过期视为未命中 ----------------------------------


async def test_cache_hit_makes_no_second_request(tmp_path: Path) -> None:
    resolver, counter = make_resolver(tmp_path, lambda request: httpx.Response(200, text=_SIMPLE_PAGE))

    first = await resolver.resolve("http://example.test/a")
    assert counter.calls == 1
    second = await resolver.resolve("http://example.test/a")

    assert counter.calls == 1  # 第二次完全没碰网络
    assert second.ok is True
    assert second.title == first.title == "标题"
    assert second.text == first.text == "正文"
    assert second.fetched_at == first.fetched_at


async def test_expired_cache_is_a_miss(tmp_path: Path) -> None:
    clock = FakeClock()
    resolver, counter = make_resolver(
        tmp_path, lambda request: httpx.Response(200, text=_SIMPLE_PAGE), clock=clock
    )

    await resolver.resolve("http://example.test/a")
    assert counter.calls == 1

    clock.now += 86_401  # 超过默认 TTL 86400 秒
    await resolver.resolve("http://example.test/a")
    assert counter.calls == 2  # 过期视为未命中，重新抓


# ---- html_to_text / extract_title -------------------------------------------


def test_html_to_text_strips_script_and_keeps_paragraphs() -> None:
    page = (
        "<html><head>"
        "<script>var leak='should-not-appear';</script>"
        "<style>.a{color:red}</style>"
        "</head><body>"
        "<p>第一段&amp;实体</p><div>第二段</div>"
        "<script>window.x=1;</script><br>第三段"
        "</body></html>"
    )
    text = html_to_text(page)
    assert "should-not-appear" not in text
    assert "window.x" not in text
    assert "color:red" not in text
    assert text == "第一段&实体\n第二段\n第三段"


def test_html_to_text_collapses_inline_whitespace() -> None:
    assert html_to_text("<p>好   多\t空格</p>") == "好 多 空格"


def test_extract_title_prefers_og_title() -> None:
    page = '<meta property="og:title" content="OG &amp; 标题"><title>页面标题</title>'
    assert extract_title(page) == "OG & 标题"


def test_extract_title_falls_back_to_title() -> None:
    assert extract_title("<title>只有  标题</title>") == "只有 标题"


# ---- 缓存上限：最旧先淘汰，绝不碰 host-failures.json -------------------------


async def test_eviction_drops_oldest_and_spares_host_failures(tmp_path: Path) -> None:
    cache_dir = tmp_path / "link-cache"
    cache_dir.mkdir()
    (cache_dir / "host-failures.json").write_text("{}", encoding="utf-8")

    clock = FakeClock()
    counter = CountingTransport(lambda request: httpx.Response(200, text=_SIMPLE_PAGE))
    resolver = LinkResolver(
        LinkConfig(cache_dir=str(cache_dir), cache_max_entries=2),
        transport=httpx.MockTransport(counter),
        clock=clock,
    )

    urls = ["http://example.test/1", "http://example.test/2", "http://example.test/3"]
    for url in urls:
        await resolver.resolve(url)
        clock.now += 10.0  # 每个 URL 的 fetched_at 依次变新

    names = {p.name for p in cache_dir.iterdir()}
    assert "host-failures.json" in names  # T8 的失败结论文件绝不能被误删
    oldest_hash = hashlib.sha256(urls[0].encode("utf-8")).hexdigest()
    assert not (cache_dir / f"{oldest_hash}.json").exists()
    for kept in urls[1:]:
        kept_hash = hashlib.sha256(kept.encode("utf-8")).hexdigest()
        assert (cache_dir / f"{kept_hash}.json").exists()
    assert counter.calls == 3


# ---- 录制的 B 站 fixture：离线、浏览器级 UA 下 200 + 正确标题 -----------------


async def test_bilibili_fixture_offline(tmp_path: Path) -> None:
    page = (FIXTURES_DIR / "bilibili_video.html").read_text(encoding="utf-8")

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=page.encode("utf-8"),
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    resolver, counter = make_resolver(tmp_path, responder)
    result = await resolver.resolve("https://www.bilibili.com/video/BV1xx411c7mD")

    assert counter.calls == 1
    assert result.ok is True
    assert result.status == 200
    assert result.title == (
        "【双语】每日宇航员采访NASA局长，谈阿尔忒弥斯计划和NASA改革_哔哩哔哩_bilibili"
    )
    assert "阿尔忒弥斯" in result.text
    assert "__INITIAL_STATE__" not in result.text
    assert "Everyday Astronaut" not in result.text


# ---- 截断与便捷入口 ----------------------------------------------------------


async def test_max_chars_truncation(tmp_path: Path) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body><p>" + "字" * 100 + "</p></body></html>")

    resolver, _ = make_resolver(tmp_path, responder, max_chars=20)
    result = await resolver.resolve("http://example.test/long")

    assert result.ok is True
    assert len(result.text) <= 20
    assert len(result.title) <= 20


async def test_resolve_url_helper(tmp_path: Path) -> None:
    cfg = LinkConfig(cache_dir=str(tmp_path / "c"))
    result = await resolve_url(
        "http://example.test/x",
        cfg,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=_SIMPLE_PAGE)
        ),
    )
    assert result.ok is True
    assert result.title == "标题"


def test_link_resolution_is_frozen() -> None:
    resolution = LinkResolution(
        ok=True, url="http://x", status=200, title="t", text="b", reason="ok", fetched_at=1.0
    )
    with pytest.raises(AttributeError):
        resolution.ok = False  # type: ignore[misc]
