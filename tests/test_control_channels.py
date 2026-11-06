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
import yaml
from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML

from dafeiyu import control as control_module
from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, create_app
from dafeiyu.ids import is_valid_chat_id, normalize_chat_id
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType
from dafeiyu.settings_local import write_overrides


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


async def test_health_detail_forwards_channel_degraded_reason(tmp_path: Path) -> None:
    """桥侧降级原因要穿到 /api/health/detail，面板才知道为什么「已连接（降级）」。"""
    adapter = FakeWeChatAdapter()
    control, service = make_control(tmp_path, adapters={"wechat": adapter})
    app = create_app(control, "")
    service.channel_status = lambda: {
        "wechat": {
            "connected": True,
            "degraded": True,
            "last_event_at": None,
            "last_disconnect_at": None,
            "last_send_at": None,
            "logged_in": True,
            "desktop": "interactive",
            "reason": "inbound_localdb_unavailable",
        }
    }

    async with running(app) as client:
        detail = await (await client.get("/api/health/detail")).json()

    entry = detail["channels"]["wechat"]
    assert entry["degraded"] is True
    assert entry["reason"] == "inbound_localdb_unavailable"


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


# ------------------------------------------------------------------ mind
async def test_get_mind_defaults_are_off(tmp_path: Path) -> None:
    control, _service = make_control(tmp_path)
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.get("/api/mind")
        body = await resp.json()

    assert resp.status == 200
    assert body["enabled"] is False
    assert body["observer_enabled"] is False
    assert body["effective"] is False
    assert body["brain_supports_observer"] is False
    assert body["group_count"] == 0
    # 白名单为空 = 没有观察对象，估算必须是 0。
    assert body["estimated_daily_calls"] == 0


async def test_get_mind_estimate_multiplies_by_group_count(tmp_path: Path) -> None:
    control, _service = make_control(tmp_path)
    control.settings = replace(
        control.settings,
        groups=replace(
            control.settings.groups, whitelist=("qq:1", "qq:2", "qq:3")
        ),
    )
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.get("/api/mind")
        body = await resp.json()

    assert resp.status == 200
    assert body["group_count"] == 3
    assert body["estimated_daily_calls"] == body["observer_daily_calls"] * 3


async def test_put_mind_writes_overrides_and_get_reflects(
    tmp_path: Path, monkeypatch
) -> None:
    overrides = tmp_path / "settings.local.yaml"
    monkeypatch.setattr(control_module, "OVERRIDES_PATH", overrides)
    write_overrides(
        {"brain": {"mode": "free"}, "groups": {"whitelist": ["qq:1"]}},
        overrides,
    )
    control, _service = make_control(tmp_path)
    config = tmp_path / "config.yaml"
    config_before = config.read_bytes()
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.put(
            "/api/mind",
            json={"observer_enabled": True, "observer_daily_calls": 12},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["observer_enabled"] is True
        assert body["observer_daily_calls"] == 12
        assert body["restart_required"] is True
        fetched = await (await client.get("/api/mind")).json()

    assert fetched["observer_enabled"] is True
    assert fetched["observer_daily_calls"] == 12
    # 正式配置是用户手改文件：面板绝不能重写它。
    assert config.read_bytes() == config_before
    text = overrides.read_text(encoding="utf-8")
    assert "observer_enabled: true" in text
    assert "observer_daily_calls: 12" in text
    after = yaml.safe_load(text)
    assert after["mind"] == {"observer_enabled": True, "observer_daily_calls": 12}
    assert after["brain"] == {"mode": "free"}
    assert after["groups"] == {"whitelist": ["qq:1"]}


@pytest.mark.parametrize(
    "payload",
    [
        {"observer_enabled": "yes"},
        {"observer_daily_calls": -1},
        {"observer_daily_calls": 0},
        {"observer_daily_calls": 1001},
        {"observer_daily_calls": True},
        {"unknown": 1},
    ],
)
async def test_put_mind_rejects_invalid_without_writing(
    tmp_path: Path, monkeypatch, payload: dict[str, Any]
) -> None:
    overrides = tmp_path / "settings.local.yaml"
    monkeypatch.setattr(control_module, "OVERRIDES_PATH", overrides)
    write_overrides({"mind": {"observer_enabled": False}}, overrides)
    control, _service = make_control(tmp_path)
    config = tmp_path / "config.yaml"
    overrides_before = overrides.read_bytes()
    config_before = config.read_bytes()
    app = create_app(control, "")

    async with running(app) as client:
        resp = await client.put("/api/mind", json=payload)
        body = await resp.json()

    assert resp.status == 400
    assert "error" in body
    assert overrides.read_bytes() == overrides_before
    assert config.read_bytes() == config_before


# ------------------------------------------------------------------ workspace 文件
def make_workspace_control(tmp_path: Path, *, enabled: bool = True):
    """把 control 的 workspace.dir 指到 tmp_path/workspace，测试不碰真实工作区。"""
    control, _service = make_control(tmp_path)
    workspace_dir = str(tmp_path / "workspace") if enabled else ""
    control.settings = replace(
        control.settings,
        workspace=replace(control.settings.workspace, dir=workspace_dir),
    )
    return control


async def test_workspace_file_serves_image_bytes(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\nfixture"
    target = tmp_path / "workspace" / "images" / "a.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    app = create_app(make_workspace_control(tmp_path), "")

    async with running(app) as client:
        resp = await client.get(
            "/api/workspace/file", params={"path": "images/a.png"}
        )
        body = await resp.read()

        # Windows 反斜杠写法必须等价于 posix 写法。
        backslash = await client.get(
            "/api/workspace/file", params={"path": "images\\a.png"}
        )
        backslash_body = await backslash.read()

    assert resp.status == 200
    assert body == payload
    assert resp.headers["Content-Type"] == "image/png"
    assert resp.headers["Content-Length"] == str(len(payload))
    assert resp.headers["Cache-Control"] == "private, max-age=60"
    assert backslash.status == 200
    assert backslash_body == payload


async def test_workspace_file_serves_reply_pdf(tmp_path: Path) -> None:
    payload = b"%PDF-1.4 fixture"
    target = tmp_path / "workspace" / "replies" / "b.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    app = create_app(make_workspace_control(tmp_path), "")

    async with running(app) as client:
        resp = await client.get(
            "/api/workspace/file", params={"path": "replies/b.pdf"}
        )
        body = await resp.read()

    assert resp.status == 200
    assert body == payload
    assert resp.headers["Content-Type"] == "application/pdf"


@pytest.mark.parametrize(
    "path",
    [
        "images/../config.yaml",
        "images\\..\\..\\x.png",
        "replies/../../x.png",
        "C:\\Windows\\win.ini",
        "\\\\server\\share\\x.png",
        "images",
        "images/missing.png",
        "images/a.exe",
    ],
)
async def test_workspace_file_rejects_bad_paths(tmp_path: Path, path: str) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "images").mkdir(parents=True)
    (workspace / "images" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    # 越界目标放一份哨兵内容：404 响应里不允许出现它。
    (workspace / "config.yaml").write_text("persona: SECRET\n", encoding="utf-8")
    app = create_app(make_workspace_control(tmp_path), "")

    async with running(app) as client:
        resp = await client.get("/api/workspace/file", params={"path": path})
        body = await resp.read()

    assert resp.status == 404
    assert b"not found" in body
    assert b"SECRET" not in body
    assert b"PNG" not in body
    assert len(body) < 64


async def test_workspace_file_rejects_oversized_file(tmp_path: Path) -> None:
    target = tmp_path / "workspace" / "images" / "huge.png"
    target.parent.mkdir(parents=True)
    # 稀疏写：只落 1 个字节，但文件大小超过 20 MiB。
    with target.open("wb") as handle:
        handle.seek(20 * 1024 * 1024)
        handle.write(b"x")
    app = create_app(make_workspace_control(tmp_path), "")

    async with running(app) as client:
        resp = await client.get(
            "/api/workspace/file", params={"path": "images/huge.png"}
        )
        body = await resp.read()

    assert resp.status == 404
    assert b"not found" in body


@pytest.mark.parametrize("path", ["", "images/", "/etc/passwd"])
async def test_workspace_file_missing_or_empty_path_is_404(
    tmp_path: Path, path: str
) -> None:
    (tmp_path / "workspace" / "images").mkdir(parents=True)
    app = create_app(make_workspace_control(tmp_path), "")

    async with running(app) as client:
        resp = await client.get("/api/workspace/file", params={"path": path})
        no_param = await client.get("/api/workspace/file")

    assert resp.status == 404
    assert no_param.status == 404


async def test_workspace_file_disabled_workspace_is_404(tmp_path: Path) -> None:
    target = tmp_path / "workspace" / "images" / "a.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    app = create_app(make_workspace_control(tmp_path, enabled=False), "")

    async with running(app) as client:
        resp = await client.get(
            "/api/workspace/file", params={"path": "images/a.png"}
        )
        body = await resp.read()

    assert resp.status == 404
    assert b"not found" in body
