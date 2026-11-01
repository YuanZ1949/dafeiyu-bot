"""微信接入占位适配器。

2026-10 调研结论：开源的个人微信 hook 方案已基本消亡 —— WeChatFerry 仓库已归档，
Gewechat 官方宣布停止维护。仍可选的路径只有三条：

1. WeChatPadPro（付费 iPad 协议，LangBot 内置 wechatpad_api）—— 稳定些，但要钱，仍违规；
2. wechatauto-replica / wxauto（Windows 桌面 RPA）—— 免费，但依赖微信窗口常驻，
   微信一升级就可能失效；
3. 企业微信 / 公众号 —— 合规稳定，但进不了普通微信群。

接入时实现与 QQAdapter 相同的 Adapter 协议，在 __main__.py 把 WeChatAdapter 换掉即可，
主体路由 / 记忆 / 人设代码不需要改。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..models import ChatType, Message


class WeChatAdapter:
    """未实现的微信适配器：保留插头，v1 不接。"""

    def __init__(self) -> None:
        self.on_message: Callable[[Message], Awaitable[None]] | None = None

    async def start(self) -> None:
        raise NotImplementedError("微信接入未实现，见 dafeiyu/gateway/wechat.py docstring")

    async def stop(self) -> None:
        return None

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str | None:
        raise NotImplementedError("微信接入未实现，见 dafeiyu/gateway/wechat.py docstring")
