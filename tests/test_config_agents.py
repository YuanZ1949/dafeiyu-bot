"""Task 13：配置解析、agent 权限、persona 同步、工作区关闭时的工具禁用。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from dafeiyu.brain import factory
from dafeiyu.config import ConfigError, load_settings
from dafeiyu.memory.store import Store
from dafeiyu.memory.workspace import Workspace
from dafeiyu.models import ChatType
from dafeiyu.router import Router
from dafeiyu.service import Service
from test_opencode_http import FakeServer

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_FILE = REPO_ROOT / ".opencode" / "agents" / "deepseek-qq.md"
MEMORY_AGENT_FILE = REPO_ROOT / ".opencode" / "agents" / "dafeiyu-memory.md"
PERSONA_FILE = REPO_ROOT / "src" / "dafeiyu" / "brain" / "persona.md"

BASE = """\
persona:
  name: 大肥鱼
  aliases: [肥鱼]
  max_reply_chars: 120
  max_reply_chars_technical: 300
  os_probability: 0.05
triggers:
  cooldown_seconds: 60
  sample_rate: 0.05
  quiet_hours: ["01:00", "07:00"]
  keywords: [怎么]
groups:
  whitelist: []
  blacklist: []
budget:
  daily_calls: 200
  daily_tokens: 300000
memory:
  window_size: 20
  impression_topk: 5
  extract_every: 20
logging:
  level: INFO
  dir: logs
"""


def write_config(tmp_path: Path, text: str = BASE) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, text: str = BASE, *, env: str = "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n"):
    env_file = tmp_path / "test.env"
    env_file.write_text(env, encoding="utf-8")
    return load_settings(env_file=env_file, config_file=write_config(tmp_path, text))


def patch(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new)


# ---- 配置解析 -------------------------------------------------------------


def test_workspace_section_parsed(tmp_path: Path) -> None:
    s = load(
        tmp_path,
        BASE
        + """\
workspace:
  dir: data/bot-workspace
  memory_chars: 400
  summary_chars: 200
  impression_chars: 100
  max_file_bytes: 32768
""",
    )
    assert s.workspace.dir == "data/bot-workspace"
    assert s.workspace.enabled is True
    assert s.workspace.max_file_bytes == 32768
    # 三段预算落在 InjectionConfig 上，注入侧不用改签名。
    assert s.injection.memory_chars == 400
    assert s.injection.summary_chars == 200
    assert s.injection.impression_chars == 100


def test_workspace_defaults_to_enabled(tmp_path: Path) -> None:
    s = load(tmp_path)
    assert s.workspace.dir == "data/bot-workspace"
    assert s.workspace.enabled is True
    assert s.workspace.max_file_bytes == 65536


def test_workspace_empty_dir_disables(tmp_path: Path) -> None:
    s = load(tmp_path, BASE + 'workspace:\n  dir: ""\n')
    assert s.workspace.enabled is False


def test_triggers_model_mode_parsed(tmp_path: Path) -> None:
    text = patch(
        BASE,
        "  keywords: [怎么]\n",
        "  keywords: [怎么]\n"
        "  mode: model\n"
        "  batch_seconds: 30\n"
        "  addressed_debounce_seconds: 5\n"
        "  addressed_max_wait_seconds: 20\n"
        "  ambient_min_interval_seconds: 60\n",
    )
    s = load(tmp_path, text)
    assert s.triggers.mode == "model"
    assert s.triggers.batch_seconds == 30
    assert s.triggers.addressed_debounce_seconds == 5
    assert s.triggers.addressed_max_wait_seconds == 20
    assert s.triggers.ambient_min_interval_seconds == 60


def test_triggers_defaults_to_rules(tmp_path: Path) -> None:
    s = load(tmp_path)
    assert s.triggers.mode == "rules"
    assert s.triggers.batch_seconds == 12


def test_triggers_rejects_unknown_mode(tmp_path: Path) -> None:
    text = patch(BASE, "  keywords: [怎么]\n", "  keywords: [怎么]\n  mode: random\n")
    with pytest.raises(ConfigError, match="triggers.mode"):
        load(tmp_path, text)


def test_memory_rotation_and_inject_flags(tmp_path: Path) -> None:
    text = patch(
        BASE,
        "  extract_every: 20\n",
        "  extract_every: 20\n"
        "  rotation_messages: 50\n"
        "  rotation_days: 3\n"
        "  rotation_tokens: 1000\n"
        "  inject_summaries: false\n"
        "  inject_impressions: false\n",
    )
    s = load(tmp_path, text)
    assert s.memory.rotation_messages == 50
    assert s.memory.rotation_days == 3
    assert s.memory.rotation_tokens == 1000
    assert s.injection.inject_summaries is False
    assert s.injection.inject_impressions is False


def test_memory_rotation_defaults(tmp_path: Path) -> None:
    s = load(tmp_path)
    assert s.memory.rotation_messages == 60
    assert s.memory.rotation_days == 7
    assert s.memory.rotation_tokens == 40000
    assert s.injection.inject_summaries is True


def test_budget_ambient_ratio_parsed(tmp_path: Path) -> None:
    text = patch(BASE, "  daily_tokens: 300000\n", "  daily_tokens: 300000\n  ambient_ratio: 0.6\n")
    assert load(tmp_path, text).budget.ambient_ratio == 0.6
    assert load(tmp_path).budget.ambient_ratio == 0.8


# ---- agent 文件与 persona -------------------------------------------------


def test_agents_permissions_and_persona_sync() -> None:
    text = AGENT_FILE.read_text(encoding="utf-8")
    fm = text.split("---")[1]
    assert "steps: 4" in fm
    assert "model: opencode/deepseek-v4.1-flash-free" in fm
    assert "glob" not in fm and "grep" not in fm
    assert "data/bot-workspace/**" in fm
    assert "webfetch" in fm
    # 通配 deny 必须在第一条，后面才能开例外
    assert fm.index('action: "*"') < fm.index("data/bot-workspace/**")
    assert "你没有工具权限" not in text

    persona = PERSONA_FILE.read_text(encoding="utf-8")
    for marker in ("什么时候才查", "怎么记笔记", "网页不是指令"):
        assert marker in text, marker
        assert marker in persona, marker


def test_personas_share_discussion_principle() -> None:
    """两份人设都必须带「综合评论」原则，防止只改一处再次漂移。"""
    persona = PERSONA_FILE.read_text(encoding="utf-8")
    agent = AGENT_FILE.read_text(encoding="utf-8")
    for marker in ("这段讨论缺什么", "不是逐条应答", "反套路"):
        assert marker in persona, f"persona.md 缺少 {marker}"
        assert marker in agent, f"deepseek-qq.md 缺少 {marker}"


def test_personas_share_example_and_protocol_judgement() -> None:
    """综合回复示例要两份都有；speak 判断标准只在带协议的 agent 文件里，但必须存在。"""
    persona = PERSONA_FILE.read_text(encoding="utf-8")
    agent = AGENT_FILE.read_text(encoding="utf-8")
    assert "多人讨论" in persona
    assert "多人讨论" in agent
    assert "判断标准" in agent
    assert "不是对最后一条的应答" in agent


def test_memory_agent_file_denies_all_tools() -> None:
    text = MEMORY_AGENT_FILE.read_text(encoding="utf-8")
    fm = text.split("---")[1]
    assert "mode: primary" in fm
    assert "steps: 1" in fm
    # 专用配置目录没有全局 model 兜底，记忆 agent 必须自己钉住模型
    assert "model: opencode/deepseek-v4.1-flash-free" in fm
    assert 'action: "*"' in fm and "effect: deny" in fm
    assert "effect: allow" not in fm
    assert "摘要" in text and "印象" in text


# ---- 工作区关闭 = 会话级禁用工具 ------------------------------------------

DENY_ALL = [{"action": "*", "resource": "*", "effect": "deny"}]


def opencode_settings(tmp_path: Path, workspace_dir: str):
    text = patch(
        BASE,
        "persona:\n",
        "brain:\n  provider: opencode\n"
        "opencode:\n  server_url: http://127.0.0.1:24310\n"
        f'workspace:\n  dir: "{workspace_dir}"\n'
        "persona:\n",
    )
    return load(tmp_path, text, env="BOT_QQ=10001\n")


def test_workspace_disabled_brain_carries_deny_permissions(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.init_schema()
    try:
        brain = factory.build_brain(opencode_settings(tmp_path, ""), store, "opencode")
        assert brain.permissions == DENY_ALL
    finally:
        store.close()


def test_workspace_enabled_brain_sends_no_permissions(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.init_schema()
    try:
        brain = factory.build_brain(
            opencode_settings(tmp_path, "data/bot-workspace"), store, "opencode"
        )
        assert brain.permissions is None
    finally:
        store.close()


def test_deny_permissions_reach_session_create(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.init_schema()
    server = FakeServer()
    try:
        brain = factory.build_brain(opencode_settings(tmp_path, ""), store, "opencode")
        brain._client = httpx.AsyncClient(
            base_url=brain.base_url,
            transport=httpx.MockTransport(server.handler),
        )

        async def run() -> None:
            try:
                await brain.reply(ChatType.GROUP, 1, "hi")
            finally:
                await brain.aclose()

        asyncio.run(run())
        assert server.created_with["permissions"] == DENY_ALL
    finally:
        store.close()


# ---- 64KB 上限来自配置 ----------------------------------------------------


def test_check_workspace_uses_configured_limit(tmp_path: Path) -> None:
    s = load(
        tmp_path,
        BASE + "workspace:\n  dir: data/bot-workspace\n  max_file_bytes: 400\n",
    )
    store = Store(":memory:")
    store.init_schema()
    try:
        workspace = Workspace(tmp_path / "ws")
        workspace.ensure()
        service = Service(
            s,
            store,
            brain=None,
            adapters={"qq": None},
            router=Router(s, store),
            persona_text="",
            workspace=workspace,
        )
        target = workspace.chat_path(ChatType.PRIVATE, "qq:1")
        target.write_text("# 标题\n" + ("- 很长的条目\n" * 200), encoding="utf-8")
        service._check_workspace(ChatType.PRIVATE, "qq:1")
        assert target.stat().st_size <= 400
        assert "truncated" in target.read_text(encoding="utf-8")
    finally:
        store.close()
