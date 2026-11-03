"""出站后端：配置校验、hook 传输、auto 回落、Sender/客户端闸门。

原则（协议 §7.6）：

- hook 的 ``ok:true`` 只算 ``typed``，真实送达由本地库回读升级；
- auto 只在**可证明没发出去**时回落 UIA；超时/5xx/响应损坏绝不回落，
  否则会重复发送；
- ``outbound=hook/auto`` 时客户端不再要求桌面可交互（锁屏也能下发）。

不装 pyweixin、不碰真桌面、不发真 HTTP：opener / 时钟 / 驱动全部注入。
"""

from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest

from dafeiyu.config import ConfigError, WeChatConfig, load_settings
from dafeiyu.gateway.wechat import WeChatAdapter
from dafeiyu.gateway.wechat_bridge import sender as sender_module
from dafeiyu.gateway.wechat_bridge.__main__ import (
    _default_receipt_verifier,
    _startup_checks,
)
from dafeiyu.gateway.wechat_bridge.outbound import (
    AutoTransport,
    HookTransport,
    build_transport,
)
from dafeiyu.gateway.wechat_bridge.receipt import ReceiptVerifier
from dafeiyu.gateway.wechat_bridge.sender import Prepared, Sender, SendResult
from dafeiyu.models import ChatType

GROUP = "锦绣壹号9#1101"
USERNAME = "12345678@chatroom"
HOOK_URL = "http://127.0.0.1:9999/send"


# ------------------------------------------------------------------ 配置
#: load_settings 要求的基础段（与 tests/test_config.py 的 BASE_YAML 对齐）。
_BASE_YAML = """\
persona:
  name: 溟月
  aliases: [大肥鱼, 小鲸鱼, 溟月, 鱼鱼, 肥鱼]
  max_reply_chars: 120
  max_reply_chars_technical: 300
  os_probability: 0.05
triggers:
  cooldown_seconds: 60
  sample_rate: 0.05
  quiet_hours: ["01:00", "07:00"]
  keywords: [怎么, 为什么, 为啥]
groups:
  whitelist: []
  blacklist: []
budget:
  daily_calls: 200
  daily_tokens: 300000
memory:
  window_size: 20
  impression_topk: 5
  extract_every: 20
logging:
  level: INFO
  dir: logs
"""


def _load_wechat(tmp_path: Path, section: str) -> WeChatConfig:
    env = tmp_path / ".env"
    env.write_text(
        "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n", encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    body = "".join(f"    {line}\n" for line in section.splitlines())
    config.write_text(
        f"{_BASE_YAML}gateway:\n  wechat:\n{body}", encoding="utf-8"
    )
    return load_settings(env_file=env, config_file=config).wechat


def _wechat(**overrides: Any) -> WeChatConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "account_nickname": "小菜鱼",
        "group_allowlist": frozenset({GROUP}),
    }
    values.update(overrides)
    return WeChatConfig(**values)


def test_outbound_defaults_to_uia_and_requires_desktop(tmp_path: Path) -> None:
    wechat = _load_wechat(tmp_path, "enabled: true\n")

    assert wechat.outbound == "uia"
    assert wechat.hook_url == ""
    assert wechat.send_verify is True
    assert wechat.send_verify_timeout_seconds == 8.0
    assert wechat.send_verify_interval_seconds == 0.5
    assert wechat.outbound_requires_desktop is True


def test_outbound_hook_parsed_and_no_desktop_requirement(tmp_path: Path) -> None:
    wechat = _load_wechat(
        tmp_path,
        "\n".join(
            [
                "enabled: true",
                "outbound: hook",
                f"hook_url: {HOOK_URL}",
                "hook_timeout_seconds: 2.5",
                "send_verify: false",
                "send_verify_timeout_seconds: 3",
                "send_verify_interval_seconds: 0.25",
            ]
        ),
    )

    assert wechat.outbound == "hook"
    assert wechat.hook_url == HOOK_URL
    assert wechat.hook_timeout_seconds == 2.5
    assert wechat.send_verify is False
    assert wechat.send_verify_timeout_seconds == 3.0
    assert wechat.send_verify_interval_seconds == 0.25
    assert wechat.outbound_requires_desktop is False


def test_outbound_auto_does_not_require_desktop() -> None:
    assert _wechat(outbound="auto").outbound_requires_desktop is False


@pytest.mark.parametrize(
    ("section", "needle"),
    [
        ("outbound: carrier-pigeon", "outbound"),
        ("outbound: hook\nhook_url: ftp://x/send", "hook_url"),
        ("outbound: hook\nhook_timeout_seconds: 0", "hook_timeout_seconds"),
        ("outbound: uia\nsend_verify_timeout_seconds: -1", "send_verify_timeout"),
        (
            "outbound: uia\nsend_verify_interval_seconds: 0",
            "send_verify_interval",
        ),
    ],
)
def test_invalid_outbound_config_raises(
    tmp_path: Path, section: str, needle: str
) -> None:
    with pytest.raises(ConfigError, match=needle):
        _load_wechat(tmp_path, "enabled: true\n" + section)


@pytest.mark.parametrize("mode", ["hook", "auto"])
def test_startup_checks_require_hook_url(mode: str) -> None:
    with pytest.raises(ConfigError, match="hook_url"):
        _startup_checks(_wechat(outbound=mode, local_db_enabled=True))


def test_startup_checks_hook_requires_localdb() -> None:
    """hook 没有 UIA 兜底：本地库关着就永远解析不出群 username。"""
    with pytest.raises(ConfigError, match="local_db_enabled"):
        _startup_checks(_wechat(outbound="hook", hook_url=HOOK_URL))

    # auto 可以回落 UIA，本地库关着也允许启动。
    _startup_checks(_wechat(outbound="auto", hook_url=HOOK_URL))


def test_startup_checks_accept_hook_with_url_and_uia_without() -> None:
    _startup_checks(
        _wechat(outbound="hook", hook_url=HOOK_URL, local_db_enabled=True)
    )
    _startup_checks(_wechat(outbound="uia"))


# ------------------------------------------------------------------ HTTP 假件
class FakeResponse:
    def __init__(self, body: bytes | str = b'{"ok": true}', status: int = 200) -> None:
        self.status = status
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeOpener:
    """opener 可以是返回 FakeResponse 的工厂，也可以是抛出的异常。"""

    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(float(timeout or 0.0))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _hook(outcome: Any = None, *, resolver: Any = None) -> tuple[HookTransport, FakeOpener]:
    opener = FakeOpener(outcome if outcome is not None else FakeResponse())
    transport = HookTransport(
        HOOK_URL,
        resolver=resolver or (lambda name: USERNAME),
        timeout=1.5,
        opener=opener,
    )
    return transport, opener


# ------------------------------------------------------------------ Hook
def test_hook_prepare_resolves_chatroom_username() -> None:
    transport, _ = _hook()

    prepared = transport.prepare(GROUP)

    assert isinstance(prepared, Prepared)
    assert prepared.chat_name == GROUP
    assert prepared.target == USERNAME


@pytest.mark.parametrize(
    "resolver",
    [None, lambda name: None, lambda name: "", lambda name: (_ for _ in ()).throw(RuntimeError("boom"))],
)
def test_hook_prepare_unresolved_is_explicit_not_sent(resolver: Any) -> None:
    transport = HookTransport(HOOK_URL, resolver=resolver)

    result = transport.prepare(GROUP)

    assert isinstance(result, SendResult)
    assert result.ok is False
    assert result.error_code == "chat_name_unresolved"
    assert result.not_sent is True
    assert transport.requires_desktop is False


def test_hook_prepare_rejects_blank_name() -> None:
    result = HookTransport(HOOK_URL).prepare("   ")

    assert isinstance(result, SendResult)
    assert result.error_code == "invalid_argument"


def test_hook_send_posts_json_and_returns_typed() -> None:
    transport, opener = _hook(FakeResponse('{"ok": true, "message_id": "h-1"}'))

    result = transport.send(
        Prepared(chat_name=GROUP, target=USERNAME), "你好", 0.0
    )

    assert result.ok is True
    assert result.delivered == "typed"
    assert result.message_id == "h-1"
    request = opener.requests[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload == {
        "action": "send_text",
        "chatroom": USERNAME,
        "chat_name": GROUP,
        "text": "你好",
    }
    assert request.get_method() == "POST"
    assert opener.timeouts == [1.5]


def test_hook_send_without_url_is_unavailable_not_unknown() -> None:
    transport = HookTransport("", resolver=lambda name: USERNAME)

    result = transport.send(
        Prepared(chat_name=GROUP, target=USERNAME), "hi", 0.0
    )

    assert result.error_code == "hook_unavailable"
    assert result.not_sent is True


def test_hook_send_explicit_sent_false_keeps_backend_code() -> None:
    transport, _ = _hook(FakeResponse('{"ok": false, "sent": false, "error_code": "not_logged_in"}'))

    result = transport.send(Prepared(chat_name=GROUP, target=USERNAME), "hi", 0.0)

    assert result.ok is False
    assert result.error_code == "not_logged_in"
    assert result.not_sent is True


def test_hook_send_sent_false_without_code_defaults_to_rejected() -> None:
    transport, _ = _hook(FakeResponse('{"ok": false, "sent": false}'))

    result = transport.send(Prepared(chat_name=GROUP, target=USERNAME), "hi", 0.0)

    assert result.error_code == "hook_rejected"


@pytest.mark.parametrize(
    "outcome",
    [
        FakeResponse("not json"),
        FakeResponse("[]"),
        FakeResponse('{"ok": "yes"}'),
        FakeResponse(b"\xff\xfe"),
        FakeResponse('{"ok": true}', status=500),
        FakeResponse('{"ok": true}', status=302),
        TimeoutError("slow"),
        urllib.error.HTTPError(HOOK_URL, 502, "bad gateway", {}, None),
        urllib.error.URLError(TimeoutError("slow")),
        ValueError("opener exploded"),
    ],
)
def test_hook_unknown_outcomes_never_claim_success(outcome: Any) -> None:
    transport, _ = _hook(outcome)

    result = transport.send(Prepared(chat_name=GROUP, target=USERNAME), "hi", 0.0)

    assert result.ok is False
    assert result.error_code == "hook_unknown"
    assert result.not_sent is False


@pytest.mark.parametrize(
    ("outcome", "not_sent"),
    [
        (urllib.error.URLError(ConnectionRefusedError(10061, "refused")), True),
        (ConnectionRefusedError(10061, "refused"), True),
        # 裸 OSError 可能发生在请求中途（写了一半），不能证明没发出去。
        (OSError("socket died"), False),
    ],
)
def test_hook_connection_failures_are_provably_not_sent(
    outcome: Any, not_sent: bool
) -> None:
    transport, _ = _hook(outcome)

    result = transport.send(Prepared(chat_name=GROUP, target=USERNAME), "hi", 0.0)

    assert result.ok is False
    assert result.error_code == "hook_unavailable"
    assert result.not_sent is not_sent


# ------------------------------------------------------------------ Auto
class FakeUia:
    name = "uia"
    requires_desktop = True
    may_use_desktop = True

    def __init__(self, outcome: Any = None) -> None:
        self._outcome = outcome
        self.calls: list[tuple[str, Any]] = []

    def prepare(self, chat_name: str) -> Any:
        self.calls.append(("prepare", chat_name))
        if isinstance(self._outcome, SendResult):
            return self._outcome
        return Prepared(chat_name=chat_name, target="uia")

    def send(self, prepared: Prepared, text: str, delay: float) -> SendResult:
        self.calls.append(("send", text))
        return SendResult(True, None, "observed", "uia-1", prepared.chat_name)


def _auto(outcome: Any = None) -> tuple[AutoTransport, HookTransport, FakeUia]:
    hook, _ = _hook(outcome)
    uia = FakeUia()
    return AutoTransport(hook, lambda: uia), hook, uia


def test_auto_uses_hook_when_it_works() -> None:
    transport, _, uia = _auto(FakeResponse('{"ok": true}'))

    result = transport.send(Prepared(chat_name=GROUP), "hi", 0.0)

    assert result.ok is True
    assert result.delivered == "typed"
    assert uia.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        FakeResponse('{"ok": false, "sent": false, "error_code": "hook_rejected"}'),
        # 自定义 error_code 只要 sent:false 也是「可证明没发」，同样要回落。
        FakeResponse('{"ok": false, "sent": false, "error_code": "not_logged_in"}'),
        urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
    ],
)
def test_auto_falls_back_only_when_provably_not_sent(outcome: Any) -> None:
    transport, _, uia = _auto(outcome)

    result = transport.send(Prepared(chat_name=GROUP), "hi", 0.0)

    assert result.ok is True
    assert result.message_id == "uia-1"
    assert [call[0] for call in uia.calls] == ["prepare", "send"]


def test_auto_falls_back_when_chatroom_unresolved() -> None:
    hook = HookTransport(HOOK_URL, resolver=lambda name: None, opener=FakeOpener(FakeResponse()))
    uia = FakeUia()
    transport = AutoTransport(hook, lambda: uia)

    result = transport.send(Prepared(chat_name=GROUP), "hi", 0.0)

    assert result.ok is True
    assert uia.calls[0] == ("prepare", GROUP)


@pytest.mark.parametrize(
    "outcome",
    [
        TimeoutError("slow"),
        urllib.error.HTTPError(HOOK_URL, 500, "server error", {}, None),
        FakeResponse("garbage"),
    ],
)
def test_auto_does_not_fall_back_when_result_unknown(outcome: Any) -> None:
    """结果不明时回落 = 可能发两次；宁可失败等人工确认。"""
    transport, _, uia = _auto(outcome)

    result = transport.send(Prepared(chat_name=GROUP), "hi", 0.0)

    assert result.ok is False
    assert result.error_code == "hook_unknown"
    assert uia.calls == []


def test_auto_returns_uia_prepare_failure() -> None:
    hook, _ = _hook(urllib.error.URLError(ConnectionRefusedError(10061, "refused")))
    failure = SendResult(False, "desktop_unavailable", "failed", chat_name=GROUP)
    uia = FakeUia(outcome=failure)
    transport = AutoTransport(hook, lambda: uia)

    result = transport.send(Prepared(chat_name=GROUP), "hi", 0.0)

    assert result is failure
    assert uia.calls == [("prepare", GROUP)]


def test_auto_rejects_blank_name_and_lazily_reuses_uia() -> None:
    transport, _, uia = _auto(urllib.error.URLError(ConnectionRefusedError(10061, "refused")))

    blank = transport.prepare("  ")
    assert isinstance(blank, SendResult)
    assert blank.error_code == "invalid_argument"

    transport.send(Prepared(chat_name=GROUP), "one", 0.0)
    transport.send(Prepared(chat_name=GROUP), "two", 0.0)
    assert [call[1] for call in uia.calls if call[0] == "send"] == ["one", "two"]


# ------------------------------------------------------------------ build
def test_build_transport_uia_returns_none() -> None:
    assert build_transport(_wechat(), uia_driver=object()) is None


def test_build_transport_hook_uses_localdb_resolver() -> None:
    poller = SimpleNamespace(
        localdb=SimpleNamespace(resolve_username=lambda name: USERNAME)
    )

    transport = build_transport(
        _wechat(outbound="hook", hook_url=HOOK_URL, hook_timeout_seconds=3.0),
        uia_driver=object(),
        poller=poller,
    )

    assert isinstance(transport, HookTransport)
    assert transport.requires_desktop is False
    prepared = transport.prepare(GROUP)
    assert isinstance(prepared, Prepared)
    assert prepared.target == USERNAME


def test_build_transport_auto_and_unknown_fallback() -> None:
    auto = build_transport(
        _wechat(outbound="auto", hook_url=HOOK_URL), uia_driver=object()
    )
    assert isinstance(auto, AutoTransport)

    assert build_transport(_wechat(outbound="bogus"), uia_driver=object()) is None


def test_build_transport_without_localdb_cannot_resolve() -> None:
    transport = build_transport(
        _wechat(outbound="hook", hook_url=HOOK_URL),
        uia_driver=object(),
        poller=SimpleNamespace(),
    )

    assert isinstance(transport, HookTransport)
    result = transport.prepare(GROUP)
    assert isinstance(result, SendResult)
    assert result.error_code == "chat_name_unresolved"


# ------------------------------------------------------------------ Sender
class NoUiaDriver:
    """hook 后端不该碰到的驱动：任何调用都让测试失败。"""

    def __init__(self) -> None:
        self.forced = 0

    def force_global_config(self) -> None:
        self.forced += 1

    def open_chat(self, name: str) -> Any:
        raise AssertionError(f"hook 后端不应开窗：{name}")

    def send_text(self, *args: Any) -> None:
        raise AssertionError("hook 后端不应打字")

    def snapshot(self, handle: Any) -> Any:
        raise AssertionError("hook 后端不应回轮询")


def test_sender_hook_backend_sends_while_desktop_locked(monkeypatch) -> None:
    monkeypatch.setattr(sender_module, "desktop_state", lambda: "locked")
    driver = NoUiaDriver()
    transport, opener = _hook(FakeResponse('{"ok": true, "message_id": "h-2"}'))
    sender = Sender(
        _wechat(outbound="hook", hook_url=HOOK_URL),
        driver=driver,
        transport=transport,
        sleep=lambda seconds: None,
    )
    try:
        result = sender.submit(GROUP, "锁屏也要发")
    finally:
        sender.close()

    assert result.ok is True
    assert result.delivered == "typed"
    assert result.message_id == "h-2"
    assert driver.forced == 0
    assert len(opener.requests) == 1


def test_sender_hook_backend_skips_global_config_fix() -> None:
    driver = NoUiaDriver()
    transport, _ = _hook()

    sender = Sender(
        _wechat(outbound="hook", hook_url=HOOK_URL),
        driver=driver,
        transport=transport,
        sleep=lambda seconds: None,
    )
    sender.close()

    assert driver.forced == 0


# ------------------------------------------------------------------ 回执接线
def test_default_receipt_verifier_requires_switch_and_localdb() -> None:
    source = SimpleNamespace(available=True)

    assert (
        _default_receipt_verifier(
            _wechat(send_verify=False), SimpleNamespace(localdb=source)
        )
        is None
    )
    assert _default_receipt_verifier(_wechat(), SimpleNamespace(localdb=None)) is None
    assert _default_receipt_verifier(_wechat(), SimpleNamespace()) is None

    verifier = _default_receipt_verifier(
        _wechat(
            send_verify_timeout_seconds=4.0,
            send_verify_interval_seconds=0.25,
        ),
        SimpleNamespace(localdb=source),
    )

    assert isinstance(verifier, ReceiptVerifier)
    assert verifier._timeout == 4.0
    assert verifier._interval == 0.25


# ------------------------------------------------------------------ 客户端闸门
class FakeWs:
    def __init__(self, adapter: WeChatAdapter, receipt: dict[str, Any]) -> None:
        self._adapter = adapter
        self._receipt = receipt
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        command = json.loads(raw)
        self.sent.append(command)
        future = self._adapter._pending[command["id"]]
        future.set_result({"v": 1, "id": command["id"], **self._receipt})


def _connected_adapter(config: WeChatConfig) -> tuple[WeChatAdapter, FakeWs]:
    adapter = WeChatAdapter(config)
    adapter._socket_open = True
    adapter._health_connected = True
    adapter._last_health_at = time.monotonic()
    adapter._desktop = "locked"
    ws = FakeWs(
        adapter,
        {
            "ok": True,
            "error_code": None,
            "delivered": "observed",
            "message_id": "3:9",
        },
    )
    adapter._ws = ws
    return adapter, ws


async def test_client_sends_with_locked_desktop_when_outbound_hook() -> None:
    adapter, ws = _connected_adapter(_wechat(outbound="hook", hook_url=HOOK_URL))

    result = await adapter.send(ChatType.GROUP, "wxg:abc", "锁屏也能发")

    assert result == "3:9"
    assert ws.sent[-1]["action"] == "send_text"
    assert ws.sent[-1]["params"]["text"] == "锁屏也能发"


async def test_client_still_blocks_locked_desktop_for_uia() -> None:
    adapter, ws = _connected_adapter(_wechat())

    with pytest.raises(Exception, match="桌面不可交互"):
        await adapter.send(ChatType.GROUP, "wxg:abc", "不该发出")
    assert ws.sent == []
