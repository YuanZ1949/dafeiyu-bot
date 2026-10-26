from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from ..timeutil import now_shanghai
from .redact import redact

log = logging.getLogger(__name__)

FIELDS = (
    "id",
    "ts",
    "actor",
    "action",
    "target",
    "result",
    "detail",
    "request_id",
    "ip",
)

# detail 的键名本身就能说明值是凭据：按「最后一段」匹配，兼容 napcat_token、
# DEEPSEEK_API_KEY 这类带前缀的键；token_usage 这种描述性键不在此列。
_SENSITIVE_KEY = re.compile(
    r"(?i)^(?:[a-z0-9]+[-_])*"
    r"(?:authorization|set[-_]cookie|x[-_]control[-_]token"
    r"|api[-_]?key|apikey|(?:secret|private|access)[-_]?key"
    r"|password|passwd|secret|token)$"
)


class AuditLog:
    """append-only JSONL 审计；写入失败只记日志，不阻塞主操作。"""

    def __init__(
        self,
        path: Path,
        max_bytes: int = 10 * 1024 * 1024,
        backups: int = 3,
        sanitize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max(1, int(max_bytes))
        self.backups = max(1, int(backups))
        self.sanitize = sanitize
        self._clock = clock or now_shanghai
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("审计目录创建失败：%s", exc)

    def write(
        self,
        action: str,
        *,
        actor: str = "system",
        target: str = "",
        result: str = "ok",
        detail: dict[str, Any] | None = None,
        request_id: str = "",
        ip: str = "",
    ) -> dict[str, Any]:
        payload = dict(detail or {})
        if self.sanitize is not None:
            try:
                payload = self.sanitize(payload)
            except Exception:  # sanitize 坏了不能把审计写死
                log.warning("审计 detail 脱敏失败", exc_info=True)
        # sanitize 之后再对字符串值递归脱敏，结构（dict / list / tuple）保持不变。
        payload = _redact_value(payload)
        event = {
            "id": str(uuid.uuid4()),
            "ts": self._clock().isoformat(timespec="seconds"),
            "actor": actor,
            "action": action,
            "target": target,
            "result": result,
            "detail": payload,
            "request_id": request_id,
            "ip": ip,
        }
        try:
            # bot 与 supervisor 会写同一份 JSONL；写入和轮转必须跨进程串行，
            # 否则两侧同时轮转可能覆盖对方刚归档的文件。
            with _audit_lock(self.path):
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._rotate()
        except OSError as exc:
            log.warning("审计写入失败：%s", exc)
        return event

    def query(self, limit: int = 50, before: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        rows: list[dict[str, Any]] = []
        for file in self._files():
            try:
                lines = file.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(raw, dict):
                    continue
                row = {name: raw.get(name, _default(name)) for name in FIELDS}
                ts = str(row["ts"] or "")
                if before and ts >= before:
                    continue
                rows.append(row)
        rows.sort(key=lambda row: str(row["ts"]), reverse=True)
        return rows[:limit]

    def _files(self) -> list[Path]:
        files = [self.path]
        for index in range(1, self.backups + 1):
            candidate = self.path.with_name(f"{self.path.name}.{index}")
            if candidate.exists():
                files.append(candidate)
        return files

    def _rotate(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
        try:
            if oldest.exists():
                oldest.unlink()
            for index in range(self.backups - 1, 0, -1):
                source = self.path.with_name(f"{self.path.name}.{index}")
                if source.exists():
                    os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
            self.path.touch()
        except OSError as exc:
            log.warning("审计轮转失败：%s", exc)


@contextmanager
def _audit_lock(path: Path) -> Iterator[None]:
    """对审计文件加跨进程排他锁；拿不到锁时退化为无锁写入。"""
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError as exc:
        log.warning("审计锁文件打开失败，降级为无锁写入：%s", exc)
        yield
        return
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.01)
            if not locked:
                log.warning("审计锁等待超时，降级为无锁写入")
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _redact_value(value: Any, key: str = "") -> Any:
    """递归脱敏 detail：敏感键整体打码，其余字符串值走文本规则。

    键名（``token`` / ``password`` / ``Authorization`` / ``DEEPSEEK_API_KEY``
    之类）本身就能说明值是凭据，哪怕值里没有 ``key=value`` 形状也要打码；
    普通键（如 ``note``）只做文本替换，``token 用量`` 这类中文文案保持原样。
    结构（dict / list / tuple）保持不变。
    """
    if key and _SENSITIVE_KEY.match(key.strip()):
        return "***"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            item_key: _redact_value(item, item_key if isinstance(item_key, str) else "")
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def _default(name: str) -> Any:
    if name == "detail":
        return {}
    return ""
