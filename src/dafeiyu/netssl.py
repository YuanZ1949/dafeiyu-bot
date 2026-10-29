"""进程级 TLS 信任上下文。

`httpx` 在 `verify=True` 时为每个客户端调用
`ssl.create_default_context(cafile=certifi.where())`，即重新解析 certifi 的 118 张根证书 PEM。
本机实测单次约 50 秒，直接把 supervisor 的 30 秒就绪门槛顶穿。

这里改为「系统信任库 + 进程内复用」：Windows 证书库与 Linux/macOS 系统 CA 都是桌面程序
本来就该信任的根，加载耗时 0.1 秒级。`ssl.create_default_context()` 仍会读取
`SSL_CERT_FILE` / `SSL_CERT_DIR`，企业自定义根证书照旧生效。
"""

from __future__ import annotations

import ssl
import threading

__all__ = ["default_ssl_context"]

_context: ssl.SSLContext | None = None
_lock = threading.Lock()


def default_ssl_context() -> ssl.SSLContext:
    """返回进程内共享的、开启完整校验的 TLS 上下文。"""
    global _context
    if _context is None:
        with _lock:
            if _context is None:
                _context = _build()
    return _context


def _build() -> ssl.SSLContext:
    try:
        return ssl.create_default_context()
    except Exception:  # noqa: BLE001 - 系统信任库不可用时退回 certifi：宁可慢，也不能不校验
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
