"""OpenAI 兼容端点的模型探测与上下文分级。

只走 HTTP：绝不调用 opencode CLI 或任何子进程——用户机器上可能已经跑着一个
共享的 opencode 服务，CLI 探测会打到那个服务上，既不代表用户要填的端点，
也可能打扰用户正在用的会话。
"""

from __future__ import annotations

from typing import Any

import httpx

from .brain import provider_config

# 与两条配置生成路径共用同一个函数对象：端点规整规则只允许有一处定义。
normalize_base_url = provider_config.normalize_base_url

# 面板按 p90 上下文窗口给模型分档：低于 0.75 倍偏红，高于 1.5 倍算绿。
DEFAULT_P90_CONTEXT = 21062
PROBE_ERROR_MESSAGE = "地址不可达或不是 OpenAI 兼容接口"


class ProbeError(Exception):
    """探测失败。

    `timed_out` 用于让控制口把超时映射成 504、其余失败映射成 502；
    对外的文案仍保持同一句，免得把 httpx 的异常细节漏给面板。
    """

    def __init__(
        self, message: str = PROBE_ERROR_MESSAGE, *, timed_out: bool = False
    ) -> None:
        super().__init__(message)
        self.timed_out = timed_out


async def _get(url: str, api_key: str, timeout: float) -> Any:
    """GET 一个 URL，返回解码后的 JSON。

    这是模块里唯一的 HTTP 出口，测试替换它就不用起服务。非 2xx、超时、
    非 JSON 响应统统收敛成 ProbeError；key 为空时连 Authorization 头都不发。
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException as exc:
        raise ProbeError(timed_out=True) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ProbeError() from exc


async def probe_models(
    base_url: str, *, api_key: str = "", timeout: float = 8.0
) -> list[str]:
    """探测 `{base}/models`，返回模型 id 列表（顺序保持服务端给的样子）。

    兼容两种响应形状：标准 OpenAI 的 `{"data": [{"id": ...}]}`，以及直接给
    一个 id 对象数组的本地服务。其它形状一律按「不是 OpenAI 兼容接口」处理。
    """
    base = normalize_base_url(base_url)
    if not base:
        raise ProbeError("base_url 不能为空")
    payload = await _get(f"{base}/models", api_key, timeout)
    if isinstance(payload, dict):
        payload = payload.get("data")
    if not isinstance(payload, list):
        raise ProbeError()
    ids: list[str] = []
    for item in payload:
        model_id = item.get("id") if isinstance(item, dict) else None
        if model_id:
            ids.append(str(model_id))
    return ids


def context_grade(context: int, *, p90: int = DEFAULT_P90_CONTEXT) -> str:
    """按 p90 上下文窗口把模型分成红/黄/绿三档，给面板上色用。"""
    if context < 0.75 * p90:
        return "red"
    if context < 1.5 * p90:
        return "yellow"
    return "green"
