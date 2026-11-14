"""「此刻」注入：时间感 / 群名 / 距上次开口 / 上一句是谁在说。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from dafeiyu import service as service_module
from dafeiyu.config import (
    BudgetConfig,
    GroupsConfig,
    InjectionConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    Settings,
    TriggerConfig,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message
from dafeiyu.router import Router
from dafeiyu.service import Service, _duration_text, _period_of_day

SHANGHAI = timezone(timedelta(hours=8))
GROUP = "qq:1001"
PRIVATE = "qq:2002"
NOW = datetime(2026, 11, 3, 23, 10, tzinfo=SHANGHAI)


class FakeAdapter:
    bot_id = "qq:999"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatType, str, str]] = []

    def on_message(self, handler) -> None:
        pass

    async def send(self, chat_type: ChatType, chat_id: str, text: str) -> str:
        self.sent.append((chat_type, chat_id, text))
        return "bot-1"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class GroupInfoAdapter(FakeAdapter):
    """有 call_action 的适配器（QQ 网关）；记录查询次数。"""

    def __init__(self, name: str = "我们一起测试吖") -> None:
        super().__init__()
        self.name = name
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action: str, params: dict) -> dict:
        self.calls.append((action, params))
        return {"group_name": self.name}


def make_settings() -> Settings:
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
        triggers=TriggerConfig(
            60, 0.0, ("01:00", "07:00"), ("怎么",), mode="model", batch_seconds=0.0
        ),
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({PRIVATE})),
        budget=BudgetConfig(200, 300000),
        memory=MemoryConfig(20, 5, 20),
        logging=LoggingConfig("INFO", "logs"),
    )


def build(tmp_path, *, adapter=None, injection: InjectionConfig | None = None):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()
    settings = make_settings()
    adapter = adapter or FakeAdapter()
    router = Router(settings, store, clock=lambda: NOW)
    service = Service(
        settings,
        store,
        object(),
        {"qq": adapter},
        router,
        "你是溟月",
        injection=injection or InjectionConfig(),
    )
    return service, store, adapter


def make_msg(content: str = "在吗", *, chat_type=ChatType.GROUP, chat_id=GROUP,
             nickname: str = "阿鱼") -> Message:
    return Message(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id="qq:2002",
        nickname=nickname,
        content=content,
        message_id="m1",
        segments=[],
        is_bot=False,
        timestamp=0,
    )


def test_scene_hint_covers_time_group_gap_and_speaker(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, store, _adapter = build(tmp_path)
    service._group_names[GROUP] = ("我们一起测试吖", time.monotonic())
    store.save_chat_state(
        ChatType.GROUP, GROUP, {"last_bot_spoke": NOW.timestamp() - 7200}
    )

    scene = service._scene_hint(ChatType.GROUP, GROUP, [make_msg(nickname="阿明")])

    assert scene.startswith("【此刻】")
    assert "11-03 周二 23:10" in scene
    assert "深夜" in scene
    assert "本群：我们一起测试吖" in scene
    assert "距你上次开口 2 小时" in scene
    assert "刚才是 阿明 在说" in scene


def test_private_chat_has_no_group_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, _store, _adapter = build(tmp_path)

    scene = service._scene_hint(
        ChatType.PRIVATE, PRIVATE, [make_msg(chat_type=ChatType.PRIVATE, chat_id=PRIVATE)]
    )

    assert "【此刻】" in scene
    assert "本群" not in scene


def test_recent_speech_uses_short_phrase(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, store, _adapter = build(tmp_path)
    store.save_chat_state(
        ChatType.GROUP, GROUP, {"last_bot_spoke": NOW.timestamp() - 5}
    )

    scene = service._scene_hint(ChatType.GROUP, GROUP, [make_msg()])

    assert "你刚刚才说过话" in scene
    assert "距你上次开口" not in scene


def test_missing_state_still_produces_scene(tmp_path, monkeypatch) -> None:
    """没有 chat_state（新群/刚重启）时不能编造「上次开口」。"""
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, _store, _adapter = build(tmp_path)

    scene = service._scene_hint(ChatType.GROUP, GROUP, [make_msg()])

    assert "11-03 周二 23:10" in scene
    assert "距你上次开口" not in scene
    assert "你刚刚才说过话" not in scene


async def test_schedule_group_name_fetches_once_then_caches(tmp_path) -> None:
    adapter = GroupInfoAdapter()
    service, _store, _adapter = build(tmp_path, adapter=adapter)

    service._schedule_group_name(GROUP)
    task = next(iter(service._bg_tasks))
    await task

    assert service._group_names[GROUP][0] == "我们一起测试吖"
    assert adapter.calls == [("get_group_info", {"group_id": 1001})]

    service._schedule_group_name(GROUP)  # 缓存未过期：不再起任务
    assert service._bg_tasks == set()
    assert len(adapter.calls) == 1


async def test_refresh_group_name_without_call_action_is_silent(tmp_path) -> None:
    """微信桥没有 call_action：跳过，不抛异常、只留空名占位。"""
    service, _store, _adapter = build(tmp_path)

    await service._refresh_group_name(GROUP)

    assert service._group_names[GROUP][0] == ""
    assert service._scene_hint(ChatType.GROUP, GROUP, [make_msg()]).count("本群") == 0


async def test_refresh_group_name_failure_is_silent(tmp_path) -> None:
    class Broken(GroupInfoAdapter):
        async def call_action(self, action: str, params: dict) -> dict:
            raise RuntimeError("gateway down")

    service, _store, _adapter = build(tmp_path, adapter=Broken())

    await service._refresh_group_name(GROUP)

    assert service._group_names[GROUP][0] == ""
    # 失败占位同样有 TTL：网关抽风时不至于每条消息都去打一次。
    service._schedule_group_name(GROUP)
    assert service._bg_tasks == set()


def test_prompt_starts_with_scene(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, _store, _adapter = build(tmp_path)

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [make_msg()])

    assert prompt.startswith("【此刻】")


def test_scene_injection_can_be_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, _store, _adapter = build(
        tmp_path, injection=InjectionConfig(scene_enabled=False)
    )

    prompt = service._build_prompt(ChatType.GROUP, GROUP, [make_msg()])

    assert "【此刻】" not in prompt


def test_scene_hint_includes_atmosphere_when_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, store, _adapter = build(
        tmp_path, injection=InjectionConfig(atmosphere_enabled=True)
    )
    now_ts = int(NOW.timestamp())
    for index in range(10):
        store.add_message(
            Message(
                chat_type=ChatType.GROUP,
                chat_id=GROUP,
                user_id=f"qq:{index % 5}",
                nickname=f"人{index}",
                content="为什么？" if index % 2 else "哈哈哈",
                message_id=f"seed-{index}",
                timestamp=now_ts - 300 + index * 20,
            )
        )

    scene = service._scene_hint(ChatType.GROUP, GROUP, [make_msg()])

    assert "群气氛：" in scene
    assert "聊得正热" in scene
    assert "好几个人在场" in scene


def test_scene_hint_omits_atmosphere_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, _store, _adapter = build(tmp_path)

    scene = service._scene_hint(ChatType.GROUP, GROUP, [make_msg()])

    assert "群气氛" not in scene


# ---- T3：多消息批次不再只报最后一个人（GAP-4）----------------------------


def test_multi_message_scene_lists_all_speakers(tmp_path, monkeypatch) -> None:
    """多条批次列出本批说话人，且整句不得含「刚是」（含「刚才是」）两个字。"""
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, _store, _adapter = build(tmp_path)

    scene = service._scene_hint(
        ChatType.GROUP,
        GROUP,
        [make_msg("今天聊什么", nickname="阿明"), make_msg("随便呀", nickname="阿花")],
    )

    assert "这批是 阿明、阿花 在说" in scene
    assert "刚是" not in scene


def test_single_message_scene_keeps_last_speaker_phrase(tmp_path, monkeypatch) -> None:
    """单条批次保留原句「刚才是 X 在说」。"""
    monkeypatch.setattr(service_module, "now_shanghai", lambda: NOW)
    service, _store, _adapter = build(tmp_path)

    scene = service._scene_hint(ChatType.GROUP, GROUP, [make_msg(nickname="阿明")])

    assert "刚才是 阿明 在说" in scene


def test_period_of_day_and_duration_labels() -> None:
    assert _period_of_day(2) == "凌晨"
    assert _period_of_day(7) == "清晨"
    assert _period_of_day(9) == "上午"
    assert _period_of_day(12) == "中午"
    assert _period_of_day(15) == "下午"
    assert _period_of_day(18) == "傍晚"
    assert _period_of_day(21) == "晚上"
    assert _period_of_day(23) == "深夜"
    assert _duration_text(59) == "1 分钟"
    assert _duration_text(3599) == "59 分钟"
    assert _duration_text(7300) == "2 小时"
    assert _duration_text(86400 * 3) == "3 天"
