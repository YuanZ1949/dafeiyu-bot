"""OpenAI 兼容端点的模型探测与上下文分级。

只走 HTTP：绝不调用 opencode CLI 或任何子进程——用户机器上可能已经跑着一个
共享的 opencode 服务，CLI 探测会打到那个服务上，既不代表用户要填的端点，
也可能打扰用户正在用的会话。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .brain import provider_config
from .netssl import default_ssl_context
from .brain.opencode_http import DENY_ALL_PERMISSIONS, _error_status

# 与两条配置生成路径共用同一个函数对象：端点规整规则只允许有一处定义。
normalize_base_url = provider_config.normalize_base_url
# 免费档的模型 id 也只有一处定义：探测默认值直接用配置生成那边的常量。
FREE_MODEL = provider_config.FREE_MODEL

log = logging.getLogger(__name__)

# 面板按 p90 上下文窗口给模型分档：低于 0.75 倍偏红，高于 1.5 倍算绿。
# 静态页里另有一份同名常量（index.html 不能 import Python），测试会钉住两边一致。
CONTEXT_P90 = 21062
# 兼容旧名字：外部还在按这个常量取值，规则本身只允许有一个数字来源。
DEFAULT_P90_CONTEXT = CONTEXT_P90
PROBE_ERROR_MESSAGE = "地址不可达或不是 OpenAI 兼容接口"
# 免费档自检结果的缓存时长：面板每 2 秒轮询，不能每条请求都去打一次模型。
FREE_PROBE_TTL = 600
# 探测只发一句最小 prompt：能观测到 provider 的 401/429，又几乎不耗额度。
FREE_PROBE_PROMPT = "hi"
# 探测 session 一律拒掉全部工具：自检不该读文件、查网页或写下任何东西。
FREE_PROBE_PERMISSIONS = list(DENY_ALL_PERMISSIONS)
# prompt 受理后，最多以这个间隔看一次消息有没有把 provider 错误写回来。
FREE_PROBE_POLL_SECONDS = 0.4


class ProbeError(Exception):
    """探测失败。

    `timed_out` 让控制口把超时映射成 504、其余失败映射成 502；`status` 是上游
    真的回了状态码时的那个码（没回就是 None），控制口据此保留「返回 HTTP xxx」
    这种可诊断的文案。对外的默认文案仍保持同一句，不把 httpx 异常细节漏给面板。
    """

    def __init__(
        self,
        message: str = PROBE_ERROR_MESSAGE,
        *,
        timed_out: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.timed_out = timed_out
        self.status = status


async def _get(url: str, api_key: str, timeout: float) -> Any:
    """GET 一个 URL，返回解码后的 JSON。

    这是模块里唯一的 HTTP 出口，测试替换它就不用起服务。非 2xx、超时、
    非 JSON、非法 URL 统统收敛成 ProbeError；key 为空时连 Authorization 头都不发。
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=default_ssl_context()) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException as exc:
        raise ProbeError(timed_out=True) from exc
    except httpx.HTTPStatusError as exc:
        # 上游确实回了状态码：控制口据此保留「返回 HTTP xxx」的可诊断文案。
        raise ProbeError(status=exc.response.status_code) from exc
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        # InvalidURL 既不继承 HTTPError 也不继承 ValueError，必须单列，
        # 否则用户填错端口（http://h:abc/v1）会漏成 500。
        raise ProbeError() from exc


async def _post(client: Any, path: str, payload: Any, timeout: float) -> Any:
    """POST 到调用方给的服务（免费档探测时是我们自己的 opencode）。

    与 ``_get`` 一样是模块级出口：测试替换它就不用起服务。传输对象由调用方
    注入——探测不自己猜地址，也不在这里创建 client。非 2xx 直接抛异常，
    由调用方统一收敛成「不可用」。
    """
    response = await client.post(path, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


async def _messages_once(
    client: Any, session_id: str, timeout: float
) -> list[dict[str, Any]] | None:
    """读一次临时 session 的消息；读不出来返回 None。

    读取只是「加分项」：prompt 已被受理时，读不到消息不能反过来判成不可用，
    所以这里的任何异常都吞掉（含测试里传进来的 ``object()``）。
    """
    try:
        response = await client.get(
            f"/api/session/{session_id}/message",
            params={"limit": 20},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 - 读失败只算「没看到」
        # 吞掉是对的，但要留痕：否则探测结果突然变 True 时无从追。
        log.debug("探测消息读取失败", exc_info=True)
        return None
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict)]


def _session_id(payload: Any) -> str:
    """从建 session 的响应里取 id；形状不认识就当没建出来。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload
    if not isinstance(data, dict):
        return ""
    session_id = data.get("id")
    return session_id if isinstance(session_id, str) else ""


def _round_failure(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """本轮有 assistant 以 error 收尾时返回它的 error 字典（可能为空）。

    临时 session 只发一句 prompt，所以「任意一条 assistant 报错」就是失败，
    不依赖服务端给的是正序还是倒序。
    """
    for message in messages:
        if message.get("type") != "assistant":
            continue
        if message.get("finish") != "error":
            continue
        error = message.get("error")
        return error if isinstance(error, dict) else {}
    return None


def _round_settled(messages: list[dict[str, Any]]) -> bool:
    """已经拿到结论：助手已结束（finish 非 error）或本轮已 idle。"""
    for message in messages:
        if message.get("type") == "idle":
            return True
        if message.get("type") == "assistant" and message.get("finish"):
            return True
    return False


async def probe_free_model(
    client: Any, *, model: str = FREE_MODEL, timeout: float = 8.0
) -> bool:
    """探测免费模型当前是否可用：能建 session 且 prompt 被受理即算可用。

    只走 HTTP：建一个临时 session、发一句最小 prompt，再读一眼消息里有没有
    provider 写的 4xx/5xx。

    ``model`` 仅用于会话标签（`probe:<model>` 标题），不参与选模型：实际模型
    由 opencode 生成配置决定。传了非默认值时发一条 warning，免得调用方误以为
    这个参数能换模型。

    任何异常（401/403/404/429/超时/连不上/模型不存在）都返回 False：
    这个结果只决定面板亮不亮黄条，绝不能抛给调用方或拖住启动。
    """
    if model != FREE_MODEL:
        log.warning(
            "免费模型探测：model 参数（%s）仅用于会话标签，实际模型由 opencode 生成配置决定",
            model,
        )
    deadline = time.monotonic() + max(float(timeout), 0.0)

    def _budget() -> float:
        # 每次请求也按总预算截断：不能 create / prompt / 读消息各耗满一个 timeout。
        return max(deadline - time.monotonic(), 0.1)

    session_id = ""
    try:
        created = await _post(
            client,
            "/api/session",
            {
                "title": f"probe:{model}",
                "permissions": FREE_PROBE_PERMISSIONS,
            },
            _budget(),
        )
        session_id = _session_id(created)
        if not session_id:
            log.warning("免费模型探测：opencode 没有返回 session id")
            return False
        await _post(
            client,
            f"/api/session/{session_id}/prompt",
            {"text": FREE_PROBE_PROMPT},
            _budget(),
        )
        while True:
            messages = await _messages_once(client, session_id, _budget())
            if messages is None:
                # 读不到消息不代表模型不可用：prompt 已被受理，按可用处理。
                return True
            failure = _round_failure(messages)
            if failure is not None:
                log.warning(
                    "免费模型探测：provider 报错（HTTP %s）",
                    _error_status(failure) or "?",
                )
                return False
            if _round_settled(messages):
                return True
            if time.monotonic() >= deadline:
                # 观测窗口用完还没看到错误：prompt 受理即视为可用。
                return True
            await asyncio.sleep(FREE_PROBE_POLL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - 自检失败只置标志，绝不打断启动
        log.warning("免费模型探测失败：%s", exc.__class__.__name__)
        return False
    finally:
        if session_id:
            try:
                # 清理也吃总预算：探测已经把时间用完时，删除不能再用一整份
                # timeout 把请求拖住（面板/启动都在等这个后台任务）。
                await client.delete(
                    f"/api/session/{session_id}", timeout=min(1.0, _budget())
                )
            except Exception:  # noqa: BLE001 - 清理失败不影响判定
                log.debug("探测 session 删除失败：%s", session_id, exc_info=True)


async def probe_models(
    base_url: str, *, api_key: str = "", timeout: float = 8.0
) -> list[str]:
    """探测 `{base}/models`，返回模型 id 列表（顺序保持服务端给的样子）。

    兼容三种响应形状：标准 OpenAI 的 `{"data": [{"id": ...}]}`、直接给 id 对象
    数组的本地服务，以及直接给字符串 id 数组（`["qwen3", "llama"]`）的本地服务。
    其它形状一律按「不是 OpenAI 兼容接口」处理。
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
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            model_id = item.get("id")
        else:
            model_id = None
        if model_id:
            ids.append(str(model_id))
    return ids


def context_grade(context: int, *, p90: int = CONTEXT_P90) -> str:
    """按 p90 上下文窗口把模型分成红/黄/绿三档，给面板上色用。"""
    if context < 0.75 * p90:
        return "red"
    if context < 1.5 * p90:
        return "yellow"
    return "green"
