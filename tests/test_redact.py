from __future__ import annotations

import time

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


def test_redact_keeps_token_usage_key_unchanged() -> None:
    """token_usage 是描述性键，不是凭据。"""
    assert redact("token_usage: 1234") == "token_usage: 1234"


def test_redact_prefixed_credentials() -> None:
    """带前缀的凭据键不能漏：\\b 被下划线挡住的老问题。"""
    assert redact("DEEPSEEK_API_KEY: sk-live-1") == "DEEPSEEK_API_KEY: ***"
    assert redact("my_api_key: sk-live-1") == "my_api_key: ***"
    assert redact("SECRET_KEY=abc") == "SECRET_KEY=***"
    assert redact("my_private_key: abc") == "my_private_key: ***"
    assert redact("ACCESS_KEY=abc") == "ACCESS_KEY=***"


def test_redact_prefixed_credentials_in_json() -> None:
    assert redact('{"DEEPSEEK_API_KEY": "sk-live-1"}') == '{"DEEPSEEK_API_KEY": "***"}'
    assert redact("{'my_api_key': 'sk-live-1'}") == "{'my_api_key': '***'}"
    assert (
        redact('{"my_private_key": "abc", "n": 1}')
        == '{"my_private_key": "***", "n": 1}'
    )


def test_redact_canonicalizes_lowercase_security_headers() -> None:
    """三个安全头输出规范头名，其余键名不归一化。"""
    assert redact("set-cookie: sid=abc; HttpOnly") == "Set-Cookie: ***"
    assert redact("x-control-token: s3cret") == "X-Control-Token: ***"


def test_redact_json_unterminated_value_terminates_fast() -> None:
    """回归：引号值分支曾指数回溯（200 个反斜杠 + 无闭合引号会挂死）。"""
    raw = '{"token": "' + "\\" * 200
    started = time.perf_counter()
    out = redact(raw)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"redact 耗时 {elapsed:.3f}s"
    assert out == raw  # 没有闭合引号，无法安全判定值边界 → 原样返回


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
