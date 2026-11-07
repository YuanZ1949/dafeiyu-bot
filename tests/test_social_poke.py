"""戳一戳：三道门控 + 冷却/每日上限 + 固定短句，且绝不调用模型。"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from dafeiyu.budget import BudgetAction, BudgetDecision
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
from dafeiyu.social import POKE_REPLIES, PokeReactor

QQ_CHAT = "qq:1001"
SHANGHAI = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 8, 12, 0, tzinfo=SHANGHAI)
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

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


class FakeClock:
    """可推进的 epoch 秒时钟：戳一戳的冷却与每日上限都读它。"""

    def __init__(self, start: datetime) -> None:
        self.now = start.timestamp()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingBrain:
    """只数调用次数：戳一戳走任何模型路径都算失败。"""

    def __init__(self) -> None:
        self.calls = 0

    async def reply(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("poke 不应调用模型")

    async def chat(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("poke 不应调用模型")

    async def aclose(self) -> None:
        return None


class FakeAdapter:
    """最小适配器：记录发送；启动/停机只需存在。"""

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
    tmp_path: Path,
    clock: FakeClock,
    *,
    social: SocialConfig | None = None,
) -> tuple[Service, FakeAdapter, CountingBrain, Router]:
    settings = dataclasses.replace(
        make_settings(),
        social=social or SocialConfig(poke_enabled=True),
    )
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    router = Router(settings, store, clock=lambda: NOON)
    adapter = FakeAdapter()
    brain = CountingBrain()
    service = Service(
        settings,
        store,
        brain,
        {"qq": adapter},
        router,
        "test",
        poke_reactor=PokeReactor(settings.social, clock=clock),
    )
    return service, adapter, brain, router


def poke(chat_id: str = QQ_CHAT, chat_type: ChatType = ChatType.GROUP) -> NoticeEvent:
    return NoticeEvent(
        kind="group_poke",
        chat_type=chat_type,
        chat_id=chat_id,
        user_id="qq:2002",  # 被戳者
        operator_id="qq:3003",  # 戳的人
    )


def test_phrase_pool_is_short_and_deterministic() -> None:
    assert len(POKE_REPLIES) >= 6
    assert len(set(POKE_REPLIES)) == len(POKE_REPLIES)
    for phrase in POKE_REPLIES:
        assert 0 < len(phrase) <= 20

    clock = FakeClock(NOON)
    reactor = PokeReactor(SocialConfig(poke_enabled=True), clock=clock)
    replies: list[str] = []
    for _ in range(3):
        clock.advance(301)
        claimed = reactor.claim(ChatType.GROUP, QQ_CHAT)
        assert claimed is not None
        replies.append(claimed)
    assert replies == list(POKE_REPLIES[:3])


async def test_first_poke_sends_exactly_one_message(tmp_path: Path) -> None:
    clock = FakeClock(NOON)
    service, adapter, _brain, _router = build_service(tmp_path, clock)

    await service.handle_notice(poke())

    assert len(adapter.sent) == 1
    chat_type, chat_id, text = adapter.sent[0]
    assert (chat_type, chat_id) == (ChatType.GROUP, QQ_CHAT)
    assert text in POKE_REPLIES


async def test_second_poke_within_cooldown_sends_nothing(tmp_path: Path) -> None:
    clock = FakeClock(NOON)
    service, adapter, _brain, _router = build_service(tmp_path, clock)

    await service.handle_notice(poke())
    clock.advance(299)  # 还差 1 秒才到 300 秒冷却
    await service.handle_notice(poke())

    assert len(adapter.sent) == 1


async def test_quiet_window_sends_nothing(tmp_path: Path) -> None:
    clock = FakeClock(NOON)
    service, adapter, _brain, router = build_service(tmp_path, clock)
    router.quiet(ChatType.GROUP, QQ_CHAT, 10)

    await service.handle_notice(poke())

    assert adapter.sent == []


async def test_degraded_budget_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NOON)
    service, adapter, _brain, _router = build_service(tmp_path, clock)

    def degraded(*args: Any, **kwargs: Any) -> BudgetDecision:
        return BudgetDecision(BudgetAction.DEGRADE, "budget_test", "per_chat")

    monkeypatch.setattr(service.budget_guard, "evaluate", degraded)

    await service.handle_notice(poke())

    assert adapter.sent == []


async def test_poke_never_calls_the_model(tmp_path: Path) -> None:
    clock = FakeClock(NOON)
    service, adapter, brain, _router = build_service(tmp_path, clock)

    await service.handle_notice(poke())

    assert len(adapter.sent) == 1
    assert brain.calls == 0


async def test_quiet_intensity_sends_nothing(tmp_path: Path) -> None:
    clock = FakeClock(NOON)
    service, adapter, _brain, _router = build_service(tmp_path, clock)
    service.behavior = dataclasses.replace(service.behavior, chat_intensity="quiet")

    await service.handle_notice(poke())

    assert adapter.sent == []


async def test_daily_cap_blocks_21st_poke(tmp_path: Path) -> None:
    clock = FakeClock(NOON)
    service, adapter, _brain, _router = build_service(
        tmp_path,
        clock,
        social=SocialConfig(
            poke_enabled=True, poke_cooldown_seconds=300, poke_daily_cap=20
        ),
    )

    for _ in range(21):
        clock.advance(301)  # 每次都越过冷却，只让每日上限生效
        await service.handle_notice(poke())

    assert len(adapter.sent) == 20


async def test_invalid_poke_is_ignored(tmp_path: Path) -> None:
    clock = FakeClock(NOON)
    service, adapter, _brain, _router = build_service(tmp_path, clock)

    # 未知 chat_type：运行时类型被破坏，也不能抛异常。
    await service.handle_notice(poke(chat_type=cast(Any, "bogus")))
    # 空 chat_id：同样只忽略。
    await service.handle_notice(poke(chat_id=""))

    assert adapter.sent == []


def write_env(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.env"
    p.write_text(content, encoding="utf-8")
    return p


def write_yaml(tmp_path: Path, content: str = BASE_YAML) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_social_config_defaults_when_section_missing(tmp_path: Path) -> None:
    env = write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n")
    settings = load_settings(env_file=env, config_file=write_yaml(tmp_path))

    assert settings.social.poke_enabled is False
    assert settings.social.poke_cooldown_seconds == 300
    assert settings.social.poke_daily_cap == 20
    assert settings.social.join_welcome_enabled is False
    assert settings.social.join_welcome == ""
    assert settings.social.join_welcome_daily_cap == 10


def test_example_config_enables_poke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # chdir 到空目录：data/settings.local.yaml 覆盖层不会从真实仓库泄漏进来。
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    env = write_env(tmp_path, "BOT_QQ=10001\n")

    settings = load_settings(env_file=env, config_file=tmp_path / "config.yaml")

    assert settings.social.poke_enabled is True
    assert settings.social.poke_cooldown_seconds == 300
    assert settings.social.poke_daily_cap == 20
    assert settings.social.join_welcome_enabled is False
    assert settings.social.join_welcome == ""
    assert settings.social.join_welcome_daily_cap == 10


def test_example_config_social_block_matches_dataclass() -> None:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    social_raw = raw["social"]

    field_names = {f.name for f in dataclasses.fields(SocialConfig)}
    assert set(social_raw) == field_names
    assert social_raw["poke_enabled"] is True
