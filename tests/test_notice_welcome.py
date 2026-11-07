"""进群欢迎：默认关、单占位符渲染、按会话每日上限；退群/禁言绝不发送。"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    SocialConfig,
    TriggerConfig,
    load_settings,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, NoticeEvent
from dafeiyu.router import Router
from dafeiyu.service import Service

QQ_CHAT = "qq:1001"
DAY = "2026-11-08"

BASE_YAML = """\
persona:
  name: 溟月
  aliases: [大肥鱼]
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


def make_settings() -> Settings:
    """与 tests/test_notice_pipeline.py 同一份显式配置，不依赖环境变量。"""
    return Settings(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        bot_qq=999,
        ws_host="127.0.0.1",
        ws_port=8080,
        ws_token="",
        db_path=":memory:",
        persona=PersonaConfig("溟月", ("大肥鱼",), 120, 300, 0.05),
        triggers=TriggerConfig(60, 0.0, ("01:00", "07:00"), ("怎么",)),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({"qq:2002"})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


class FakeAdapter:
    """最小适配器：记录发送；欢迎链路只看 ``sent``。"""

    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []
        self.message_handler: Any = None
        self.recall_handler: Any = None
        self.notice_handler: Any = None

    def on_message(self, handler: Any) -> None:
        self.message_handler = handler

    def on_recall(self, handler: Any) -> None:
        self.recall_handler = handler

    def on_notice(self, handler: Any) -> None:
        self.notice_handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return f"bot-{len(self.sent)}"


def build_service(
    tmp_path: Path, social: SocialConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[Service, FakeAdapter, Store]:
    settings = dataclasses.replace(make_settings(), social=social)
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    router = Router(settings, store)
    adapter = FakeAdapter()
    service = Service(settings, store, None, {"qq": adapter}, router, "test")
    # 固定「当日」：每日上限测试跨午夜也不会翻车。
    monkeypatch.setattr(service, "_join_welcome_day", lambda: DAY)
    return service, adapter, store


def welcome(user_id: str = "qq:4004") -> NoticeEvent:
    return NoticeEvent(
        kind="group_increase",
        chat_type=ChatType.GROUP,
        chat_id=QQ_CHAT,
        user_id=user_id,
    )


async def test_switch_off_sends_nothing_even_with_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, _store = build_service(
        tmp_path,
        SocialConfig(join_welcome_enabled=False, join_welcome="欢迎 {user_id}"),
        monkeypatch,
    )

    await service.handle_notice(welcome())

    assert adapter.sent == []


async def test_enabled_template_sends_exactly_one_rendered_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, _store = build_service(
        tmp_path,
        SocialConfig(join_welcome_enabled=True, join_welcome="欢迎 {user_id}"),
        monkeypatch,
    )

    await service.handle_notice(welcome())

    assert len(adapter.sent) == 1
    chat_type, chat_id, text = adapter.sent[0]
    assert (chat_type, chat_id) == (ChatType.GROUP, QQ_CHAT)
    assert text == "欢迎 qq:4004"


async def test_empty_template_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, _store = build_service(
        tmp_path,
        SocialConfig(join_welcome_enabled=True, join_welcome="   "),
        monkeypatch,
    )

    await service.handle_notice(welcome())

    assert adapter.sent == []


async def test_11th_welcome_in_same_day_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, store = build_service(
        tmp_path,
        SocialConfig(
            join_welcome_enabled=True,
            join_welcome="欢迎 {user_id}",
            join_welcome_daily_cap=10,
        ),
        monkeypatch,
    )

    for _ in range(11):
        await service.handle_notice(welcome())

    assert len(adapter.sent) == 10
    # 计数落在 mind_state 的约定键上（任务 13 复用同一范式）。
    key = f"join_welcome/group/{QQ_CHAT}/{DAY}"
    assert store.get_mind_state(key) == "10"


async def test_group_ban_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, _store = build_service(
        tmp_path,
        SocialConfig(join_welcome_enabled=True, join_welcome="欢迎 {user_id}"),
        monkeypatch,
    )

    await service.handle_notice(
        NoticeEvent(
            kind="group_ban",
            chat_type=ChatType.GROUP,
            chat_id=QQ_CHAT,
            user_id="qq:4004",
            operator_id="qq:3003",
            duration=60,
        )
    )

    assert adapter.sent == []


async def test_unknown_placeholder_sends_template_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, _store = build_service(
        tmp_path,
        SocialConfig(join_welcome_enabled=True, join_welcome="欢迎 {foo}"),
        monkeypatch,
    )

    await service.handle_notice(welcome())

    assert len(adapter.sent) == 1
    assert adapter.sent[0][2] == "欢迎 {foo}"


async def test_nickname_falls_back_to_user_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, _store = build_service(
        tmp_path,
        SocialConfig(
            join_welcome_enabled=True,
            join_welcome="欢迎 {nickname}（{user_id}）",
        ),
        monkeypatch,
    )

    await service.handle_notice(welcome())

    assert len(adapter.sent) == 1
    assert adapter.sent[0][2] == "欢迎 qq:4004（qq:4004）"


def write_env(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.env"
    p.write_text(content, encoding="utf-8")
    return p


def write_yaml(tmp_path: Path, content: str = BASE_YAML) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_join_welcome_config_defaults_when_section_missing(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path))

    assert settings.social.join_welcome_daily_cap == 10
    assert settings.social.join_welcome_enabled is False
    assert settings.social.join_welcome == ""
