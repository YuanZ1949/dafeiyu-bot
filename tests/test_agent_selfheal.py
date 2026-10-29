"""启动自愈：supervisor 起来之前，按当前档位把两个 agent 文件重渲一次。

以前切档只在「用户在面板上点保存」那一刻写 agent 文件。只要进程在切档后
没有真正重启（或改了 `config.yaml`/`settings.local.yaml` 直接冷启动），
`opencode` 就会带着上一个档位的模型跑一整个会话——聊天用错模型、记忆旁路
继续撞免费额度。所以启动前要无条件对齐一次，且必须幂等（内容没变不写）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from dafeiyu.brain.provider_config import (
    agent_path,
    ensure_agent_files,
    memory_agent_path,
)
from dafeiyu.config import BrainConfig, LocalModelConfig, OpenCodeConfig


def _settings(
    mode: str,
    *,
    project_dir: str = "",
    key_model: str = "deepseek-flash",
    lean: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        brain=BrainConfig(mode=mode, key_model=key_model),
        opencode=OpenCodeConfig(project_dir=project_dir),
        local_model=LocalModelConfig(model="qwen3", lean=lean),
    )


def _front_matter(text: str) -> dict:
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end]))


def test_ensure_agent_files_writes_both_agents_for_key_mode(tmp_path):
    rewritten = ensure_agent_files(_settings("key"), root=tmp_path)

    chat = agent_path(tmp_path)
    memory = memory_agent_path(tmp_path)
    assert sorted(rewritten) == sorted([str(chat), str(memory)])
    assert _front_matter(chat.read_text(encoding="utf-8"))["model"] == (
        "dafeiyu-key/deepseek-flash"
    )
    # 记忆旁路必须和聊天同一个模型：以前它写死免费模型，免费额度一耗尽
    # 整条记忆链路就瘫了。
    assert _front_matter(memory.read_text(encoding="utf-8"))["model"] == (
        "dafeiyu-key/deepseek-flash"
    )


def test_ensure_agent_files_rewrites_stale_mode_and_stays_idempotent(tmp_path):
    ensure_agent_files(_settings("free"), root=tmp_path)
    rewritten = ensure_agent_files(_settings("key"), root=tmp_path)
    assert len(rewritten) == 2
    assert "dafeiyu-key/deepseek-flash" in agent_path(tmp_path).read_text(
        encoding="utf-8"
    )

    # 内容没变就不该再写：启动是高频动作，不能每次都刷盘。
    assert ensure_agent_files(_settings("key"), root=tmp_path) == []


def test_ensure_agent_files_uses_configured_project_dir(tmp_path):
    settings = _settings("free", project_dir=str(tmp_path))
    rewritten = ensure_agent_files(settings)

    assert len(rewritten) == 2
    assert agent_path(tmp_path).exists()
    assert memory_agent_path(tmp_path).exists()


def test_ensure_agent_files_keeps_lean_local_agent_without_tools(tmp_path):
    ensure_agent_files(_settings("local_model", lean=True), root=tmp_path)

    chat = agent_path(tmp_path).read_text(encoding="utf-8")
    assert "data/bot-workspace/**" not in chat
    assert 'action: webfetch\n    resource: "*"\n    effect: allow' not in chat
    assert "本轮没有工具" in chat
    assert _front_matter(chat)["model"] == "dafeiyu-local/qwen3"
    assert _front_matter(memory_agent_path(tmp_path).read_text(encoding="utf-8"))[
        "steps"
    ] == 1


def test_ensure_agent_files_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError):
        ensure_agent_files(_settings("bogus"), root=tmp_path)


class _Logs:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write(self, channel: str, message: str) -> None:
        self.lines.append((channel, message))


def test_selfheal_logs_rewritten_files(monkeypatch):
    from dafeiyu.supervisor import main as sup_main

    monkeypatch.setattr(
        sup_main, "ensure_agent_files", lambda settings: ["a.md", "b.md"]
    )
    logs = _Logs()

    assert sup_main.selfheal_agent_files(object(), logs) == ["a.md", "b.md"]
    assert logs.lines == [
        ("supervisor", "agent 文件已按当前档位刷新：a.md、b.md")
    ]


def test_selfheal_failure_only_logs(monkeypatch):
    from dafeiyu.supervisor import main as sup_main

    def boom(settings):
        raise OSError("模板读不到")

    monkeypatch.setattr(sup_main, "ensure_agent_files", boom)
    logs = _Logs()

    assert sup_main.selfheal_agent_files(object(), logs) == []
    assert logs.lines and "agent 文件自愈失败（OSError）" in logs.lines[0][1]
    assert "模板读不到" in logs.lines[0][1]
