"""通道启用判据：bot 接线与 supervisor 进程注册共用同一份真相。"""

from __future__ import annotations

from ..config import Settings

WECHAT_PROVIDERS = frozenset({"wechat", "both"})


def wechat_enabled_for(settings: Settings) -> bool:
    """只有 provider 选中微信且 ``gateway.wechat.enabled`` 时才启用微信通道。

    先看 provider（字符串比较），qq 通道不会触碰 ``settings.wechat``：未启用时
    连 ``bridge_token`` 都不该读，更不该按桥端口去连接。
    """
    return (
        settings.gateway.provider in WECHAT_PROVIDERS
        and settings.wechat.enabled
    )
