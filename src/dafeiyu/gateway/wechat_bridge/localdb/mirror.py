"""SQLCipher 本地库的只读解密镜像。

``Mirror`` 把一个微信 ``*.db``（连同 ``*-wal``）增量解密成可直接用
``sqlite3`` 打开的只读副本；``MirrorSet`` 按库路径管理一组 ``Mirror``，
做节流刷新。设计目标是**正确性优先**：

* 每次写镜像前先校验 HMAC，坏页直接抛错，本轮不改镜像；
* 主库按页 SHA-1 增量，源文件变短时截断；
* WAL 每轮都从第 0 帧重放（不记录偏移），因此天然幂等；
* 镜像里只有解密后的页，没有 WAL 头，清理后就是普通 SQLite 文件。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .sqlcipher import PAGE_SIZE, SALT_SIZE, CipherError, decrypt_page
from .wal import WalError, WalImage, read_wal_file

__all__ = [
    "Mirror",
    "MirrorError",
    "MirrorSet",
    "SyncResult",
]

logger = logging.getLogger(__name__)

#: 每个库的状态文件名后缀（与镜像同目录）。
_STATE_SUFFIX = ".state.json"

#: 消息库里永远不需要镜像的文件特征。
_MESSAGE_EXCLUDED_MARKERS = ("_fts", "kvdb", "weclaw")
#: 消息库里以这些前缀开头的库同样跳过。
_MESSAGE_EXCLUDED_PREFIXES = ("biz_",)


class MirrorError(RuntimeError):
    """镜像读取/解密/写入失败；消息为中文且不含密钥。"""


@dataclass(frozen=True)
class SyncResult:
    """一次 ``Mirror.sync()`` 的结果。"""

    changed: bool
    pages_written: int
    wal_frames: int
    full: bool


@dataclass(frozen=True)
class _State:
    salt: str
    pages: Mapping[int, str]
    source_size: int


def _normalize_rel(rel: str) -> str:
    """把 ``contact\\contact.db`` 规整成 ``contact/contact.db``。"""
    text = str(rel).strip().replace("\\", "/")
    if not text:
        raise ValueError("库相对路径为空")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise ValueError(f"库路径必须是相对路径：{rel}")
    while text.startswith("./"):
        text = text[2:]
    parts = text.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"非法库路径：{rel}")
    return "/".join(parts)


def _is_excluded_message_db(name: str) -> bool:
    """消息库是否属于 FTS / 业务 / kvdb / weclaw 这类不需要镜像的库。"""
    lowered = name.lower()
    if any(marker in lowered for marker in _MESSAGE_EXCLUDED_MARKERS):
        return True
    return any(lowered.startswith(prefix) for prefix in _MESSAGE_EXCLUDED_PREFIXES)


class Mirror:
    """单个 SQLCipher 库的增量解密镜像。"""

    def __init__(
        self,
        source: Path,
        enc_key: bytes,
        mirror_path: Path,
        state_path: Path,
        *,
        page_size: int = PAGE_SIZE,
    ) -> None:
        if page_size != PAGE_SIZE:
            raise ValueError(f"只支持 {PAGE_SIZE} 字节页，收到 {page_size}")
        if len(enc_key) != 32:
            raise ValueError(f"enc_key 必须是 32 字节，收到 {len(enc_key)}")
        self.source: Path = Path(source)
        self.mirror_path: Path = Path(mirror_path)
        self.state_path: Path = Path(state_path)
        self.enc_key: bytes = bytes(enc_key)
        self.page_size: int = page_size
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def sync(self) -> SyncResult:
        """同步一次：主库增量 + WAL 全量重放。"""
        with self._lock:
            return self._sync_locked()

    # --------------------------------------------------------------- internals
    def _sync_locked(self) -> SyncResult:
        try:
            handle = self.source.open("rb")
        except OSError as exc:
            raise MirrorError(f"无法读取源库 {self.source}：{exc}") from exc

        with handle:
            try:
                source_size = os.fstat(handle.fileno()).st_size
                header = handle.read(SALT_SIZE)
            except OSError as exc:
                raise MirrorError(f"读取源库头部失败 {self.source}：{exc}") from exc
            if len(header) < SALT_SIZE:
                raise MirrorError(
                    f"源库头部不完整（{len(header)} 字节）：{self.source}"
                )
            salt = header[:SALT_SIZE]
            total_pages = source_size // self.page_size
            try:
                handle.seek(0)
            except OSError as exc:
                raise MirrorError(f"定位源库失败 {self.source}：{exc}") from exc

            # 先把 WAL 完整解析并解密：任何一页 HMAC 失败都在写镜像之前暴露。
            wal = self._read_wal(salt)

            state = self._load_state()
            full = (
                state is None
                or state.salt != salt.hex()
                or not self.mirror_path.is_file()
            )
            mirror_before = self._mirror_size()

            try:
                if full:
                    hashes, pages_written = self._rebuild(handle, salt, total_pages)
                else:
                    if state is None:  # 理论上不可达：full 已覆盖 None
                        raise MirrorError("镜像状态在同步过程中失效")
                    hashes, changed = self._scan_incremental(
                        handle, salt, total_pages, state
                    )
                    pages_written = len(changed)
                    self._write_pages(changed)
            except CipherError as exc:
                raise MirrorError(
                    f"源库页解密失败（{self.source}）：{exc}"
                ) from exc

        wal_frames = 0
        try:
            if wal.frames:
                self._apply_wal(wal)
                wal_frames = len(wal.frames)
            final_pages = wal.dbsize if wal.has_commit else total_pages
            self._truncate_mirror(final_pages)
        except OSError as exc:
            raise MirrorError(f"写入镜像失败 {self.mirror_path}：{exc}") from exc

        mirror_after = self._mirror_size()
        resized = mirror_after != mirror_before
        self._save_state(salt.hex(), hashes, source_size)
        return SyncResult(
            changed=full or pages_written > 0 or wal_frames > 0 or resized,
            pages_written=pages_written,
            wal_frames=wal_frames,
            full=full,
        )

    def _read_wal(self, salt: bytes) -> WalImage:
        wal_path = Path(f"{self.source}-wal")
        try:
            return read_wal_file(
                wal_path, self.enc_key, db_salt=salt, page_size=self.page_size
            )
        except WalError as exc:
            raise MirrorError(f"解析 WAL 失败 {wal_path}：{exc}") from exc
        except CipherError as exc:
            raise MirrorError(f"WAL 页解密失败 {wal_path}：{exc}") from exc

    def _read_page(self, handle: BinaryIO, pgno: int) -> bytes:
        try:
            raw = handle.read(self.page_size)
        except OSError as exc:
            raise MirrorError(
                f"读取源库第 {pgno} 页失败 {self.source}：{exc}"
            ) from exc
        if len(raw) != self.page_size:
            raise MirrorError(
                f"源库第 {pgno} 页不完整（{len(raw)} 字节）：{self.source}"
            )
        return raw

    def _rebuild(
        self, handle: BinaryIO, salt: bytes, total_pages: int
    ) -> tuple[dict[int, str], int]:
        """全量重建：先写临时文件，全部校验通过后再原子替换镜像。"""
        temp_path = self.mirror_path.with_name(self.mirror_path.name + ".rebuild.tmp")
        hashes: dict[int, str] = {}
        try:
            self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("wb") as out:
                for pgno in range(1, total_pages + 1):
                    raw = self._read_page(handle, pgno)
                    hashes[pgno] = hashlib.sha1(raw).hexdigest()
                    out.write(decrypt_page(raw, self.enc_key, pgno, salt))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_path, self.mirror_path)
        except OSError as exc:
            raise MirrorError(f"重建镜像失败 {self.mirror_path}：{exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)
        return hashes, total_pages

    def _scan_incremental(
        self,
        handle: BinaryIO,
        salt: bytes,
        total_pages: int,
        state: _State,
    ) -> tuple[dict[int, str], dict[int, bytes]]:
        """按密文页 SHA-1 找出变化页，并在写盘前完成全部解密校验。"""
        hashes: dict[int, str] = {}
        changed: dict[int, bytes] = {}
        for pgno in range(1, total_pages + 1):
            raw = self._read_page(handle, pgno)
            digest = hashlib.sha1(raw).hexdigest()
            hashes[pgno] = digest
            if state.pages.get(pgno) != digest:
                changed[pgno] = decrypt_page(raw, self.enc_key, pgno, salt)
        return hashes, changed

    def _write_pages(self, pages: Mapping[int, bytes]) -> None:
        if not pages:
            return
        try:
            with self.mirror_path.open("r+b") as out:
                size = out.seek(0, os.SEEK_END)
                needed = max(
                    (pgno - 1) * self.page_size + self.page_size for pgno in pages
                )
                if size < needed:
                    out.truncate(needed)
                for pgno in sorted(pages):
                    out.seek((pgno - 1) * self.page_size)
                    out.write(pages[pgno])
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            raise MirrorError(f"写入镜像失败 {self.mirror_path}：{exc}") from exc

    def _apply_wal(self, wal: WalImage) -> None:
        """按顺序覆盖写 WAL 帧，最后按 dbsize 截断（每轮从第 0 帧重放）。"""
        final_pages = wal.dbsize
        try:
            with self.mirror_path.open("r+b") as out:
                for frame in wal.frames:
                    if frame.pgno > final_pages:
                        continue
                    out.seek((frame.pgno - 1) * self.page_size)
                    out.write(frame.page)
                out.truncate(final_pages * self.page_size)
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            raise MirrorError(f"应用 WAL 失败 {self.mirror_path}：{exc}") from exc

    def _truncate_mirror(self, pages: int) -> None:
        target = pages * self.page_size
        try:
            with self.mirror_path.open("r+b") as out:
                size = out.seek(0, os.SEEK_END)
                if size == target:
                    return
                out.truncate(target)
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            raise MirrorError(f"截断镜像失败 {self.mirror_path}：{exc}") from exc

    def _mirror_size(self) -> int:
        try:
            return self.mirror_path.stat().st_size
        except OSError:
            return 0

    def _load_state(self) -> _State | None:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, ValueError) as exc:
            logger.debug("镜像状态不可用，改为全量：%s：%s", self.state_path, exc)
            return None
        if not isinstance(payload, dict):
            return None
        salt = payload.get("salt")
        pages = payload.get("pages")
        source_size = payload.get("source_size")
        if not isinstance(salt, str):
            return None
        if not isinstance(pages, dict):
            return None
        if not isinstance(source_size, int) or isinstance(source_size, bool):
            return None
        parsed: dict[int, str] = {}
        for key, value in pages.items():
            try:
                pgno = int(key)
            except (TypeError, ValueError):
                return None
            if pgno < 1 or not isinstance(value, str):
                return None
            parsed[pgno] = value
        return _State(salt=salt, pages=parsed, source_size=source_size)

    def _save_state(
        self, salt_hex: str, hashes: Mapping[int, str], source_size: int
    ) -> None:
        payload = {
            "salt": salt_hex,
            "pages": {str(pgno): digest for pgno, digest in sorted(hashes.items())},
            "source_size": source_size,
        }
        temp_path = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            with temp_path.open("w", encoding="utf-8", newline="\n") as out:
                out.write(text)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_path, self.state_path)
        except OSError as exc:
            raise MirrorError(f"写入镜像状态失败 {self.state_path}：{exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)


class MirrorSet:
    """按库路径管理一组 ``Mirror``，按需创建并节流刷新。"""

    def __init__(
        self,
        db_root: Path,
        keys: Mapping[str, bytes],
        out_dir: Path,
        *,
        refresh_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if refresh_seconds < 0:
            raise ValueError(f"refresh_seconds 不能为负：{refresh_seconds}")
        self.db_root: Path = Path(db_root)
        self.out_dir: Path = Path(out_dir)
        self.refresh_seconds: float = float(refresh_seconds)
        self.keys: dict[str, bytes] = {
            _normalize_rel(rel): bytes(key) for rel, key in keys.items()
        }
        self._clock: Callable[[], float] = clock
        self._mirrors: dict[str, Mirror] = {}
        self._last_sync: dict[str, float] = {}
        self.last_errors: dict[str, str] = {}

    def path_for(self, rel: str) -> Path:
        """返回某个库的镜像路径；首次调用会创建并同步，过期则刷新。

        失败（缺密钥、源库不可读、坏页）会抛 :class:`MirrorError` 或
        ``ValueError``，不会静默返回旧镜像。
        """
        normalized = _normalize_rel(rel)
        mirror = self._mirror_for(normalized)
        self._sync_if_due(normalized, mirror)
        return mirror.mirror_path

    def sync(self) -> None:
        """同步 contact 和所有需要的 message 库；单库失败不影响其它库。"""
        self.last_errors = {}
        for rel in self._discover():
            try:
                mirror = self._mirror_for(rel)
                self._sync_if_due(rel, mirror)
            except Exception as exc:  # noqa: BLE001 - 单库失败必须隔离
                message = str(exc) or type(exc).__name__
                self.last_errors[rel] = message
                logger.warning("本地库镜像同步失败：%s：%s", rel, message)

    def paths(self) -> list[str]:
        """已发现（未必要已同步）的库相对路径，供 repository 枚举消息分库。

        只读目录结构，不触发解密；真正取镜像仍然走 :meth:`path_for`。
        """
        return self._discover()

    def _mirror_for(self, rel: str) -> Mirror:
        existing = self._mirrors.get(rel)
        if existing is not None:
            return existing
        key = self.keys.get(rel)
        if key is None:
            raise MirrorError(f"缺少本地库密钥：{rel}")
        parts = rel.split("/")
        source = self.db_root.joinpath(*parts)
        mirror_path = self.out_dir.joinpath(*parts)
        state_path = mirror_path.with_name(mirror_path.name + _STATE_SUFFIX)
        mirror = Mirror(source, key, mirror_path, state_path)
        self._mirrors[rel] = mirror
        return mirror

    def _sync_if_due(self, rel: str, mirror: Mirror) -> None:
        now = self._clock()
        last = self._last_sync.get(rel)
        due = (
            last is None
            or self.refresh_seconds <= 0
            or (now - last) >= self.refresh_seconds
        )
        if not due:
            return
        mirror.sync()
        self._last_sync[rel] = now

    def _discover(self) -> list[str]:
        rels = ["contact/contact.db"]
        message_dir = self.db_root / "message"
        try:
            candidates = sorted(message_dir.glob("message_*.db"))
        except OSError:
            candidates = []
        for path in candidates:
            if not path.is_file():
                continue
            if _is_excluded_message_db(path.name):
                continue
            rels.append(f"message/{path.name}")
        return rels
