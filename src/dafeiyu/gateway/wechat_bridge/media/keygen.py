"""微信 4.2 图片 ``.dat`` 的 AES 密钥发现（``cfgDword``），只读。

V2 容器的 AES-128 密钥不是常量，而是::

    aes_key = md5(f"{cfgDword}{wxid}").hexdigest()[:16].encode()
    xor_key = cfgDword & 0xFF

``cfgDword`` 只存在于正在运行的 ``Weixin.exe`` 进程内存里（实测磁盘配置
文件里没有）。本模块的做法：

1. 从 V2 样本的 XOR 尾反推单字节 XOR key（JPEG 尾 ``FFD9`` / PNG ``IEND``
   等已知结束标记，命中即唯一）；
2. 只读扫描 ``Weixin.exe`` 内存里的 4 字节对齐 u32，先按低字节过滤，
   再用候选派生密钥解第一块 AES 密文，要求出现**强图片头**；
3. 用 ``decode_dat`` 完整解一次样本，确认候选不是误命中；
4. 把 ``cfgDword`` 落盘缓存，下次启动先验缓存，失效才重新扫描。

设计约束：纯标准库；内存访问只走 ``keyscan.ProcessMemory``（``PROCESS_VM_READ``
+ ``VirtualQueryEx``）；日志不打印密钥/明文；任何失败都返回 ``None`` 或
``False``，绝不向调用方抛异常。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import sys
import threading
import time
from array import array
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..localdb import keyscan
from .aes import CipherError, aes_ecb_decrypt
from .dat import decode_dat

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_KEY_CACHE",
    "MediaKey",
    "MediaKeyResolver",
    "derive_aes_key",
    "derive_emoticon_key",
    "guess_xor_key",
]

#: 密钥缓存默认落盘位置：与本地库镜像 / keys.json 同一个目录。
DEFAULT_KEY_CACHE = Path("data/wechat-bridge/localdb/media_key.json")

#: V2 容器签名与头部长度（与 :mod:`dat` 保持一致）。
_V2_SIGNATURE = b"\x07\x08V2\x08\x07"
_V2_HEADER_SIZE = 15

#: 已知图片结束标记，用于从文件尾反推单字节 XOR key；按长度从长到短匹配。
#:
#: 刻意不收单字节的 GIF 尾 ``;``：单字节标记对任何文件都「恒成立」，等于
#: 没有校验，会把非 V2 样本也当成可猜 key 的图片。
_TAIL_MARKERS: tuple[bytes, ...] = (
    b"IEND\xaeB`\x82",
    b"\xff\xd9",
)

#: 单次扫描的墙钟上限：进程内存很大时不至于把机器人卡死。
_DEFAULT_SCAN_SECONDS = 30.0
#: 扫描失败后的冷却时间：避免每来一张图就重扫一遍。
_DEFAULT_RETRY_SECONDS = 300.0
#: 每次读取的内存块大小。
_CHUNK = 1 << 20
#: 找 wxid 时块间保留的重叠字节，避免字符串被切断。
_NEEDLE_OVERLAP = 128

#: wxid 邻近扫描半径：``cfgDword``（cfg+0x40）与 wxid（cfg+0x48）同处一个配置
#: 结构，先只验证附近 u32，首次定位可从 70s 降到 2s。
_PROXIMITY_RADIUS = 4096

#: 全量兜底时只考虑可能是账号 uin 的 u32（约 93% 的随机 u32 超出该范围）。
_UIN_MAX = 4_000_000_000

#: JPEG SOI 之后允许的常见标记（APP0/APP1/APP2/DQT/COM/DRI/APP14）。
_JPEG_MARKERS = frozenset({0xE0, 0xE1, 0xE2, 0xDB, 0xFE, 0xC4, 0xEE})


@dataclass(frozen=True)
class MediaKey:
    """一组可用的微信图片解码参数。"""

    aes_key: bytes
    xor_key: int


def derive_aes_key(dword: int, wxid: str) -> bytes:
    """按微信规则派生 AES-128 密钥（16 个 ASCII 十六进制字符）。"""
    digest = hashlib.md5(f"{dword}{wxid}".encode("utf-8")).hexdigest()
    return digest[:16].encode("ascii")


def derive_emoticon_key(dword: int, wxid: str) -> bytes:
    """派生表情缓存（``cache/*/Emoticon/...``）的 AES-128-CBC 密钥。

    与图片 ``.dat`` 的差别有两处，容易混：

    - 派生串多一个 ``EMOTICON`` 后缀；
    - 取 **raw digest 的前 16 字节**，而不是它的 ASCII 十六进制前 16 个字符。
    """
    return hashlib.md5(f"{dword}{wxid}EMOTICON".encode("utf-8")).digest()[:16]


def guess_xor_key(sample: bytes) -> int | None:
    """从 V2 样本尾部反推单字节 XOR key；认不出格式返回 ``None``。"""
    if len(sample) < _V2_HEADER_SIZE:
        return None
    xor_size = int.from_bytes(sample[10:14], "little")
    if xor_size == 0:
        # 没有 XOR 段：key 取 0 即可，AES 头仍然能验证。
        return 0
    if xor_size > len(sample) - _V2_HEADER_SIZE:
        return None
    for marker in _TAIL_MARKERS:
        length = len(marker)
        if len(sample) < length:
            continue
        xor_key = sample[-1] ^ marker[-1]
        if bytes(value ^ xor_key for value in sample[-length:]) == marker:
            return xor_key
    return None


def _strong_image_head(block: bytes) -> bool:
    """只认足够长的图片头，避免 ``BM``/``RIFF`` 这类短魔数造成误命中。"""
    if len(block) < 16:
        return False
    if block.startswith(b"\x89PNG\r\n\x1a\n"):
        return block[12:16] == b"IHDR"
    if block.startswith(b"\xff\xd8\xff"):
        return block[3] in _JPEG_MARKERS
    if block.startswith((b"GIF87a", b"GIF89a")):
        width, height = int.from_bytes(block[6:8], "little"), int.from_bytes(
            block[8:10], "little"
        )
        return width > 0 and height > 0
    if block.startswith(b"wxgf"):
        return True
    if block.startswith(b"RIFF") and block[8:12] == b"WEBP":
        return True
    return False


def _iter_words(data: bytes) -> Iterator[int]:
    """按小端产出 4 字节对齐的 u32（数据长度不足时忽略尾巴）。"""
    usable = len(data) - (len(data) % 4)
    if usable < 4:
        return
    words = array("I")
    if words.itemsize != 4:  # pragma: no cover - 仅异常平台
        for offset in range(0, usable, 4):
            yield int.from_bytes(data[offset : offset + 4], "little")
        return
    words.frombytes(data[:usable])
    if sys.byteorder != "little":  # pragma: no cover - Windows 都是小端
        words.byteswap()
    yield from words


class MediaKeyResolver:
    """解析并缓存微信图片密钥；线程安全，失败有冷却时间。

    参数全部可注入，方便单测完全不碰真实进程与真实微信目录：

    - ``db_root``：``db_storage`` 目录；留空用 ``keyscan.find_db_root()``；
    - ``storage_root``：配置里的媒体根目录（账号根），优先级高于 ``db_root``；
    - ``wxid``：自己的 wxid；留空从 ``db_root`` 目录名解析；
    - ``pids`` / ``memory_factory``：进程枚举与只读内存视图；
    - ``cache_path``：密钥缓存 JSON；``None`` 表示不落盘。
    """

    def __init__(
        self,
        *,
        db_root: str | Path | None = None,
        storage_root: str | Path | None = None,
        wxid: str = "",
        cache_path: str | Path | None = DEFAULT_KEY_CACHE,
        pids: Callable[[], Sequence[int]] = keyscan.weixin_pids,
        memory_factory: Callable[[int], Any] = keyscan.ProcessMemory,
        clock: Callable[[], float] = time.time,
        scan_seconds: float = _DEFAULT_SCAN_SECONDS,
        retry_seconds: float = _DEFAULT_RETRY_SECONDS,
    ) -> None:
        self._db_root = Path(db_root) if db_root else None
        self._storage_root = str(storage_root or "").strip()
        self._wxid = str(wxid or "").strip()
        self._cache_path = Path(cache_path) if cache_path else None
        self._pids = pids
        self._memory_factory = memory_factory
        self._clock = clock
        self._scan_seconds = max(1.0, float(scan_seconds))
        self._retry_seconds = max(0.0, float(retry_seconds))
        self._lock = threading.RLock()
        self._key: MediaKey | None = None
        self._dword = 0
        self._retry_after = 0.0
        self._resolved = False
        self._cache_loaded = False
        self._cache_values: tuple[int, int] | None = None

    # ------------------------------------------------------------- 公共接口
    def resolve(self, sample: bytes | bytearray | memoryview) -> MediaKey | None:
        """为 V2 样本解析密钥；非 V2、缺少 wxid 或扫描失败都返回 ``None``。"""
        try:
            data = bytes(sample)
        except (TypeError, ValueError):
            return None
        if not data.startswith(_V2_SIGNATURE):
            return None

        with self._lock:
            if self._key is not None:
                return self._key
            if self._resolved:
                # 已尝试过且失败：冷却期内直接放弃。
                if self._clock() < self._retry_after:
                    return None
            else:
                self._load_cache(data)
                if self._key is not None:
                    return self._key
                self._resolved = True

            if self._clock() < self._retry_after:
                return None
            key = self._discover(data)
            if key is None:
                self._retry_after = self._clock() + self._retry_seconds
                return None
            self._key = key
            self._save_cache()
            return key

    def prewarm(self) -> bool:
        """后台预热：找一张真实 V2 样本并解析密钥；成功返回 ``True``。"""
        sample = self._find_sample()
        if sample is None:
            return False
        return self.resolve(sample) is not None

    def cached(self, sample: bytes | bytearray | memoryview) -> MediaKey | None:
        """只用已解析结果或落盘缓存，**绝不扫描内存**。

        适合在事件环上调用：``Service`` 在逐条消息解码时用它，未命中再把
        扫描交给后台线程。
        """
        with self._lock:
            try:
                data = bytes(sample)
            except (TypeError, ValueError):
                return None
            is_v2 = data.startswith(_V2_SIGNATURE)
            if self._key is not None:
                # 微信可能在机器人运行期间重启并换 key：用当前样本的首块密文
                # 验一次。验不过就丢弃（下一次 resolve 会重新扫），否则会一直
                # 拿着过期 key 把所有图片解成占位。
                if is_v2 and not self._matches(self._key, data):
                    log.info("微信图片密钥已失效（微信可能重启过），等待重新扫描")
                    self._drop_key()
                    return None
                return self._key
            if not is_v2:
                return None
            self._load_cache(data)
            return self._key

    @property
    def key(self) -> MediaKey | None:
        return self._key

    @property
    def dword(self) -> int:
        """已解析的 ``cfgDword``；还没拿到时为 0。"""
        return self._dword

    def emoticon_key(self) -> bytes | None:
        """表情缓存用的 AES-128-CBC 密钥（``IV == key``）；拿不到返回 ``None``。

        只读已解析结果或落盘缓存，**绝不扫描内存**，可在事件环上调用。
        表情没有 V2 头可供校验，因此这里只要求缓存里的 wxid 与当前账号一致；
        真正的正确性由调用方「解密后必须出现 ``wxgf``」来证明。
        """
        with self._lock:
            dword = self._dword
            if not dword:
                values = self._cache_values_from_disk()
                if values is None:
                    return None
                dword = values[0]
                self._dword = dword
            wxid = self._resolve_wxid()
            if not wxid:
                return None
            return derive_emoticon_key(dword, wxid)

    def _drop_key(self) -> None:
        """丢弃内存里的 key，允许下一次 ``resolve`` 重新扫描。"""
        self._key = None
        self._dword = 0
        self._resolved = False
        self._retry_after = 0.0
        # 落盘缓存可能也过期了，别在同一进程里反复读同一个坏文件。
        self._cache_loaded = True
        self._cache_values = None

    @staticmethod
    def _matches(key: MediaKey, sample: bytes) -> bool:
        """当前 key 能否把样本首块解成强图片头（``wxgf`` 也算）。"""
        block = sample[_V2_HEADER_SIZE : _V2_HEADER_SIZE + 16]
        if len(block) != 16:
            return True  # 样本太短：无法证伪，保守放行。
        try:
            plain = aes_ecb_decrypt(key.aes_key, block)
        except (CipherError, ValueError, TypeError):
            return False
        return _strong_image_head(plain)

    # ------------------------------------------------------------- 缓存
    def _load_cache(self, sample: bytes) -> None:
        values = self._cache_values_from_disk()
        if values is None:
            return
        dword, xor_key = values
        wxid = self._resolve_wxid()
        if not wxid:
            return
        key = MediaKey(derive_aes_key(dword, wxid), xor_key)
        if not self._validates(key, sample):
            log.debug("缓存的微信图片密钥已失效，将重新扫描")
            return
        self._key = key
        self._dword = dword
        log.info("使用缓存的微信图片密钥：cfgDword=%s", dword)

    def _cache_values_from_disk(self) -> tuple[int, int] | None:
        """读取并校验落盘缓存，返回 ``(dword, xor_key)``。

        只校验结构与 wxid，不用样本验证 AES key：表情密钥只需要 ``dword``，
        而表情缓存启动时不一定有 V2 图片样本可用。
        """
        if self._cache_loaded:
            return self._cache_values
        self._cache_loaded = True
        if self._cache_path is None:
            return None
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        dword = payload.get("dword")
        xor_key = payload.get("xor_key")
        if not isinstance(dword, int) or isinstance(dword, bool):
            return None
        if not isinstance(xor_key, int) or not 0 <= xor_key <= 0xFF:
            return None
        wxid = self._resolve_wxid()
        if not wxid or payload.get("wxid") != wxid:
            return None
        self._cache_values = (dword, xor_key)
        return self._cache_values

    def _save_cache(self) -> None:
        if self._cache_path is None or self._key is None or not self._dword:
            return
        payload = {
            "db_root": str(self._db_root or ""),
            "wxid": self._resolve_wxid(),
            "dword": self._dword,
            "xor_key": self._key.xor_key,
            "saved_at": int(self._clock()),
        }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._cache_path.with_name(self._cache_path.name + ".tmp")
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(temp, self._cache_path)
        except OSError as exc:
            log.warning("微信图片密钥缓存写入失败（本次结果仍可用）：%s", exc)

    # ------------------------------------------------------------- 扫描
    def _discover(self, sample: bytes) -> MediaKey | None:
        wxid = self._resolve_wxid()
        if not wxid:
            log.debug("无法确定自己的 wxid，跳过微信图片密钥扫描")
            return None
        xor_key = guess_xor_key(sample)
        if xor_key is None:
            log.debug("无法从 V2 样本尾部反推 XOR key，跳过密钥扫描")
            return None
        dword = self._scan(sample, xor_key, wxid)
        if dword is None:
            log.warning("未能从微信进程内存里定位图片密钥（cfgDword）")
            return None
        key = MediaKey(derive_aes_key(dword, wxid), xor_key)
        if not self._validates(key, sample):
            log.debug("候选 cfgDword 无法完整解出样本，忽略")
            return None
        self._dword = dword
        log.info("已定位微信图片密钥：cfgDword=%s", dword)
        return key

    def _scan(self, sample: bytes, xor_key: int, wxid: str) -> int | None:
        needle = wxid.encode("utf-8")
        block = sample[_V2_HEADER_SIZE : _V2_HEADER_SIZE + 16]
        if len(block) != 16:
            return None
        deadline = self._clock() + self._scan_seconds
        try:
            pids = list(self._pids())
        except Exception as exc:  # noqa: BLE001 - 枚举失败只是没找到
            log.debug("枚举微信进程失败：%s", exc)
            return None
        holders: list[int] = []
        # 第一轮只做「wxid 邻近」扫描：命中只需要 1-2 秒，且不会为无关进程
        # 付全量扫描的代价（主进程通常持有 wxid，辅助进程没有）。
        for pid in pids:
            if self._clock() > deadline:
                log.info("微信图片密钥扫描超时，未命中")
                return None
            memory = self._open_memory(pid)
            if memory is None:
                continue
            try:
                if needle and not self._contains(memory, needle):
                    continue
                holders.append(pid)
                hit = self._scan_near(memory, block, xor_key, wxid, deadline, needle)
            finally:
                self._close_memory(memory)
            if hit is not None:
                return hit
        # 第二轮：全量兜底。配置结构布局变化、cfgDword 离 wxid 很远时才会走到。
        for pid in holders:
            if self._clock() > deadline:
                log.info("微信图片密钥全量扫描超时，未命中")
                return None
            memory = self._open_memory(pid)
            if memory is None:
                continue
            try:
                hit = self._scan_full(memory, block, xor_key, wxid, deadline)
            finally:
                self._close_memory(memory)
            if hit is not None:
                return hit
        return None

    def _open_memory(self, pid: int) -> Any:
        try:
            return self._memory_factory(pid)
        except Exception as exc:  # noqa: BLE001 - 单个进程失败不影响其它
            log.debug("打开微信进程 PID=%s 失败：%s", pid, exc)
            return None

    @staticmethod
    def _close_memory(memory: Any) -> None:
        close = getattr(memory, "close", None)
        if callable(close):
            close()

    def _contains(self, memory: Any, needle: bytes) -> bool:
        """进程里是否出现 wxid：主进程才持有它，可跳过大量辅助进程。"""
        overlap = _NEEDLE_OVERLAP
        for _base, data in memory.iter_chunks(chunk=_CHUNK, overlap=overlap):
            if needle in data:
                return True
        return False

    def _scan_near(
        self,
        memory: Any,
        block: bytes,
        xor_key: int,
        wxid: str,
        deadline: float,
        needle: bytes,
    ) -> int | None:
        """只验证 wxid 邻近窗口里的 u32。"""
        seen: set[int] = set()
        for _base, data in memory.iter_chunks(
            chunk=_CHUNK, overlap=_NEEDLE_OVERLAP
        ):
            if self._clock() > deadline:
                return None
            position = data.find(needle)
            while position >= 0:
                low = max(0, position - _PROXIMITY_RADIUS)
                high = min(len(data), position + len(needle) + _PROXIMITY_RADIUS)
                low -= low % 4
                for offset in range(low, high - 3, 4):
                    word = int.from_bytes(data[offset : offset + 4], "little")
                    if word & 0xFF != xor_key or word in seen:
                        continue
                    seen.add(word)
                    if self._try_word(word, block, wxid):
                        return word
                position = data.find(needle, position + 1)
        return None

    def _scan_full(
        self,
        memory: Any,
        block: bytes,
        xor_key: int,
        wxid: str,
        deadline: float,
    ) -> int | None:
        """全量兜底：扫描进程内所有 4 字节对齐的 u32。"""
        seen: set[int] = set()
        for _base, data in memory.iter_chunks(chunk=_CHUNK, overlap=0):
            if self._clock() > deadline:
                return None
            for word in _iter_words(data):
                if word & 0xFF != xor_key or word in seen or word >= _UIN_MAX:
                    continue
                seen.add(word)
                if self._try_word(word, block, wxid):
                    return word
        return None

    def _try_word(self, word: int, block: bytes, wxid: str) -> bool:
        """用候选 uin 派生 AES key，验证它能否解出强图片头。"""
        key = derive_aes_key(word, wxid)
        try:
            plain = aes_ecb_decrypt(key, block)
        except (CipherError, ValueError, TypeError):
            return False
        return _strong_image_head(plain)

    # ------------------------------------------------------------- 环境
    def _validates(self, key: MediaKey, sample: bytes) -> bool:
        try:
            decoded = decode_dat(
                sample, aes_key=key.aes_key, xor_key=key.xor_key
            )
        except Exception:  # noqa: BLE001 - 校验失败一律当作不可用
            return False
        return decoded is not None and _strong_image_head(decoded[:16])

    def _resolve_wxid(self) -> str:
        if self._wxid:
            return self._wxid
        root = self._resolve_db_root()
        if root is None:
            return ""
        self._wxid = str(keyscan.self_username(root) or "")
        return self._wxid

    def _resolve_db_root(self) -> Path | None:
        if self._db_root is not None:
            return self._db_root
        if self._storage_root:
            account = Path(self._storage_root)
            candidate = account / "db_storage"
            self._db_root = candidate if candidate.is_dir() else account
            return self._db_root
        try:
            self._db_root = keyscan.find_db_root()
        except Exception:  # noqa: BLE001 - 探测失败只是没密钥
            self._db_root = None
        return self._db_root

    def _account_root(self) -> Path | None:
        if self._storage_root:
            return Path(self._storage_root)
        root = self._resolve_db_root()
        return Path(root).parent if root is not None else None

    def _find_sample(self) -> bytes | None:
        """从真实 attach 目录挑一个可读的 V2 ``.dat`` 当样本。"""
        account = self._account_root()
        if account is None:
            return None
        attach = account / "msg" / "attach"
        try:
            candidates = list(
                itertools.islice(attach.glob("*/20*/Img/*.dat"), 60)
            )
        except OSError:
            return None
        # 优先小体积缩略图：读得快，且几乎总是 V2。
        candidates.sort(
            key=lambda path: (
                0 if path.name.endswith("_t.dat") else 1,
                self._size(path),
            )
        )
        for path in candidates:
            try:
                with path.open("rb") as handle:
                    head = handle.read(16)
                if not head.startswith(_V2_SIGNATURE):
                    continue
                return path.read_bytes()
            except OSError:
                continue
        return None

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0
