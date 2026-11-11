import base64
import json
import time
from typing import Any

import httpx
import pytest

from dafeiyu.brain.client import (
    FALLBACK_REPLY,
    BrainError,
    BrainReply,
    parse_brain_reply,
    salvage_reply,
)
from dafeiyu.brain.opencode_http import OpenCodeHttpBrain
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message


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
        round_system: str | None = None,
        round_extra: dict[str, Any] | None = None,
        finish: str = "",
        wait_404: bool = False,
    ) -> None:
        self.answer = answer
        self.complete = complete
        self.text_parts = text_parts
        self.order = order
        self.unit = float(unit)
        self.missing_tokens = missing_tokens
        self.steps = list(steps) if steps is not None else None
        self.error = error
        # 本轮窗口里额外出现的消息：模拟 opencode 自己写的 system 提示 / 外来注入。
        self.round_system = round_system
        self.round_extra = round_extra
        # assistant 的 finish 标记（stop / length / aborted…）：为空表示不带。
        self.finish = finish
        # 旧版服务端没有 /wait 这个实验端点：让它整条 404。
        self.wait_404 = wait_404
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

    def _append_round_extras(self, sid: str) -> None:
        """在 assistant 之后追加本轮额外消息，确保落在审计窗口内。"""
        if self.round_system is not None:
            self.messages[sid].append(
                {
                    "id": "msg_sys",
                    "type": "system",
                    "time": {"created": self._stamp()},
                    "metadata": {
                        "notice": "instructions",
                        "instructionSources": ["core/date"],
                    },
                    "text": self.round_system,
                }
            )
        if self.round_extra is not None:
            entry = dict(self.round_extra)
            entry["time"] = {"created": self._stamp()}
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
            if self.wait_404:
                return httpx.Response(404, json={"message": "not found"})
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
                elif self.finish:
                    entry["finish"] = self.finish
                self.messages[sid].append(entry)
            else:
                self._append_step(sid, self.steps[0])
            self._append_round_extras(sid)
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


# ---- 404 分类：session 不存在 / 端点不存在 ------------------------------


def _creations(server: FakeServer) -> int:
    return sum(1 for call in server.calls if call == ("POST", "/api/session"))


async def test_wait_404_does_not_rebuild_session(store: Store) -> None:
    """/wait 端点 404（旧版服务端没有它）不能被当成 session 失效。"""
    server = FakeServer(wait_404=True)
    brain = make_brain(store, server)

    with pytest.raises(BrainError) as excinfo:
        await brain.reply(ChatType.PRIVATE, "42", "在吗")
    await brain.aclose()

    message = str(excinfo.value)
    assert "wait" in message
    assert "404" in message
    assert excinfo.value.status == 404
    # 只新建了一次：没有因为 /wait 的 404 触发「清映射 + 重建再发一遍」。
    assert _creations(server) == 1
    assert sum(
        1 for _method, path in server.calls if path.endswith("/prompt")
    ) == 1

    # 已有映射时也不清：会话本身是好的，坏的只是 wait 端点。
    store.set_opencode_session(ChatType.PRIVATE, "7", "ses_old")
    other = FakeServer(wait_404=True)
    brain2 = make_brain(store, other)
    with pytest.raises(BrainError):
        await brain2.reply(ChatType.PRIVATE, "7", "在吗")
    await brain2.aclose()

    assert _creations(other) == 0
    assert store.get_opencode_session(ChatType.PRIVATE, "7") == "ses_old"


async def test_session_404_rebuilds_once(store: Store) -> None:
    """session messages 404 → 重建一次；重建后仍 404 → 报出端点，不再循环。"""
    store.set_opencode_session(ChatType.PRIVATE, "42", "ses_gone")
    server = FakeServer()
    server.missing.update({"ses_gone", "ses_new"})
    brain = make_brain(store, server)

    with pytest.raises(BrainError) as excinfo:
        await brain.reply(ChatType.PRIVATE, "42", "在吗")
    await brain.aclose()

    message = str(excinfo.value)
    assert "404" in message
    assert "/api/session/" in message
    # 只重建了一次，第二次 404 直接上报。
    assert _creations(server) == 1


# ---- 失败轮次：新建的 session 要删掉 ------------------------------------


async def test_failed_new_session_is_deleted(store: Store) -> None:
    server = FakeServer(complete=False)
    brain = make_brain(store, server, timeout=0.05)

    with pytest.raises(BrainError, match="超时"):
        await brain.reply(ChatType.PRIVATE, "42", "在吗")
    await brain.aclose()

    deletes = [call for call in server.calls if call == ("DELETE", "/api/session/ses_new")]
    assert len(deletes) == 1
    assert store.get_opencode_session(ChatType.PRIVATE, "42") is None


# ---- 审计：良性 system 不算注入，未知类型仍然算 -------------------------


def _round_messages() -> list[dict[str, Any]]:
    return [
        {"id": "msg_u", "type": "user", "time": {"created": 100.0}},
        {
            "id": "msg_a",
            "type": "assistant",
            "time": {"created": 110.0},
            "content": [{"type": "text", "text": "在"}],
        },
        {
            "id": "msg_sys",
            "type": "system",
            "time": {"created": 120.0},
            "metadata": {
                "notice": "instructions",
                "instructionSources": ["core/date"],
            },
            "text": "Today's date is now: Fri Nov 06 2026",
        },
    ]


async def test_audit_ignores_benign_system_instructions(store: Store) -> None:
    server = FakeServer()
    server.messages["ses_old"] = _round_messages()
    brain = make_brain(store, server)

    assert await brain._audit_round("ses_old", 50.0, 200.0) == []
    await brain.aclose()


async def test_benign_system_does_not_clear_session(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    store.set_opencode_session(ChatType.PRIVATE, "42", "ses_old")
    server = FakeServer(round_system="Today's date is now: Fri Nov 06 2026")
    brain = make_brain(store, server)

    with caplog.at_level("WARNING"):
        reply = await brain.reply(ChatType.PRIVATE, "42", "在吗")
    await brain.aclose()

    assert reply.reply == "在群聊卖萌"
    # 良性 system 提示不算污染：映射保留，下一轮继续复用同一个 session。
    assert store.get_opencode_session(ChatType.PRIVATE, "42") == "ses_old"
    assert "出现外来消息" not in caplog.text


async def test_unknown_message_type_still_clears_session(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    store.set_opencode_session(ChatType.PRIVATE, "42", "ses_old")
    server = FakeServer(
        round_extra={
            "id": "msg_syn",
            "type": "synthetic",
            "content": [{"type": "text", "text": "外部注入"}],
        }
    )
    brain = make_brain(store, server)

    with caplog.at_level("WARNING"):
        reply = await brain.reply(ChatType.PRIVATE, "42", "在吗")
    await brain.aclose()

    assert reply.reply == "在群聊卖萌"
    assert store.get_opencode_session(ChatType.PRIVATE, "42") is None
    assert "msg_syn" in caplog.text


async def test_audit_failure_keeps_reply(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """审计只是旁路：消息接口抖一下，已拿到的回复不能丢。"""

    async def boom(*args: Any, **kwargs: Any) -> list[str]:
        raise RuntimeError("message endpoint blip")

    server = FakeServer()
    brain = make_brain(store, server)
    monkeypatch.setattr(brain, "_audit_round", boom)

    with caplog.at_level("WARNING"):
        reply = await brain.reply(ChatType.PRIVATE, "42", "在吗")
    await brain.aclose()

    assert reply.reply == "在群聊卖萌"
    assert any("审计" in record.getMessage() for record in caplog.records)


# ---- 新 session 第一轮：重放本地最近对话 --------------------------------


def _seed_history(store: Store) -> None:
    """写两条已投递消息，并推进投递游标（模拟上一轮已经聊过）。"""
    for message_id, content, is_bot in (
        ("h1", "昨天聊到鱼缸和水草", False),
        ("h2", "是啊，水草也长起来了", True),
    ):
        store.add_message(
            Message(
                chat_type=ChatType.PRIVATE,
                chat_id="42",
                user_id="qq:1",
                nickname="阿鱼" if not is_bot else "溟月",
                content=content,
                message_id=message_id,
                is_bot=is_bot,
                timestamp=1,
            )
        )
    store.mark_delivered(ChatType.PRIVATE, "42", "h2")


async def test_new_session_replays_recent_local_history(store: Store) -> None:
    _seed_history(store)
    server = FakeServer()
    brain = make_brain(store, server)

    await brain.reply(ChatType.PRIVATE, "42", "接着说")
    await brain.aclose()

    prompt = server.prompts[0]
    assert prompt.startswith("【此前对话】")
    assert "昨天聊到鱼缸和水草" in prompt
    assert "水草也长起来了" in prompt
    assert "不要复述" in prompt
    assert prompt.endswith("接着说")


async def test_existing_session_does_not_replay_history(store: Store) -> None:
    _seed_history(store)
    store.set_opencode_session(ChatType.PRIVATE, "42", "ses_old")
    server = FakeServer()
    brain = make_brain(store, server)

    await brain.reply(ChatType.PRIVATE, "42", "接着说")
    await brain.aclose()

    assert "【此前对话】" not in server.prompts[0]
    assert server.prompts[0] == "接着说"


async def test_replay_only_happens_on_first_new_session_round(store: Store) -> None:
    _seed_history(store)
    server = FakeServer()
    brain = make_brain(store, server)

    await brain.reply(ChatType.PRIVATE, "42", "第一句")
    await brain.reply(ChatType.PRIVATE, "42", "第二句")
    await brain.aclose()

    assert "【此前对话】" in server.prompts[0]
    assert "【此前对话】" not in server.prompts[1]


async def test_history_query_failure_does_not_block_reply(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("db broken")

    monkeypatch.setattr(store, "messages_page", boom)
    server = FakeServer()
    brain = make_brain(store, server)

    reply = await brain.reply(ChatType.PRIVATE, "42", "在吗")
    await brain.aclose()

    assert reply.reply == "在群聊卖萌"
    assert server.prompts[0] == "在吗"


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


async def test_reply_fresh_replays_history_in_a_new_session(store: Store) -> None:
    """额度失败后的兜底：换新 session，把系统提示 + 本地窗口重放一遍。"""
    store.set_opencode_session(ChatType.PRIVATE, 42, "ses_old")
    server = FakeServer()
    brain = make_brain(store, server)

    reply = await brain.reply_fresh(
        ChatType.PRIVATE,
        42,
        "刚才说到哪了",
        system="你是溟月",
        history=[
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早啊"},
        ],
    )
    await brain.aclose()

    prompt = server.prompts[-1]
    assert prompt.startswith("你是溟月")
    assert "user: 早" in prompt
    assert "assistant: 早啊" in prompt
    assert prompt.endswith("刚才说到哪了")
    # 旧会话被丢掉，新会话接管后续轮次。
    assert store.get_opencode_session(ChatType.PRIVATE, 42) == "ses_new"
    assert reply.reply == "在群聊卖萌"


async def test_forget_treats_empty_delete_body_as_success(store: Store) -> None:
    """opencode 的 DELETE 回 204 空 body：不能当成「返回的不是 JSON」而失败。"""
    store.set_opencode_session(ChatType.PRIVATE, 42, "ses_old")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    brain = OpenCodeHttpBrain(
        base_url="http://opencode.test",
        store=store,
        password="pw",
        agent="deepseek-qq",
        timeout=2.0,
        transport=httpx.MockTransport(handler),
    )
    await brain.forget(ChatType.PRIVATE, 42)
    await brain.aclose()

    assert [r.method for r in seen] == ["DELETE"]
    assert not store.get_opencode_session(ChatType.PRIVATE, 42)


async def test_forget_clears_mapping_even_if_remote_delete_fails(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """远端删不掉也必须清映射：否则下一轮又把该忘的 session 接回来。"""
    store.set_opencode_session(ChatType.PRIVATE, 42, "ses_old")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    brain = OpenCodeHttpBrain(
        base_url="http://opencode.test",
        store=store,
        password="pw",
        agent="deepseek-qq",
        timeout=2.0,
        transport=httpx.MockTransport(handler),
    )
    with caplog.at_level("WARNING"):
        await brain.forget(ChatType.PRIVATE, 42)
    await brain.aclose()

    assert not store.get_opencode_session(ChatType.PRIVATE, 42)
    assert "仍清空映射" in caplog.text


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


async def test_truncated_json_uses_fallback(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """finish=length 且 JSON 残缺：走 parse_brain_reply 的截断兜底，别发半截协议。"""
    server = FakeServer(answer='{"speak": true, "reply": "abc', finish="length")
    brain = make_brain(store, server)

    with caplog.at_level("WARNING"):
        reply = await brain.reply(ChatType.PRIVATE, "42", "在吗")
    await brain.aclose()

    assert reply.reply == FALLBACK_REPLY
    assert any("finish_reason=length" in rec.getMessage() for rec in caplog.records)


def test_parse_clean_json_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        reply = parse_brain_reply('{"speak": true, "reply": "好"}')

    assert reply.reply == "好"
    assert not caplog.records


def test_parse_reads_read_and_think_fields() -> None:
    content = (
        '{"read": "大家在聊新模型，有点犹豫", "think": "补一句使用感受", '
        '"speak": true, "reply": "本鱼试过，写短活还行", "reason": "有人问到"}'
    )
    reply = parse_brain_reply(content)

    assert reply.read == "大家在聊新模型，有点犹豫"
    assert reply.think == "补一句使用感受"
    assert reply.speak is True
    assert reply.reply == "本鱼试过，写短活还行"


def test_parse_legacy_format_leaves_read_think_empty() -> None:
    reply = parse_brain_reply('{"speak": false, "reply": "", "reason": "没必要接"}')

    assert reply.speak is False
    assert reply.read == ""
    assert reply.think == ""


def test_parse_read_think_truncated_and_flattened() -> None:
    long = "很长的读" * 30
    reply = parse_brain_reply(f'{{"read": "{long}", "think": "第一行\\n第二行", "reply": "好"}}')

    assert len(reply.read) == 40
    assert "\n" not in reply.think
    assert reply.think == "第一行 第二行"


def test_brain_reply_positional_construction_still_works() -> None:
    reply = BrainReply(True, "好", "理由", 1, 2, True)

    assert reply.read == "" and reply.think == ""


def test_check_response_keeps_http_status() -> None:
    response = httpx.Response(429, text="slow down")
    with pytest.raises(BrainError) as excinfo:
        OpenCodeHttpBrain._check_response(response)
    assert excinfo.value.status == 429


async def test_provider_error_status_code_is_kept(store: Store) -> None:
    """文案里没有 429 时，靠 provider 给的 statusCode 也要能识别限流。"""
    server = FakeServer(
        text_parts=[],
        error={"type": "provider.error", "message": "slow down", "statusCode": 429},
    )
    brain = make_brain(store, server, timeout=2.0)
    with pytest.raises(BrainError) as excinfo:
        await brain.reply(ChatType.PRIVATE, 42, "在吗")
    await brain.aclose()

    assert excinfo.value.status == 429
    assert "429" not in str(excinfo.value)


def test_error_status_accepts_common_field_names() -> None:
    from dafeiyu.brain.opencode_http import _error_status

    assert _error_status({"statusCode": 429}) == 429
    assert _error_status({"status_code": "503"}) == 503
    assert _error_status({"code": 402}) == 402
    assert _error_status({"code": "rate_limit_exceeded"}) is None
    assert _error_status({"status": True}) is None


async def test_named_error_nested_status_is_kept(store: Store) -> None:
    """NamedError 风格（name/data）也要能取到状态码和原因。"""
    server = FakeServer(
        text_parts=[],
        error={
            "name": "APIError",
            "data": {"statusCode": 429, "message": "slow down"},
        },
    )
    brain = make_brain(store, server, timeout=2.0)
    with pytest.raises(BrainError) as excinfo:
        await brain.reply(ChatType.PRIVATE, 42, "在吗")
    await brain.aclose()

    assert excinfo.value.status == 429
    assert "APIError" in str(excinfo.value)
    assert "slow down" in str(excinfo.value)
