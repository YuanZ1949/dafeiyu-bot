"""T3：MirrorRouter 的渲染、队列、指纹、错误分类与落库隔离。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest

import dafeiyu.mirror as mirror_module
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    ImagesConfig,
    LoggingConfig,
    MemoryConfig,
    MirrorConfig,
    MirrorPairConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
    WorkspaceConfig,
)
from dafeiyu.gateway.base import GatewayError
from dafeiyu.memory.store import Store
from dafeiyu.mirror import MirrorItem, MirrorRouter, RenderedLines
from dafeiyu.models import Attachment, ChatType, Message, Segment
from dafeiyu.service import Service
from dafeiyu.textwidth import display_width

QQ = "qq:1001"
WXG = "wxg:group"


# 他人转发消息的线上形态（wire）：字体无关的左锚气泡（首行 `╭ 正文`、末行 `╰`）。
# 期望串全部硬编码，不靠被测实现生成。
WIRE_SHUNLU = "╭ 阿鱼\n│ 顺路\n╰"
WIRE_YI = "╭ 阿鱼\n│ 一\n╰"
WIRE_SAN = "╭ 阿鱼\n│ 三\n╰"
WIRE_YIDUI = "╭ 阿鱼\n│ 一对\n╰"
WIRE_ERDUI = "╭ 阿鱼\n│ 二对\n╰"
WIRE_XINNIANCHENG = "╭ 新昵称\n│ 换群\n╰"
WIRE_XINDUI = "╭ 阿鱼\n│ 新对\n╰"
WIRE_DIER = "╭ 阿鱼\n│ 第二\n╰"
WIRE_VIDEO = "╭ 阿鱼\n│ [视频]\n╰"
WIRE_IMAGE = "╭ 阿鱼\n│ [图片]\n╰"


class Recorder:
    """最小内层 adapter：记录发送、可注入延迟/异常。"""

    def __init__(
        self,
        bot_id: str,
        *,
        delay: float = 0.0,
        fail: Any = None,
    ) -> None:
        self.bot_id = bot_id
        self.connected = True
        self.sent: list[tuple[str, str]] = []
        self.calls = 0
        self.delay = delay
        self.fail = fail

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            exc = self.fail()
            if exc is not None:
                raise exc
        self.sent.append((chat_id, text))
        return f"{self.bot_id}:{self.calls}"


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
        persona=PersonaConfig("溟月", ("溟月",), 120, 300, 0.05),
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        mirror=MirrorConfig(
            enabled=True,
            pairs=(MirrorPairConfig(qq=QQ, wechat=WXG),),
        ),
    )


def make_router(
    tmp_path: Path,
    *,
    qq: Recorder | None = None,
    wechat: Recorder | None = None,
) -> tuple[MirrorRouter, Store, dict[str, Recorder]]:
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    settings = make_settings()
    adapters = {
        "qq": qq or Recorder("qq:999"),
        "wechat": wechat or Recorder("wxg:self"),
    }
    return MirrorRouter(settings, store, adapters), store, adapters


def make_msg(
    chat_id: str,
    content: str,
    *,
    user_id: str = "qq:2002",
    message_id: str = "m1",
    nickname: str = "阿鱼",
    is_bot: bool = False,
    segments: list[Segment] | None = None,
    attachments: list[Attachment] | None = None,
    kind: str = "text",
) -> Message:
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id=user_id,
        nickname=nickname,
        content=content,
        message_id=message_id,
        segments=segments or [],
        attachments=attachments or [],
        is_bot=is_bot,
        kind=kind,
    )


def render(router: MirrorRouter, message: Message) -> list[str]:
    """入库形态（store）：与气泡上线前的历史行为逐字节一致。"""
    direction = router._direction(message.chat_id)
    assert direction is not None
    source_channel, target = direction
    return router._render(router._snapshot(message, target, source_channel)).store


def render_wire(router: MirrorRouter, message: Message) -> list[str]:
    """线上形态（wire）：别人转来的消息是三行圆角气泡。"""
    direction = router._direction(message.chat_id)
    assert direction is not None
    source_channel, target = direction
    return router._render(router._snapshot(message, target, source_channel)).wire


async def wait_until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


# ---------------------------------------------------------------- 渲染
def test_render_human_has_nickname_prefix(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    assert render(router, make_msg(QQ, "你好")) == ["阿鱼：你好"]


def test_render_bot_has_no_prefix(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    assert render(router, make_msg(QQ, "你好", is_bot=True)) == ["你好"]


def test_render_qq_pure_image_uses_attachments_not_content(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        QQ,
        "[图片]",
        segments=[Segment("image", {"url": "http://example/a.png"})],
        attachments=[Attachment(kind="image", source="http://example/a.png")],
    )
    assert render(router, message) == ["阿鱼：[图片]"]


def test_render_wechat_voice_from_kind(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        WXG,
        "[语音]",
        user_id="wxg:human",
        kind="voice",
        attachments=[Attachment(kind="voice")],
    )
    assert render(router, message) == ["阿鱼：[语音]"]


def test_render_wechat_link_card_keeps_title(tmp_path: Path) -> None:
    """微信链接卡片：桥接已把 <title> 放进 content，转发时必须保留。

    线上事故：知乎卡片到 QQ 只剩一个 ``[link]`` —— 正文被 ``kind != text`` 整段
    丢掉，占位又兜底成英文 kind。
    """
    router, _store, _adapters = make_router(tmp_path)
    title = "中国新能源企业在芬建厂涉人口贩运调查，出海中企该如何规避海外用工合规风险？"
    message = make_msg(WXG, title, user_id="wxg:human", kind="link")
    assert render(router, message) == ["阿鱼：" + title]
    wire = render_wire(router, message)
    assert len(wire) == 1
    lines = wire[0].splitlines()
    bodies = [lines[0][2:]] + [line[2:] for line in lines[1:-1]]
    assert "".join(bodies) == "阿鱼" + title
    assert "[link]" not in wire[0]
    assert "[链接]" not in wire[0]


def test_render_wechat_link_card_with_url_keeps_both_lines(tmp_path: Path) -> None:
    """标题 + 链接两行都要转发；桥接不再伪造 link 占位附件。

    线上事故：微信知乎卡片到 QQ 变成「标题 [链接]」，``<url>`` 根本没被
    提取，收件人点不开原文。
    """
    router, _store, _adapters = make_router(tmp_path)
    url = "https://www.zhihu.com/question/2107239235015263538"
    message = make_msg(WXG, f"数学家称与 AI 合作证明了黎曼猜想\n{url}", user_id="wxg:human", kind="link")
    assert render(router, message) == [f"阿鱼：数学家称与 AI 合作证明了黎曼猜想\n{url}"]
    wire = render_wire(router, message)
    assert len(wire) == 1
    assert url in wire[0]
    assert "[链接]" not in wire[0]
    assert "[link]" not in wire[0]


def test_render_wechat_link_card_without_title_keeps_chinese_placeholder(
    tmp_path: Path,
) -> None:
    """标题没解析出来时留中文占位，而不是把整条消息吞掉或漏出英文 kind。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(WXG, "", user_id="wxg:human", kind="link")
    assert render(router, message) == ["阿鱼：[链接]"]


def test_render_wechat_quote_and_card_keep_text(tmp_path: Path) -> None:
    """引用回复与普通卡片同样属于可读正文，标题不得被丢掉。"""
    router, _store, _adapters = make_router(tmp_path)
    quote = make_msg(WXG, "引用正文", user_id="wxg:human", kind="quote")
    assert render(router, quote) == ["阿鱼：引用正文"]
    card = make_msg(WXG, "卡片标题", user_id="wxg:human", kind="card")
    assert render(router, card) == ["阿鱼：卡片标题"]


def test_render_wechat_empty_text_is_dropped(tmp_path: Path) -> None:
    """空正文的纯文本不产生任何线上消息（改动前后一致，不造占位）。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(WXG, "", user_id="wxg:human", kind="text")
    rendered = router._render(router._snapshot(message, QQ, "wechat"))
    assert rendered.wire == [] and rendered.store == []


def test_render_wechat_image_kind_still_uses_placeholder(tmp_path: Path) -> None:
    """图片等媒体 kind 不把 content 的 [图片] 当正文重复，仍只出一个占位。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        WXG,
        "[图片]",
        user_id="wxg:human",
        kind="image",
        attachments=[Attachment(kind="image", path="images/x.png")],
    )
    assert render(router, message) == ["阿鱼：[图片]"]


def test_render_wechat_unknown_kind_never_leaks_english(tmp_path: Path) -> None:
    """未知 kind 的兜底占位必须是中文：英文 kind 不能漏进群里。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(WXG, "", user_id="wxg:human", kind="hologram")
    assert render(router, message) == ["阿鱼：[其它消息]"]


def test_render_file_keeps_name_and_forward_count(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    file_msg = make_msg(
        QQ,
        "[文件] a.txt",
        attachments=[Attachment(kind="file", name="a.txt")],
    )
    assert render(router, file_msg) == ["阿鱼：[文件] a.txt"]

    forward_msg = make_msg(
        QQ,
        "[合并转发]",
        segments=[Segment("node", {"content": [{"name": "a"}, {"name": "b"}]})],
        attachments=[Attachment(kind="forward")],
    )
    assert render(router, forward_msg) == ["阿鱼：[合并转发 2 条]"]


# ---------------------------------------------------------------- 昵称映射
def _router_with_nicknames(
    tmp_path: Path, nicknames: tuple[tuple[str, str], ...]
) -> MirrorRouter:
    settings = dataclasses.replace(
        make_settings(),
        mirror=MirrorConfig(
            enabled=True,
            pairs=(MirrorPairConfig(qq=QQ, wechat=WXG),),
            nicknames=nicknames,
        ),
    )
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    return MirrorRouter(
        settings,
        store,
        {"qq": Recorder("qq:999"), "wechat": Recorder("wxg:self")},
    )


def test_nickname_override_replaces_display_name(tmp_path: Path) -> None:
    router = _router_with_nicknames(tmp_path, (("qq:2002", "老鱼"),))
    assert render(router, make_msg(QQ, "你好")) == ["老鱼：你好"]


def test_nickname_override_is_scoped_by_platform(tmp_path: Path) -> None:
    """同一个裸 id 在两个平台是两个人：只配了微信侧就不能改 QQ 的显示名。"""
    router = _router_with_nicknames(tmp_path, (("wxg:2002", "微信老鱼"),))
    assert render(router, make_msg(QQ, "你好")) == ["阿鱼：你好"]


def test_nickname_override_ignored_for_unknown_user(tmp_path: Path) -> None:
    router = _router_with_nicknames(tmp_path, (("qq:9999", "别人"),))
    assert render(router, make_msg(QQ, "你好")) == ["阿鱼：你好"]


# ---------------------------------------------------------------- 引用/@
def test_render_at_degrades_to_text(tmp_path: Path) -> None:
    """@ 段不再被丢掉：被 @ 的是 bot 就用本体名，其余统一 @群友。"""
    router, _store, _adapters = make_router(tmp_path)
    to_bot = make_msg(
        QQ,
        "@溟月 看这个",
        segments=[
            Segment("at", {"qq": "999"}),
            Segment("text", {"text": "看这个"}),
        ],
    )
    assert render(router, to_bot) == ["阿鱼：@溟月 看这个"]

    to_other = make_msg(
        QQ,
        "@群友 看这个",
        segments=[
            Segment("at", {"qq": "2002"}),
            Segment("text", {"text": "看这个"}),
        ],
    )
    assert render(router, to_other) == ["阿鱼：@群友 看这个"]

    to_all = make_msg(
        QQ,
        "@全体成员 看这个",
        segments=[
            Segment("at", {"qq": "all"}),
            Segment("text", {"text": "看这个"}),
        ],
    )
    assert render(router, to_all) == ["阿鱼：@全体成员 看这个"]


def test_render_at_only_media_keeps_at(tmp_path: Path) -> None:
    """只有 @ + 图片时也不能把 @ 吞掉（以前会只剩 [图片]）。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        QQ,
        "@群友 [图片]",
        segments=[
            Segment("at", {"qq": "2002"}),
            Segment("image", {"url": "http://x/a.png"}),
        ],
        attachments=[Attachment(kind="image", source="http://x/a.png")],
    )
    assert render(router, message) == ["阿鱼：@群友 [图片]"]


def test_render_quoted_text_becomes_hint(tmp_path: Path) -> None:
    """能拿到引用原文时带上一行提示，但截断到 REPLY_HINT_MAX。"""
    router, _store, _adapters = make_router(tmp_path)
    message = dataclasses.replace(
        make_msg(QQ, "同意"),
        reply_to_text="明天十点开会",
        reply_to_author="小明",
    )
    assert render(router, message) == ["阿鱼：[回复 小明：明天十点开会] 同意"]

    long_quote = dataclasses.replace(
        make_msg(QQ, "同意"),
        reply_to_text="字" * (mirror_module.REPLY_HINT_MAX + 20),
    )
    rendered = render(router, long_quote)
    assert rendered[0].startswith("阿鱼：[回复 字")
    assert "…]" in rendered[0]


def test_render_reply_without_text_keeps_marker(tmp_path: Path) -> None:
    """QQ 引用只有 id 拿不到原文：至少留一个 [回复] 标记。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        QQ,
        "同意",
        segments=[
            Segment("reply", {"id": "42"}),
            Segment("text", {"text": "同意"}),
        ],
    )
    assert render(router, message) == ["阿鱼：[回复] 同意"]


def test_render_wechat_quote_hint_from_envelope(tmp_path: Path) -> None:
    """微信侧的引用由桥带出原文，镜像到 QQ 时同样降级成一行。"""
    router, _store, _adapters = make_router(tmp_path)
    message = dataclasses.replace(
        make_msg(WXG, "好", user_id="wxg:human", message_id="w1"),
        reply_to_text="今天加班",
        reply_to_author="老王",
    )
    assert render(router, message) == ["阿鱼：[回复 老王：今天加班] 好"]


# ---------------------------------------------------------------- 合并转发
def test_render_forward_uses_nodes_instead_of_count(tmp_path: Path) -> None:
    """能拿到节点时搬「谁说了什么」，不再只在正文后追加 [合并转发 N 条]。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        QQ,
        "[聊天记录]\n小明: 明天十点开会\n小红: 收到",
        segments=[
            Segment(
                "forward",
                {
                    "id": "f1",
                    "content": [
                        {
                            "sender": {"nickname": "小明"},
                            "message": [
                                {"type": "text", "data": {"text": "明天十点开会"}}
                            ],
                        },
                        {
                            "sender": {"nickname": "小红"},
                            "message": [{"type": "text", "data": {"text": "收到"}}],
                        },
                    ],
                },
            )
        ],
    )
    expected = "阿鱼：[聊天记录]\n小明: 明天十点开会\n小红: 收到"
    assert render(router, message) == [expected]


def test_render_forward_without_nodes_keeps_count(tmp_path: Path) -> None:
    """拉不到节点（只有 id）时退回条数占位，且不与文本摘要重复。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        QQ,
        "[合并转发]",
        segments=[Segment("forward", {"id": "f1"})],
    )
    assert render(router, message) == ["阿鱼：[合并转发 1 条]"]


def test_render_forward_node_media_clip_and_sender_fallback(tmp_path: Path) -> None:
    """节点内媒体降级成占位、缺 sender 用群友、长正文截断。"""
    router, _store, _adapters = make_router(tmp_path)
    long_text = "字" * (mirror_module.FORWARD_NODE_CLIP + 10)
    nodes = [
        {
            "message": [
                {"type": "at", "data": {"qq": "all"}},
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "image", "data": {"url": "http://x/a.png"}},
            ]
        },
        {"sender": {"user_id": "42"}, "message": long_text},
        {"sender": {"nickname": "小明"}, "message": "短"},
    ]
    message = make_msg(
        QQ,
        "[聊天记录]",
        segments=[Segment("forward", {"content": nodes})],
    )
    rendered = render(router, message)[0]
    assert "阿鱼：[聊天记录]\n群友: @全体成员 看这个[图片]" in rendered
    assert "\n42: 字" in rendered
    assert "…" in rendered
    assert rendered.endswith("\n小明: 短")


def test_render_forward_caps_node_count(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    nodes = [
        {"sender": {"nickname": f"群友{i}"}, "message": f"消息{i}"}
        for i in range(mirror_module.FORWARD_MAX_NODES + 2)
    ]
    message = make_msg(
        QQ,
        "[聊天记录]",
        segments=[Segment("forward", {"content": nodes})],
    )
    rendered = render(router, message)[0]
    last = mirror_module.FORWARD_MAX_NODES - 1
    assert f"群友{last}: 消息{last}" in rendered
    assert f"群友{mirror_module.FORWARD_MAX_NODES}: " not in rendered


def test_render_forward_with_attachment_keeps_forward_content(tmp_path: Path) -> None:
    """图片附件与合并转发同时出现：附件走占位，转发内容补在后面不丢。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(
        QQ,
        "[图片]",
        segments=[
            Segment("image", {"url": "http://x/a.png"}),
            Segment(
                "forward",
                {"content": [{"sender": {"nickname": "小明"}, "message": "在吗"}]},
            ),
        ],
        attachments=[Attachment(kind="image", source="http://x/a.png")],
    )
    assert render(router, message) == ["阿鱼：[图片] [聊天记录]\n小明: 在吗"]


def test_render_long_text_store_is_single_line(tmp_path: Path) -> None:
    """长正文入库仍是**一行** ``昵称：正文``（分块只发生在线上兜底路径）。"""
    router, _store, _adapters = make_router(tmp_path)
    chunks = render(router, make_msg(QQ, "字" * 1200))
    assert chunks == ["阿鱼：" + "字" * 1200]


# ---------------------------------------------------------------- 消息泡
def test_wire_bubble_shape_for_human(tmp_path: Path) -> None:
    """别人转来的消息线上是左锚气泡（昵称独占第一行）；入库仍是单行（store 零变化）。"""
    router, _store, _adapters = make_router(tmp_path)
    wire = render_wire(router, make_msg(QQ, "你好"))
    lines = wire[0].splitlines()
    assert lines[0] == "╭ 阿鱼"
    assert lines[1] == "│ 你好"
    assert lines[-1] == "╰"
    assert wire == ["╭ 阿鱼\n│ 你好\n╰"]
    # 入库基线（改动前硬编码）：单行「昵称：正文」。
    assert render(router, make_msg(QQ, "你好")) == ["阿鱼：你好"]


def test_wire_bubble_has_no_right_border_or_padding(tmp_path: Path) -> None:
    """左锚形态：没有右边框、没有上下横线、行尾不留补白空格。

    闭合框在手机字体 / 会折叠行尾空格的客户端上必然歪，所以整条路径都不该再
    出现 ``╮`` / ``╯`` / ``─``，也不该靠补空格对齐。
    """
    router, _store, _adapters = make_router(tmp_path)
    content = "甲" * 15 + "abcdefghij" + "乙" * 15
    for entry in render_wire(router, make_msg(QQ, content)):
        assert "╮" not in entry and "╯" not in entry and "─" not in entry
        for line in entry.splitlines():
            assert line == line.rstrip()
    # 入库形态不受气泡影响：与改动前逐字节一致。
    assert render(router, make_msg(QQ, content)) == ["阿鱼：" + content]


def test_wire_bubble_wraps_by_display_width(tmp_path: Path) -> None:
    """框内每行正文按显示宽度换行，且不超过 bubble_max_content_width。"""
    router, _store, _adapters = make_router(tmp_path)
    content = "甲" * 15 + "abcdefghij" + "乙" * 15
    wire = render_wire(router, make_msg(QQ, content))
    assert len(wire) == 1
    lines = wire[0].splitlines()
    assert len(lines) > 2
    bodies = [lines[0][2:]] + [line[2:] for line in lines[1:-1]]
    limit = MirrorConfig().bubble_max_content_width
    assert all(display_width(body) <= limit for body in bodies)
    assert "".join(bodies) == "阿鱼" + content


def test_wire_bubble_never_splits_url_in_card_text(tmp_path: Path) -> None:
    """卡片正文里的链接必须整段留在同一行（左锚气泡不能把它拦腰折断）。

    线上事故：QQ 小程序卡渲染成「[卡片] 知乎 https://zhuanlan.zhihu.com/p/…」，
    24 列折行把地址切成 5 行、每行带「│ 」前缀，微信不再识别成可点链接。
    """
    router, _store, _adapters = make_router(tmp_path)
    url = (
        "https://zhuanlan.zhihu.com/p/2108679792132362474"
        "?share_code=eP1JNe7gDMxt&utm_psn=2108724483657941689"
    )
    message = make_msg(QQ, f"[卡片] 知乎 {url}")
    wire = render_wire(router, message)
    assert len(wire) == 1
    url_lines = [line for line in wire[0].splitlines() if "https://" in line]
    assert url_lines == [f"│ {url}"]
    # 入库形态逐字节保留原文（气泡只管线上）。
    assert render(router, message) == [f"阿鱼：[卡片] 知乎 {url}"]


def test_wire_bubble_keeps_url_followed_by_text(tmp_path: Path) -> None:
    """链接后面还有字：链接整段不拆，后续文字另起一行。"""
    router, _store, _adapters = make_router(tmp_path)
    url = "https://example.com/a?b=1&c=2"
    wire = render_wire(router, make_msg(QQ, f"看这个 {url} 很有意思"))[0]
    assert url in wire
    assert [line for line in wire.splitlines() if url in line] == [f"│ {url}"]
    assert "很有意思" in wire


def test_render_qq_card_segment_does_not_add_unknown_placeholder(tmp_path: Path) -> None:
    """QQ 卡片段（json/xml）的可读正文已在 text_lines 里，不再补 [其它消息]。"""
    router, _store, _adapters = make_router(tmp_path)
    url = "https://zhuanlan.zhihu.com/p/1"
    message = make_msg(
        QQ,
        f"[卡片] 知乎 {url}",
        segments=[Segment(type="json", data={"data": "{}"})],
    )
    assert render(router, message) == [f"阿鱼：[卡片] 知乎 {url}"]
    assert "[其它消息]" not in render_wire(router, message)[0]


def test_wire_bubble_rows_fit_client_container(tmp_path: Path) -> None:
    """整行（含「│ 」前缀）要给客户端容器留余量。

    手机端气泡容器约 30 列；正文给到 30 时每行实际 32 列，客户端会自己再折
    一次，折出来的字落在行首、把左锚撑破（线上截图实测）。所以整行必须明显
    小于 30。
    """
    router, _store, _adapters = make_router(tmp_path)
    wire = render_wire(router, make_msg(QQ, "甲" * 60))
    assert len(wire) == 1
    for line in wire[0].splitlines():
        assert display_width(line) <= 28


def test_wire_bubble_only_drops_invisible_break_spaces(tmp_path: Path) -> None:
    """线上只丢「换行点上的空格」；其余字符逐字保留，入库逐字节不变。

    行尾空白必须剥掉（客户端会折叠，留着反而造成右侧参差），所以带空格的正文
    在换行点会少一个不可见空格——这是唯一允许的差异。
    """
    router, _store, _adapters = make_router(tmp_path)
    content = "明早 6:45 的发射 " + "再看一遍 回放"
    rendered = router._render(router._snapshot(make_msg(QQ, content), WXG, "qq"))
    assert len(rendered.wire) == 1
    lines = rendered.wire[0].splitlines()
    joined = "".join([lines[0][2:]] + [line[2:] for line in lines[1:-1]])
    assert joined.replace(" ", "") == ("阿鱼" + content).replace(" ", "")
    assert all(line == line.rstrip() for line in lines)
    # 入库形态一个字符都不少。
    assert rendered.store == ["阿鱼：" + content]


def test_store_matches_pre_bubble_baseline(tmp_path: Path) -> None:
    """入库形态与改动前基线逐字节相同（期望值硬编码，不用新实现生成）。"""
    router, _store, _adapters = make_router(tmp_path)
    assert render(router, make_msg(QQ, "你好")) == ["阿鱼：你好"]
    assert render(router, make_msg(QQ, "你好", is_bot=True)) == ["你好"]
    assert render(router, make_msg(QQ, "[图片] 同意")) == ["阿鱼：[图片] 同意"]


def test_wire_bot_item_has_no_box_characters(tmp_path: Path) -> None:
    """机器人自己转发的条目不加框：wire 与 store 相同、不含任何框字符。"""
    router, _store, _adapters = make_router(tmp_path)
    message = make_msg(QQ, "你好", is_bot=True)
    rendered = router._render(router._snapshot(message, WXG, "qq"))
    assert rendered.wire == rendered.store == ["你好"]
    assert not any(ch in "╭╮╰╯│─" for text in rendered.wire for ch in text)


def test_wire_bubble_never_names_the_platform(tmp_path: Path) -> None:
    """气泡里不得出现平台名（「QQ」「微信」），平台由谁转的一目了然即可。"""
    router, _store, _adapters = make_router(tmp_path)
    wire = render_wire(router, make_msg(QQ, "那边讨论很热闹，搬过来一起看"))
    joined = "\n".join(wire)
    assert "QQ" not in joined
    assert "微信" not in joined


def test_bubble_disabled_keeps_nickname_on_its_own_line(tmp_path: Path) -> None:
    """bubble_enabled=false 时不套框，但昵称仍独占第一行（store 保持单行契约）。"""
    settings = dataclasses.replace(
        make_settings(),
        mirror=MirrorConfig(
            enabled=True,
            pairs=(MirrorPairConfig(qq=QQ, wechat=WXG),),
            bubble_enabled=False,
        ),
    )
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    router = MirrorRouter(
        settings,
        store,
        {"qq": Recorder("qq:999"), "wechat": Recorder("wxg:self")},
    )
    rendered = router._render(router._snapshot(make_msg(QQ, "你好"), WXG, "qq"))
    assert rendered.wire == ["阿鱼\n你好"]
    assert rendered.store == ["阿鱼：你好"]


def test_long_text_is_one_bubble_not_many(tmp_path: Path) -> None:
    """一条长消息只套一个气泡：线上 1 条、入库 1 条（676 字线上事故的回归）。"""
    router, _store, _adapters = make_router(tmp_path)
    body = "字" * 800
    message = make_msg(QQ, body)
    rendered = router._render(router._snapshot(message, WXG, "qq"))
    assert len(rendered.wire) == len(rendered.store) == 1
    assert rendered.store == ["阿鱼：" + body]
    lines = rendered.wire[0].splitlines()
    assert lines[0] == "╭ 阿鱼"
    assert lines[1].startswith("│ ")
    assert lines[-1] == "╰"
    assert all(line.startswith("│ ") for line in lines[1:-1])
    bodies = [lines[0][2:]] + [line[2:] for line in lines[1:-1]]
    assert "".join(bodies) == "阿鱼" + body
    assert len(rendered.wire[0]) <= mirror_module.WIRE_MAX_CHARS


def test_extreme_long_text_falls_back_to_chunks(tmp_path: Path) -> None:
    """只有整段套框后超过 WIRE_MAX_CHARS 才拆，且每块仍是完整气泡。"""
    router, _store, _adapters = make_router(tmp_path)
    body = "字" * 6000
    message = make_msg(QQ, body)
    rendered = router._render(router._snapshot(message, WXG, "qq"))
    assert len(rendered.wire) == len(rendered.store) > 1
    for entry in rendered.wire:
        assert len(entry) <= mirror_module.WIRE_MAX_CHARS
        lines = entry.splitlines()
        assert lines[0].startswith("╭ ")
        assert lines[-1] == "╰"
        assert all(line.startswith("│ ") for line in lines[1:-1])
    # 入库仍是单行分块，不含框字符，拼接结果与改动前一致。
    assert "".join(rendered.store) == "阿鱼：" + body
    assert not any(ch in "╭╮╰╯│─" for text in rendered.store for ch in text)


# ---------------------------------------------------------------- 队列
async def test_two_directions_do_not_block_each_other(tmp_path: Path) -> None:
    slow = Recorder("qq:999", delay=0.3)
    fast = Recorder("wxg:self")
    router, _store, adapters = make_router(tmp_path, qq=slow, wechat=fast)
    router.start()
    try:
        router.observe_inbound(
            make_msg(WXG, "慢方向", user_id="wxg:human", message_id="w1")
        )
        router.observe_inbound(
            make_msg(QQ, "快方向", user_id="qq:human", message_id="q1")
        )
        await wait_until(lambda: len(fast.sent) == 1)
        assert slow.sent == []
        await wait_until(lambda: len(slow.sent) == 1)
    finally:
        await router.stop()


class GatedRecorder(Recorder):
    """指定目标群的发送先卡在 gate 上，用来验证其它对不被拖住。"""

    def __init__(self, bot_id: str, gate: asyncio.Event, blocked: str) -> None:
        super().__init__(bot_id)
        self.gate = gate
        self.blocked = blocked

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        if chat_id == self.blocked:
            await self.gate.wait()
        return await super().send(chat_type, chat_id, text)


def _two_pair_router(
    tmp_path: Path,
    *,
    wechat: Recorder | None = None,
) -> tuple[MirrorRouter, dict[str, Recorder]]:
    """两对 (qq:1001 ⇄ wxg:a) 与 (qq:2002 ⇄ wxg:b)，QQ 侧共用一个 adapter。"""
    settings = dataclasses.replace(
        make_settings(),
        mirror=MirrorConfig(
            enabled=True,
            pairs=(
                MirrorPairConfig(qq="qq:1001", wechat="wxg:a"),
                MirrorPairConfig(qq="qq:2002", wechat="wxg:b"),
            ),
        ),
    )
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    adapters = {
        "qq": Recorder("qq:999"),
        "wechat": wechat or Recorder("wxg:self"),
    }
    return MirrorRouter(settings, store, adapters), adapters


async def test_same_direction_pairs_do_not_block_each_other(tmp_path: Path) -> None:
    """同一个方向（QQ→微信）上，一对卡住不能拖住另一对。"""
    gate = asyncio.Event()
    wechat = GatedRecorder("wxg:self", gate, "wxg:a")
    router, _adapters = _two_pair_router(tmp_path, wechat=wechat)
    router.start()
    try:
        router.observe_inbound(make_msg("qq:1001", "卡住", message_id="a1"))
        router.observe_inbound(make_msg("qq:2002", "顺路", message_id="b1"))
        await wait_until(
            lambda: ("wxg:b", WIRE_SHUNLU) in wechat.sent
        )
        # 卡住的那一对还没发出去，另一对已经送达。
        assert wechat.sent == [("wxg:b", WIRE_SHUNLU)]
    finally:
        gate.set()
        await router.stop()


async def test_queue_full_on_one_pair_does_not_drop_other_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """队列按目标群拆：灌满一对只丢自己的，另一对照常排队并送达。"""
    monkeypatch.setattr(mirror_module, "QUEUE_MAXSIZE", 1)
    router, adapters = _two_pair_router(tmp_path)
    # 不 start：队列只进不出，便于把一对灌满。
    router.observe_inbound(make_msg("qq:1001", "一", message_id="a1"))
    router.observe_inbound(make_msg("qq:1001", "二", message_id="a2"))
    router.observe_inbound(make_msg("qq:2002", "三", message_id="b1"))
    assert router.stats()["dropped"] == 1

    router.start()
    try:
        await wait_until(lambda: len(adapters["wechat"].sent) == 2)
    finally:
        await router.stop()
    assert set(adapters["wechat"].sent) == {
        ("wxg:a", WIRE_YI),
        ("wxg:b", WIRE_SAN),
    }


# ---------------------------------------------------------------- 多对路由
async def test_multiple_pairs_route_to_their_own_target(tmp_path: Path) -> None:
    settings = dataclasses.replace(
        make_settings(),
        mirror=MirrorConfig(
            enabled=True,
            pairs=(
                MirrorPairConfig(qq="qq:1001", wechat="wxg:group"),
                MirrorPairConfig(qq="qq:2002", wechat="wxg:other"),
            ),
        ),
    )
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    adapters = {
        "qq": Recorder("qq:999"),
        "wechat": Recorder("wxg:self"),
    }
    router = MirrorRouter(settings, store, adapters)
    router.start()
    try:
        router.observe_inbound(make_msg("qq:1001", "一对", message_id="m1"))
        router.observe_inbound(make_msg("qq:2002", "二对", message_id="m2"))
        await wait_until(lambda: router.stats()["sent"] == 2)
        assert adapters["qq"].sent == []
        assert set(adapters["wechat"].sent) == {
            ("wxg:group", WIRE_YIDUI),
            ("wxg:other", WIRE_ERDUI),
        }
        assert router._direction("qq:1001") == ("qq", "wxg:group")
        assert router._direction("wxg:other") == ("wechat", "qq:2002")
        assert router._direction("qq:999999") is None
    finally:
        await router.stop()


# ------------------------------------------------------------ 热更新
async def test_set_config_swaps_routes_and_nicknames(tmp_path: Path) -> None:
    """面板保存后不重启：旧群不再转发，新群按新目标与新昵称发送。"""
    router, _store, adapters = make_router(tmp_path)
    router.start()
    try:
        assert router._direction(QQ) == ("qq", WXG)
        settings = dataclasses.replace(
            make_settings(),
            mirror=MirrorConfig(
                enabled=True,
                pairs=(MirrorPairConfig(qq="qq:2001", wechat="wxg:other"),),
                nicknames=(("qq:3003", "新昵称"),),
            ),
        )
        await router.set_config(settings)

        assert router._direction(QQ) is None
        assert router._direction("qq:2001") == ("qq", "wxg:other")
        router.observe_inbound(
            make_msg("qq:2001", "换群", user_id="qq:3003", message_id="m1")
        )
        await wait_until(lambda: adapters["wechat"].sent)
        assert adapters["wechat"].sent == [("wxg:other", WIRE_XINNIANCHENG)]
        # worker 与目标群一一对应：旧对收摊、新对起两个，总数仍是 2。
        assert set(router._tasks) == {"qq:2001", "wxg:other"}
    finally:
        await router.stop()


async def test_set_config_starts_and_stops_workers(tmp_path: Path) -> None:
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    adapters = {"qq": Recorder("qq:999"), "wechat": Recorder("wxg:self")}
    router = MirrorRouter(
        dataclasses.replace(make_settings(), mirror=MirrorConfig()),
        store,
        adapters,
    )
    assert router._tasks == {}
    assert router._direction(QQ) is None

    enabled = dataclasses.replace(
        make_settings(),
        mirror=MirrorConfig(
            enabled=True, pairs=(MirrorPairConfig(qq=QQ, wechat=WXG),)
        ),
    )
    await router.set_config(enabled)
    assert len(router._tasks) == 2
    assert router._direction(QQ) == ("qq", WXG)

    await router.set_config(dataclasses.replace(make_settings(), mirror=MirrorConfig()))
    assert router._tasks == {}
    assert router._direction(QQ) is None


async def test_set_config_retires_only_removed_pair_workers(tmp_path: Path) -> None:
    """面板删掉一对：只收摊那一对的 worker，另一对照常转发。"""
    router, adapters = _two_pair_router(tmp_path)
    router.start()
    try:
        assert set(router._tasks) == {"qq:1001", "wxg:a", "qq:2002", "wxg:b"}
        remaining = dataclasses.replace(
            make_settings(),
            mirror=MirrorConfig(
                enabled=True,
                pairs=(MirrorPairConfig(qq="qq:2002", wechat="wxg:b"),),
            ),
        )
        await router.set_config(remaining)

        assert set(router._tasks) == {"qq:2002", "wxg:b"}
        assert set(router._queues) == {"qq:2002", "wxg:b"}
        # 被删掉的对立即不再转发；留下的对不受影响。
        router.observe_inbound(make_msg("qq:1001", "旧对", message_id="old"))
        router.observe_inbound(make_msg("qq:2002", "新对", message_id="new"))
        await wait_until(lambda: adapters["wechat"].sent)
        await asyncio.sleep(0.05)
        assert adapters["wechat"].sent == [("wxg:b", WIRE_XINDUI)]
    finally:
        await router.stop()


# ---------------------------------------------------------------- 落库
async def test_text_success_stores_mirror_row_and_excludes_it_from_context(
    tmp_path: Path,
) -> None:
    router, store, _adapters = make_router(tmp_path)
    router.start()
    try:
        router.observe_inbound(
            make_msg(QQ, "大家好", user_id="qq:2002", message_id="q1")
        )
        await wait_until(lambda: router.stats()["sent"] == 1)
    finally:
        await router.stop()

    rows = store.recent_bot_messages(since_ts=0)
    assert (WXG, "阿鱼：大家好", rows[0][2]) in rows

    row = store._conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND mirror = 1", (WXG,)
    ).fetchone()
    assert row is not None
    assert row["mirror"] == 1
    assert row["is_bot"] == 1
    assert row["timestamp"] > 0

    human = make_msg(WXG, "接一句", user_id="wxg:human", message_id="w1")
    store.add_message(human)
    service = object.__new__(Service)
    service.store = store
    service.settings = router.settings
    assert service._previous_message(human) is None
    assert service._history_before(ChatType.GROUP, WXG, [human]) == []


async def test_pure_media_stores_only_explanation_line(tmp_path: Path) -> None:
    router, store, _adapters = make_router(tmp_path)
    router.start()
    try:
        router.observe_inbound(
            make_msg(
                QQ,
                "[图片]",
                user_id="qq:2002",
                message_id="q1",
                segments=[Segment("image", {"url": "http://example/a.png"})],
                attachments=[Attachment(kind="image", source="http://example/a.png")],
            )
        )
        await wait_until(lambda: router.stats()["sent"] == 1)
    finally:
        await router.stop()
    stored = store._conn.execute(
        "SELECT content FROM messages WHERE chat_id = ? AND mirror = 1", (WXG,)
    ).fetchall()
    assert [row["content"] for row in stored] == ["阿鱼：[图片]"]


# ---------------------------------------------------------------- 错误
async def test_not_connected_retries_three_times_then_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mirror_module, "RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    failing = Recorder(
        "wxg:self",
        fail=lambda: GatewayError("down", error_code="not_connected"),
    )
    router, _store, _adapters = make_router(tmp_path, wechat=failing)
    router.start()
    try:
        router.observe_inbound(make_msg(QQ, "重试", user_id="qq:2002", message_id="q1"))
        await wait_until(lambda: router.stats()["degraded"] == 1)
    finally:
        await router.stop()
    assert failing.calls == 4
    assert router.stats()["retries"] == 3
    assert router.stats()["sent"] == 0


def test_default_retry_window_covers_bridge_restart() -> None:
    """默认退避要盖住 NapCat 重启/桥重连窗口（≥60 秒），否则短暂断线就丢镜像。"""
    assert len(mirror_module.RETRY_BACKOFF_SECONDS) >= 5
    assert sum(mirror_module.RETRY_BACKOFF_SECONDS) >= 60.0


async def test_hook_not_ready_is_retried_until_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mirror_module, "RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    remaining = {"failures": 2}

    def fail() -> GatewayError | None:
        if remaining["failures"] <= 0:
            return None
        remaining["failures"] -= 1
        return GatewayError(
            "hook not ready", error_code="hook_not_ready", not_sent=True
        )

    flaky = Recorder("wxg:self", fail=fail)
    router, _store, _adapters = make_router(tmp_path, wechat=flaky)
    router.start()
    try:
        router.observe_inbound(
            make_msg(QQ, "未就绪", user_id="qq:2002", message_id="q1")
        )
        await wait_until(lambda: router.stats()["sent"] == 1)
    finally:
        await router.stop()
    assert flaky.calls == 3
    assert router.stats()["retries"] == 2
    assert router.stats()["degraded"] == 0


async def test_hook_unknown_without_not_sent_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mirror_module, "RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    failing = Recorder(
        "wxg:self",
        fail=lambda: GatewayError("boom", error_code="hook_unknown"),
    )
    router, _store, _adapters = make_router(tmp_path, wechat=failing)
    router.start()
    try:
        router.observe_inbound(
            make_msg(QQ, "结果不明", user_id="qq:2002", message_id="q1")
        )
        await wait_until(lambda: router.stats()["degraded"] == 1)
    finally:
        await router.stop()
    assert failing.calls == 1
    assert router.stats()["retries"] == 0
    assert router.stats()["sent"] == 0
    assert router.stats()["last_error"] == "hook_unknown"


async def test_stats_reports_queue_depth_workers_and_recent_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """面板/告警共用 stats：积压、worker、最近失败（不含正文）。"""
    monkeypatch.setattr(mirror_module, "RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    failing = Recorder(
        "wxg:self",
        fail=lambda: GatewayError("boom", error_code="hook_unknown"),
    )
    router, _store, _adapters = make_router(tmp_path, wechat=failing)
    router.start()
    try:
        router.observe_inbound(
            make_msg(QQ, "结果不明", user_id="qq:2002", message_id="q1")
        )
        await wait_until(lambda: router.stats()["degraded"] == 1)
        stats = router.stats()
    finally:
        await router.stop()

    assert stats["queues"][WXG] == 0
    assert stats["queues"][QQ] == 0
    assert stats["workers"] == sorted([QQ, WXG])
    assert stats["failures_5m"] == 1
    failure = stats["recent_failures"][0]
    assert failure["code"] == "hook_unknown"
    assert failure["direction"] == "qq->wechat"
    assert failure["target"] == WXG
    assert failure["kind"] == "发送"
    # 失败记录只有错误码与目标，不携带消息正文。
    assert "结果不明" not in json.dumps(stats, ensure_ascii=False)


class MediaRecorder(Recorder):
    """在 Recorder 上补 send_media：记录 (kind, path, name)。"""

    def __init__(self, bot_id: str, *, accept: bool = True) -> None:
        super().__init__(bot_id)
        self.media: list[tuple[str, str, str]] = []
        self.accept = accept

    async def send_media(
        self,
        chat_type: ChatType,
        chat_id: str,
        kind: str,
        path: str,
        *,
        name: str = "",
    ) -> bool:
        self.calls += 1
        if not self.accept:
            return False
        self.media.append((kind, path, name))
        return True


async def test_qq_video_reaches_wechat_as_file(tmp_path: Path) -> None:
    """微信 hook 没有视频原语：物化成功的 mp4 按文件发，而不是只留占位。"""
    ws = tmp_path / "ws"
    (ws / "videos").mkdir(parents=True)
    clip = ws / "videos" / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    settings = dataclasses.replace(
        make_settings(),
        workspace=WorkspaceConfig(dir=str(ws)),
        images=ImagesConfig(enabled=True),
    )
    store = Store(tmp_path / "mirror.db")
    store.init_schema()
    wechat = MediaRecorder("wxg:self")
    router = MirrorRouter(
        settings, store, {"qq": Recorder("qq:999"), "wechat": wechat}
    )
    router.start()
    try:
        router.observe_inbound(
            make_msg(
                QQ,
                "",
                message_id="q1",
                kind="video",
                attachments=[
                    Attachment(kind="video", path=str(clip), name="clip.mp4")
                ],
            )
        )
        await wait_until(lambda: wechat.media)
    finally:
        await router.stop()

    assert wechat.sent == []
    assert wechat.media[0][0] == "file"
    assert wechat.media[0][1].endswith("clip.mp4")
    assert wechat.media[0][2] == "clip.mp4"


async def test_timeout_sends_once_and_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mirror_module, "RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    failing = Recorder("wxg:self", fail=lambda: TimeoutError("late"))
    router, _store, _adapters = make_router(tmp_path, wechat=failing)
    router.start()
    try:
        router.observe_inbound(make_msg(QQ, "超时", user_id="qq:2002", message_id="q1"))
        await wait_until(lambda: router.stats()["degraded"] == 1)
    finally:
        await router.stop()
    assert failing.calls == 1
    assert router.stats()["last_error"] == "timeout"


def test_overflow_drops_newest_and_counts(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    for index in range(mirror_module.QUEUE_MAXSIZE + 1):
        router.observe_inbound(
            make_msg(QQ, f"m{index}", user_id="qq:2002", message_id=f"q{index}")
        )
    assert router._queues[WXG].qsize() == mirror_module.QUEUE_MAXSIZE
    assert router.stats()["dropped"] == 1


async def test_worker_exception_continues_with_next_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, _store, adapters = make_router(tmp_path)
    original = router._render
    calls = {"count": 0}

    def flaky(item: MirrorItem) -> RenderedLines:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return original(item)

    monkeypatch.setattr(router, "_render", flaky)
    router.start()
    try:
        router.observe_inbound(make_msg(QQ, "第一", user_id="qq:2002", message_id="q1"))
        router.observe_inbound(make_msg(QQ, "第二", user_id="qq:2002", message_id="q2"))
        await wait_until(lambda: len(adapters["wechat"].sent) == 1)
    finally:
        await router.stop()
    assert adapters["wechat"].sent == [(WXG, WIRE_DIER)]
    assert "RuntimeError" in str(router.stats()["last_error"])


# ---------------------------------------------------------------- 指纹
def test_human_duplicate_not_swallowed_but_own_echo_consumed(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    router._register_fingerprints(WXG, "hello", ())

    human = make_msg(WXG, "hello", user_id="wxg:human", message_id="w1")
    router.observe_inbound(human)
    assert router._queues[QQ].qsize() == 1
    assert router.stats()["deduped"] == 0

    # 人类消息没有消费指纹；本账号回显在 10s 内仍然被吞掉。
    router._register_fingerprints(WXG, "hello", ())
    echo = make_msg(WXG, "hello", user_id="wxg:self", message_id="w2")
    router.observe_inbound(echo)
    assert router._queues[QQ].qsize() == 1
    assert router.stats()["deduped"] == 1


async def test_expired_item_counts_and_is_not_sent(tmp_path: Path) -> None:
    router, _store, adapters = make_router(tmp_path)
    router.start()
    try:
        router._enqueue(
            MirrorItem(
                source_chat_id=QQ,
                target_chat_id=WXG,
                source_message_id="old",
                nickname="阿鱼",
                is_bot=False,
                text_lines=("过期",),
                attachments=(),
                segments=(),
                source_channel="qq",
                created_at=time.time() - 400,
            ),
        )
        await wait_until(lambda: router.stats()["expired"] == 1)
    finally:
        await router.stop()
    assert adapters["wechat"].sent == []
    assert router.stats()["sent"] == 0


def test_message_exists_blocks_redelivered_frame(tmp_path: Path) -> None:
    router, store, _adapters = make_router(tmp_path)
    message = make_msg(QQ, "已经入库", user_id="qq:2002", message_id="q1")
    store.add_message(message)
    router.observe_inbound(message)
    assert router.stats()["deduped"] == 1
    assert router._queues == {}


def test_private_chat_is_never_mirrored(tmp_path: Path) -> None:
    """私聊不互通：只有群消息进入镜像，即使 chat_id 恰好在配对里也不发。"""
    router, _store, _adapters = make_router(tmp_path)
    private = dataclasses.replace(
        make_msg(QQ, "悄悄话", message_id="p1"), chat_type=ChatType.PRIVATE
    )
    router.observe_inbound(private)

    assert router._queues == {}
    assert router.stats()["sent"] == 0
    assert router.stats()["deduped"] == 0


# ---------------------------------------------------------------- 卡片补标题
CARD_URL = "https://zhuanlan.zhihu.com/p/2108679792132362474?share_code=eP1JNe7gDMxt"


async def test_card_line_gets_page_title_before_delivery(tmp_path: Path) -> None:
    """QQ 卡片只带应用名时，投递前补上页面标题；地址逐字保留、独立成行。"""
    router, store, adapters = make_router(tmp_path)
    calls: list[str] = []

    async def fake_title(url: str) -> str:
        calls.append(url)
        return "如何看待本鱼的记忆力"

    router.link_title = fake_title
    router.start()
    try:
        router.observe_inbound(
            make_msg(
                QQ, f"[卡片] 知乎 {CARD_URL}", user_id="qq:2002", message_id="q1"
            )
        )
        await wait_until(lambda: router.stats()["sent"] == 1)
    finally:
        await router.stop()

    assert calls == [CARD_URL]
    # 地址必须整行出现：折行拆开过（线上旧形态）客户端就认不出链接了。
    assert f"│ {CARD_URL}" in adapters["wechat"].sent[0][1].split("\n")
    row = store._conn.execute(
        "SELECT content FROM messages WHERE chat_id = ? AND mirror = 1", (WXG,)
    ).fetchone()
    assert row["content"] == f"阿鱼：[卡片] 如何看待本鱼的记忆力 {CARD_URL}"


async def test_card_title_kept_when_fetch_returns_nothing(tmp_path: Path) -> None:
    """取不到标题（空串 / 太短）时原样发送，不能凭空改写卡片正文。"""
    router, store, _adapters = make_router(tmp_path)

    async def no_title(_url: str) -> str:
        return "知乎"

    router.link_title = no_title
    router.start()
    try:
        router.observe_inbound(
            make_msg(
                QQ, f"[卡片] 知乎 {CARD_URL}", user_id="qq:2002", message_id="q1"
            )
        )
        await wait_until(lambda: router.stats()["sent"] == 1)
    finally:
        await router.stop()

    row = store._conn.execute(
        "SELECT content FROM messages WHERE chat_id = ? AND mirror = 1", (WXG,)
    ).fetchone()
    assert row["content"] == f"阿鱼：[卡片] 知乎 {CARD_URL}"


async def test_plain_link_message_does_not_fetch_title(tmp_path: Path) -> None:
    """非卡片消息里的链接不抓标题：那条链接交给客户端自己生成预览。"""
    router, _store, _adapters = make_router(tmp_path)
    calls: list[str] = []

    async def fake_title(url: str) -> str:
        calls.append(url)
        return "不该被用到"

    router.link_title = fake_title
    router.start()
    try:
        router.observe_inbound(
            make_msg(QQ, f"看这个 {CARD_URL}", user_id="qq:2002", message_id="q1")
        )
        await wait_until(lambda: router.stats()["sent"] == 1)
    finally:
        await router.stop()

    assert calls == []
