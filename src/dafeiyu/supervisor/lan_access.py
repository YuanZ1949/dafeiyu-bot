"""面板默认对局域网开放时的启动期准备。

面板默认绑定 ``0.0.0.0``，非 loopback 绑定会强制登录；为了不让全新部署
因为「没配密码」直接起不来，口令按以下顺序解析：

1. ``data/dashboard.password``（面板内改密写回的值；重启后必须优先）
2. ``DAFEIYU_DASHBOARD_PASSWORD``（仅首次引导，长度校验在 config 层）
3. 自动生成并原子落盘，写不进去时退化为仅本次进程内存生效

会话签名密钥单独落在 ``data/dashboard.secret``，跨重启复用；否则每次重启
都会让所有已登录浏览器的 cookie 失效。

写盘与文件加固复用 control token 的实现（temp + ``os.replace``、icacls/0600）。
"""

from __future__ import annotations

import socket
from pathlib import Path

from ..config import DashboardConfig, is_loopback_host
from .control_token import REPO_ROOT, ControlToken

PASSWORD_PATH = REPO_ROOT / "data" / "dashboard.password"
SECRET_PATH = REPO_ROOT / "data" / "dashboard.secret"


def ensure_dashboard_password(
    dashboard: DashboardConfig,
    *,
    path: Path = PASSWORD_PATH,
) -> str:
    """返回本次进程要用的面板口令；鉴权关闭时返回空串且不产生文件。"""
    if not dashboard.auth_required:
        return ""
    existing = ControlToken(path).load()
    if existing:
        return existing
    if dashboard.password:
        return dashboard.password
    return ControlToken(path).load_or_create()


def ensure_dashboard_secret(*, path: Path = SECRET_PATH) -> bytes:
    """返回跨重启复用的会话签名密钥；缺失时生成并原子落盘。"""
    return ControlToken(path).load_or_create().encode("ascii")


def _detect_lan_ip() -> str:
    """探测本机出口地址；UDP connect 不发包，只让内核选路由。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return ""


def dashboard_urls(host: str, port: int, *, lan_ip: str | None = None) -> list[str]:
    """面板访问地址：本机回环始终给，绑非回环时再补一条局域网地址。"""
    urls = [f"http://127.0.0.1:{port}/"]
    if is_loopback_host(host):
        return urls
    ip = _detect_lan_ip() if lan_ip is None else lan_ip
    if ip and ip != "127.0.0.1":
        urls.append(f"http://{ip}:{port}/")
    return urls


__all__ = [
    "PASSWORD_PATH",
    "SECRET_PATH",
    "dashboard_urls",
    "ensure_dashboard_password",
    "ensure_dashboard_secret",
]
