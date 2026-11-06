"""Service 多通道路由：发送按 chat_id 前缀选适配器，状态与停机覆盖所有通道。"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, cast

from fakes import FakeChannelAdapter
from test_service import make_settings

from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service

ROOT = Path(__file__).resolve().parents[1]
QQ_CHAT = "qq:1001"
WX_CHAT = "wxg:0d5c1f2a3b4c"


def make_msg(chat_id: str) -> Message:
    user_id = "wxg:u1" if chat_id.startswith("wxg:") else "qq:2002"
    return Message(
        chat_type=ChatType.GROUP,
        chat_id=chat_id,
        user_id=user_id,
        nickname="阿鱼",
        content="/help",
        message_id="m1",
        segments=[],
        is_bot=False,
        at_bot=False,
        reply_to_bot=False,
        timestamp=0,
    )


def make_service(tmp_path: Path):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    qq = FakeChannelAdapter(bot_id="qq:999", chat_id=QQ_CHAT)
    wx = FakeChannelAdapter()
    router = Router(settings, store, clock=lambda: None)
    service = Service(
        settings,
        store,
        None,
        {"qq": qq, "wechat": wx},
        router,
        "test",
    )
    return service, store, qq, wx


async def test_send_routes_to_adapter_of_message_channel(tmp_path):
    service, _store, qq, wx = make_service(tmp_path)

    await service._cmd_help(make_msg(WX_CHAT), is_admin=False)
    await service._cmd_help(make_msg(QQ_CHAT), is_admin=False)

    assert [chat_id for _chat_type, chat_id, _text in wx.sent] == [WX_CHAT]
    assert [chat_id for _chat_type, chat_id, _text in qq.sent] == [QQ_CHAT]


async def test_channel_status_covers_every_adapter(tmp_path):
    service, _store, _qq, wx = make_service(tmp_path)
    wx.connected = False
    wx.degraded = True
    wx.last_event_at = 123.0
    wx.last_send_at = 456.0

    status = service.channel_status()

    assert set(status) == {"qq", "wechat"}
    assert status["wechat"] == {
        "connected": False,
        "degraded": True,
        "last_event_at": 123.0,
        "last_disconnect_at": None,
        "last_send_at": 456.0,
    }
    assert status["qq"]["connected"] is True
    assert status["qq"]["degraded"] is False


def test_channel_status_raw_fallback_adds_wechat_extras(tmp_path):
    """没有 snapshot() 时走裸属性：微信多两个键，QQ 保持 5 键。"""

    class RawChannelAdapter:
        def __init__(self, *, wechat: bool) -> None:
            self.connected = True
            self.degraded = False
            self.last_event_at = 123.0
            self.last_disconnect_at = None
            self.last_send_at = 456.0
            if wechat:
                self.logged_in = False
                self.desktop = "locked"

    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    qq = RawChannelAdapter(wechat=False)
    wx = RawChannelAdapter(wechat=True)
    service = Service(
        settings,
        store,
        None,
        {"qq": qq, "wechat": wx},
        Router(settings, store, clock=lambda: None),
        "test",
    )

    status = service.channel_status()

    assert status["wechat"] == {
        "connected": True,
        "degraded": False,
        "last_event_at": 123.0,
        "last_disconnect_at": None,
        "last_send_at": 456.0,
        "logged_in": False,
        "desktop": "locked",
    }
    assert set(status["qq"]) == {
        "connected",
        "degraded",
        "last_event_at",
        "last_disconnect_at",
        "last_send_at",
    }


def test_channel_status_forwards_health_reason_only_when_present(tmp_path):
    """降级原因有才带键：空串保持旧形状，有值原样透传给面板。"""

    class ReasonAdapter:
        def __init__(self, reason: str) -> None:
            self.connected = True
            self.degraded = bool(reason)
            self.last_event_at = None
            self.last_disconnect_at = None
            self.last_send_at = None
            self.logged_in = True
            self.desktop = "interactive"
            self.health_reason = reason

    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    qq_adapter: Any = ReasonAdapter("")
    wx_adapter: Any = ReasonAdapter("inbound_localdb_unavailable")
    service = Service(
        settings,
        store,
        None,
        {"qq": qq_adapter, "wechat": wx_adapter},
        Router(settings, store, clock=cast(Any, lambda: None)),
        "test",
    )

    status = service.channel_status()

    assert "reason" not in status["qq"]
    assert status["wechat"]["reason"] == "inbound_localdb_unavailable"
    assert status["wechat"]["degraded"] is True


class _RawStatusAdapter:
    """没有 snapshot() 的最小适配器：channel_status 走裸属性分支。"""

    def __init__(self) -> None:
        self.connected = True
        self.degraded = False
        self.last_event_at = 123.0
        self.last_disconnect_at = None
        self.last_send_at = 456.0


class _ReceiptStatusAdapter(_RawStatusAdapter):
    def __init__(self, receipts: dict) -> None:
        super().__init__()
        self._receipts = receipts

    def receipt_stats(self) -> dict:
        return dict(self._receipts)


class _BrokenReceiptStatusAdapter(_RawStatusAdapter):
    def receipt_stats(self) -> dict:
        raise RuntimeError("回执统计坏了")


def _service_with_adapters(tmp_path, adapters):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    service = Service(
        settings,
        store,
        None,
        adapters=adapters,
        router=Router(settings, store, clock=lambda: None),
        persona_text="test",
    )
    return service, store


def test_channel_status_fallback_exposes_receipt_stats(tmp_path):
    receipts = {
        "observed": 3,
        "typed": 1,
        "unverified": 2,
        "failed": 1,
        "total": 7,
        "unverified_rate": 0.333,
        "last_at": 789.0,
    }
    service, _store = _service_with_adapters(
        tmp_path,
        {
            "qq": _RawStatusAdapter(),
            "wechat": _ReceiptStatusAdapter(receipts),
        },
    )

    status = service.channel_status()

    assert status["wechat"]["receipts"] == receipts
    assert status["wechat"]["connected"] is True
    assert "receipts" not in status["qq"]


def test_channel_status_fallback_without_receipt_stats_has_no_key(tmp_path):
    service, _store = _service_with_adapters(
        tmp_path, {"wechat": _RawStatusAdapter()}
    )

    status = service.channel_status()

    assert "receipts" not in status["wechat"]
    assert status["wechat"] == {
        "connected": True,
        "degraded": False,
        "last_event_at": 123.0,
        "last_disconnect_at": None,
        "last_send_at": 456.0,
    }


def test_channel_status_receipt_stats_error_keeps_other_fields(tmp_path):
    service, _store = _service_with_adapters(
        tmp_path, {"wechat": _BrokenReceiptStatusAdapter()}
    )

    status = service.channel_status()

    assert "receipts" not in status["wechat"]
    assert status["wechat"] == {
        "connected": True,
        "degraded": False,
        "last_event_at": 123.0,
        "last_disconnect_at": None,
        "last_send_at": 456.0,
    }



async def test_start_and_stop_visit_every_adapter(tmp_path):
    service, _store, qq, wx = make_service(tmp_path)

    await service.start()
    assert qq.started is True
    assert wx.started is True

    await service.stop()
    assert qq.stopped is True
    assert wx.stopped is True


async def test_bot_chunks_use_channel_bot_id(tmp_path):
    service, store, _qq, _wx = make_service(tmp_path)

    service._store_bot_chunks(make_msg(WX_CHAT), [("在呢", "wxg:1")])

    stored = store.recent_messages(ChatType.GROUP, WX_CHAT, 1)
    assert stored[0].user_id == "wxg:self"


def test_every_service_construction_passes_adapters_map():
    params = list(inspect.signature(Service.__init__).parameters)
    assert params[4] == "adapters"

    violations: list[str] = []
    for path in sorted([*ROOT.joinpath("src").rglob("*.py"), *ROOT.joinpath("tests").rglob("*.py")]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "Service":
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            if "adapter" in keywords:
                violations.append(f"{path}:{node.lineno} 仍用 adapter= 关键字")
            elif "adapters" not in keywords and (
                len(node.args) < 4 or not isinstance(node.args[3], ast.Dict)
            ):
                violations.append(f"{path}:{node.lineno} 第 4 个位置参数不是通道字典")

    assert violations == []
