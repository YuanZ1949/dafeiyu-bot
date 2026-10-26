from __future__ import annotations

import pytest

from dafeiyu.supervisor.redact import redact


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Authorization: Bearer abc.def", "Authorization: ***"),
        ("authorization: Basic dXNlcjpwYXNz", "Authorization: ***"),
        ("X-Control-Token: s3cret", "X-Control-Token: ***"),
        ("DEEPSEEK_API_KEY=sk-live-123", "DEEPSEEK_API_KEY=***"),
        ("NAPCAT_QUICK_PASSWORD=abc123", "NAPCAT_QUICK_PASSWORD=***"),
        ("api_key: sk-1", "api_key: ***"),
        ("password = hunter2", "password = ***"),
        ("Set-Cookie: sid=abc; HttpOnly", "Set-Cookie: ***"),
    ],
)
def test_redact_secrets(raw: str, expected: str) -> None:
    assert redact(raw) == expected


def test_redact_keeps_chinese_token_usage() -> None:
    assert redact("今日 token 用量 1234") == "今日 token 用量 1234"


def test_redact_preserves_original_key_case() -> None:
    """普通键名不做大小写归一化，原样保留。"""
    assert redact("API_KEY: sk-1") == "API_KEY: ***"
    assert redact("Password=hunter2") == "Password=***"


def test_redact_bearer_anywhere() -> None:
    assert redact("curl -H 'x' Bearer abc.def") == "curl -H 'x' Bearer ***"


def test_redact_handles_multiline_and_blank_lines() -> None:
    raw = "start\nAuthorization: Bearer abc\n\nSet-Cookie: sid=1\ntail"
    assert redact(raw) == "start\nAuthorization: ***\n\nSet-Cookie: ***\ntail"


def test_redact_leaves_plain_text_untouched() -> None:
    raw = "普通日志 1234 ABC"
    assert redact(raw) == raw


def test_redact_json_quoted_keys() -> None:
    """JSON 事件流里的引号键同样要打码，且不破坏结构。"""
    raw = '{"token": "sk-1", "note": "token 用量", "n": 1}'
    assert redact(raw) == '{"token": "***", "note": "token 用量", "n": 1}'


def test_redact_keeps_line_endings() -> None:
    assert redact("Authorization: Bearer a\r\nnext") == "Authorization: ***\r\nnext"


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer abc.def",
        "DEEPSEEK_API_KEY=sk-live-123",
        '{"password": "hunter2"}',
        "今日 token 用量 1234",
    ],
)
def test_redact_is_idempotent(raw: str) -> None:
    once = redact(raw)
    assert redact(once) == once
