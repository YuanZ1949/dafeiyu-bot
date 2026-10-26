from __future__ import annotations

import pytest

from dafeiyu.supervisor.auth import DashboardAuth, safe_next_path


def make_auth(**kwargs) -> DashboardAuth:
    options = {
        "enabled": True,
        "username": "admin",
        "password": "correct-horse-battery",
        "session_seconds": 3600,
        "secret": b"test-secret-32-bytes-long-000000",
    }
    options.update(kwargs)
    return DashboardAuth(**options)


def test_session_cookie_round_trip_and_expiry() -> None:
    auth = make_auth()

    token = auth.issue_cookie(now=1_000)

    assert auth.verify_cookie(token, now=1_001) is True
    assert auth.verify_cookie(token, now=4_600) is True
    assert auth.verify_cookie(token, now=4_601) is False


def test_session_cookie_rejects_tampering_and_other_secret() -> None:
    auth = make_auth()
    token = auth.issue_cookie(now=1_000)

    body, signature = token.split(".", 1)
    tampered = f"{body}.{signature[:-1]}x"
    assert auth.verify_cookie(tampered, now=1_001) is False

    other = make_auth(secret=b"another-secret-32-bytes-long-0000")
    assert other.verify_cookie(token, now=1_001) is False
    assert auth.verify_cookie("not-a-cookie", now=1_001) is False


def test_credentials_require_both_username_and_password() -> None:
    auth = make_auth()

    assert auth.verify_credentials("admin", "correct-horse-battery") is True
    assert auth.verify_credentials("admin", "wrong") is False
    assert auth.verify_credentials("someone", "correct-horse-battery") is False
    assert auth.verify_credentials("", "") is False


def test_rate_limit_blocks_after_five_failures_and_clears() -> None:
    auth = make_auth()

    for index in range(4):
        auth.record_failure("127.0.0.1", now=1_000 + index)
    assert auth.is_rate_limited("127.0.0.1", now=1_004) is False

    auth.record_failure("127.0.0.1", now=1_005)
    assert auth.is_rate_limited("127.0.0.1", now=1_005) is True

    auth.clear_failures("127.0.0.1")
    assert auth.is_rate_limited("127.0.0.1", now=1_006) is False

    for index in range(5):
        auth.record_failure("127.0.0.1", now=1_100 + index)
    assert auth.is_rate_limited("127.0.0.1", now=1_406) is False


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("", "/"),
        ("/", "/"),
        ("/api/status?x=1", "/api/status?x=1"),
        ("/login?next=%2Fapi%2Fstatus", "/login?next=%2Fapi%2Fstatus"),
        ("//evil.example", "/"),
        ("http://evil.example", "/"),
        ("https://evil.example/x", "/"),
        ("/\\evil.example", "/"),
        ("/%2f%2fevil.example", "/"),
    ],
)
def test_safe_next_path_only_allows_site_relative_paths(
    supplied: str, expected: str
) -> None:
    assert safe_next_path(supplied) == expected
