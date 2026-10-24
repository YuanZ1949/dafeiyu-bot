import base64

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
    """按 opencode v2 的形状回应：message 列表倒序（最新在前）。"""

    def __init__(
        self,
        answer: str = '{"speak": true, "reply": "在群聊卖萌", "reason": "ok"}',
        complete: bool = True,
        text_parts: list[dict] | None = None,
    ) -> None:
        self.answer = answer
        self.complete = complete
        self.text_parts = text_parts
        self.known = {"ses_old"}
        self.messages: dict[str, list[dict]] = {"ses_old": []}
        self.missing: set[str] = set()
        self.prompts: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.auth: list[str] = []
        self.created = 1000.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        self.auth.append(request.headers.get("authorization", ""))

        if path == "/api/session" and request.method == "POST":
            self.known.add("ses_new")
            self.messages["ses_new"] = []
            return httpx.Response(200, json={"data": {"id": "ses_new"}})

        parts = path.strip("/").split("/")
        sid = parts[2] if len(parts) > 2 else ""
        if sid in self.missing or sid not in self.known:
            return httpx.Response(404, json={"message": "SessionNotFound"})

        if len(parts) > 3 and parts[3] == "prompt":
            self.prompts.append(httpx.Response(200).json if False else _body_text(request))
            self.created += 10
            user_created = self.created
            self.messages[sid].append(
                {"id": "msg_u", "type": "user", "time": {"created": user_created}}
            )
            self.created += 10
            content = (
                self.text_parts
                if self.text_parts is not None
                else [{"type": "text", "text": self.answer}]
            )
            entry = {
                "id": "msg_a",
                "type": "assistant",
                "time": {"created": self.created},
                "content": content,
            }
            if self.complete:
                entry["time"]["completed"] = self.created + 5
            self.messages[sid].append(entry)
            return httpx.Response(
                200, json={"data": {"id": "msg_u", "time": {"created": user_created}}}
            )

        if len(parts) > 3 and parts[3] == "message":
            return httpx.Response(
                200, json={"data": list(reversed(self.messages[sid]))}
            )

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
