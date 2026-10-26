from __future__ import annotations

from pathlib import Path

from test_control_api import make_control, running

from dafeiyu.models import ChatType


async def test_debug_trigger_returns_service_result_and_builds_message(
    tmp_path: Path,
) -> None:
    control, service, app = make_control(tmp_path)
    captured = []

    def explain(msg, *, seed=None):
        captured.append((msg, seed))
        return {
            "verdict": "model",
            "mode": "rules",
            "reason": "hard",
            "hard_source": "at",
            "checks": [],
            "metrics": {},
        }

    service.explain_trigger = explain

    async with running(app) as client:
        resp = await client.post(
            "/api/debug/trigger",
            json={
                "chat_type": "group",
                "chat_id": 1001,
                "user_id": 2002,
                "nickname": "阿伟",
                "content": "大肥鱼在吗",
                "at_bot": True,
                "reply_to_bot": False,
                "seed": 7,
            },
        )
        data = await resp.json()

    assert resp.status == 200
    assert data["verdict"] == "model"
    assert data["hard_source"] == "at"
    assert len(captured) == 1
    msg, seed = captured[0]
    assert seed == 7
    assert msg.chat_type is ChatType.GROUP
    assert msg.chat_id == 1001
    assert msg.user_id == 2002
    assert msg.nickname == "阿伟"
    assert msg.content == "大肥鱼在吗"
    assert msg.at_bot is True
    assert msg.reply_to_bot is False
    assert msg.message_id
    assert msg.timestamp > 0
    assert control.store.count_messages(ChatType.GROUP, 1001) == 0


async def test_debug_trigger_rejects_bad_payload(tmp_path: Path) -> None:
    control, service, app = make_control(tmp_path)
    service.explain_trigger = lambda msg, *, seed=None: {"verdict": "model"}

    bad_bodies = [
        {"chat_type": "channel", "chat_id": 1, "user_id": 2, "content": "x"},
        {"chat_type": "group", "chat_id": 0, "user_id": 2, "content": "x"},
        {"chat_type": "group", "chat_id": 1, "user_id": 0, "content": "x"},
        {"chat_type": "group", "chat_id": 1, "user_id": 2, "content": "   "},
        {"chat_type": "group", "chat_id": 1, "user_id": 2, "content": "x" * 2001},
        {
            "chat_type": "group",
            "chat_id": 1,
            "user_id": 2,
            "content": "x",
            "at_bot": "true",
        },
        {
            "chat_type": "group",
            "chat_id": 1,
            "user_id": 2,
            "content": "x",
            "reply_to_bot": 1,
        },
        {
            "chat_type": "group",
            "chat_id": 1,
            "user_id": 2,
            "content": "x",
            "nickname": 123,
        },
    ]

    async with running(app) as client:
        for body in bad_bodies:
            resp = await client.post("/api/debug/trigger", json=body)
            assert resp.status == 400, body


async def test_debug_trigger_reports_unsupported_service(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)

    async with running(app) as client:
        resp = await client.post(
            "/api/debug/trigger",
            json={
                "chat_type": "group",
                "chat_id": 1,
                "user_id": 2,
                "content": "x",
            },
        )

    assert resp.status == 503
