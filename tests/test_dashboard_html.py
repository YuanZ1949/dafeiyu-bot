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


def test_html_shows_memory_health_line() -> None:
    """记忆卡要有一行体检：缺口天数、最近摘要、熔断原因。"""
    html = read_html()
    assert 'id="memory-health"' in html
    assert "/api/memory/health" in html
    assert "记忆摘要落后" in html


def test_html_memory_health_lists_pending_slots() -> None:
    """缺口要落到「哪个会话、哪天、多少条」，不能只报天数。"""
    html = read_html()
    match = re.search(
        r"async function loadMemoryHealth\(\) \{\n(.*?)\n\}", html, re.DOTALL
    )
    assert match is not None, "loadMemoryHealth 必须仍是一段可直接断言的函数体"
    body = match.group(1)
    assert "pending_slots" in body
    assert "条" in body  # 明细必须带消息条数这个单位
    assert "pending_days" in body  # 旧后端没有 pending_slots 时的兜底


def test_memory_session_rows_trigger_summary_and_impressions() -> None:
    """会话行直接带 chat_type/chat_id，用户不用把 ID 抄到上面的输入框。"""
    html = read_html()
    assert 'add("摘要", "", () => triggerSummary(s.chat_type, s.chat_id));' in html
    assert 'add("印象", "", () => triggerExtract(s.chat_type, s.chat_id));' in html


def test_memory_manual_target_lives_in_advanced_and_has_own_type() -> None:
    """手输 ID 保留但收进高级；目标类型不能借用显示筛选器。"""
    html = read_html()
    advanced = html.index('<details id="memory-advanced">')
    assert html.index('id="memory-chat-id"') > advanced
    assert 'id="memory-target-type"' in html

    match = re.search(r"function memoryTarget\(\) \{\n(.*?)\n\}", html, re.DOTALL)
    assert match is not None, "memoryTarget 必须仍是一段可直接断言的函数体"
    body = match.group(1)
    assert 'memory-target-type' in body
    assert 'memory-chat-filter' not in body


def test_summary_date_defaults_to_yesterday_but_is_selectable() -> None:
    """摘要日期默认昨天，可自选；生成时把日期一起发给后端。"""
    html = read_html()
    assert 'id="summary-date" type="date"' in html
    assert "function yesterdayIso()" in html
    assert '$("summary-date").value = yesterdayIso();' in html

    match = re.search(
        r"async function triggerSummary\(chatType, chatId\) \{\n(.*?)\n\}",
        html,
        re.DOTALL,
    )
    assert match is not None
    body = match.group(1)
    assert "summaryDate()" in body
    assert "chatLabel(target.chat_type, target.chat_id)" in body


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
    # 初始状态是「没有已保存 Key」；有 Key 时由 brain.key.has_key 换成复用提示。
    assert 'placeholder="输入 API Key"' in match.group(0)
    assert "已保存 Key，留空沿用" in html
    assert "已保存令牌，留空沿用" in html


def test_html_probe_url_carries_base_url() -> None:
    html = read_html()
    assert "/api/brain/models?base_url=" in html


def test_html_brain_payload_shape_and_mode_selection() -> None:
    html = read_html()
    # 提交体必须仍带三档各自字段；pin 住这些键，避免重构时静默改协议。
    for token in (
        'mode: "key"',
        "key: {",
        'mode: "local_model"',
        "local,",
        "base_url:",
        "model:",
        "api_key:",
        "context",
        "lean",
    ):
        assert token in html, token
    # 选中态优先读服务端 mode；旧的前缀推断只作为 fallback，且必须是 startsWith。
    assert "brain.mode" in html
    assert 'model.startsWith("dafeiyu-local/")' in html
    assert 'model.startsWith("dafeiyu-key/")' in html
    assert 'model.includes("dafeiyu-local/")' not in html
    assert 'model.includes("dafeiyu-key/")' not in html


def test_html_probe_and_context_edge_cases_are_explained() -> None:
    html = read_html()
    # 探测失败不清空已选模型；返回不是数组时单独报格式错误。
    assert "返回格式异常" in html
    assert "select.innerHTML = '<option value=\"\">探测失败</option>'" not in html
    # 0 不是合法窗口值，但空值表示使用默认。
    assert "0 表示默认，可留空" in html
    assert 'raw === "0"' in html
    # 200 但 JSON 不可解析时不能报成「切换失败」。
    assert "服务返回无法解析，可能已生效，请刷新确认" in html
    # 重启提示只由一处补足，后端 warnings 已经带了就不再重复。
    assert html.count("需重启 opencode 后生效") == 1


def test_html_free_banner_needs_explicit_unavailable_flag() -> None:
    """免费档黄条的判定条件：只有「免费档 + 自检明确 false」才亮。"""
    html = read_html()
    assert 'id="free-banner"' in html
    match = re.search(
        r"function renderFreeBanner\(data\) \{\n(.*?)\n\}", html, re.DOTALL
    )
    assert match is not None, "renderFreeBanner 必须仍是一段可直接断言的函数体"
    body = match.group(1)

    assert 'brain.mode === "free"' in body
    assert "brain.free_available === false" in body
    # /api/status 顶层从来没有 free_available（它在 brain 里），旧分支是死代码。
    assert "data.free_available" not in body


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


def test_html_polling_intervals_are_throttled() -> None:
    """面板轮询降频：状态 5s、记忆体检 30s。

    `/api/memory/health` 每次落一次 DB 查询，是最重的一个；2s 状态轮询
    也把 supervisor 压得太紧。这里钉住这两条间隔，并确保 2s 不再回来。
    """
    html = read_html()
    # 定时器段：从第一个 setInterval 到最后一条，单独截出来断言，避免误匹配
    # 配置下拉里的数字（如重载间隔选项）。
    start = html.index("setInterval(refresh,")
    end = html.index("document.addEventListener(\"visibilitychange\"")
    timers = html[start:end]

    match = re.search(r"setInterval\(refresh, (\d+)\);", timers)
    assert match is not None, "状态刷新必须仍是 setInterval(refresh, N)"
    assert match.group(1) == "5000"

    match = re.search(r"setInterval\(loadMemory, (\d+)\);", timers)
    assert match is not None, "记忆体检必须仍是 setInterval(loadMemory, N)"
    assert match.group(1) == "30000"

    # 旧的 2s 状态轮询不能再出现（全文件范围，含可能的注释/旧写法）。
    assert "setInterval(refresh, 2000)" not in html


def test_html_config_panel_reflects_soft_target_and_hard_limits() -> None:
    """人设一行不能再把软目标写成实际截断值，硬安全阀必须一起显示。"""
    html = read_html()
    assert "reply_hard_total" in html
    assert "reply_hard_part" in html
    assert "软目标" in html
    assert "安全阀" in html
    # 微信自查名与 persona 别名都要能从 /api/config 回到面板。
    assert "self_nicknames" in html
    assert '"称呼"' in html or "称呼" in html


def test_html_has_mobile_layout_rules() -> None:
    """手机端：窄屏媒体查询、控件铺满、抽屉全宽、iOS 输入框不缩放。"""
    html = read_html()
    assert 'content="width=device-width, initial-scale=1, viewport-fit=cover"' in html
    assert "@media (max-width: 720px)" in html
    assert "@media (max-width: 420px)" in html
    assert ".grid-2, .grid-3, .mem-grid { grid-template-columns: 1fr; }" in html
    assert "header { position: static;" in html
    assert ".timeline-drawer { width: 100vw; border-left: none; }" in html
    assert "input, select, textarea { font-size: 16px; }" in html


def test_html_surfaces_wechat_degraded_reason() -> None:
    """微信通道降级时必须把桥侧 reason 翻成中文显示，不能只写「已连接（降级）」。"""
    html = read_html()
    # 映射表与渲染入口都在，避免 reason 只是被吞掉。
    assert "DEGRADED_REASONS" in html
    assert "degradedReasonText" in html
    assert "inbound_localdb_unavailable" in html
    assert "本地库不可用" in html
    # 只在降级且带 reason 时追加一行，正常通道不多出空行。
    assert 'item.degraded && item.reason' in html
    assert '"降级原因"' in html


def test_html_health_detail_unwraps_bot_frame() -> None:
    """健康快照的 bot 字段嵌在 data.bot 下，前端必须先解嵌套再渲染。

    /api/health/detail 是 `{ok, generated_at, processes, bot, napcat, disk,
    backups, alerts}`；uptime/version/gateway/channels/traffic/scheduler/quota/
    memory 都在 bot 里。读错层级会让通道全显示「未启用」、时长/版本变「—」。
    """
    html = read_html()
    assert "data.bot" in html

    match = re.search(r"function renderHealth\(data\) \{\n(.*?)\n\}", html, re.DOTALL)
    assert match is not None, "renderHealth 必须仍是一段可直接断言的函数体"
    body = match.group(1)

    # bot 健康帧内的字段一律从解嵌套后的 bot 取。
    for token in (
        "bot.uptime_seconds",
        "bot.version",
        "bot.traffic",
        "bot.scheduler",
        "bot.quota",
        "bot.memory",
        "bot.channels",
        "bot.gateway",
    ):
        assert token in body, token
    # 顶层字段不能跟着改成 bot.*。
    assert "data.ok" in body
    assert "data.generated_at" in body
    assert "data.disk" in body
    # 旧的错误层级不能再出现。
    assert "data.channels" not in body
    assert "data.traffic" not in body
    assert "data.memory" not in body
    assert "data.gateway" not in body
    # 占位与降级文案保持不变。
    assert '"未启用"' in body
    assert '"已连接（降级）"' in body
    assert '"降级原因"' in body


def test_sessions_auto_refresh_keeps_loaded_pages() -> None:
    """「加载更多」之后，定时/回前台刷新不能把列表重置回第一页。"""
    html = read_html()
    assert "sessionsOffset <= SESSIONS_PAGE" in html
    assert "setInterval(loadSessions, 15000)" not in html


def test_html_has_read_only_evals_card() -> None:
    """黄金集趋势卡：只读、在用量卡之后、空态与准确率占位都要有。"""
    html = read_html()
    assert 'id="evals-card"' in html
    assert html.index('id="evals-card"') > html.index('id="usage-bars"')
    assert "/api/evals" in html
    assert 'id="evals-list"' in html
    assert "还没有评测结果" in html
    assert "evals-empty" in html
    assert "loadEvals();" in html

    match = re.search(r"function evalAccuracy\(accuracy\) \{\n(.*?)\n\}", html, re.DOTALL)
    assert match is not None, "evalAccuracy 必须仍是一段可直接断言的函数体"
    body = match.group(1)
    assert "—" in body
    assert "accuracy === null" in body
    assert "100" in body


def test_html_has_mirror_config_card() -> None:
    """群互通卡片：配对 + 昵称两组输入、下拉数据源与读写接口都要接上。"""
    html = read_html()
    assert 'id="mirror-pairs"' in html
    assert 'id="mirror-nicknames"' in html
    assert 'id="btn-add-mirror-pair"' in html
    assert 'id="btn-add-mirror-nickname"' in html
    assert 'id="mirror-qq-options"' in html
    assert 'id="mirror-wechat-options"' in html
    assert "/api/mirror" in html
    assert "loadMirror();" in html
    assert "saveMirror" in html
    # 昵称键必须带平台前缀：面板提示与后端校验口径一致。
    assert "qq:123456 / wxg:abc123" in html


def test_login_html_is_mobile_friendly() -> None:
    login = HTML_PATH.with_name("login.html").read_text(encoding="utf-8")
    assert "viewport-fit=cover" in login
    assert "@media (max-width: 420px)" in login
    assert "font-size: 16px;" in login


def test_html_decision_replay_renders_seen_messages() -> None:
    """思考回放要展示「当时看到」：昵称/正文转义，bot 显示「我」，空则提示已清理。"""
    html = read_html()
    assert "当时看到" in html
    assert "decision-messages" in html
    assert "decision-msg-who" in html
    assert "（消息已清理）" in html
    assert "decisionMessagesHtml(item)" in html

    match = re.search(
        r"function decisionMessagesHtml\(item\) \{\n(.*?)\n\}", html, re.DOTALL
    )
    assert match is not None, "decisionMessagesHtml 必须仍是一段可直接断言的函数体"
    body = match.group(1)
    assert "msg.is_bot" in body
    assert '"我"' in body
    assert "escapeHtml(who)" in body
    assert "escapeHtml(text)" in body


async def test_dashboard_models_proxy_forwards_query_string(tmp_path: Path) -> None:
    seen: list[tuple[str, str, str]] = []

    async def models(request: web.Request) -> web.Response:
        seen.append(
            (
                request.path,
                request.query.get("base_url", ""),
                request.query.get("api_key", ""),
            )
        )
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
            "/api/brain/models"
            "?base_url=http%3A%2F%2F127.0.0.1%3A1234%2Fv1"
            "&api_key=sk%2Fa%23b"
        )
        assert resp.status == 200
        assert await resp.json() == {"models": ["qwen3"]}
        # 代理必须原样转发编码串：如果先解码再转发，`#` 会变成片段，bot 只能
        # 收到 `sk/a`；这里断言 bot 拿到的是完整解码值。
        assert seen[0][1] == "http://127.0.0.1:1234/v1"
        assert seen[0][2] == "sk/a#b"
        assert seen[0][0] == "/api/brain/models"

        # 不带查询串时保持原来的请求路径，一个字节都不变。
        await dash_client.get("/api/brain/models")
        assert seen[-1][0] == "/api/brain/models"
        assert seen[-1][1] == ""
        assert seen[-1][2] == ""
    finally:
        if dash_client is not None:
            await dash_client.close()
        await bot_client.close()
