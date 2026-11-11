import asyncio
import json

import pytest

from dafeiyu.brain.client import BrainError
from dafeiyu.brain.opencode import OpenCodeBrain, _RunResult
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.init_schema()
    yield s
    s.close()


def test_parse_events_extracts_last_text_and_session() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {"type": "text", "sessionID": "ses_1", "part": {"text": "第一段"}}
            ),
            json.dumps(
                {"type": "text", "sessionID": "ses_1", "part": {"text": "最终"}}
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_1",
                    "part": {"reason": "stop"},
                }
            ),
        ]
    )
    parsed = OpenCodeBrain._parse_events(stdout)
    assert parsed == {
        "session_id": "ses_1",
        "text": "最终",
        "finish_reason": "stop",
        "error": "",
    }


def test_parse_events_reads_error_message() -> None:
    stdout = json.dumps(
        {
            "type": "error",
            "sessionID": "",
            "error": {"type": "unknown", "message": "Session not found"},
        }
    )
    parsed = OpenCodeBrain._parse_events(stdout)
    assert parsed["error"] == "Session not found"
    assert parsed["text"] == ""


def test_build_command_contains_agent_and_session(store: Store) -> None:
    brain = OpenCodeBrain("C:/opencode.exe", store, agent="fish")
    assert brain._build_command("你好", "ses_1") == [
        "C:/opencode.exe",
        "run",
        "--standalone",
        "--agent",
        "fish",
        "--format",
        "json",
        "--session",
        "ses_1",
        "你好",
    ]
    assert brain._build_command("你好", None)[-1] == "你好"
    assert "--session" not in brain._build_command("你好", None)


def test_build_command_contains_server(store: Store) -> None:
    brain = OpenCodeBrain(
        "C:/opencode.exe",
        store,
        agent="fish",
        server_url="http://127.0.0.1:4310",
    )
    assert brain._build_command("你好", "ses_1") == [
        "C:/opencode.exe",
        "run",
        "--server",
        "http://127.0.0.1:4310",
        "--agent",
        "fish",
        "--format",
        "json",
        "--session",
        "ses_1",
        "你好",
    ]
    assert "--standalone" not in brain._build_command("你好", "ses_1")


async def test_reply_creates_then_reuses_session(store: Store) -> None:
    brain = OpenCodeBrain("opencode.exe", store)
    calls: list[tuple[str, str | None]] = []

    async def fake_run(text: str, session_id: str | None) -> _RunResult:
        calls.append((text, session_id))
        return _RunResult(
            0,
            "",
            "",
            session_id or "ses_new",
            '{"speak": true, "reply": "在"}',
            "stop",
            "",
        )

    brain._run = fake_run  # type: ignore[method-assign]

    first = await brain.reply(ChatType.PRIVATE, 42, "在吗")
    second = await brain.reply(ChatType.PRIVATE, 42, "再来")

    assert (first.speak, first.reply) == (True, "在")
    assert (second.speak, second.reply) == (True, "在")
    assert calls == [("在吗", None), ("再来", "ses_new")]
    assert store.get_opencode_session(ChatType.PRIVATE, 42) == "ses_new"


async def test_reply_retries_stale_session(store: Store) -> None:
    store.set_opencode_session(ChatType.PRIVATE, 42, "ses_old")
    brain = OpenCodeBrain("opencode.exe", store)
    calls: list[str | None] = []

    async def fake_run(text: str, session_id: str | None) -> _RunResult:
        calls.append(session_id)
        if session_id:
            return _RunResult(
                1,
                json.dumps(
                    {
                        "type": "error",
                        "sessionID": "",
                        "error": {"type": "unknown", "message": "Session not found"},
                    }
                ),
                "",
                "",
                "",
                "",
                "Session not found",
            )
        return _RunResult(
            0,
            "",
            "",
            "ses_new",
            '{"speak": true, "reply": "新会话"}',
            "stop",
            "",
        )

    brain._run = fake_run  # type: ignore[method-assign]
    result = await brain.reply(ChatType.PRIVATE, 42, "还在吗")

    assert result.reply == "新会话"
    assert calls == ["ses_old", None]
    assert store.get_opencode_session(ChatType.PRIVATE, 42) == "ses_new"


async def test_reply_raises_on_non_stale_error(store: Store) -> None:
    brain = OpenCodeBrain("opencode.exe", store)

    async def fake_run(text: str, session_id: str | None) -> _RunResult:
        return _RunResult(
            1, "", "provider.auth 403", "", "", "", "provider.auth 403"
        )

    brain._run = fake_run  # type: ignore[method-assign]
    with pytest.raises(BrainError, match="OpenCode 调用失败"):
        await brain.reply(ChatType.PRIVATE, 42, "在吗")
    assert store.get_opencode_session(ChatType.PRIVATE, 42) is None


async def test_forget_clears_session(store: Store) -> None:
    store.set_opencode_session(ChatType.GROUP, 7, "ses_group")
    brain = OpenCodeBrain("opencode.exe", store)
    await brain.forget(ChatType.GROUP, 7)
    assert store.get_opencode_session(ChatType.GROUP, 7) is None


class NeverExitingProc:
    """communicate() 永不返回的假进程：只有被 kill 才会结束。"""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self._exited = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        await self._exited.wait()
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0


async def test_cancelled_run_kills_process(monkeypatch, store: Store) -> None:
    """取消（而不是超时）也必须杀掉 CLI 子进程，否则会留下孤儿。"""
    proc = NeverExitingProc()

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    brain = OpenCodeBrain("opencode.exe", store, timeout=60.0)

    task = asyncio.create_task(brain._run("在吗", None))
    await asyncio.sleep(0.05)
    assert proc.killed is False

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.killed is True
    assert proc.returncode is not None
