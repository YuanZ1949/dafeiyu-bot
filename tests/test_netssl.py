"""TLS 上下文复用与 httpx 构造点守卫。

背景：`httpx` 在 `verify=True` 时会调用 `ssl.create_default_context(cafile=certifi.where())`，
每次构造客户端都重新解析 certifi 的 118 张根证书 PEM。本机实测单次 50 秒，
直接顶穿 supervisor 的 30 秒就绪门槛。改用系统信任库 + 进程内复用后为 0.1 秒。
"""

from __future__ import annotations

import ast
import ssl
import time
from pathlib import Path

import pytest

from dafeiyu import netssl


def test_default_ssl_context_is_reused() -> None:
    first = netssl.default_ssl_context()
    second = netssl.default_ssl_context()
    assert first is second


def test_default_ssl_context_verifies() -> None:
    ctx = netssl.default_ssl_context()
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_httpx_client_construction_is_fast() -> None:
    import asyncio

    import httpx

    start = time.perf_counter()
    client = httpx.AsyncClient(verify=netssl.default_ssl_context())
    elapsed = time.perf_counter() - start
    asyncio.run(client.aclose())
    # 基线是 httpx 默认 verify=True 的 50s 级；这里留足余量，只钉住「不再重建根证书库」。
    assert elapsed < 2.0, f"客户端构造耗时 {elapsed:.2f}s，说明仍在重建默认根证书库"


SRC = Path(__file__).resolve().parents[1] / "src" / "dafeiyu"


def _httpx_client_calls() -> list[tuple[Path, int]]:
    found: list[tuple[Path, int]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in {"Client", "AsyncClient"}:
                continue
            owner = func.value
            if isinstance(owner, ast.Attribute) and owner.attr == "httpx":
                found.append((path, node.lineno))
            elif isinstance(owner, ast.Name) and owner.id == "httpx":
                found.append((path, node.lineno))
    return found


def test_every_httpx_client_passes_verify() -> None:
    calls = _httpx_client_calls()
    assert calls, "没有找到任何 httpx 客户端构造点，扫描逻辑失效"

    missing: list[str] = []
    for path, lineno in calls:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.lineno == lineno:
                keywords = {kw.arg for kw in node.keywords}
                if "verify" not in keywords:
                    missing.append(f"{path.relative_to(SRC)}:{lineno}")
                break

    assert missing == [], "以下 httpx 客户端没有显式 verify，会重建 certifi 根证书库：" + ", ".join(missing)


@pytest.mark.parametrize("module", ["dafeiyu.brain.opencode_http", "dafeiyu.model_probe", "dafeiyu.brain.lmstudio"])
def test_modules_use_shared_context(module: str) -> None:
    import importlib

    mod = importlib.import_module(module)
    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "netssl" in source, f"{module} 未引用 netssl，仍会用 httpx 默认根证书库"
