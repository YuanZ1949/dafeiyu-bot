"""微信 4.1 本地库只读解密镜像（SQLCipher 4 + WAL）。

三个子模块各管一件事：``sqlcipher`` 只做页解密/HMAC，``wal`` 只解析 WAL，
``mirror`` 把两者拼成可增量同步的只读副本。所有模块都只依赖标准库，导入
本包不会加载 ``bcrypt.dll``（Windows 之外也不会在 import 期崩溃）。
"""

from __future__ import annotations

from .mirror import Mirror, MirrorError, MirrorSet, SyncResult
from .sqlcipher import (
    CipherError,
    decrypt_page,
    derive_mac_key,
    verify_page,
)
from .wal import (
    WalError,
    WalFrame,
    WalImage,
    read_wal,
    read_wal_file,
)

__all__ = [
    "CipherError",
    "Mirror",
    "MirrorError",
    "MirrorSet",
    "SyncResult",
    "WalError",
    "WalFrame",
    "WalImage",
    "decrypt_page",
    "derive_mac_key",
    "read_wal",
    "read_wal_file",
    "verify_page",
]
