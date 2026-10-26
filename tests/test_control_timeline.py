from __future__ import annotations

from pathlib import Path

from test_control_api import make_control, running

from dafeiyu.models import ChatType, Message

GROUP = ChatType.GROUP
CHAT_ID = 100


def seed(control) -> None:
    store = control.store
    for index, (content, is_bot) in enumerate(
        (("第一条", False), ("第二条", True), ("第三条", False))
    ):
        store.add_message(
            Message(
                chat_type=GROUP,
                chat_id=CHAT_ID,
                user_id=7,
                nickname="鱼片",
                content=content,
                message_id=f"m{index}",
                is_bot=is_bot,
                at_bot=index == 2,
                reply_to_bot=index == 1,
                timestamp=1_700_000_000 + index,
            )
        )
    store.upsert_summary(GROUP, CHAT_ID, "2026-10-25", "聊了显卡")
    store.add_session_summary(GROUP, CHAT_ID, "上一轮聊了钓鱼")
    store.upsert_impression(GROUP, CHAT_ID, 7, "preference", "喜欢猫", 0.8)
    store.bump_session_stats(GROUP, CHAT_ID, messages=3, tokens=42)
    store.record_usage(GROUP, CHAT_ID, "reply", 10, 5)


async def test_timeline_returns_page_metrics_and_context(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    seed(control)

    async with running(app) as client:
        resp = await client.get(
            f"/api/sessions/group/{CHAT_ID}/timeline?limit=2"
        )
        data = await resp.json()

    assert resp.status == 200
    assert [m["content"] for m in data["messages"]] == ["第二条", "第三条"]
    assert data["has_more"] is True
    assert isinstance(data["next_before"], int)
    assert data["anchor_found"] is False
    assert data["chat"] == {
        "chat_type": "group",
        "chat_id": CHAT_ID,
        "message_total": 3,
        "summary_total": 1,
        "impression_total": 1,
    }
    assert data["session_summary"] == "上一轮聊了钓鱼"
    assert [s["date"] for s in data["summaries"]] == ["2026-10-25"]
    assert [i["content"] for i in data["impressions"]] == ["喜欢猫"]
    assert data["usage"]["calls"] == 1
    assert data["usage"]["tokens"] == 15
    assert data["session"]["delivered_count"] == 3
    assert data["session"]["token_estimate"] == 42
    assert data["messages"][1]["at_bot"] is True
    assert data["messages"][0]["reply_to_bot"] is True


async def test_timeline_supports_before_and_anchor(tmp_path: Path) -> None:
    control, _, app = make_control(tmp_path)
    seed(control)

    async with running(app) as client:
        first = await (
            await client.get(f"/api/sessions/group/{CHAT_ID}/timeline?limit=2")
        ).json()
        older = await (
            await client.get(
                f"/api/sessions/group/{CHAT_ID}/timeline"
                f"?limit=2&before={first['next_before']}"
            )
        ).json()
        anchored = await (
            await client.get(
                f"/api/sessions/group/{CHAT_ID}/timeline?limit=2&anchor=m1"
            )
        ).json()
        missing = await (
            await client.get(
                f"/api/sessions/group/{CHAT_ID}/timeline?limit=2&anchor=nope"
            )
        ).json()

    assert [m["message_id"] for m in older["messages"]] == ["m0"]
    assert older["has_more"] is False
    assert [m["message_id"] for m in anchored["messages"]] == ["m0", "m1"]
    assert anchored["anchor_found"] is True
    assert [m["message_id"] for m in missing["messages"]] == ["m1", "m2"]
    assert missing["anchor_found"] is False


async def test_timeline_rejects_bad_parameters(tmp_path: Path) -> None:
    _, _, app = make_control(tmp_path)

    async with running(app) as client:
        bad_type = await client.get(f"/api/sessions/room/{CHAT_ID}/timeline")
        bad_id = await client.get("/api/sessions/group/0/timeline")
        bad_limit = await client.get(
            f"/api/sessions/group/{CHAT_ID}/timeline?limit=0"
        )
        bad_before = await client.get(
            f"/api/sessions/group/{CHAT_ID}/timeline?before=abc"
        )
        empty_anchor = await client.get(
            f"/api/sessions/group/{CHAT_ID}/timeline?anchor="
        )
        both = await client.get(
            f"/api/sessions/group/{CHAT_ID}/timeline?before=1&anchor=m1"
        )

    assert bad_type.status == 400
    assert bad_id.status == 400
    assert bad_limit.status == 400
    assert bad_before.status == 400
    assert empty_anchor.status == 400
    assert both.status == 400
