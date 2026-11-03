"""task 23：控制口的按通道寻址与微信急停/恢复/注入。

全部用假适配器驱动，不碰真实微信；所有 kill_switch_file 都落在 tmp_path，
绝不在仓库里创建 `data/wechat.stop`。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML

from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, create_app
from dafeiyu.ids import is_valid_chat_id, normalize_chat_id
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType


# ------------------------------------------------------------------ id 校验
@pytest.mark.parametrize("value", ["100", "wxg:abc", "qq:1", "a.b-c_d@e"])
def test_chat_id_accepts_strings(value: str) -> None:
    assert is_valid_chat_id(value) is True
    assert normalize_chat_id(value) == value


@pytest.mark.parametrize(
    "value", ["", "a b", "../etc", "a/b", "x" * 129, None, True]
)
def test_chat_id_rejects_invalid(value: Any) -> None:
    assert is_valid_chat_id(value) is False
    with pytest.raises(ValueError):
        normalize_chat_id(value)


# ------------------------------------------------------------------ 假替身
class FakeWeChatAdapter:
    def __init__(self) -> None:
        self._connected = True
        self.resume_calls = 0
        self.dispatched: list[dict[str, Any]] = []
        self.kill_switch_entered = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def enter_kill_switch(self) -> None:
        self.kill_switch_entered += 1
        self._connected = False

    async def resume(self) -> None:
        self.resume_calls += 1
        self._connected = True

    async def dispatch_envelope(self, envelope: dict[str, Any]) -> None:
        self.dispatched.append(envelope)


class FakeSendAdapter:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.sent: list[tuple[ChatType, Any, str]] = []

    async def send(self, chat_type: ChatType, chat_id: Any, text: str) -> None:
        self.sent.append((chat_type, chat_id, text))


class FakeChannelService:
    def __init__(self, adapters: dict[str, Any]) -> None:
        self.adapters = adapters

    def channel_status(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "connected": bool(getattr(adapter, "connected", False)),
                "degraded": False,
                "last_event_at": None,
                "last_disconnect_at": None,
                "last_send_at": None,
            }
            for name, adapter in self.adapters.items()
        }


@asynccontextmanager
async def running(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


# ------------------------------------------------------------------ 夹具
def make_settings(tmp_path: Path, *, admins: frozenset[str] | None = None):
    config = tmp_path / "config.yaml"
    config.write_text(TEST_CONFIG_YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)
    # 所有用例的急停文件都落在 tmp_path：绝不允许写仓库里的 data/wechat.stop。
    settings = replace(
        settings,
        wechat=replace(
            settings.wechat, kill_switch_file=str(tmp_path / "wechat.stop")
        ),
    )
    if admins is not None:
        settings = replace(settings, groups=replace(settings.groups, admins=admins))
    return settings


def make_control(
    tmp_path: Path,
    *,
    adapters: dict[str, Any] | None = None,
    admins: frozenset[str] | None = None,
):
    settings = make_settings(tmp_path, admins=admins)
    store = Store(tmp_path / "test.db")
    store.init_schema()
    service = FakeChannelService(adapters or {})
    control = BrainControl(
        settings,
        store,
        service,
        FakeSendAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )
    return control, service


def _enable_wechat(control, *, allow_test_inject: bool = True):
    control.settings = replace(
        control.settings,
        wechat=replace(
            control.settings.wechat,
            enabled=True,
            allow_test_inject=allow_test_inject,
            kill_switch_file=str(Path(control.settings.wechat.kill_switch_file)),
        ),
    )


# ------------------------------------------------------------------ stop
async def test_wechat_stop_writes_kill_file_and_is_idempotent(
    tmp_path: Path,
) -> None:
    adapter = FakeWeChatAdapter()
    control, _service = make_control(tmp_path, adapters={"wechat": adapter})
    _enable_wechat(control)
    kill = Path(control.settings.wechat.kill_switch_file)
    app = create_app(control, "")

    async with running(app) as client:
        first = await client.post("/api/channels/wechat/stop")
        assert first.status == 200
        assert await first.json() == {"ok": True, "event": "wechat_stopped"}
        assert kill.exists()
        assert kill.read_text(encoding="utf-8") == "stop\n"

        detail = await (await client.get("/api/health/detail")).json()
        assert detail["channels"]["wechat"]["connected"] is False

        second = await client.post("/api/channels/wechat/stop")
        assert second.status == 200
        assert await second.json() == {"ok": True, "event": "wechat_stopped"}

    assert adapter.kill_switch_entered == 2


# ------------------------------------------------------------------ resume
async def test_wechat_resume_requires_kill_file_removed(tmp_path: Path) -> None:
    adapter = FakeWeChatAdapter()
    control, _service = make_control(tmp_path, adapters={"wechat": adapter})
    _enable_wechat(control)
    kill = Path(control.settings.wechat.kill_switch_file)
    app = create_app(control, "")

    async with running(app) as client:
        kill.write_text("stop\n", encoding="utf-8")
        blocked = await client.post("/api/channels/wechat/resume")
        assert blocked.status == 409
        assert adapter.resume_calls == 0

        kill.unlink()
        ok = await client.post("/api/channels/wechat/resume")
        assert ok.status == 200
        assert await ok.json() == {"ok": True, "event": "wechat_resumed"}
        assert adapter.resume_calls == 1


# ------------------------------------------------------------------ inject
async def test_wechat_inject_disabled_is_404(tmp_path: Path) -> None:
    adapter = FakeWeChatAdapter()
    control, _service = make_control(tmp_path, adapters={"wechat": adapter})
    _enable_wechat(control, allow_test_inject=False)
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.post(
            "/api/channels/wechat/inject",
            json={"v": 1, "type": "message", "chat_id": "wxg:abc"},
        )

    assert resp.status == 404
    assert adapter.dispatched == []


async def test_wechat_inject_without_adapter_is_404(tmp_path: Path) -> None:
    control, _service = make_control(tmp_path, adapters={})
    _enable_wechat(control, allow_test_inject=True)
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.post(
            "/api/channels/wechat/inject",
            json={"v": 1, "type": "message", "chat_id": "wxg:abc"},
        )

    assert resp.status == 404


async def test_wechat_inject_dispatches_when_connected(tmp_path: Path) -> None:
    adapter = FakeWeChatAdapter()
    control, _service = make_control(tmp_path, adapters={"wechat": adapter})
    _enable_wechat(control, allow_test_inject=True)
    envelope = {
        "v": 1,
        "type": "message",
        "chat_id": "wxg:abc",
        "message_id": "1:1",
        "text": "x",
    }
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.post("/api/channels/wechat/inject", json=envelope)
        body = await resp.json()

    assert resp.status == 200
    assert body == {"ok": True}
    assert adapter.dispatched == [envelope]


async def test_wechat_inject_rejects_kill_switch_before_dispatch(
    tmp_path: Path,
) -> None:
    adapter = FakeWeChatAdapter()
    control, _service = make_control(tmp_path, adapters={"wechat": adapter})
    _enable_wechat(control, allow_test_inject=True)
    kill = Path(control.settings.wechat.kill_switch_file)
    kill.write_text("stop\n", encoding="utf-8")
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.post(
            "/api/channels/wechat/inject",
            json={"v": 1, "type": "message", "chat_id": "wxg:abc"},
        )

    assert resp.status == 409
    assert adapter.dispatched == []


async def test_wechat_inject_rejects_disconnected_before_dispatch(
    tmp_path: Path,
) -> None:
    adapter = FakeWeChatAdapter()
    adapter._connected = False
    control, _service = make_control(tmp_path, adapters={"wechat": adapter})
    _enable_wechat(control, allow_test_inject=True)
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.post(
            "/api/channels/wechat/inject",
            json={"v": 1, "type": "message", "chat_id": "wxg:abc"},
        )

    assert resp.status == 409
    assert adapter.dispatched == []


# ------------------------------------------------------------ notify/admin
async def test_notify_admin_picks_wechat_adapter_for_wxg_target(
    tmp_path: Path,
) -> None:
    wechat = FakeSendAdapter()
    qq = FakeSendAdapter()
    control, _service = make_control(
        tmp_path,
        adapters={"qq": qq, "wechat": wechat},
        admins=frozenset({"wxg:abc"}),
    )
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.post("/api/notify/admin", json={"text": "告警"})

    assert resp.status == 200
    assert wechat.sent == [(ChatType.PRIVATE, "wxg:abc", "告警")]
    assert qq.sent == []


async def test_notify_admin_picks_qq_adapter_for_bare_numeric_target(
    tmp_path: Path,
) -> None:
    wechat = FakeSendAdapter()
    qq = FakeSendAdapter()
    control, _service = make_control(
        tmp_path,
        adapters={"qq": qq, "wechat": wechat},
        admins=frozenset({"12345"}),
    )
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.post("/api/notify/admin", json={"text": "告警"})

    assert resp.status == 200
    assert qq.sent == [(ChatType.PRIVATE, "12345", "告警")]
    assert wechat.sent == []
