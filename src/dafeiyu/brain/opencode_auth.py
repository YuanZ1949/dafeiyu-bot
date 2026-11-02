"""opencode serve 的 Basic 认证口令（P7）。

opencode v2 的 serve 强制 Basic 认证：用户名固定 ``opencode``，密码由环境变量
``OPENCODE_SERVER_PASSWORD`` 决定。supervisor 拉起 serve 时如果没把口令传下去，
serve 每次启动都会随机生成一个，bot 拿不到就全是 401。这里统一口令的解析与
注入方式，让「真正拉起 serve 的进程」和「连 serve 的 bot 进程」拿到同一个值。

安全约定：口令只放进程环境，**绝不落盘、不写日志、不进异常消息**。随机口令
因此天然是一次性的——同一次启动里两个子进程共用即可，重启换新无妨。
"""

from __future__ import annotations

import secrets

# serve 与 bot 子进程共用的变量名；名字由 opencode 侧约定，不能改。
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"

# token_hex(16) 正好是 32 位十六进制，与部署文档 P7 的手动口令格式一致。
_TOKEN_BYTES = 16


def resolve_password(configured: str) -> str:
    """返回本次启动使用的 serve 口令。

    - 配置或环境给了值（strip 后非空）：原样返回，供 serve 与 bot 共用；
    - 没给：每次调用生成一个新的 32 位十六进制口令。

    调用方只应把它写进子进程 env，不要写日志或落盘。
    """
    value = (configured or "").strip()
    if value:
        return value
    return secrets.token_hex(_TOKEN_BYTES)


def password_env(password: str) -> dict[str, str]:
    """把口令包成可直接 ``dict.update`` 进子进程 env 的映射。"""
    return {PASSWORD_ENV: password}
