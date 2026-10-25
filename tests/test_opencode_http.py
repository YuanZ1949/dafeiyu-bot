import base64
import json
import time

import httpx
import pytest

from dafeiyu.brain.client import FALLBACK_REPLY, BrainError, parse_brain_reply, salvage_reply
from dafeiyu.brain.opencode_http import OpenCodeHttpBrain
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.init_schema()
    yield s
    s.close()


class FakeServer:
    """按 opencode v2 的形状回应：message 列表默认倒序（最新在前）。

    ``steps`` 模式模拟多步生成：prompt 只落下第一步，其余步骤在第一次
    ``wait`` 时补齐；每条 assistant 都带一个 ``webfetch`` tool part，
    供 on_tool 用例使用。
    """

    def __init__(
        self,
        answer: str = '{"speak": true, "reply": "在群聊卖萌", "reason": "ok"}',
        complete: bool = True,
        text_parts: list[dict] | None = None,
        *,
        order: str = "desc",
        unit: float = 1.0,
        missing_tokens: bool = False,
        steps: list[tuple[str, str]] | None = None,
        error: dict | None = None,
    ) -> None:
        self.answer = answer
        self.complete = complete
        self.text_parts = text_parts
        self.order = order
        self.unit = float(unit)
        self.missing_tokens = missing_tokens
        self.steps = list(steps) if steps is not None else None
        self.error = error
        self.pending_steps = list(self.steps[1:]) if self.steps else []
        self.known = {"ses_old"}
        self.messages: dict[str, list[dict]] = {"ses_old": []}
        self.missing: set[str] = set()
        self.prompts: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.auth: list[str] = []
        self.created = 1000.0
        self.created_with: dict = {}
        self.delete_missing: set[str] = set()

    def _stamp(self) -> float:
        self.created += 10
        return self.created * self.unit

    def _append_user(self, sid: str) -> float:
        created = self._stamp()
        self.messages[sid].append(
            {"id": "msg_u", "type": "user", "time": {"created": created}}
        )
        return created

    def _append_step(self, sid: str, step: tuple[str, str]) -> None:
        text, status = step
        tool_status = "completed" if status == "done" else status
        created = self._stamp()
        entry = {
            "id": f"msg_a{len(self.messages[sid])}",
            "type": "assistant",
            "time": {"created": created},
            "content": [
                {"type": "text", "text": text},
                {"type": "tool", "name": "webfetch", "state": {"status": tool_status}},
            ],
        }
        if status == "done":
            entry["time"]["completed"] = created + 5
        if not self.missing_tokens:
            entry["tokens"] = {"input": 100, "output": 35}
        self.messages[sid].append(entry)

    def _idle(self) -> bool:
        if not self.complete or self.pending_steps:
            return False
        return self.steps is None or self.steps[-1][1] == "done"

    @property
    def last_created(self) -> float:
        values = [
            float((m.get("time") or {}).get("created") or 0)
            for items in self.messages.values()
            for m in items
        ]
        return max(values, default=self.created)

    def _slot(self, sid: str) -> list[dict]:
        self.known.add(sid)
        return self.messages.setdefault(sid, [])

    def inject_synthetic(self, sid: str, text: str, *, created: float | None = None) -> None:
        """模拟 POST /synthetic 塞进来的消息：type 不在白名单内。"""
        stamp = self.last_created + 1 if created is None else created
        self._slot(sid).append(
            {
                "id": "msg_syn",
                "type": "synthetic",
                "time": {"created": stamp},
                "content": [{"type": "text", "text": text}],
            }
        )

    def inject_user(self, sid: str, text: str, *, created: float) -> None:
        """窗口外的 user 消息：type 合法，但时间戳不属于本轮。"""
        self._slot(sid).append(
            {
                "id": "msg_out",
                "type": "user",
                "time": {"created": created},
                "content": [{"type": "text", "text": text}],
            }
        )

    def inject_normal_round(self, sid: str) -> None:
        """一轮完全正常的会话：user / assistant / idle 都在窗口内。"""
        items = self._slot(sid)
        for kind in ("user", "assistant", "idle"):
            items.append(
                {"id": f"msg_{kind}", "type": kind, "time": {"created": self._stamp()}}
            )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        self.auth.append(request.headers.get("authorization", ""))

        if path == "/api/session" and request.method == "POST":
            self.created_with = json.loads(request.content.decode("utf-8") or "{}")
            self.known.add("ses_new")
            self.messages["ses_new"] = []
            return httpx.Response(200, json={"data": {"id": "ses_new"}})

        parts = path.strip("/").split("/")

        if request.method == "DELETE" and len(parts) == 3 and parts[1] == "session":
            sid = parts[2]
            if sid in self.delete_missing or sid not in self.known:
                return httpx.Response(404, json={"message": "SessionNotFound"})
            self.known.discard(sid)
            self.messages.pop(sid, None)
            return httpx.Response(200, json={"data": {}})

        if (
            len(parts) >= 5
            and parts[1] == "experimental"
            and parts[2] == "session"
            and parts[4] == "wait"
        ):
            wait_sid = parts[3]
            if wait_sid in self.missing or wait_sid not in self.known:
                return httpx.Response(404, json={"message": "SessionNotFound"})
            if self.pending_steps:
                self._append_step(wait_sid, self.pending_steps.pop(0))
            if self._idle():
                return httpx.Response(204)
            return httpx.Response(200, json={"data": {}})

        sid = parts[2] if len(parts) > 2 else ""
        if sid in self.missing or sid not in self.known:
            return httpx.Response(404, json={"message": "SessionNotFound"})

        if len(parts) > 3 and parts[3] == "prompt":
            self.prompts.append(_body_text(request))
            user_created = self._append_user(sid)
            if self.steps is None:
                content = (
                    self.text_parts
                    if self.text_parts is not None
                    else [{"type": "text", "text": self.answer}]
                )
                created = self._stamp()
                entry = {
                    "id": "msg_a",
                    "type": "assistant",
                    "time": {"created": created},
                    "content": content,
                }
                if self.complete:
                    entry["time"]["completed"] = created + 5
                if not self.missing_tokens:
                    entry["tokens"] = {"input": 100, "output": 35}
                if self.error is not None:
                    entry["finish"] = "error"
                    entry["error"] = self.error
                self.messages[sid].append(entry)
            else:
                self._append_step(sid, self.steps[0])
            return httpx.Response(
                200, json={"data": {"id": "msg_u", "time": {"created": user_created}}}
            )

        if len(parts) > 3 and parts[3] == "message":
            items = list(self.messages[sid])
            if self.order != "asc":
                items.reverse()
            return httpx.Response(200, json={"data": items})

        return httpx.Response(404, json={"message": "not found"})


def _body_text(request: httpx.Request) -> str:
    raw = request.content.decode("utf-8")
    return str(__import__("json").loads(raw).get("text", ""))


def make_brain(
    store: Store,
    server: FakeServer,
    *,
    timeout: float = 2.0,
    password: str = "pw",
) -> OpenCodeHttpBrain:
    return OpenCodeHttpBrain(
        base_url="http://opencode.test",
        store=store,
        password=password,
        agent="deepseek-qq",
        timeout=timeout,
        transport=httpx.MockTransport(server.handler),
    )


async def test_reply_creates_session_and_parses_structured_text(store: Store) -> None:
    server = FakeServer()
    brain = make_brain(store, server)
    reply = await brain.reply(ChatType.PRIVATE, 504567327, "在吗")
    await brain.aclose()

    assert reply.speak is True
    assert reply.reply == "在群聊卖萌"
    assert server.prompts == ["在吗"]
    assert store.get_opencode_session(ChatType.PRIVATE, 504567327) == "ses_new"
    assert ("POST", "/api/session") in server.calls


async def test_reply_reuses_stored_session(store: Store) -> None:
    store.set_opencode_session(ChatType.PRIVATE, 42, "ses_old")
    server = FakeServer()
    brain = make_brain(store, server)
    reply = await brain.reply(ChatType.PRIVATE, 42, "再来")
    await brain.aclose()

    assert reply.reply == "在群聊卖萌"
    assert ("POST", "/api/session") not in server.calls
    assert ("POST", "/api/session/ses_old/prompt") in server.calls


async def test_reply_rebuilds_after_unknown_session(store: Store) -> None:
    store.set_opencode_session(ChatType.PRIVATE, 42, "ses_gone")
    server = FakeServer()
    server.missing.add("ses_gone")
    brain = make_brain(store, server)
    reply = await brain.reply(ChatType.PRIVATE, 42, "在吗")
    await brain.aclose()

    assert reply.reply == "在群聊卖萌"
    assert store.get_opencode_session(ChatType.PRIVATE, 42) == "ses_new"


async def test_reply_times_out_without_completed_message(store: Store) -> None:
    server = FakeServer(complete=False)
    brain = make_brain(store, server, timeout=0.05)
    with pytest.raises(BrainError, match="超时"):
        await brain.reply(ChatType.PRIVATE, 42, "在吗")
    await brain.aclose()


async def test_reply_raises_when_assistant_has_no_text(store: Store) -> None:
    server = FakeServer(text_parts=[{"type": "tool", "name": "read"}])
    brain = make_brain(store, server, timeout=0.05)
    with pytest.raises(BrainError, match="超时"):
        await brain.reply(ChatType.PRIVATE, 42, "在吗")
    await brain.aclose()


async def test_reply_surfaces_provider_error_without_waiting(store: Store) -> None:
    server = FakeServer(
        text_parts=[],
        error={"type": "provider.quota", "message": "Free usage limit reached"},
    )
    brain = make_brain(store, server, timeout=2.0)
    started = time.monotonic()
    with pytest.raises(BrainError) as excinfo:
        await brain.reply(ChatType.PRIVATE, 42, "在吗")
    elapsed = time.monotonic() - started
    await brain.aclose()

    message = str(excinfo.value)
    assert "provider.quota" in message
    assert "Free usage limit reached" in message
    assert elapsed < 1.0


async def test_auth_header_is_basic_opencode(store: Store) -> None:
    server = FakeServer()
    brain = make_brain(store, server, password="s3cret")
    await brain.reply(ChatType.PRIVATE, 42, "在吗")
    await brain.aclose()

    expected = "Basic " + base64.b64encode(b"opencode:s3cret").decode("ascii")
    assert server.auth and server.auth[0] == expected


async def test_no_auth_header_without_password(store: Store) -> None:
    server = FakeServer()
    brain = make_brain(store, server, password="")
    await brain.reply(ChatType.PRIVATE, 42, "在吗")
    await brain.aclose()

    assert server.auth and server.auth[0] == ""


async def test_forget_clears_stored_session(store: Store) -> None:
    store.set_opencode_session(ChatType.PRIVATE, 42, "ses_old")
    server = FakeServer()
    brain = make_brain(store, server)
    await brain.forget(ChatType.PRIVATE, 42)
    await brain.aclose()

    assert not store.get_opencode_session(ChatType.PRIVATE, 42)


# ---- 解析层：三种真实损坏都要能救回来 --------------------------------


def test_salvage_broken_quote() -> None:
    broken = '{"speak": true, "reply": "他说 "你好" 就走了"}'
    assert salvage_reply(broken) == (True, '他说 "你好" 就走了')


def test_salvage_double_json_takes_first_object() -> None:
    doubled = '{"speak": true, "reply": "A"}{"speak": true, "reply": "B"}'
    assert salvage_reply(doubled) == (True, "A")


def test_salvage_returns_none_for_truncated() -> None:
    assert salvage_reply('{"speak": true, "reply": "abc') is None


def test_parse_cleans_replacement_chars(caplog: pytest.LogCaptureFixture) -> None:
    content = '{"speak": true, "reply": "跑\ufffd?opencode 上"}'
    with caplog.at_level("WARNING"):
        reply = parse_brain_reply(content)

    assert "\ufffd" not in reply.reply
    assert "opencode" in reply.reply
    assert any("替换符" in rec.getMessage() for rec in caplog.records)


def test_parse_logs_warning_when_salvaged(caplog: pytest.LogCaptureFixture) -> None:
    broken = '{"speak": true, "reply": "他说 "你好" 就走了"}'
    with caplog.at_level("WARNING"):
        reply = parse_brain_reply(broken)

    assert reply.reply == '他说 "你好" 就走了'
    assert any("抢救" in rec.getMessage() for rec in caplog.records)


def test_parse_logs_warning_when_falling_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        reply = parse_brain_reply('{"speak": true, "reply": "abc', finish_reason="length")

    assert reply.reply == FALLBACK_REPLY
    assert any("降级" in rec.getMessage() for rec in caplog.records)


def test_parse_clean_json_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        reply = parse_brain_reply('{"speak": true, "reply": "好"}')

    assert reply.reply == "好"
    assert not caplog.records
