"""微信 4.1 本地库（SQLCipher 4）页解密原语。

本模块**只做密码学**：不定位微信目录、不写任何文件、不记录密钥。上层
（``mirror``）负责把解密后的页拼成只读镜像。

微信 4.1（Windows，实测 4.1.15.13）的页布局::

    PAGE = 4096
    [0:16]      salt（只有 page 1 有意义，其余页是密文开头）
    [0:4016]    密文（page 1 为 [16:4016]）
    [4016:4032] IV
    [4032:4096] HMAC-SHA512(mac_key, 密文 + IV + 小端 pgno)

AES-256-CBC 走 Windows CNG（``bcrypt.dll``），只用标准库；非 Windows 或
DLL 缺失时抛 :class:`CipherError`，import 本模块不会失败。
"""

from __future__ import annotations

import ctypes
import functools
import hashlib
import hmac
import struct
import sys
import threading
from ctypes import wintypes
from typing import Any

__all__ = [
    "HMAC_OFFSET",
    "HMAC_SIZE",
    "IV_OFFSET",
    "PAGE_SIZE",
    "RESERVE_SIZE",
    "SALT_SIZE",
    "SQLITE_HEADER",
    "CipherError",
    "aes_cbc_decrypt",
    "decrypt_page",
    "derive_mac_key",
    "verify_page",
]

#: 微信 4.1 的页大小，固定 4096。
PAGE_SIZE = 4096
#: 每页尾部保留区（SQLCipher 4 默认 80 字节）。
RESERVE_SIZE = 80
#: salt 长度（只在 page 1 的头部）。
SALT_SIZE = 16
#: IV 在页内的偏移。
IV_OFFSET = PAGE_SIZE - RESERVE_SIZE  # 4016
#: HMAC 在页内的偏移。
HMAC_OFFSET = PAGE_SIZE - 64  # 4032
#: HMAC 长度。
HMAC_SIZE = 64
#: 解密后 page 1 的固定开头。
SQLITE_HEADER = b"SQLite format 3\x00"

#: SQLCipher 4 的密钥长度（AES-256）。
_AES_KEY_SIZE = 32
#: HMAC 密钥派生用的 PBKDF2 迭代次数（SQLCipher 4 默认值）。
_MAC_KDF_ITERATIONS = 2
#: mac_salt 的异或掩码。
_MAC_SALT_XOR = 0x3A

_bcrypt_lib: Any | None = None
_bcrypt_lock = threading.Lock()


class CipherError(RuntimeError):
    """页解密 / HMAC 校验失败。

    所有消息都是中文且**不包含密钥**，可以直接进日志。
    """


def _status_text(status: int) -> str:
    """把 NTSTATUS 规整成十六进制文本。"""
    return f"NTSTATUS={status & 0xFFFFFFFF:#010x}"


def _declare_bcrypt(lib: Any) -> None:
    """声明 CNG 函数签名。

    ``ctypes`` 默认把参数当 ``c_int``；句柄在 64 位下是指针，不声明 argtypes
    会截断成 32 位，调用直接崩。这里和本机验证过的 spike 保持完全一致。
    """
    lib.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.ULONG,
    ]
    lib.BCryptOpenAlgorithmProvider.restype = ctypes.c_long

    lib.BCryptSetProperty.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        ctypes.c_char_p,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    lib.BCryptSetProperty.restype = ctypes.c_long

    lib.BCryptGenerateSymmetricKey.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_char_p,
        wintypes.ULONG,
        ctypes.c_char_p,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    lib.BCryptGenerateSymmetricKey.restype = ctypes.c_long

    lib.BCryptDecrypt.argtypes = [
        wintypes.HANDLE,
        ctypes.c_char_p,
        wintypes.ULONG,
        ctypes.c_void_p,
        ctypes.c_char_p,
        wintypes.ULONG,
        ctypes.c_char_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.ULONG,
    ]
    lib.BCryptDecrypt.restype = ctypes.c_long

    lib.BCryptDestroyKey.argtypes = [wintypes.HANDLE]
    lib.BCryptDestroyKey.restype = ctypes.c_long

    lib.BCryptCloseAlgorithmProvider.argtypes = [wintypes.HANDLE, wintypes.ULONG]
    lib.BCryptCloseAlgorithmProvider.restype = ctypes.c_long


def _bcrypt() -> Any:
    """惰性加载 ``bcrypt.dll``；失败时抛 :class:`CipherError`。"""
    global _bcrypt_lib
    cached = _bcrypt_lib
    if cached is not None:
        return cached

    if sys.platform != "win32":
        raise CipherError("SQLCipher 页解密依赖 Windows CNG（bcrypt.dll），当前平台不支持")

    with _bcrypt_lock:
        cached = _bcrypt_lib
        if cached is not None:
            return cached
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:  # 理论上不可达：win32 一定有 WinDLL
            raise CipherError("当前 Python 缺少 ctypes.WinDLL，无法加载 bcrypt.dll")
        try:
            lib = win_dll("bcrypt")
        except OSError as exc:
            raise CipherError(f"无法加载 Windows CNG 库 bcrypt.dll：{exc}") from exc
        _declare_bcrypt(lib)
        _bcrypt_lib = lib
        return lib


def _check_key(enc_key: bytes) -> None:
    if not isinstance(enc_key, (bytes, bytearray)):
        raise TypeError("enc_key 必须是 bytes")
    if len(enc_key) != _AES_KEY_SIZE:
        raise ValueError(f"AES-256 需要 32 字节密钥，收到 {len(enc_key)} 字节")


@functools.lru_cache(maxsize=16)
def derive_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    """由 AES 密钥和数据库 salt 派生页 HMAC 密钥。

    ``mac_salt = salt XOR 0x3A``，再跑 ``PBKDF2-HMAC-SHA512(iter=2, dklen=32)``。
    这是 SQLCipher 4 的固定算法；缓存只是省掉每页一次 PBKDF2。

    注意：本函数只接受 ``bytes``（可哈希），且**禁止**把结果写日志。
    """
    _check_key(enc_key)
    if len(salt) != SALT_SIZE:
        raise ValueError(f"salt 必须是 {SALT_SIZE} 字节，收到 {len(salt)} 字节")
    mac_salt = bytes(value ^ _MAC_SALT_XOR for value in salt)
    return hashlib.pbkdf2_hmac(
        "sha512", enc_key, mac_salt, _MAC_KDF_ITERATIONS, dklen=32
    )


def _check_page(page: bytes, pgno: int) -> None:
    if not isinstance(page, (bytes, bytearray)):
        raise TypeError("page 必须是 bytes")
    if len(page) != PAGE_SIZE:
        raise ValueError(f"页长度必须是 {PAGE_SIZE}，收到 {len(page)}")
    if pgno < 1:
        raise ValueError(f"页号从 1 开始，收到 {pgno}")


def _resolve_salt(page: bytes, pgno: int, salt: bytes | None) -> bytes:
    """确定这一页 HMAC 用的 16 字节 salt。"""
    if salt is not None:
        value = bytes(salt)
        if len(value) != SALT_SIZE:
            raise ValueError(f"salt 必须是 {SALT_SIZE} 字节，收到 {len(value)} 字节")
        return value
    if pgno == 1:
        return page[:SALT_SIZE]
    raise ValueError("解密非首页时必须显式提供数据库 salt（来自 page 1 头部）")


def _hmac_payload(page: bytes, pgno: int) -> bytes:
    """参与 HMAC 的字节：page 1 跳过 salt，直到 HMAC 之前（**含 IV**）。

    SQLCipher 4 的页校验范围是「密文 + 16 字节 IV」，即 page 1 的
    ``[16:4032]``、其它页的 ``[0:4032]``。只喂密文会在真实库上全部校验失败。
    """
    start = SALT_SIZE if pgno == 1 else 0
    return page[start:HMAC_OFFSET]


def _cipher_payload(page: bytes, pgno: int) -> bytes:
    """AES-CBC 的密文区：page 1 跳过 salt，直到 IV 之前（**不含 IV**）。"""
    start = SALT_SIZE if pgno == 1 else 0
    return page[start:IV_OFFSET]


def verify_page(
    page: bytes,
    enc_key: bytes,
    pgno: int,
    salt: bytes | None = None,
) -> bool:
    """校验一页的 HMAC-SHA512。

    ``salt`` 是数据库 salt（page 1 的 ``[0:16]``）；page 1 可以省略，非首页
    必须显式传入。长度/参数不合法抛 ``ValueError``，HMAC 不匹配返回 ``False``。
    """
    _check_page(page, pgno)
    _check_key(enc_key)
    actual_salt = _resolve_salt(page, pgno, salt)
    mac_key = derive_mac_key(bytes(enc_key), actual_salt)
    digest = hmac.new(mac_key, _hmac_payload(page, pgno), hashlib.sha512)
    digest.update(struct.pack("<I", pgno))
    return hmac.compare_digest(digest.digest(), page[HMAC_OFFSET:PAGE_SIZE])


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-CBC 解密（无填充，输入必须是 16 的整数倍）。"""
    _check_key(key)
    if len(iv) != 16:
        raise ValueError(f"AES-CBC 的 IV 必须是 16 字节，收到 {len(iv)}")
    if not data:
        raise ValueError("AES-CBC 密文不能为空")
    if len(data) % 16:
        raise ValueError(f"AES-CBC 密文长度必须是 16 的整数倍，收到 {len(data)}")

    lib = _bcrypt()
    h_alg = wintypes.HANDLE()
    status = lib.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0)
    if status != 0:
        raise CipherError(f"打开 CNG AES 算法失败（{_status_text(status)}）")
    try:
        mode = "ChainingModeCBC\x00".encode("utf-16-le")
        status = lib.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0)
        if status != 0:
            raise CipherError(f"设置 CNG CBC 模式失败（{_status_text(status)}）")

        h_key = wintypes.HANDLE()
        status = lib.BCryptGenerateSymmetricKey(
            h_alg, ctypes.byref(h_key), None, 0, bytes(key), len(key), 0
        )
        if status != 0:
            raise CipherError(f"CNG 生成对称密钥失败（{_status_text(status)}）")
        try:
            iv_buf = ctypes.create_string_buffer(bytes(iv), len(iv))
            out = ctypes.create_string_buffer(len(data))
            written = ctypes.c_ulong(0)
            status = lib.BCryptDecrypt(
                h_key,
                bytes(data),
                len(data),
                None,
                iv_buf,
                len(iv),
                out,
                len(out),
                ctypes.byref(written),
                0,
            )
            if status != 0:
                raise CipherError(f"CNG AES 解密失败（{_status_text(status)}）")
            if written.value != len(data):
                raise CipherError(
                    f"CNG AES 解密输出长度异常：期望 {len(data)}，实际 {written.value}"
                )
            return out.raw[: written.value]
        finally:
            lib.BCryptDestroyKey(h_key)
    finally:
        lib.BCryptCloseAlgorithmProvider(h_alg, 0)


def decrypt_page(
    page: bytes,
    enc_key: bytes,
    pgno: int,
    salt: bytes | None = None,
) -> bytes:
    """校验并解密一页，返回 4096 字节的明文 SQLite 页。

    页尾 80 字节保留区在明文里一定是 ``0x00``（SQLite 不存数据）。page 1 的
    开头固定还原成 ``b"SQLite format 3\\x00"``。HMAC 不通过抛
    :class:`CipherError`，绝不返回未经验证的明文。
    """
    if not verify_page(page, enc_key, pgno, salt):
        raise CipherError(f"第 {pgno} 页 HMAC 校验失败（salt 不匹配或页已损坏）")

    iv = page[IV_OFFSET:IV_OFFSET + 16]
    ciphertext = _cipher_payload(page, pgno)
    plain = aes_cbc_decrypt(bytes(enc_key), iv, ciphertext)
    if pgno == 1:
        return SQLITE_HEADER + plain + b"\x00" * RESERVE_SIZE
    return plain + b"\x00" * RESERVE_SIZE
