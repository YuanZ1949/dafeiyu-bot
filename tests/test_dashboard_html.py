"""大脑三档面板的静态契约与 /api/brain/models 代理转发。

面板是手写静态页，没有构建步骤：这些断言直接钉住 DOM 结构、敏感字段的
渲染方式，以及模型探测的查询串必须原样穿过 supervisor 代理。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML, TEST_DASHBOARD_LOOPBACK_YAML

from dafeiyu.config import load_settings
from dafeiyu.model_probe import CONTEXT_P90
from dafeiyu.supervisor.dashboard import create_app as create_dashboard_app
from dafeiyu.supervisor.logs import LogHub

HTML_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "dafeiyu"
    / "supervisor"
    / "dashboard"
    / "index.html"
)

REQUIRED_IDS = (
    "modes",
    "key-base-url",
    "key-model",
    "key-api-key",
    "local-base-url",
    "local-model-select",
    "local-context",
    "local-lean",
    "probe-btn",
    "btn-apply-brain",
)


def read_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def test_html_has_three_mode_cards() -> None:
    html = read_html()
    for mode in ("free", "key", "local_model"):
        assert f'data-mode="{mode}"' in html
    assert "#local-context" not in html  # 用 id 属性而不是 CSS 选择器
    assert 'id="local-context"' in html


def test_html_has_exactly_one_selected_mode() -> None:
    html = read_html()
    cards = re.findall(
        r'<button[^>]*class="mode-card"[^>]*data-mode="(free|key|local_model)"',
        html,
    )
    assert sorted(cards) == ["free", "key", "local_model"]
    assert 'role="radiogroup"' in html
    assert html.count('role="radio"') == 3
    assert html.count('aria-checked="true"') == 1
    assert html.count('aria-checked="false"') == 2
    # 键盘可达：方向键在 radiogroup 上移动选中项。
    assert "ArrowDown" in html and "ArrowUp" in html


def test_html_has_every_credential_field() -> None:
    html = read_html()
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in html, element_id


def test_html_key_field_is_password_and_reuse_is_explained() -> None:
    html = read_html()
    match = re.search(r'<input[^>]*id="key-api-key"[^>]*>', html)
    assert match is not None
    assert 'type="password"' in match.group(0)
    assert 'autocomplete="new-password"' in match.group(0)
    assert "留空则沿用已保存的 Key" in match.group(0)


def test_html_probe_url_carries_base_url() -> None:
    html = read_html()
    assert "/api/brain/models?base_url=" in html


def test_html_context_p90_matches_model_probe() -> None:
    html = read_html()
    assert CONTEXT_P90 == 21062
    assert f"CONTEXT_P90 = {CONTEXT_P90}" in html


def test_html_never_renders_a_stored_secret() -> None:
    html = read_html()
    # 不落任何浏览器存储，也不从服务端数据里读 api_key 回填到 DOM。
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert ".api_key" not in html
    assert not re.search(r"(?:data|brain|response|result)\.api_key", html)
    # 密钥输入框只能由用户输入，不能带 value="..." 预置。
    for element_id in ("key-api-key", "local-api-key"):
        match = re.search(rf'<input[^>]*id="{element_id}"[^>]*>', html)
        if match is not None:
            assert "value=" not in match.group(0)


async def test_dashboard_models_proxy_forwards_query_string(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    async def models(request: web.Request) -> web.Response:
        seen.append((request.path_qs, request.query.get("base_url", "")))
        return web.json_response({"models": ["qwen3"]})

    bot_app = web.Application()
    bot_app.router.add_get("/api/brain/models", models)
    bot_client = TestClient(TestServer(bot_app))
    await bot_client.start_server()

    config = tmp_path / "config.yaml"
    config.write_text(TEST_CONFIG_YAML + TEST_DASHBOARD_LOOPBACK_YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)

    dash_client = None
    try:
        dash_app = create_dashboard_app(
            SimpleNamespace(specs={}),
            settings,
            LogHub(tmp_path / "logs"),
            tmp_path / "qrcode.png",
            str(bot_client.server.make_url("/")).rstrip("/"),
        )
        dash_client = TestClient(TestServer(dash_app))
        await dash_client.start_server()

        resp = await dash_client.get(
            "/api/brain/models",
            params={"base_url": "http://127.0.0.1:1234/v1"},
        )
        assert resp.status == 200
        assert await resp.json() == {"models": ["qwen3"]}
        assert seen == [
            (
                "/api/brain/models?base_url=http://127.0.0.1:1234/v1",
                "http://127.0.0.1:1234/v1",
            )
        ]

        # 不带查询串时保持原来的请求路径，一个字节都不变。
        await dash_client.get("/api/brain/models")
        assert seen[-1] == ("/api/brain/models", "")
    finally:
        if dash_client is not None:
            await dash_client.close()
        await bot_client.close()
