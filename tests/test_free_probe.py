"""免费档可用性自检：只走 HTTP，任何异常都降级成「不可用」。

探测发生在 opencode 已 ready 之后：建一个临时 session、发一句最小 prompt，
再看一眼消息里有没有 provider 写的 4xx/5xx。自检只负责让面板亮黄条，
所以任何失败都只返回 False，绝不抛给启动流程，也绝不碰 subprocess / CLI。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import httpx
from conftest import TEST_CONFIG_YAML

from dafeiyu import control as control_module
from dafeiyu import model_probe
from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl
from dafeiyu.memory.store import Store
from dafeiyu.model_probe import probe_free_model


class FailingGet:
    """替换 model_probe._get：模拟上游直接 401。"""

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls: list[tuple] = []

    async def __call__(self, url, api_key, timeout):
        self.calls.append((url, api_key, timeout))
        raise model_probe.ProbeError(status=self.status)


class FailingPost:
    """替换 model_probe._post：建 session / 发 prompt 都直接 401。"""

    def __init__(self, status: int) -> None:
        self.status = status
        self.paths: list[str] = []

    async def __call__(self, client, path, payload, timeout):
        self.paths.append(path)
        raise model_probe.ProbeError(status=self.status)


class OkayPost:
    """替换 model_probe._post：建 session 返回 id，prompt 正常受理。"""

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, client, path, payload, timeout):
        self.paths.append(path)
        if path == "/api/session":
            return {"data": {"id": "ses_probe"}}
        return {"data": {}}


class TimeoutPost:
    """替换 model_probe._post：请求超时。"""

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, client, path, payload, timeout):
        self.paths.append(path)
        raise httpx.ReadTimeout("slow")


def _boom(*args, **kwargs):
    raise AssertionError("不允许调用子进程")


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class ErrorClient:
    """假传输：建 session / 发 prompt 都成功，但消息里写着 provider 的 429。"""

    def __init__(self, status: int = 429) -> None:
        self.status = status
        self.deleted: list[str] = []

    async def get(self, url, params=None, timeout=None):
        return FakeResponse(
            {
                "data": [
                    {
                        "type": "assistant",
                        "finish": "error",
                        "error": {"statusCode": self.status},
                    }
                ]
            }
        )

    async def delete(self, url, timeout=None):
        self.deleted.append(url)
        return FakeResponse({})


async def test_probe_returns_false_on_auth_error(monkeypatch):
    monkeypatch.setattr(model_probe, "_get", FailingGet(401))
    monkeypatch.setattr(model_probe, "_post", FailingPost(401))
    assert await probe_free_model(object()) is False


async def test_probe_returns_true_on_success(monkeypatch):
    monkeypatch.setattr(model_probe, "_post", OkayPost())
    assert await probe_free_model(object()) is True


async def test_probe_never_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(model_probe, "_post", TimeoutPost())
    assert await probe_free_model(object()) is False


def test_probe_uses_http_not_subprocess(monkeypatch):
    # 模块里不该有 subprocess 属性（这条已由 tests/test_model_probe.py 钉住），
    # 这里再把标准库入口钉死：任何 shell-out 都会炸。
    assert not hasattr(model_probe, "subprocess")
    monkeypatch.setattr(subprocess, "run", _boom, raising=False)
    # 钉住返回值：只断言「没起子进程」的话，函数整个返回 None 也能过。
    assert asyncio.run(probe_free_model(object())) is False


async def test_messages_once_logs_read_failure(caplog):
    """读消息失败可以吞，但不能静默：至少留一条 debug。"""

    class BrokenGet:
        async def get(self, url, params=None, timeout=None):
            raise httpx.ConnectError("boom")

    caplog.set_level(logging.DEBUG, logger="dafeiyu.model_probe")
    assert await model_probe._messages_once(BrokenGet(), "ses_x", 1.0) is None
    assert any("探测消息读取失败" in r.message for r in caplog.records)


async def test_probe_warns_that_model_only_labels_the_session(monkeypatch, caplog):
    """``model`` 不参与选模型：传非默认值时必须显式提醒调用方。"""
    monkeypatch.setattr(model_probe, "_post", OkayPost())
    caplog.set_level(logging.WARNING, logger="dafeiyu.model_probe")

    assert await probe_free_model(object(), model="qwen-other") is True

    assert any("仅用于会话标签" in r.message for r in caplog.records)


async def test_probe_detail_reports_provider_status_as_reason(monkeypatch):
    monkeypatch.setattr(model_probe, "_post", OkayPost())

    result = await model_probe.probe_free_model_detail(ErrorClient(429))

    assert result.available is False
    assert result.reason == "上游报错（HTTP 429）"


async def test_probe_detail_reports_transport_failure_as_reason(monkeypatch):
    monkeypatch.setattr(model_probe, "_post", TimeoutPost())

    result = await model_probe.probe_free_model_detail(object())

    assert result.available is False
    assert "ReadTimeout" in result.reason


async def test_probe_cleanup_requests_use_the_remaining_budget(monkeypatch):
    """清理 DELETE 不能再按整份 timeout 拖住：用剩余预算，且最多 1s。"""
    real_monotonic = model_probe.time.monotonic
    offset = {"value": 0.0}
    monkeypatch.setattr(
        model_probe.time, "monotonic", lambda: real_monotonic() + offset["value"]
    )

    class BudgetClient:
        def __init__(self, spend: float) -> None:
            self.spend = spend
            self.deletes: list[float] = []

        async def get(self, url, params=None, timeout=None):
            offset["value"] += self.spend
            return FakeResponse(
                {"data": [{"type": "assistant", "finish": "stop"}]}
            )

        async def delete(self, url, timeout=None):
            self.deletes.append(timeout)
            return FakeResponse({})

    monkeypatch.setattr(model_probe, "_post", OkayPost())

    # 预算还剩 8s：清理最多用 1s，不能再用整份 timeout。
    fast = BudgetClient(0.0)
    assert await probe_free_model(fast, timeout=8.0) is True
    assert fast.deletes == [1.0]

    # 探测把预算耗到只剩 ~0.3s：清理必须跟着剩余预算走。
    offset["value"] = 0.0
    slow = BudgetClient(7.7)
    assert await probe_free_model(slow, timeout=8.0) is True
    assert 0.1 <= slow.deletes[0] <= 0.5


async def test_probe_sends_one_prompt_and_treats_provider_error_as_unavailable(
    monkeypatch,
):
    # 只看 session 建没建成不够：provider 的 401/429 是在 prompt 之后写进消息的。
    post = OkayPost()
    client = ErrorClient(429)
    monkeypatch.setattr(model_probe, "_post", post)

    assert await probe_free_model(client) is False
    assert post.paths == ["/api/session", "/api/session/ses_probe/prompt"]
    # 临时 session 必须清掉，不管判定结果是什么。
    assert client.deleted == ["/api/session/ses_probe"]


def _free_mode_control(tmp_path, client):
    """最小可用的 BrainControl：免费档 + 带 HTTP 传输的假 brain。"""
    config = tmp_path / "config.yaml"
    config.write_text(TEST_CONFIG_YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)
    settings = replace(
        settings,
        brain=replace(
            settings.brain,
            provider="opencode",
            mode="free",
            mode_from_overrides=False,
        ),
    )
    store = Store(tmp_path / "test.db")
    store.init_schema()
    return BrainControl(
        settings,
        store,
        SimpleNamespace(brain=SimpleNamespace(_client=client)),
        SimpleNamespace(connected=True),
        runtime_path=str(tmp_path / "runtime.json"),
    )


def test_free_probe_client_logs_missing_transport(tmp_path, caplog):
    """没有 HTTP 传输时跳过自检，但必须留一条 debug：否则看不出「没探」。"""
    control = _free_mode_control(tmp_path, None)
    caplog.set_level(logging.DEBUG, logger="dafeiyu.control")

    assert control._free_probe_client() is None

    assert any("未找到 HTTP 传输" in r.message for r in caplog.records)


class DownClient:
    """opencode 正在重启：任何请求都连不上。"""

    async def get(self, url, params=None, timeout=None):
        raise httpx.ConnectError("opencode 未就绪")


async def test_control_keeps_last_result_when_opencode_is_not_ready(
    tmp_path, monkeypatch
):
    """未就绪时本轮跳过刷新：旧值不被 False 覆盖，也不缓存这一轮。"""
    calls: list[object] = []

    async def fake_probe(client):
        calls.append(client)
        return model_probe.FreeProbeResult(False, "上游报错（HTTP 429）")

    monkeypatch.setattr(control_module, "probe_free_model_detail", fake_probe)
    control = _free_mode_control(tmp_path, DownClient())
    control.runtime = replace(control.runtime, free_available=True)
    control._free_probe_at = 1.0

    await control._run_free_probe()

    assert calls == []  # 未就绪：连探测都不发
    assert control.runtime.free_available is True
    assert control.snapshot()["free_available"] is True
    assert control._free_probe_at == 0.0  # 这一轮不算「已探测」，下轮立刻重试


async def test_control_probes_once_per_ttl_and_exposes_flag(tmp_path, monkeypatch):
    calls: list[object] = []

    async def fake_probe(client):
        calls.append(client)
        return model_probe.FreeProbeResult(False, "上游报错（HTTP 429）")

    monkeypatch.setattr(control_module, "probe_free_model_detail", fake_probe)
    client = object()
    control = _free_mode_control(tmp_path, client)

    control.schedule_free_probe()
    first = control._free_probe_task
    assert first is not None
    await first
    assert calls == [client]
    # 结果进了内存里的 runtime，并同时出现在 /api/brain 与健康快照里。
    assert control.snapshot()["free_available"] is False
    assert control.snapshot()["free_reason"] == "上游报错（HTTP 429）"
    assert control.health_snapshot()["free_available"] is False
    assert control.health_snapshot()["free_reason"] == "上游报错（HTTP 429）"

    control.schedule_free_probe()  # TTL 内不重复探测
    assert control._free_probe_task is first

    control.schedule_free_probe(force=True)  # 强制重探会起新任务
    second = control._free_probe_task
    assert second is not None and second is not first
    await second
    assert calls == [client, client]


async def test_control_skips_probe_outside_free_mode(tmp_path):
    control = _free_mode_control(tmp_path, object())
    control.settings = replace(
        control.settings,
        brain=replace(control.settings.brain, provider="token"),
    )

    control.schedule_free_probe(force=True)

    assert control._free_probe_task is None
    assert control.snapshot()["free_available"] is None
