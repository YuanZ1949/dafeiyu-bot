"""回复协议 v2：可选 at / attachments，旧格式保持逐字兼容。"""

from __future__ import annotations

from dafeiyu.brain.client import (
    OUTPUT_INSTRUCTION,
    parse_at,
    parse_brain_reply,
    parse_reply_attachments,
)


def test_legacy_json_yields_empty_at_and_attachments() -> None:
    reply = parse_brain_reply('{"speak": true, "reply": "hi"}')

    assert reply.speak is True
    assert reply.reply == "hi"
    assert reply.at == ()
    assert reply.attachments == ()


def test_valid_at_and_one_attachment() -> None:
    reply = parse_brain_reply(
        '{"speak": true, "reply": "看图", '
        '"attachments": [{"type": "image", "path": "replies/a.png", "name": "图"}], '
        '"at": ["阿强"]}'
    )

    assert len(reply.attachments) == 1
    attachment = reply.attachments[0]
    assert attachment.kind == "image"
    assert attachment.path == "replies/a.png"
    assert attachment.name == "图"
    assert attachment.size == 0
    assert attachment.duration == 0
    assert reply.at == ("阿强",)


def test_more_than_two_attachments_truncated() -> None:
    reply = parse_brain_reply(
        '{"speak": true, "reply": "看图", "attachments": ['
        '{"type": "image", "path": "replies/a.png"},'
        '{"type": "file", "path": "replies/b.pdf"},'
        '{"type": "image", "path": "replies/c.png"}]}'
    )

    assert [a.path for a in reply.attachments] == ["replies/a.png", "replies/b.pdf"]


def test_sticker_uses_name_and_ignores_path() -> None:
    parsed = parse_reply_attachments(
        [
            {"type": "sticker", "name": "178", "path": "replies/x.webp"},
            {"type": "sticker", "name": "摸鱼"},
        ]
    )

    assert [(a.kind, a.name, a.path) for a in parsed] == [
        ("sticker", "178", ""),
        ("sticker", "摸鱼", ""),
    ]


def test_sticker_without_name_dropped() -> None:
    parsed = parse_reply_attachments(
        [
            {"type": "sticker"},
            {"type": "sticker", "name": "   "},
            {"type": "sticker", "name": "1"},
        ]
    )

    assert [a.name for a in parsed] == ["1"]


def test_unknown_attachment_type_dropped() -> None:
    reply = parse_brain_reply(
        '{"speak": true, "reply": "看图", "attachments": ['
        '{"type": "exe", "path": "replies/a.exe"},'
        '{"type": "image", "path": "replies/b.png"}]}'
    )

    assert [a.kind for a in reply.attachments] == ["image"]
    assert reply.attachments[0].path == "replies/b.png"


def test_non_string_path_dropped() -> None:
    parsed = parse_reply_attachments(
        [
            {"type": "image", "path": 123},
            {"type": "file", "path": ""},
            {"type": "file", "path": "replies/ok.pdf"},
        ]
    )

    assert [a.path for a in parsed] == ["replies/ok.pdf"]


def test_attachment_path_and_name_limits() -> None:
    parsed = parse_reply_attachments(
        [
            {"type": "image", "path": "a" * 201},
            {"type": "image", "path": "replies/a.png", "name": "n" * 61},
        ]
    )

    assert len(parsed) == 1
    assert len(parsed[0].name) == 60


def test_attachment_size_and_duration_coerced_to_int() -> None:
    parsed = parse_reply_attachments(
        [
            {
                "type": "video",
                "path": "replies/a.mp4",
                "size": "2048",
                "duration": "oops",
            },
            {"type": "voice", "path": "replies/a.mp3", "size": None},
        ]
    )

    assert parsed[0].size == 2048
    assert parsed[0].duration == 0
    assert parsed[1].size == 0


def test_at_dedupes_and_drops_empty() -> None:
    reply = parse_brain_reply(
        '{"speak": true, "reply": "hi", "at": ["阿强", " 阿强 ", "", "阿明", "阿强"]}'
    )

    assert reply.at == ("阿强", "阿明")


def test_at_capped_at_three() -> None:
    assert parse_at(["a", "b", "c", "d"]) == ("a", "b", "c")
    assert parse_at([1, 2]) == ()
    assert parse_at({}) == ()


def test_attachments_as_string_or_object_returns_empty() -> None:
    for raw in ('"x"', '{"type": "image", "path": "replies/a.png"}'):
        reply = parse_brain_reply(
            '{"speak": true, "reply": "hi", "attachments": ' + raw + "}"
        )
        assert reply.attachments == ()

    assert parse_reply_attachments(None) == ()
    assert parse_reply_attachments("x") == ()
    assert parse_reply_attachments({"type": "image"}) == ()


def test_speak_false_still_parsed_with_new_fields() -> None:
    reply = parse_brain_reply(
        '{"speak": false, "reply": "", "reason": "不用接", '
        '"attachments": [{"type": "image", "path": "replies/a.png"}], '
        '"at": ["阿强"]}'
    )

    assert reply.speak is False
    assert reply.reply == ""
    assert reply.reason == "不用接"
    assert [a.path for a in reply.attachments] == ["replies/a.png"]
    assert reply.at == ("阿强",)


def test_salvage_path_yields_empty_at_and_attachments() -> None:
    reply = parse_brain_reply(
        '{"speak": true, "reply": "A", "at": ["阿强"]}'
        '{"speak": true, "reply": "B", "attachments": [{"type": "image", "path": "x.png"}]}'
    )

    assert reply.reply == "A"
    assert reply.at == ()
    assert reply.attachments == ()


def test_output_instruction_keeps_required_format_and_adds_optional_fields() -> None:
    assert '{"read":' in OUTPUT_INSTRUCTION
    assert '"speak": true 或 false' in OUTPUT_INSTRUCTION
    assert '"at":' in OUTPUT_INSTRUCTION
    assert '"attachments":' in OUTPUT_INSTRUCTION
    assert '"forward"' in OUTPUT_INSTRUCTION


def test_forward_nodes_parsed_and_trimmed() -> None:
    parsed = parse_reply_attachments(
        [
            {
                "type": "forward",
                "nodes": [
                    {"name": " 阿明 ", "content": " 原话 "},
                    {"name": "没有正文", "content": "   "},
                    "not-a-dict",
                    {"name": "阿强", "content": "第二句"},
                ],
            }
        ]
    )

    assert [a.kind for a in parsed] == ["forward"]
    assert parsed[0].path == ""
    assert parsed[0].nodes == (
        {"name": "阿明", "content": "原话"},
        {"name": "阿强", "content": "第二句"},
    )


def test_forward_nodes_capped_and_text_clipped() -> None:
    parsed = parse_reply_attachments(
        [
            {
                "type": "forward",
                "nodes": [
                    {"name": f"n{i}", "content": "x" * 300} for i in range(9)
                ],
            }
        ]
    )

    assert len(parsed) == 1
    assert len(parsed[0].nodes) == 5
    assert all(len(node["content"]) == 200 for node in parsed[0].nodes)


def test_forward_without_valid_nodes_dropped() -> None:
    parsed = parse_reply_attachments(
        [
            {"type": "forward"},
            {"type": "forward", "nodes": []},
            {"type": "forward", "nodes": [{"name": "空", "content": ""}]},
            {"type": "forward", "nodes": "oops"},
        ]
    )

    assert parsed == ()
