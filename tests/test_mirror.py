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

# 他人转发消息的线上形态（wire）：三行圆角气泡。
# 期望串全部硬编码，不靠被测实现生成；宽度按 display_width 手算对齐。
WIRE_SHUNLU = "╭────────────╮\n│ 阿鱼：顺路 │\n╰────────────╯"
WIRE_YI = "╭──────────╮\n│ 阿鱼：一 │\n╰──────────╯"
WIRE_SAN = "╭──────────╮\n│ 阿鱼：三 │\n╰──────────╯"
WIRE_YIDUI = "╭────────────╮\n│ 阿鱼：一对 │\n╰────────────╯"
WIRE_ERDUI = "╭────────────╮\n│ 阿鱼：二对 │\n╰────────────╯"
WIRE_XINNIANCHENG = "╭──────────────╮\n│ 新昵称：换群 │\n╰──────────────╯"
WIRE_XINDUI = "╭────────────╮\n│ 阿鱼：新对 │\n╰────────────╯"
WIRE_DIER = "╭────────────╮\n│ 阿鱼：第二 │\n╰────────────╯"
WIRE_VIDEO = "╭──────────────╮\n│ 阿鱼：[视频] │\n╰──────────────╯"
WIRE_IMAGE = "╭──────────────╮\n│ 阿鱼：[图片] │\n╰──────────────╯"


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


def test_render_long_text_uses_sender_split(tmp_path: Path) -> None:
    router, _store, _adapters = make_router(tmp_path)
    chunks = render(router, make_msg(QQ, "字" * 1200))
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks).replace("阿鱼：", "") == "字" * 1200


# ---------------------------------------------------------------- 消息泡
def test_wire_bubble_shape_for_human(tmp_path: Path) -> None:
    """别人转来的消息线上是三行圆角气泡；入库仍是单行（store 零变化）。"""
    router, _store, _adapters = make_router(tmp_path)
    wire = render_wire(router, make_msg(QQ, "你好"))
    lines = wire[0].splitlines()
    assert lines[0].startswith("╭") and lines[0].endswith("╮")
    assert lines[-1].startswith("╰") and lines[-1].endswith("╯")
    for line in lines[1:-1]:
        assert line.startswith("│") and line.endswith("│")
    assert wire == ["╭────────────╮\n│ 阿鱼：你好 │\n╰────────────╯"]
    # 入库基线（改动前硬编码）：单行「昵称：正文」。
    assert render(router, make_msg(QQ, "你好")) == ["阿鱼：你好"]


def test_wire_bubble_rows_align_with_mixed_scripts(tmp_path: Path) -> None:
    """中英混排时框内每行显示宽度仍相等（用显示宽度而非 len() 对齐）。"""
    router, _store, _adapters = make_router(tmp_path)
    content = "甲" * 15 + "abcdefghij" + "乙" * 15
    wire = render_wire(router, make_msg(QQ, content))
    for entry in wire:
        widths = {display_width(line) for line in entry.splitlines()}
        assert len(widths) == 1
    # 入库形态不受气泡影响：与改动前逐字节一致。
    assert render(router, make_msg(QQ, content)) == ["阿鱼：" + content]


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


def test_bubble_disabled_makes_wire_equal_store(tmp_path: Path) -> None:
    """bubble_enabled=false 时完全退化为今天的形态。"""
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
    assert rendered.wire == rendered.store == ["阿鱼：你好"]


def test_long_text_becomes_full_bubbles(tmp_path: Path) -> None:
    """先按 MAX_CHUNK 切正文，再给每块套一个完整气泡：不会切出半个框。"""
    router, _store, _adapters = make_router(tmp_path)
    full = "阿鱼：" + "字" * 800
    message = make_msg(QQ, "字" * 800)
    rendered = router._render(router._snapshot(message, WXG, "qq"))
    chunks = mirror_module._split(full, mirror_module.MAX_CHUNK)
    assert len(chunks) == 2
    # 线上气泡条数 == 正文按 MAX_CHUNK(500) 切出的块数。
    assert len(rendered.wire) == len(rendered.store) == len(chunks)
    for entry in rendered.wire:
        assert len(entry) <= 4500
        lines = entry.splitlines()
        assert lines[0].startswith("╭") and lines[0].endswith("╮")
        assert lines[-1].startswith("╰") and lines[-1].endswith("╯")
        assert all(
            line.startswith("│") and line.endswith("│")
            for line in lines[1:-1]
        )
    # 入库仍是单行分块，不含框字符，拼接结果与改动前一致。
    assert "".join(rendered.store) == full
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

    assert wechat.sent[0][1] == WIRE_VIDEO
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
