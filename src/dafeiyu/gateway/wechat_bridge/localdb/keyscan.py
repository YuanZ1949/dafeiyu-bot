"""微信 4.1 本地数据库密钥的**只读**提取与缓存。

背景（Windows / 微信 4.1.15.13 实测）：每个 ``.db`` 的前 16 字节是独立随机
salt，``page1[16:4096]`` 是密文。密钥**不是**以明文 ``X'<hex>'`` 字面量存在于
内存里（实测全量扫描 0 命中），而是保存在 WCDB 的
``com.Tencent.WCDB.Config.Cipher`` 对象中：对象里的值是一段用固定掩码 XOR
混淆的字符串，上下文里还带着它自己的 salt。本模块只做三件事：

1. 定位 ``~/Documents/xwechat_files/<wxid>_<suffix>/db_storage`` 并读出每个库的
   page1 / salt（``find_db_root`` / ``collect_pages`` / ``salts_of``）；
2. 用 ``VirtualQueryEx`` + ``ReadProcessMemory`` **只读**扫描微信进程内存：先按
   ``Config.Cipher`` 的结构跟随指针链取出 XOR 混淆的值并解码，再回退到「全内存
   找 ``X'<hex>'`` 字面量」（老版本构建），候选一律交给 page1 HMAC 校验筛出
   真正可用的密钥（``ProcessMemory`` / ``scan_pids`` / ``extract_keys``）；
3. 把结果落盘缓存，下次启动先验缓存，失败才重新扫描（``load_or_extract``）。

设计约束：

- 纯标准库；``psutil`` 只是可选加速，缺失时回退 ``ctypes``；
- 不实现也不修改 SQLCipher 解密逻辑，真正的 page1 HMAC 校验在 ``.sqlcipher``
  里，本模块**惰性 import**（``sqlcipher.py`` 尚不存在时本模块照样能 import）；
- 单测通过注入 ``reader`` / ``opener`` / ``verifier`` 完成，不依赖真实进程与
  真实加密实现；
- 日志只出现计数与路径，**不出现 enc_key / salt 明文**。

本模块不写数据库、不修改微信文件，内存访问也仅用 ``PROCESS_VM_READ``。
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import inspect
import json
import logging
import os
import re
import struct
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, Self, cast

__all__ = [
    "KeyScanError",
    "ProcessMemory",
    "candidates",
    "collect_pages",
    "extract_keys",
    "find_db_root",
    "load_or_extract",
    "read_process_memory",
    "salts_of",
    "scan_pids",
    "self_username",
    "weixin_pids",
]

logger = logging.getLogger(__name__)

#: SQLCipher 页大小；page1 就是从文件头读出的前 4096 字节。
PAGE_SIZE = 4096
#: 每个库 page1 开头的随机 salt 长度。
SALT_SIZE = 16
#: enc_key 的字节长度（64 个 hex 字符）。
KEY_SIZE = 32

#: 内存里候选字面量：``X'<64..192 hex>'``（大写 X 小写 x 都接受）。
_CANDIDATE_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

#: 从 collect_pages 里排除的顶层目录（微信自己的备份 / 多账号汇总目录）。
_EXCLUDED_TOP_DIRS = frozenset({"backup", "all_users"})

#: 扫描时跨 reader 块保留的尾巴长度：足够覆盖最长候选（``X'`` + 192 hex + ``'``）。
_CANDIDATE_CARRY = 256

#: 每次最多读 1 MiB，兼顾速度与单次分配的峰值内存。
_DEFAULT_CHUNK = 1 << 20
#: 相邻块之间重叠 128 字节，避免候选被块边界切断。
_DEFAULT_OVERLAP = 0x80

# Windows 常量。放在模块级只是整数，非 Windows 平台 import 本模块不受影响。
_PROCESS_VM_READ = 0x0010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MEM_COMMIT = 0x1000
_PAGE_NOACCESS = 0x01
_PAGE_GUARD = 0x100
#: 允许读取的页保护位（PAGE_READONLY / READWRITE / WRITECOPY / EXECUTE_*）。
_READABLE_PROTECT = frozenset({0x02, 0x04, 0x08, 0x20, 0x40, 0x80})
_MAX_USER_ADDRESS = 0x0000_7FFF_FFFF_FFFF
#: 64 位地址上界（构造指针模式时按 8 字节编码）。
_STRUCT_MAX_USER_ADDRESS = 0x0000_8000_0000_0000

#: 微信 4.1 在内存里用 WCDB 的 ``Config.Cipher`` 保存密钥。它的 C++ 对象
#: 布局是稳定的：字符串名 + 值对象（值对象的前 8 字节是数据指针、随后是长度），
#: 数据本身用固定掩码 XOR 混淆。直接按这个结构跟随指针比「全内存正则」可靠得多
#: （实测明文 ``X'...'`` 字面量并不存在于当前版本的进程内存里）。
_CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
_CONFIG_CIPHER_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
_CONFIG_BLOB_MAX = 1024
#: Config 值对象在 Config 对象里的偏移；对象内 [8]=数据指针，[0x10]=长度。
_CONFIG_VALUE_OFFSET = 0x88
#: 保存「字符串对象指针 + 长度」的节点相对该指针对的偏移。
_CONFIG_NODE_BACK = 0x10
#: 节点里 Config 对象指针的偏移。
_CONFIG_NODE_CONFIG_PTR = 0x28


class KeyScanError(RuntimeError):
    """密钥定位 / 内存扫描 / 缓存校验失败。消息面向最终用户，中文。"""


# ---------------------------------------------------------------------------
# 数据库目录与 page1 收集
# ---------------------------------------------------------------------------


def find_db_root(home: Path | None = None) -> Path | None:
    """返回当前活跃账号的 ``db_storage`` 目录；找不到返回 ``None``（不抛）。

    在 ``<home>/Documents/xwechat_files/*/db_storage`` 里挑「最近活跃」的一个：
    取 ``session/session.db`` 或任一 ``*.db-wal`` 的最新 mtime。多个账号同时
    存在时只有正在登录的那个会持续写 WAL，所以这个判据比目录 mtime 可靠。
    """
    base = Path(home) if home is not None else Path.home()
    root = base / "Documents" / "xwechat_files"
    try:
        candidates_ = [p for p in root.glob("*/db_storage") if p.is_dir()]
    except OSError as exc:  # 极端情况下目录不可访问
        logger.debug("枚举微信数据目录失败: %s", exc)
        return None
    if not candidates_:
        return None
    # 目录名参与排序，保证 mtime 相同时结果稳定。
    candidates_.sort(key=lambda p: p.name)
    return max(candidates_, key=lambda p: (_activity_mtime(p), p.name))


def _activity_mtime(db_root: Path) -> float:
    """账号活跃度：session.db 与所有 ``*.db-wal`` 里最新的 mtime。"""
    stamps: list[float] = []
    try:
        session_db = db_root / "session" / "session.db"
        if session_db.is_file():
            stamps.append(session_db.stat().st_mtime)
    except OSError:
        pass
    try:
        for wal in db_root.rglob("*.db-wal"):
            try:
                stamps.append(wal.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    if stamps:
        return max(stamps)
    try:
        return db_root.stat().st_mtime
    except OSError:
        return 0.0


def collect_pages(db_root: Path) -> dict[str, bytes]:
    """收集 ``db_root`` 下每个库的 page1（完整 4096 字节）。

    返回 ``rel -> page1``，``rel`` 用正斜杠（如 ``"contact/contact.db"``）。
    跳过：不足 4096 字节的文件、非 ``.db``（含 ``-wal`` / ``-shm``）、顶层
    ``Backup`` / ``all_users`` 目录、以及直接放在 ``db_root`` 根下的文件
    （只收顶层功能目录下的库）。
    """
    root = Path(db_root)
    pages: dict[str, bytes] = {}
    if not root.is_dir():
        return pages
    for current, dirs, files in os.walk(root):
        dirs.sort()
        # 只在顶层剪掉排除目录，深层同名目录不是微信的汇总目录。
        if Path(current) == root:
            dirs[:] = [d for d in dirs if d.casefold() not in _EXCLUDED_TOP_DIRS]
        for name in sorted(files):
            lower = name.casefold()
            if not lower.endswith(".db") or lower.endswith(("-wal", "-shm")):
                continue
            path = Path(current) / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if "/" not in rel:  # 根目录下的散落 .db 不是功能库
                continue
            try:
                with path.open("rb") as handle:
                    page1 = handle.read(PAGE_SIZE)
            except OSError as exc:
                logger.debug("跳过不可读的数据库 %s: %s", rel, exc)
                continue
            if len(page1) < PAGE_SIZE:
                continue
            pages[rel] = page1
    return pages


def salts_of(pages: Mapping[str, bytes]) -> dict[str, str]:
    """``rel -> page1 前 16 字节的 salt hex``。"""
    return {rel: page[:SALT_SIZE].hex() for rel, page in pages.items() if len(page) >= SALT_SIZE}


# ---------------------------------------------------------------------------
# 内存候选与进程扫描
# ---------------------------------------------------------------------------


def candidates(blob: bytes) -> Iterator[tuple[bytes, bytes | None]]:
    """从一段内存字节里按出现顺序抽出 ``(enc_key, embedded_salt)``。

    字面量形如 ``X'<64 hex>'``（salt 为 ``None``）或
    ``X'<64 hex enc_key><32 hex salt>'``。超过 96 个 hex 字符的长串按 32 字符
    步进切窗（与参考实现一致），避免漏掉拼接在一起的候选。
    """
    for match in _CANDIDATE_RE.finditer(blob):
        run = match.group(1).decode("ascii")
        starts = [0]
        if len(run) > 96:
            starts.extend(range(32, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start < 0 or start + 64 > len(run):
                continue
            try:
                enc_key = bytes.fromhex(run[start : start + 64])
            except ValueError:
                continue
            embedded: bytes | None = None
            if start + 96 <= len(run):
                try:
                    embedded = bytes.fromhex(run[start + 64 : start + 96])
                except ValueError:
                    embedded = None
            yield enc_key, embedded


def _iter_region_chunks(
    read: Callable[[int, int], bytes],
    base: int,
    size: int,
    *,
    chunk: int,
    overlap: int,
) -> Iterator[tuple[int, bytes]]:
    """把一段内存按 ``chunk`` 切开，块间保留 ``overlap`` 字节尾巴。

    ``read(address, length)`` 由调用方提供（真实进程或测试替身）。读失败
    （返回空）时不保留尾巴，直接从下一块继续。产出 ``(块首地址, 数据)``；
    带尾巴的块，其首地址已经减掉尾巴长度，保证地址与数据对齐。
    """
    offset = 0
    tail = b""
    while offset < size:
        want = min(chunk, size - offset)
        data = read(base + offset, want)
        if data:
            combined = tail + data
            yield base + offset - len(tail), combined
            tail = combined[-overlap:] if overlap > 0 else b""
        else:
            tail = b""
        offset += want


def read_process_memory(
    pid: int,
    *,
    chunk: int = _DEFAULT_CHUNK,
    overlap: int = _DEFAULT_OVERLAP,
) -> Iterator[bytes]:
    """只读遍历进程内存，按块产出字节（兼容旧接口）。

    使用 ``OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION)``
    + ``VirtualQueryEx`` + ``ReadProcessMemory``；跳过非 ``MEM_COMMIT``、
    ``PAGE_NOACCESS``、``PAGE_GUARD`` 的区域。每块之间保留 ``overlap`` 字节，
    防止候选被块边界切断。任何失败都收敛成 :class:`KeyScanError`（中文消息），
    不会把 ``OSError`` 漏给调用方。
    """
    if sys.platform != "win32":
        raise KeyScanError("密钥内存扫描仅支持 Windows（当前平台不支持）")
    if chunk <= 0:
        raise KeyScanError("扫描块大小必须为正数")
    if overlap < 0 or overlap >= chunk:
        raise KeyScanError("扫描块重叠长度必须满足 0 <= overlap < chunk")
    return _iter_process_memory(pid, chunk=chunk, overlap=overlap)


def _iter_process_memory(pid: int, *, chunk: int, overlap: int) -> Iterator[bytes]:
    with ProcessMemory(pid) as memory:
        for _base, data in memory.iter_chunks(chunk=chunk, overlap=overlap):
            yield data


class _MemoryBasicInformation(ctypes.Structure):
    """``MEMORY_BASIC_INFORMATION``（64 位布局）。"""

    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("__alignment1", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("__alignment2", ctypes.c_ulong),
    ]


def _load_kernel32() -> Any:
    """加载 kernel32 并声明本模块用到的函数签名（句柄在 64 位下必须声明）。"""
    import ctypes.wintypes as wt

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError) as exc:  # pragma: no cover - 仅异常环境
        raise KeyScanError(f"无法加载 Windows 内核接口: {exc}") from exc

    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.VirtualQueryEx.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(_MemoryBasicInformation),
        ctypes.c_size_t,
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wt.BOOL
    return kernel32


class ProcessMemory:
    """单个进程的只读内存视图：可读区域列表 + 按地址读 + 分块迭代。

    打开句柄失败或平台不支持时抛 :class:`KeyScanError`；单次读失败只返回
    ``b""``（内存可能在读的过程中被释放），由上层决定是否继续。
    """

    def __init__(self, pid: int) -> None:
        if sys.platform != "win32":
            raise KeyScanError("密钥内存扫描仅支持 Windows（当前平台不支持）")
        self.pid = int(pid)
        self._kernel32 = _load_kernel32()
        self.regions: list[tuple[int, int]] = []
        self._handle: Any = self._open()
        try:
            self.regions = self._enumerate_regions()
        except Exception:
            self.close()
            raise

    # ------------------------------------------------------------- 公共接口
    def read(self, address: int, size: int) -> bytes:
        """按绝对地址读 ``size`` 字节；失败返回已读到的部分（可能为空）。"""
        if size <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        try:
            ok = self._kernel32.ReadProcessMemory(
                self._handle,
                ctypes.c_void_p(int(address)),
                buffer,
                size,
                ctypes.byref(read),
            )
        except OSError:
            return b""
        if not ok or not read.value:
            return b""
        return buffer.raw[: read.value]

    def iter_chunks(
        self, *, chunk: int = _DEFAULT_CHUNK, overlap: int = _DEFAULT_OVERLAP
    ) -> Iterator[tuple[int, bytes]]:
        """按可读区域分块产出 ``(块起始地址, 数据)``。"""
        if chunk <= 0:
            raise KeyScanError("扫描块大小必须为正数")
        if overlap < 0 or overlap >= chunk:
            raise KeyScanError("扫描块重叠长度必须满足 0 <= overlap < chunk")
        for base, size in self.regions:
            yield from _iter_region_chunks(
                self.read, base, size, chunk=chunk, overlap=overlap
            )

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle:
            with contextlib.suppress(OSError):
                self._kernel32.CloseHandle(handle)
            self._handle = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- 内部
    def _open(self) -> Any:
        handle = self._kernel32.OpenProcess(
            _PROCESS_VM_READ | _PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid
        )
        if not handle:
            error = ctypes.get_last_error()
            raise KeyScanError(
                f"无法打开微信进程（PID={self.pid}，错误码 {error}）；"
                "请确认微信已登录，并尝试以管理员身份运行"
            )
        return handle

    def _enumerate_regions(self) -> list[tuple[int, int]]:
        regions: list[tuple[int, int]] = []
        address = 0
        while address < _MAX_USER_ADDRESS:
            info = _MemoryBasicInformation()
            queried = self._kernel32.VirtualQueryEx(
                self._handle,
                ctypes.c_void_p(address),
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not queried:
                break
            base = int(info.BaseAddress or 0)
            size = int(info.RegionSize or 0)
            if size <= 0:
                break
            protect = int(info.Protect)
            if (
                int(info.State) == _MEM_COMMIT
                and protect not in (0, _PAGE_NOACCESS)
                and not (protect & _PAGE_GUARD)
                and (protect & 0xFF) in _READABLE_PROTECT
            ):
                regions.append((base, size))
            next_address = base + size
            if next_address <= address:
                break
            address = next_address
        return regions


# ---------------------------------------------------------------------------
# WCDB Config.Cipher：按结构跟随指针取密钥
# ---------------------------------------------------------------------------


def _xor_repeat(data: bytes, mask: bytes) -> bytes:
    """用固定掩码循环 XOR（Config.Cipher 值本身的混淆方式）。"""
    if not mask:
        raise ValueError("XOR 掩码不能为空")
    return bytes(value ^ mask[index % len(mask)] for index, value in enumerate(data))


def _u64_from(data: bytes, offset: int) -> int:
    """按小端读 u64；越界返回 0（指针链任何一环坏掉都当作无候选）。"""
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]


def _probable_key(data: bytes) -> bool:
    """32 字节且不是全 0/全 1、字符分布不退化：过滤掉误命中的随机指针。"""
    if len(data) != KEY_SIZE:
        return False
    return len(set(data)) >= 15 and data not in {b"\x00" * KEY_SIZE, b"\xff" * KEY_SIZE}


def _needle_addresses(memory: ProcessMemory) -> set[int]:
    """找出 ``com.Tencent.WCDB.Config.Cipher`` 字符串在内存里的所有地址。"""
    overlap = max(0, len(_CONFIG_CIPHER_NAME) - 1)
    addresses: set[int] = set()
    for base, data in memory.iter_chunks(chunk=_DEFAULT_CHUNK, overlap=overlap):
        position = data.find(_CONFIG_CIPHER_NAME)
        while position >= 0:
            addresses.add(base + position)
            position = data.find(_CONFIG_CIPHER_NAME, position + 1)
    return addresses


def _config_blob_keys(blob: bytes) -> Iterator[tuple[bytes, bytes | None]]:
    """把 Config.Cipher 值解码成 ``(enc_key, embedded_salt)`` 候选。"""
    if not blob or len(blob) > _CONFIG_BLOB_MAX:
        return
    decoded = _xor_repeat(blob, _CONFIG_CIPHER_XOR_MASK)
    for enc_key, embedded_salt in candidates(decoded):
        if _probable_key(enc_key):
            yield enc_key, embedded_salt


def _scan_config_cipher(
    memory: ProcessMemory,
    remaining: dict[str, bytes],
    verify: Callable[[bytes, bytes], bool],
    found: dict[str, bytes],
) -> int:
    """按 Config.Cipher 结构扫描；返回本轮匹配到的库数量。"""
    needles = _needle_addresses(memory)
    if not needles:
        return 0
    patterns = [
        struct.pack("<Q", address) + struct.pack("<Q", len(_CONFIG_CIPHER_NAME))
        for address in needles
    ]
    matched = 0
    seen_blobs: set[bytes] = set()
    for base, data in memory.iter_chunks(chunk=_DEFAULT_CHUNK, overlap=0x80):
        if not remaining:
            break
        for pattern in patterns:
            position = data.find(pattern)
            while position >= 0:
                node = memory.read(base + position - _CONFIG_NODE_BACK, 0x50)
                if (
                    len(node) >= 0x40
                    and _u64_from(node, 0x10) in needles
                    and _u64_from(node, 0x18) == len(_CONFIG_CIPHER_NAME)
                ):
                    config_ptr = _u64_from(node, _CONFIG_NODE_CONFIG_PTR)
                    if 0x10000 <= config_ptr < _STRUCT_MAX_USER_ADDRESS:
                        value = memory.read(config_ptr + _CONFIG_VALUE_OFFSET, 0x28)
                        if len(value) >= 0x18:
                            data_ptr = _u64_from(value, 0x8)
                            data_len = _u64_from(value, 0x10)
                            if (
                                0 < data_len <= _CONFIG_BLOB_MAX
                                and 0x10000 <= data_ptr < _STRUCT_MAX_USER_ADDRESS
                            ):
                                blob = memory.read(data_ptr, int(data_len))
                                if blob and len(blob) == data_len and blob not in seen_blobs:
                                    seen_blobs.add(blob)
                                    for enc_key, embedded in _config_blob_keys(blob):
                                        matched += _accept_candidate(
                                            enc_key, embedded, remaining, verify, found
                                        )
                position = data.find(pattern, position + 1)
    return matched


def _scan_literal_chunks(
    chunks: Iterator[tuple[int, bytes]],
    remaining: dict[str, bytes],
    verify: Callable[[bytes, bytes], bool],
    found: dict[str, bytes],
) -> int:
    """老版本兼容路径：直接在内存里找明文 ``X'<hex>'`` 字面量。"""
    matched = 0
    for _base, data in chunks:
        for enc_key, embedded in candidates(data):
            matched += _accept_candidate(enc_key, embedded, remaining, verify, found)
        if not remaining:
            break
    return matched


def _accept_candidate(
    enc_key: bytes,
    embedded_salt: bytes | None,
    remaining: dict[str, bytes],
    verify: Callable[[bytes, bytes], bool],
    found: dict[str, bytes],
) -> int:
    """校验一个候选：只对仍然缺密钥的库做 HMAC 校验，命中即从待匹配集合删除。"""
    if embedded_salt is not None:
        salt_hex = embedded_salt.hex()
        targets = [salt_hex] if salt_hex in remaining else []
    else:
        targets = list(remaining)
    matched = 0
    for salt_hex in targets:
        page1 = remaining.get(salt_hex)
        if page1 is None:
            continue
        try:
            ok = verify(enc_key, page1)
        except ValueError:
            ok = False
        if ok:
            found[salt_hex] = enc_key
            del remaining[salt_hex]
            matched += 1
    return matched



class _ProcessInfo(Protocol):
    """``psutil.Process.info`` 里本模块用到的字段。"""

    @property
    def info(self) -> Mapping[str, object]: ...


class _PsutilModule(Protocol):
    def process_iter(self, attrs: Sequence[str]) -> Iterator[_ProcessInfo]: ...


def weixin_pids() -> list[int]:
    """返回 ``Weixin.exe`` 的进程号；找不到 / 无权限返回 ``[]``（不抛）。

    优先用 ``psutil``（已安装时更省事），缺失或调用失败时回退
    ``psapi.EnumProcesses`` + ``QueryFullProcessImageNameW``。
    """
    try:
        # 可选依赖：用 importlib 静态分析看不到它，没装也不影响本模块 import。
        psutil = cast(
            _PsutilModule, cast(object, importlib.import_module("psutil"))
        )
    except ImportError:
        return _weixin_pids_ctypes()
    try:
        pids: list[int] = []
        for proc in psutil.process_iter(["pid", "name"]):
            info = proc.info
            if str(info.get("name") or "").casefold() != "weixin.exe":
                continue
            pid = info.get("pid")
            if isinstance(pid, int):
                pids.append(pid)
    except Exception as exc:  # noqa: BLE001 - psutil 在某些受限环境会整体失败
        logger.debug("psutil 枚举进程失败，回退 ctypes: %s", exc)
        return _weixin_pids_ctypes()
    return pids


def _weixin_pids_ctypes() -> list[int]:
    """不依赖 psutil 的进程枚举；仅 Windows 可用。"""
    if sys.platform != "win32":
        return []
    import ctypes.wintypes as wt

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
    except (OSError, AttributeError) as exc:  # pragma: no cover - 仅异常环境
        logger.debug("无法加载进程枚举接口: %s", exc)
        return []

    psapi.EnumProcesses.argtypes = [
        ctypes.POINTER(wt.DWORD),
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
    ]
    psapi.EnumProcesses.restype = wt.BOOL
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.LPWSTR,
        ctypes.POINTER(wt.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL

    capacity = 4096
    buffer = (wt.DWORD * capacity)()
    needed = wt.DWORD(0)
    try:
        ok = psapi.EnumProcesses(buffer, ctypes.sizeof(buffer), ctypes.byref(needed))
    except OSError as exc:  # pragma: no cover - 防御性
        logger.debug("EnumProcesses 失败: %s", exc)
        return []
    if not ok:
        return []

    count = int(needed.value) // ctypes.sizeof(wt.DWORD)
    found: list[int] = []
    for index in range(min(count, capacity)):
        pid = int(buffer[index])
        if pid <= 0:
            continue
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            name_buffer = ctypes.create_unicode_buffer(1024)
            length = wt.DWORD(len(name_buffer))
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, name_buffer, ctypes.byref(length)
            ):
                name = name_buffer.value.rsplit("\\", 1)[-1]
                if name.casefold() == "weixin.exe":
                    found.append(pid)
        except OSError:  # pragma: no cover - 防御性
            continue
        finally:
            kernel32.CloseHandle(handle)
    return found


def _resolve_verifier(
    verifier: Callable[[bytes, bytes], bool] | None,
) -> Callable[[bytes, bytes], bool]:
    """拿到校验函数：注入的优先，否则惰性 import ``.sqlcipher.verify_page``。

    仓库里 ``sqlcipher.verify_page`` 的签名是 ``(page, enc_key, pgno, salt=None)``，
    而本模块对外注入的 verifier 约定是 ``(enc_key, page1)``。这里按第一个形参名
    适配，既不写死某一版实现，也不要求调用方知道顺序。
    """
    if verifier is not None:
        return verifier
    try:
        from .sqlcipher import verify_page
    except ImportError as exc:
        raise KeyScanError(
            "缺少 page1 HMAC 校验实现（dafeiyu.gateway.wechat_bridge.localdb.sqlcipher）"
        ) from exc

    parameters = list(inspect.signature(verify_page).parameters)
    page_first = bool(parameters) and parameters[0] == "page"

    def _verify(enc_key: bytes, page1: bytes) -> bool:
        if page_first:
            return bool(verify_page(page1, enc_key, 1))
        return bool(verify_page(enc_key, page1, 1))

    return _verify


def scan_pids(
    pids: Sequence[int],
    salt_to_page1: Mapping[str, bytes],
    *,
    reader: Callable[..., Iterator[bytes]] = read_process_memory,
    verifier: Callable[[bytes, bytes], bool] | None = None,
    opener: Callable[[int], Any] | None = None,
) -> dict[str, bytes]:
    """在若干进程里只读扫描，返回 ``salt_hex -> enc_key``。

    默认走 **结构化扫描**：先找 ``com.Tencent.WCDB.Config.Cipher`` 字符串，
    顺着 WCDB 的对象指针取出 XOR 混淆的密钥串，再回退到「全内存找明文
    ``X'<hex>'`` 字面量」（老版本微信 / 某些构建）。只有通过
    ``verifier(enc_key, page1)`` 的候选才会被采纳；匹配到的 salt 立即从待匹配
    集合里删掉（多个库可能共用同一把 key），全部匹配完提前结束。

    注入 ``reader``（或 ``opener``）时按注入的实现走，便于单测完全不碰真实进程。
    某个 PID 失败只记日志并继续下一个；一个都没匹配到抛 :class:`KeyScanError`。
    """
    verify = _resolve_verifier(verifier)
    remaining: dict[str, bytes] = dict(salt_to_page1)
    found: dict[str, bytes] = {}
    if not remaining:
        raise KeyScanError("没有可用的数据库 salt，无法校验密钥")

    pid_errors: list[str] = []
    structured = opener is not None or reader is read_process_memory

    for pid in pids:
        if not remaining:
            break
        if structured:
            factory = opener or ProcessMemory
            try:
                memory = factory(pid)
            except (KeyScanError, OSError) as exc:
                pid_errors.append(str(exc))
                logger.info("打开微信进程 PID=%s 失败，跳过：%s", pid, exc)
                continue
            try:
                _scan_config_cipher(memory, remaining, verify, found)
                if remaining:
                    _scan_literal_chunks(
                        memory.iter_chunks(
                            chunk=_DEFAULT_CHUNK, overlap=_CANDIDATE_CARRY
                        ),
                        remaining,
                        verify,
                        found,
                    )
            except (KeyScanError, OSError) as exc:
                pid_errors.append(str(exc))
                logger.info("扫描微信进程 PID=%s 失败，跳过：%s", pid, exc)
            finally:
                close = getattr(memory, "close", None)
                if callable(close):
                    close()
            continue

        try:
            stream = reader(pid)
        except (KeyScanError, OSError) as exc:
            pid_errors.append(str(exc))
            logger.info("读取微信进程 PID=%s 失败，跳过：%s", pid, exc)
            continue
        try:
            _scan_reader_stream(stream, remaining, verify, found)
        except (KeyScanError, OSError) as exc:
            pid_errors.append(str(exc))
            logger.info("扫描微信进程 PID=%s 失败，跳过：%s", pid, exc)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    if not found:
        detail = f"（{len(pid_errors)} 个进程读取失败）" if pid_errors else ""
        raise KeyScanError(
            "未能从微信进程内存里提取到可用密钥"
            f"{detail}；请确认微信已登录、版本为 4.1+，并尝试以管理员身份运行"
        )
    logger.info("内存扫描匹配 %d/%d 个数据库密钥", len(found), len(salt_to_page1))
    return found


def _scan_reader_stream(
    stream: Iterator[bytes],
    remaining: dict[str, bytes],
    verify: Callable[[bytes, bytes], bool],
    found: dict[str, bytes],
) -> None:
    """兼容路径：把注入 reader 的字节流按 ``X'<hex>'`` 字面量扫描。

    块之间保留 `_CANDIDATE_CARRY` 字节尾巴，避免候选被块边界切断；重复候选只
    校验一次（同一把 key 在内存里常出现多次）。
    """
    seen_candidates: set[tuple[bytes, bytes | None]] = set()
    attempted: set[tuple[bytes, str]] = set()
    carry = b""
    for blob in stream:
        if not blob:
            continue
        data = carry + blob if carry else blob
        carry = data[-_CANDIDATE_CARRY:] if len(data) > _CANDIDATE_CARRY else data
        for enc_key, embedded_salt in candidates(data):
            marker = (enc_key, embedded_salt)
            if marker in seen_candidates:
                continue
            seen_candidates.add(marker)
            if embedded_salt is not None:
                salt_hex = embedded_salt.hex()
                targets = [salt_hex] if salt_hex in remaining else []
            else:
                targets = list(remaining)
            for salt_hex in targets:
                page1 = remaining.get(salt_hex)
                if page1 is None:
                    continue
                pair = (enc_key, salt_hex)
                if pair in attempted:
                    continue
                attempted.add(pair)
                if verify(enc_key, page1):
                    found[salt_hex] = enc_key
                    del remaining[salt_hex]
        if not remaining:
            break


# ---------------------------------------------------------------------------
# 对外入口：提取与缓存
# ---------------------------------------------------------------------------


def extract_keys(
    db_root: Path,
    *,
    pids: Sequence[int] | None = None,
    reader: Callable[..., Iterator[bytes]] = read_process_memory,
    verifier: Callable[[bytes, bytes], bool] | None = None,
    opener: Callable[[int], Any] | None = None,
) -> dict[str, bytes]:
    """扫描内存并返回 ``rel -> enc_key``，覆盖 ``db_root`` 下所有可匹配的库。"""
    pages = collect_pages(db_root)
    if not pages:
        raise KeyScanError(f"在 {db_root} 下没有找到可解密的 .db 文件")
    salts = salts_of(pages)
    salt_to_page1: dict[str, bytes] = {}
    for rel, salt_hex in salts.items():
        salt_to_page1.setdefault(salt_hex, pages[rel])

    target_pids = list(pids) if pids is not None else weixin_pids()
    matched = scan_pids(
        target_pids,
        salt_to_page1,
        reader=reader,
        verifier=verifier,
        opener=opener,
    )
    result = {
        rel: matched[salt_hex]
        for rel, salt_hex in salts.items()
        if salt_hex in matched
    }
    if not result:
        raise KeyScanError("没有为任何数据库找到可用密钥")
    logger.info("提取完成：%d/%d 个数据库有密钥", len(result), len(salts))
    return result


def load_or_extract(
    db_root: Path,
    cache_path: Path,
    *,
    refresh: bool = False,
    pids: Sequence[int] | None = None,
    reader: Callable[..., Iterator[bytes]] = read_process_memory,
    verifier: Callable[[bytes, bytes], bool] | None = None,
    opener: Callable[[int], Any] | None = None,
) -> dict[str, bytes]:
    """先验缓存、失败再扫描，成功后原子写缓存。

    缓存是 JSON（形状与 wcdb-key-tool 一致）::

        {"contact/contact.db": {"enc_key": "<hex>", "salt": "<hex>", "size_mb": 0.2},
         "_db_dir": "..."}

    读取时同时兼容旧工具写出的反斜杠键与 ``_db_dir`` 元数据。只有**每个**
    当前库的 salt 一致且 ``verify_page`` 通过才算缓存有效；否则重新扫描。
    """
    root = Path(db_root)
    cache = Path(cache_path)
    pages = collect_pages(root)
    salts = salts_of(pages)
    verify = _resolve_verifier(verifier)

    if not refresh:
        cached = _read_cache(cache)
        if cached is not None and pages:
            keys = _validate_cache(cached, pages, salts, verify)
            if keys is not None:
                logger.info("使用缓存密钥：%d/%d 个数据库", len(keys), len(salts))
                return keys

    keys = extract_keys(
        root, pids=pids, reader=reader, verifier=verifier, opener=opener
    )
    _write_cache(cache, root, keys, pages, salts)
    return keys


def _read_cache(cache_path: Path) -> dict[str, object] | None:
    """读取缓存 JSON；不存在 / 损坏 / 不是对象都返回 ``None``。"""
    try:
        with cache_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.info("密钥缓存不可用，将重新扫描：%s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    # 旧工具写的是 Windows 反斜杠键，统一成我们的正斜杠形式。
    normalized: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        normalized[key.replace("\\", "/")] = value
    return normalized


def _validate_cache(
    cached: Mapping[str, object],
    pages: Mapping[str, bytes],
    salts: Mapping[str, str],
    verify: Callable[[bytes, bytes], bool],
) -> dict[str, bytes] | None:
    """逐库校验缓存；全部有效返回 ``rel -> enc_key``，否则 ``None``。"""
    result: dict[str, bytes] = {}
    for rel, salt_hex in salts.items():
        entry = cached.get(rel)
        if not isinstance(entry, dict):
            return None
        entry_salt = str(entry.get("salt") or "").casefold()
        enc_key_hex = str(entry.get("enc_key") or "")
        if entry_salt != salt_hex.casefold() or len(enc_key_hex) != KEY_SIZE * 2:
            return None
        try:
            enc_key = bytes.fromhex(enc_key_hex)
        except ValueError:
            return None
        if not verify(enc_key, pages[rel]):
            return None
        result[rel] = enc_key
    return result if result else None


def _write_cache(
    cache_path: Path,
    db_root: Path,
    keys: Mapping[str, bytes],
    pages: Mapping[str, bytes],
    salts: Mapping[str, str],
) -> None:
    """原子写缓存：同目录 tmp + ``os.replace``，UTF-8、indent=2。"""
    payload: dict[str, object] = {}
    for rel, enc_key in keys.items():
        salt_hex = salts.get(rel)
        if salt_hex is None:
            continue
        try:
            size = (Path(db_root) / rel).stat().st_size
        except OSError:
            size = len(pages.get(rel, b""))
        payload[rel] = {
            "enc_key": enc_key.hex(),
            "salt": salt_hex,
            "size_mb": round(size / 1024 / 1024, 2),
        }
    if not payload:
        return
    payload["_db_dir"] = str(db_root)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, cache_path)
    except OSError as exc:
        # 缓存写失败不影响本次已经拿到的密钥，下次重新扫描即可。
        logger.warning("密钥缓存写入失败（本次结果仍可用）：%s", exc)
        return
    logger.info("已写入密钥缓存：%d 个数据库", len(payload) - 1)


def self_username(db_root: Path) -> str | None:
    """从账号目录名 ``wxid_xxx_suffix`` 解析出 ``wxid_xxx``；不合格式返回 ``None``。"""
    try:
        name = Path(db_root).parent.name
    except (OSError, ValueError):
        return None
    head, sep, tail = name.rpartition("_")
    if not sep or not tail or not head.startswith("wxid_"):
        return None
    if len(head) <= len("wxid_"):
        return None
    return head
