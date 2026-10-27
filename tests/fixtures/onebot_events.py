from __future__ import annotations


def text(content: str) -> dict:
    return {"type": "text", "data": {"text": content}}


def at(qq: str | int) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def image() -> dict:
    return {"type": "image", "data": {"file": "abc.jpg"}}


def face() -> dict:
    return {"type": "face", "data": {"id": "1"}}


def reply(message_id: str | int) -> dict:
    return {"type": "reply", "data": {"id": str(message_id)}}


def json_card(embedded: str) -> dict:
    """json 段：data.data 为内嵌 JSON 字符串。"""
    return {"type": "json", "data": {"data": embedded}}


def xml_card(summary: str, xml: str = "<msg>payload</msg>") -> dict:
    """xml 段：data.summary 为 QQ 客户端生成的摘要。"""
    return {"type": "xml", "data": {"data": xml, "summary": summary}}


def forward(*nodes: dict, forward_id: str = "") -> dict:
    """合并转发段：有内联 content 节点，或只有 id。"""
    data: dict = {}
    if forward_id:
        data["id"] = forward_id
    if nodes:
        data["content"] = list(nodes)
    return {"type": "forward", "data": data}


def forward_node(nickname: str, message: list[dict] | str, user_id: int = 0) -> dict:
    return {
        "sender": {"nickname": nickname, "user_id": user_id},
        "time": 1730000000,
        "message": message,
    }


def group_event(
    *,
    segments: list[dict],
    user_id: int = 20002,
    group_id: int = 30003,
    message_id: int = 40004,
    nickname: str = "鱼片",
    card: str = "",
    time: int = 1730000000,
) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "time": time,
        "self_id": 10001,
        "message_id": message_id,
        "group_id": group_id,
        "user_id": user_id,
        "message": segments,
        "sender": {"user_id": user_id, "nickname": nickname, "card": card},
    }


def private_event(
    *,
    segments: list[dict],
    user_id: int = 20002,
    message_id: int = 50005,
    nickname: str = "鱼片",
) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "time": 1730000001,
        "self_id": 10001,
        "message_id": message_id,
        "user_id": user_id,
        "message": segments,
        "sender": {"user_id": user_id, "nickname": nickname, "card": ""},
    }
