"""图片消息物化：落盘 data/bot-workspace/images/ 并把路径写进 content。

契约（.tamako/contract.json 子项目 2）验收 1/2/3 的测试载体：
- 快路径与 enabled=false 不碰网络；
- 四类来源（url / file-http / 本地文件 / base64）各自成文；
- 无来源、下载失败、超限一律优雅退化为 [图片] 占位；
- 多图按序、文件名防路径注入、惰性清理过期文件；
- 集成：service.handle 入库前改写 content。
"""

import base64
import time
from pathlib import Path

import pytest

from dafeiyu.config import (
    BudgetConfig,
    ConfigError,
    GroupsConfig,
    ImagesConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
    WorkspaceConfig,
    _build_images,
)
from dafeiyu.images import materialize
from dafeiyu.memory.store import Store
from dafeiyu.memory.workspace import Workspace
from dafeiyu.models import ChatType, Message, Segment
from dafeiyu.router import Router
from dafeiyu.service import Service

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def make_settings(tmp_path, **images_overrides) -> Settings:
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
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset()),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
        workspace=WorkspaceConfig(dir=str(tmp_path / "ws")),
        images=ImagesConfig(**images_overrides),
    )


def make_msg(content="[图片]", segs=None, message_id="i1") -> Message:
    if segs is None:
        segs = [Segment("image", {"url": "http://x/a.png"})]
    return Message(
        chat_type=ChatType.GROUP,
        chat_id="qq:1001",
        user_id="qq:2002",
        nickname="阿鱼",
        content=content,
        message_id=message_id,
        segments=segs,
    )


def fake_fetch(data: bytes = PNG, fail: bool = False):
    """返回 (fetch, calls)；fetch 签名与 images._fetch 一致。"""
    calls: list[str] = []

    async def fetch(url: str, timeout: float, max_bytes: int) -> bytes:
        calls.append(url)
        if fail:
            raise RuntimeError("boom")
        return data

    return fetch, calls


def images_dir(tmp_path) -> Path:
    return tmp_path / "ws" / "images"


# ---------------------------------------------------------------- 快路径/开关


async def test_no_image_segments_fast_path(tmp_path):
    msg = make_msg(content="你好", segs=[])
    fetch, calls = fake_fetch()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 0
    assert msg.content == "你好"
    assert calls == []
    assert not images_dir(tmp_path).exists()


async def test_disabled_keeps_placeholder(tmp_path):
    msg = make_msg()
    fetch, calls = fake_fetch()
    got = await materialize(
        msg, make_settings(tmp_path, enabled=False), fetch=fetch
    )
    assert got == 0
    assert msg.content == "[图片]"
    assert calls == []


async def test_annotate_false_keeps_placeholder_without_fetch(tmp_path):
    # 大脑没有 read 工具时，路径字符串只是噪音：不下载、不写盘、不注记。
    msg = make_msg()
    fetch, calls = fake_fetch()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch, annotate=False)
    assert got == 0
    assert msg.content == "[图片]"
    assert calls == []
    assert not images_dir(tmp_path).exists()


# ---------------------------------------------------------------- 四类来源


async def test_http_url_annotates_and_writes_file(tmp_path):
    msg = make_msg()
    fetch, calls = fake_fetch()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 1
    assert calls == ["http://x/a.png"]
    assert msg.content.startswith("[图片]（图存 ")
    assert "/images/" in msg.content
    stored = list(images_dir(tmp_path).iterdir())
    assert len(stored) == 1
    assert stored[0].suffix == ".png"
    assert stored[0].read_bytes() == PNG
    assert stored[0].name in msg.content


async def test_gif_annotation_marks_animated(tmp_path):
    gif = b"GIF89a" + b"0" * 64

    async def fetch(url, timeout, max_bytes):
        return gif

    msg = make_msg()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 1
    assert "动图" in msg.content
    assert next(images_dir(tmp_path).iterdir()).suffix == ".gif"


async def test_file_http_source(tmp_path):
    msg = make_msg(segs=[Segment("image", {"file": "http://x/b.jpg"})])
    fetch, calls = fake_fetch()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 1
    assert calls == ["http://x/b.jpg"]


async def test_local_file_source_copied_without_fetch(tmp_path):
    src = tmp_path / "cache" / "orig.png"
    src.parent.mkdir(parents=True)
    src.write_bytes(PNG)
    msg = make_msg(segs=[Segment("image", {"file": str(src)})])
    fetch, calls = fake_fetch()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 1
    assert calls == []
    stored = list(images_dir(tmp_path).iterdir())
    assert len(stored) == 1
    assert stored[0].read_bytes() == PNG
    assert stored[0].name in msg.content


async def test_base64_source(tmp_path):
    payload = base64.b64encode(PNG).decode("ascii")
    msg = make_msg(segs=[Segment("image", {"base64": payload})])
    fetch, calls = fake_fetch()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 1
    assert calls == []
    stored = list(images_dir(tmp_path).iterdir())
    assert len(stored) == 1
    assert stored[0].suffix == ".png"


# ---------------------------------------------------------------- 优雅退化


async def test_no_source_keeps_placeholder(tmp_path):
    msg = make_msg(segs=[Segment("image", {"file": "abc.image"})])
    fetch, calls = fake_fetch()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 0
    assert msg.content == "[图片]"
    assert calls == []
    assert not images_dir(tmp_path).exists()


async def test_fetch_failure_keeps_placeholder(tmp_path):
    msg = make_msg()
    fetch, _ = fake_fetch(fail=True)
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 0
    assert msg.content == "[图片]"


async def test_oversize_rejected_keeps_placeholder(tmp_path):
    msg = make_msg()
    fetch, _ = fake_fetch(data=PNG)  # 72 字节
    got = await materialize(msg, make_settings(tmp_path, max_bytes=10), fetch=fetch)
    assert got == 0
    assert msg.content == "[图片]"
    assert not images_dir(tmp_path).exists() or not list(images_dir(tmp_path).iterdir())


async def test_exception_in_materialize_never_raises(tmp_path):
    # fetch 抛任意异常都不得冒泡到 service.handle
    msg = make_msg()

    async def angry(url, timeout, max_bytes):
        raise ValueError("nope")

    got = await materialize(msg, make_settings(tmp_path), fetch=angry)
    assert got == 0
    assert msg.content == "[图片]"


# ---------------------------------------------------------------- 多图与命名


async def test_multi_image_replaces_in_order(tmp_path):
    msg = make_msg(content="看图 [图片] 再看 [图片]")
    msg.segments = [
        Segment("image", {"url": "http://x/1.png"}),
        Segment("image", {"url": "http://x/2.png"}),
    ]
    fetch, calls = fake_fetch()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 2
    assert len(calls) == 2
    assert msg.content.count("（图存 ") == 2
    assert len(list(images_dir(tmp_path).iterdir())) == 2
    # 两图来源不同 → 文件名（源 md6）不同
    names = {p.name for p in images_dir(tmp_path).iterdir()}
    assert len(names) == 2


async def test_filename_rejects_path_traversal(tmp_path):
    msg = make_msg(segs=[Segment("image", {"url": "http://evil.test/../../../x.png"})])
    fetch, _ = fake_fetch()
    got = await materialize(msg, make_settings(tmp_path), fetch=fetch)
    assert got == 1
    stored = list(images_dir(tmp_path).iterdir())
    assert len(stored) == 1
    assert stored[0].parent == images_dir(tmp_path)
    assert ".." not in stored[0].name


# ---------------------------------------------------------------- 惰性清理


async def test_cleanup_removes_expired_keeps_fresh(tmp_path):
    directory = images_dir(tmp_path)
    directory.mkdir(parents=True)
    old = directory / "old.jpg"
    old.write_bytes(b"x")
    stale = time.time() - 8 * 86400
    import os

    os.utime(old, (stale, stale))
    fresh = directory / "fresh.jpg"
    fresh.write_bytes(b"y")

    # 本地文件来源：不走网络也能触发落盘 + 清理
    src = tmp_path / "cache" / "new.png"
    src.parent.mkdir(parents=True)
    src.write_bytes(PNG)
    msg = make_msg(segs=[Segment("image", {"file": str(src)})])
    got = await materialize(
        msg, make_settings(tmp_path, retention_days=7)
    )
    assert got == 1
    assert not old.exists()
    assert fresh.exists()


async def test_retention_zero_disables_cleanup(tmp_path):
    directory = images_dir(tmp_path)
    directory.mkdir(parents=True)
    old = directory / "old.jpg"
    old.write_bytes(b"x")
    stale = time.time() - 99 * 86400
    import os

    os.utime(old, (stale, stale))

    src = tmp_path / "cache" / "new.png"
    src.parent.mkdir(parents=True)
    src.write_bytes(PNG)
    msg = make_msg(segs=[Segment("image", {"file": str(src)})])
    await materialize(msg, make_settings(tmp_path, retention_days=0))
    assert old.exists()


# ---------------------------------------------------------------- 配置


def test_build_images_defaults():
    cfg = _build_images({})
    assert cfg.enabled is True
    assert cfg.max_bytes == 10 * 1024 * 1024
    assert cfg.timeout_seconds == 8.0
    assert cfg.retention_days == 7


def test_build_images_overrides():
    cfg = _build_images(
        {"enabled": False, "max_bytes": 1024, "timeout_seconds": 3, "retention_days": 0}
    )
    assert cfg.enabled is False
    assert cfg.max_bytes == 1024
    assert cfg.timeout_seconds == 3.0
    assert cfg.retention_days == 0


@pytest.mark.parametrize(
    "key,value",
    [("max_bytes", 0), ("timeout_seconds", 0), ("retention_days", -1)],
)
def test_build_images_rejects_bad_values(key, value):
    with pytest.raises(ConfigError):
        _build_images({key: value})


# ---------------------------------------------------------------- 集成


class _FakeAdapter:
    bot_id = "qq:999"

    def __init__(self):
        self.sent = []
        self.handler = None

    def on_message(self, handler):
        self.handler = handler

    async def send(self, chat_type, chat_id, text):
        self.sent.append((chat_type, chat_id, text))
        return "bot-1"


class _FakeBrain:
    supports_tools = True

    async def decide(self, system, messages, max_tokens=400):
        from dafeiyu.brain.client import BrainReply

        return BrainReply(True, "在呢", "test", 10, 5)

    async def aclose(self):
        pass


async def test_handle_rewrites_content_before_store(tmp_path):
    """验收 2：完整 service.handle 链路，入库的 content 已带 images/ 路径。"""
    from datetime import datetime, timedelta, timezone

    settings = make_settings(tmp_path)
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    router = Router(settings, store, clock=lambda: datetime(
        2026, 10, 27, 12, 0, tzinfo=timezone(timedelta(hours=8))
    ))
    service = Service(
        settings, store, _FakeBrain(), {"qq": _FakeAdapter()}, router, "你是溟月",
        workspace=Workspace(tmp_path),
    )

    msg = make_msg(content="[图片]", message_id="img-1")
    fetch, _calls = fake_fetch()
    # 把 fetch 注进 materialize：service.handle 内部用默认 fetch，
    # 这里通过 monkeypatch 模块属性实现（pytest fixture 不引入，直接赋值还原）。
    import dafeiyu.images as images_mod

    original = images_mod._default_fetch
    images_mod._default_fetch = fetch
    try:
        await service.handle(msg)
    finally:
        images_mod._default_fetch = original

    stored = store.recent_messages(ChatType.GROUP, "qq:1001", 10)
    mine = [m for m in stored if m.message_id == "img-1"]
    assert mine, "消息没有落库"
    content = mine[0].content
    assert "images/" in content
    assert content.startswith("[图片]（图存 ")
    # 磁盘文件真实存在，且路径指向它
    stored_paths = list(images_dir(tmp_path).iterdir())
    assert len(stored_paths) == 1
    assert stored_paths[0].name in content
    assert stored_paths[0].exists()

async def test_handle_keeps_placeholder_without_tools(tmp_path):
    """没有 read 工具的大脑看不到图：保持占位，不下载、不写盘、不塞路径。"""
    from datetime import datetime, timedelta, timezone

    settings = make_settings(tmp_path)
    store = Store(str(tmp_path / "t2.db"))
    store.init_schema()
    router = Router(
        settings,
        store,
        clock=lambda: datetime(2026, 10, 27, 12, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    brain = _FakeBrain()
    brain.supports_tools = False
    service = Service(settings, store, brain, {"qq": _FakeAdapter()}, router, "test")

    msg = make_msg(content="[图片]", message_id="img-2")
    fetch, calls = fake_fetch()
    import dafeiyu.images as images_mod

    original = images_mod._default_fetch
    images_mod._default_fetch = fetch
    try:
        await service.handle(msg)
    finally:
        images_mod._default_fetch = original

    stored = store.recent_messages(ChatType.GROUP, "qq:1001", 10)
    mine = [m for m in stored if m.message_id == "img-2"]
    assert mine, "消息没有入库"
    assert mine[0].content == "[图片]"
    assert calls == []
    assert not images_dir(tmp_path).exists()
