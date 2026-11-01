"""桌面微信桥（UIA）的进程内包。

只放桥自己的东西：驱动门面（`driver`）、身份表、消息对齐、发送器、健康探测、
进程入口。主进程侧的客户端在 `dafeiyu.gateway.wechat`，两边只通过 v1 信封通信。

`__init__` 故意保持空：`python -m dafeiyu.gateway.wechat_bridge --help` 必须在没装
pyweixin / pywinauto 的机器上也能退出 0，所以这里不能有任何 import 副作用。
"""

from __future__ import annotations

__all__: list[str] = []
