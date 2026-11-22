"""``scripts/wechat_hook_server.py`` 的契约测试（不碰微信、不注入 DLL）。

真实的原生桥只在装了 helper + 登录微信的 Windows 上可用；这里用一个假的
``HookBackend`` 验证 HTTP 契约与错误码，确保 dafeiyu 的 ``HookTransport``
能按 :mod:`dafeiyu.gateway.wechat_bridge.outbound` 约定的字段读懂响应。
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wechat_hook_server.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("wechat_hook_server", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_module()


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.ready = True

    def health(self):
        return {"ok": self.ready, "ready": self.ready, "self_wxid": "wxid_test"}

    def send_text(self, target, text):
        self.calls.append(("text", target, text))
        if not text:
            return hook._not_sent("invalid_argument", "正文为空")
        return {"ok": True, "message_id": ""}

    def send_at(self, target, text, at):
        self.calls.append(("at", target, text, tuple(at)))
        return {"ok": True, "message_id": ""}

    def send_media(self, target, kind, path):
        self.calls.append(("media", target, kind, path))
        return {"ok": True, "message_id": ""}


def _serve(backend, *, token: str = ""):
    server = hook.create_server(backend, "127.0.0.1", 0, token=token)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _post(url: str, payload, *, token: str = ""):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# --------------------------------------------------------------------------- 目标校验


def test_validate_target_accepts_wxid_and_chatroom():
    assert hook._validate_target("filehelper") == ""
    assert hook._validate_target("wxid_abc123") == ""
    assert hook._validate_target("12345678@chatroom") == ""


def test_validate_target_rejects_blank_and_unsafe():
    assert hook._validate_target("") != ""
    assert hook._validate_target("  ") != ""
    assert hook._validate_target("群 名@chatroom") != ""
    assert hook._validate_target("群名@chatroom") != ""
    assert hook._validate_target("a" * 128) != ""
    assert hook._validate_target("a/b") != ""


# --------------------------------------------------------------------------- HTTP 契约


def test_send_text_roundtrip():
    backend = FakeBackend()
    server, base = _serve(backend)
    try:
        status, body = _post(
            f"{base}/send_text",
            {"action": "send_text", "chatroom": "filehelper", "chat_name": "文件传输助手", "text": "hi"},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200
    assert body == {"ok": True, "message_id": ""}
    assert backend.calls == [("text", "filehelper", "hi")]


def test_send_text_empty_is_explicit_not_sent():
    backend = FakeBackend()
    server, base = _serve(backend)
    try:
        _, body = _post(f"{base}/send_text", {"chatroom": "filehelper", "text": ""})
    finally:
        server.shutdown()
        server.server_close()
    assert body["ok"] is False
    assert body["sent"] is False
    assert body["error_code"] == "invalid_argument"


def test_send_at_forwards_members():
    backend = FakeBackend()
    server, base = _serve(backend)
    try:
        _, body = _post(
            f"{base}/send_at",
            {"chatroom": "1@chatroom", "text": "hi", "at": ["wxid_a", "wxid_b"]},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert body["ok"] is True
    assert backend.calls == [("at", "1@chatroom", "hi", ("wxid_a", "wxid_b"))]


def test_send_media_forwards_kind_and_path():
    backend = FakeBackend()
    server, base = _serve(backend)
    try:
        _, body = _post(
            f"{base}/send_media",
            {"chatroom": "1@chatroom", "kind": "image", "path": r"C:\tmp\a.png"},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert body["ok"] is True
    assert backend.calls == [("media", "1@chatroom", "image", r"C:\tmp\a.png")]


def test_health_reports_backend_state():
    backend = FakeBackend()
    server, base = _serve(backend)
    try:
        status, body = _get(f"{base}/health")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200
    assert body["ok"] is True
    assert body["self_wxid"] == "wxid_test"


def _fake_service(*, stop: bool = False, alive: bool = True, error=None):
    class FakeService:
        def __init__(self) -> None:
            self._stop = threading.Event()
            if stop:
                self._stop.set()
            self._thread = types.SimpleNamespace(is_alive=lambda: alive)
            self.last_error = error

    return FakeService()


def test_service_stopped_detects_stop_event_and_dead_thread():
    assert hook._service_stopped(_fake_service(stop=True)) is True
    assert hook._service_stopped(_fake_service(alive=False)) is True
    assert hook._service_stopped(_fake_service()) is False
    assert hook._service_stopped(types.SimpleNamespace()) is False


def test_backend_is_not_ready_after_helper_dies(tmp_path):
    backend = hook.HookBackend(tmp_path)
    backend._service = _fake_service(stop=True, error=RuntimeError("账号变化"))
    backend._ready.set()

    assert backend.ready is False
    health = backend.health()
    assert health["ready"] is False
    assert health["last_error"] == "RuntimeError: 账号变化"


def test_token_is_enforced_when_configured():
    backend = FakeBackend()
    server, base = _serve(backend, token="s3cret")
    try:
        status, body = _post(f"{base}/send_text", {"chatroom": "filehelper", "text": "hi"})
        ok_status, _ = _post(
            f"{base}/send_text", {"chatroom": "filehelper", "text": "hi"}, token="s3cret"
        )
    finally:
        server.shutdown()
        server.server_close()
    assert status == 401
    assert body["sent"] is False
    assert ok_status == 200


def test_unknown_path_is_404_not_crash():
    backend = FakeBackend()
    server, base = _serve(backend)
    try:
        status, body = _post(f"{base}/nope", {"chatroom": "filehelper", "text": "hi"})
    finally:
        server.shutdown()
        server.server_close()
    assert status == 404
    assert body["ok"] is False


def _open_conn(base: str) -> http.client.HTTPConnection:
    host, _, port = base.removeprefix("http://").partition(":")
    return http.client.HTTPConnection(host, int(port), timeout=5)


def test_rejected_post_drains_body_and_keeps_connection_usable():
    """404 也要读掉请求体，否则同一个长连接会被污染。

    ``protocol_version`` 是 HTTP/1.1，处理器返回后还会在同一 socket 上继续读
    下一个请求行；没读走的 body 会被当成请求行解析，把后续请求错配成 400。
    Windows 上关闭带未读数据的连接还会发 RST，客户端表现为
    ``ConnectionAbortedError: WinError 10053``（CI 里真实出现过）。
    """
    backend = FakeBackend()
    server, base = _serve(backend)
    conn = _open_conn(base)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    try:
        conn.request(
            "POST",
            "/nope",
            body=json.dumps({"chatroom": "filehelper", "text": "hi"}),
            headers=headers,
        )
        rejected = conn.getresponse()
        rejected.read()
        assert rejected.status == 404

        conn.request(
            "POST",
            "/send_text",
            body=json.dumps({"chatroom": "filehelper", "text": "ok"}),
            headers=headers,
        )
        accepted = conn.getresponse()
        payload = json.loads(accepted.read().decode("utf-8"))
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
    assert accepted.status == 200
    assert payload["ok"] is True
    assert backend.calls == [("text", "filehelper", "ok")]


def test_unauthorized_post_drains_body_and_keeps_connection_usable():
    """401 同样在鉴权失败时提前回包，必须先把 body 读掉。"""
    backend = FakeBackend()
    server, base = _serve(backend, token="s3cret")
    conn = _open_conn(base)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    try:
        conn.request(
            "POST",
            "/send_text",
            body=json.dumps({"chatroom": "filehelper", "text": "hi"}),
            headers=headers,
        )
        denied = conn.getresponse()
        denied.read()
        assert denied.status == 401

        headers["Authorization"] = "Bearer s3cret"
        conn.request(
            "POST",
            "/send_text",
            body=json.dumps({"chatroom": "filehelper", "text": "ok"}),
            headers=headers,
        )
        accepted = conn.getresponse()
        payload = json.loads(accepted.read().decode("utf-8"))
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
    assert accepted.status == 200
    assert payload["ok"] is True
    assert backend.calls == [("text", "filehelper", "ok")]


def test_invalid_json_does_not_raise():
    backend = FakeBackend()
    server, base = _serve(backend)
    request = urllib.request.Request(
        f"{base}/send_text", data=b"not json", headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status, body = response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
    assert status == 400
    assert body["error_code"] == "invalid_json"


# --------------------------------------------------------------------------- 响应工具


def test_not_sent_shape_matches_transport_expectations():
    body = hook._not_sent("hook_rejected", "x")
    assert body["ok"] is False
    assert body["sent"] is False
    assert body["error_code"] == "hook_rejected"


def test_message_id_reads_nested_report():
    class FakeService:
        last_result = {"report": {"local_id": 42}}

    assert hook._message_id(FakeService()) == "42"
    assert hook._message_id(object()) == ""


# --------------------------------------------------------------------------- @ 解析


class FakeMemberService:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.lookups = 0

    def group_members(self, room):
        self.lookups += 1
        return self.rows


ROWS = [
    {"wxid": "wxid_a", "group_nickname": "小明", "nickname": "小明", "wechat_name": "Ming"},
    {"wxid": "wxid_b", "group_nickname": "", "nickname": "小红", "wechat_name": ""},
]


def test_resolve_mentions_accepts_wxid_directly():
    service = FakeMemberService(ROWS)
    members, reason = hook._resolve_mentions(service, "1@chatroom", ["wxid_zzz"])
    assert members == ["wxid_zzz"]
    assert reason == ""
    assert service.lookups == 0


def test_resolve_mentions_matches_group_nickname_and_wechat_name():
    assert hook._resolve_mentions(FakeMemberService(ROWS), "1@chatroom", ["小明"])[0] == [
        "wxid_a"
    ]
    assert hook._resolve_mentions(FakeMemberService(ROWS), "1@chatroom", ["Ming"])[0] == [
        "wxid_a"
    ]
    assert hook._resolve_mentions(FakeMemberService(ROWS), "1@chatroom", ["小红"])[0] == [
        "wxid_b"
    ]


def test_resolve_mentions_ambiguous_name_fails_closed():
    rows = [
        {"wxid": "wxid_a", "group_nickname": "小明"},
        {"wxid": "wxid_b", "group_nickname": "小明"},
    ]
    members, reason = hook._resolve_mentions(FakeMemberService(rows), "1@chatroom", ["小明"])
    assert members is None
    assert "无法唯一解析" in reason


def test_resolve_mentions_missing_name_fails_closed():
    members, reason = hook._resolve_mentions(FakeMemberService(ROWS), "1@chatroom", ["查无此人"])
    assert members is None
    assert reason


def test_resolve_mentions_empty_at_fails_closed():
    members, reason = hook._resolve_mentions(FakeMemberService(ROWS), "1@chatroom", ["  "])
    assert members is None
    assert reason


# --------------------------------------------------------------------------- helper 版本兼容

HELPER_PROFILES = ("4.1.15.13", "4.2.0.13")


def _helper_root(tmp_path: Path, *, marker: bool = True) -> Path:
    root = tmp_path / "helper"
    native = root / "native"
    native.mkdir(parents=True)
    if marker:
        (native / "text_service.py").write_text("", encoding="utf-8")
    return root


def _fake_helper_profiles(monkeypatch, versions: tuple[str, ...] = HELPER_PROFILES) -> None:
    """把 helper 的版本表换成假的，避免测试依赖真实 helper 仓。"""
    module = types.ModuleType("wxbot_framework.native_profiles")
    module.__dict__["CORE_PROFILES"] = tuple(
        types.SimpleNamespace(version=version) for version in versions
    )
    monkeypatch.setitem(sys.modules, "wxbot_framework.native_profiles", module)
    monkeypatch.setattr(hook, "_helper_import_allowed", lambda: True)


def _boom(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("这种情况不允许导入 helper")


def test_health_exposes_supported_versions_and_ok_compat(tmp_path, monkeypatch):
    _fake_helper_profiles(monkeypatch)
    backend = hook.HookBackend(_helper_root(tmp_path))
    backend.wechat_version = "4.1.15.13"

    body = backend.health()

    assert body["supported_versions"] == HELPER_PROFILES
    assert body["wechat_version"] == "4.1.15.13"
    assert body["helper_compat"] == "ok"
    assert body["helper_version"] == ""
    assert body["helper_fingerprint_available"] is False
    # 其余字段保持不变。
    assert body["ok"] is False
    assert body["ready"] is False
    assert body["self_wxid"] == ""
    assert body["account"] == ""
    assert body["last_error"] == ""


def test_health_marks_unlisted_wechat_version_unsupported(tmp_path, monkeypatch):
    _fake_helper_profiles(monkeypatch)
    backend = hook.HookBackend(_helper_root(tmp_path))
    backend.wechat_version = "4.2.1.7"

    body = backend.health()

    assert body["helper_compat"] == "unsupported"
    assert body["supported_versions"] == HELPER_PROFILES


def test_health_compat_unknown_without_reported_version(tmp_path, monkeypatch):
    _fake_helper_profiles(monkeypatch)
    backend = hook.HookBackend(_helper_root(tmp_path))

    body = backend.health()

    assert body["helper_compat"] == "unknown"
    assert body["wechat_version"] == ""
    assert body["supported_versions"] == HELPER_PROFILES


def test_health_degrades_to_unknown_when_helper_import_fails(tmp_path, monkeypatch):
    _fake_helper_profiles(monkeypatch)
    # ``None`` 会让 import 直接抛 ImportError，模拟 helper 装坏/缺依赖。
    monkeypatch.setitem(sys.modules, "wxbot_framework.native_profiles", None)
    backend = hook.HookBackend(_helper_root(tmp_path))
    backend.wechat_version = "4.1.15.13"

    body = backend.health()

    assert body["helper_compat"] == "unknown"
    assert body["supported_versions"] == ()
    assert body["helper_fingerprint_available"] is False
    assert body["wechat_version"] == "4.1.15.13"
    assert body["ok"] is False


def test_health_skips_helper_import_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "_helper_import_allowed", lambda: True)
    monkeypatch.setattr(hook, "_core_profiles", _boom)
    backend = hook.HookBackend(_helper_root(tmp_path, marker=False))
    backend.wechat_version = "4.1.15.13"

    body = backend.health()

    assert body["helper_compat"] == "unknown"
    assert body["supported_versions"] == ()


def test_health_skips_helper_import_off_windows(tmp_path, monkeypatch):
    _fake_helper_profiles(monkeypatch)
    monkeypatch.setattr(hook, "_helper_import_allowed", lambda: False)
    monkeypatch.setattr(hook, "_core_profiles", _boom)
    backend = hook.HookBackend(_helper_root(tmp_path))
    backend.wechat_version = "4.1.15.13"

    body = backend.health()

    assert body["helper_compat"] == "unknown"
    assert body["supported_versions"] == ()


def test_health_endpoint_reports_helper_fields_as_200(tmp_path, monkeypatch):
    _fake_helper_profiles(monkeypatch)
    backend = hook.HookBackend(_helper_root(tmp_path))
    backend.wechat_version = "4.2.0.13"
    server, base = _serve(backend)
    try:
        status, body = _get(f"{base}/health")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert body["helper_compat"] == "ok"
    assert body["supported_versions"] == list(HELPER_PROFILES)
    assert body["helper_version"] == ""
    assert body["helper_fingerprint_available"] is False
    assert body["wechat_version"] == "4.2.0.13"
    assert body["ok"] is False


def test_health_endpoint_still_200_when_helper_import_fails(tmp_path, monkeypatch):
    _fake_helper_profiles(monkeypatch)
    monkeypatch.setitem(sys.modules, "wxbot_framework.native_profiles", None)
    backend = hook.HookBackend(_helper_root(tmp_path))
    server, base = _serve(backend)
    try:
        status, body = _get(f"{base}/health")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert body["helper_compat"] == "unknown"
    assert body["supported_versions"] == []
    assert body["ok"] is False
    assert body["ready"] is False
    assert body["self_wxid"] == ""
    assert body["account"] == ""
    assert body["last_error"] == ""
