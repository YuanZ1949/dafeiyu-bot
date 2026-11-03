"""通道启用判据：bot 接线与 supervisor 进程注册共用同一份真相。"""

from __future__ import annotations

from ..config import Settings

WECHAT_PROVIDERS = frozenset({"wechat", "both"})
QQ_PROVIDERS = frozenset({"qq", "both"})


def wechat_enabled_for(settings: Settings) -> bool:
    """只有 provider 选中微信且 ``gateway.wechat.enabled`` 时才启用微信通道。

    先看 provider（字符串比较），qq 通道不会触碰 ``settings.wechat``：未启用时
    连 ``bridge_token`` 都不该读，更不该按桥端口去连接。
    """
    return (
        settings.gateway.provider in WECHAT_PROVIDERS
        and settings.wechat.enabled
    )


def qq_enabled_for(settings: Settings) -> bool:
    """QQ 通道是否真的会接线：与 ``build_adapters`` 的回落规则逐条对齐。

    - ``provider=qq`` → QQ 接线；
    - ``provider=both`` → QQ 接线；
    - ``provider=wechat`` → 只有微信本身没启用（``enabled=false``）时才回落 QQ。

    supervisor 用它决定是否拉起 NapCat：provider=wechat 且微信启用时 bot 根本
    不监听 QQ 的 WS 端口，拉起 NapCat 只会每 5 秒刷一条 ECONNREFUSED。
    """
    provider = str(settings.gateway.provider)
    if provider in QQ_PROVIDERS:
        return True
    return not wechat_enabled_for(settings)

