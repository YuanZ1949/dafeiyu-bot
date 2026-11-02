from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

COOKIE_NAME = "dafeiyu_dashboard_session"
PUBLIC_PATHS = frozenset({"/login", "/api/auth/login", "/favicon.ico"})
FAILURE_LIMIT = 5
FAILURE_WINDOW_SECONDS = 300


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value + padding)
    # 末位字符的低位比特不参与解码（32 字节签名编码成 43 字符，最后一个字符
    # 只有 4 位有效），非严格解码会让「只改末位」的篡改照样验签通过。
    # 用重编码比对拒绝非规范写法，同时挡住非法字符被静默丢弃。
    if _b64encode(raw) != value:
        raise ValueError("non-canonical base64")
    return raw


def safe_next_path(value: str) -> str:
    """只接受站内相对路径，挡住 //host、绝对 URL 和反斜杠变体。"""
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(char) < 32 for char in value):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    decoded = unquote(value)
    if decoded.startswith("//") or "\\" in decoded:
        return "/"
    return value


@dataclass
class DashboardAuth:
    enabled: bool
    username: str
    password: str = field(repr=False)
    session_seconds: int = 7 * 24 * 3600
    secret: bytes = field(
        default_factory=lambda: secrets.token_bytes(32), repr=False
    )
    _failures: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def is_public(self, path: str) -> bool:
        return path in PUBLIC_PATHS

    def verify_credentials(self, username: str, password: str) -> bool:
        if not self.enabled:
            return False
        supplied_user = str(username or "").encode("utf-8")
        expected_user = self.username.encode("utf-8")
        supplied_password = str(password or "").encode("utf-8")
        expected_password = self.password.encode("utf-8")
        return hmac.compare_digest(
            supplied_user, expected_user
        ) and hmac.compare_digest(supplied_password, expected_password)

    def issue_cookie(self, now: float | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = json.dumps(
            {
                "u": self.username,
                "iat": issued_at,
                "exp": issued_at + int(self.session_seconds),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        signature = hmac.new(self.secret, payload, hashlib.sha256).digest()
        return f"{_b64encode(payload)}.{_b64encode(signature)}"

    def verify_cookie(self, token: str, now: float | None = None) -> bool:
        if not token:
            return False
        try:
            body, signature = token.split(".", 1)
            payload = _b64decode(body)
            expected = hmac.new(self.secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(_b64decode(signature), expected):
                return False
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("u") != self.username:
                return False
            issued_at = int(data.get("iat", 0))
            expires_at = int(data.get("exp", 0))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False
        current = int(time.time() if now is None else now)
        return issued_at <= current + 60 and current <= expires_at

    def is_rate_limited(self, ip: str, now: float | None = None) -> bool:
        current = float(time.time() if now is None else now)
        attempts = [
            stamp
            for stamp in self._failures.get(ip, [])
            if current - stamp < FAILURE_WINDOW_SECONDS
        ]
        if attempts:
            self._failures[ip] = attempts
        else:
            self._failures.pop(ip, None)
        return len(attempts) >= FAILURE_LIMIT

    def record_failure(self, ip: str, now: float | None = None) -> None:
        current = float(time.time() if now is None else now)
        attempts = [
            stamp
            for stamp in self._failures.get(ip, [])
            if current - stamp < FAILURE_WINDOW_SECONDS
        ]
        attempts.append(current)
        self._failures[ip] = attempts

    def clear_failures(self, ip: str) -> None:
        self._failures.pop(ip, None)
