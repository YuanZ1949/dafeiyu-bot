"""mind 预算边界：mind_chars 默认值与六段的丢弃顺序（GAP-3）。

锁定的顺序：印象 → 立场 → 近况 → 关系卡 → 未了事；episode 永不主动丢，
只剩它时才按预算截断。预算只数数据体，不数 header 与边界行。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from dafeiyu.config import InjectionConfig, _build_injection, load_settings
from dafeiyu.memory.context import (
    INJECTION_BOUNDARY,
    INJECTION_EPISODE_HEADER,
    INJECTION_IMPRESSION_HEADER,
    INJECTION_OPEN_LOOP_HEADER,
    INJECTION_RELATION_HEADER,
    INJECTION_STANCE_HEADER,
    INJECTION_STATE_HEADER,
    _mind_blocks,
)
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Episode, Message, OpenLoop

GROUP = ChatType.GROUP
CHAT = "qq:1"
USER = "qq:2"

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

# 六段各自的 block header；impressions 是唯一不带数据边界行的一段。
HEADERS = {
    "episode": INJECTION_EPISODE_HEADER,
    "loops": INJECTION_OPEN_LOOP_HEADER,
    "relations": INJECTION_RELATION_HEADER,
    "state": INJECTION_STATE_HEADER,
    "stances": INJECTION_STANCE_HEADER,
    "impressions": INJECTION_IMPRESSION_HEADER,
}


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "budget.db")
    s.init_schema()
    yield s
    s.close()


def cfg(mind_chars: int) -> InjectionConfig:
    """六段全开；各段字符上限沿用默认 150/220/200/100/160/160。"""
    return InjectionConfig(
        inject_episodes=True,
        inject_open_loops=True,
        inject_impressions=True,
        inject_state=True,
        inject_relations=True,
        inject_stances=True,
        mind_chars=mind_chars,
    )


def seed_six_sections(store: Store) -> None:
    """把六段都塞到各自的字符上限，合计 220+200+160+100+160+150 = 990。"""
    now = int(time.time())
    store.add_message(
        Message(GROUP, CHAT, USER, "阿猫", "在聊鱼缸", "m1", timestamp=now)
    )
    store.save_episode(
        Episode(
            chat_type=GROUP,
            chat_id=CHAT,
            start_message_id="m1",
            last_message_id="m1",
            topic="",
            summary="情" * 400,
            started_at=now - 60,
        )
    )
    store.add_open_loop(
        OpenLoop(GROUP, CHAT, "question", "事" * 400)
    )
    # 三条印象既撑满关系卡的 facts，也撑满「群友印象」。
    for kind in ("preference", "habit", "attitude"):
        store.upsert_impression(
            GROUP, CHAT, USER, kind, "猫" * 90, 0.9, subject_nickname="阿猫"
        )
    store.set_bot_state(focus="在" * 200, mood_note="心" * 200)
    store.upsert_stance("鱼缸", "观" * 300)


def bodies(blocks: list[str]) -> dict[str, str]:
    """从 `_mind_blocks` 的返回里剥掉 header/boundary，只留数据体。"""
    result: dict[str, str] = {}
    for block in blocks:
        for name, header in HEADERS.items():
            if not block.startswith(header):
                continue
            rest = block[len(header) :].removeprefix("\n")
            if rest.startswith(INJECTION_BOUNDARY):
                rest = rest[len(INJECTION_BOUNDARY) :].removeprefix("\n")
            result[name] = rest
            break
        else:  # pragma: no cover - 防御：出现未知 header 说明测试假设失效
            raise AssertionError(f"unknown mind block: {block[:20]!r}")
    return result


def render(store: Store, mind_chars: int) -> dict[str, str]:
    return bodies(_mind_blocks(store, GROUP, CHAT, cfg(mind_chars)))


# ---- (a) 六段总和 > 预算时，返回的数据体合计不超过预算 --------------------


def test_mind_budget_caps_total_body_chars(store: Store) -> None:
    seed_six_sections(store)

    full = render(store, mind_chars=2000)
    assert set(full) == set(HEADERS)
    total = sum(len(body) for body in full.values())
    assert total == 990 > 500
    assert {name: len(body) for name, body in full.items()} == {
        "episode": 220,
        "loops": 200,
        "relations": 160,
        "state": 100,
        "stances": 160,
        "impressions": 150,
    }

    tight = render(store, mind_chars=500)
    assert sum(len(body) for body in tight.values()) <= 500


# ---- (b) 逐步收紧预算：episode 是最后一个被保留的段 ----------------------


def test_tightening_budget_keeps_episode_last(store: Store) -> None:
    seed_six_sections(store)

    assert set(render(store, 900)) == {
        "episode",
        "loops",
        "relations",
        "state",
        "stances",
    }
    assert set(render(store, 700)) == {"episode", "loops", "relations", "state"}
    assert set(render(store, 500)) == {"episode", "loops"}

    tightest = render(store, 300)
    assert set(tightest) == {"episode"}
    assert sum(len(body) for body in tightest.values()) <= 300


# ---- (c) 关系卡先于未了事消失 -------------------------------------------


def test_relations_disappear_before_loops(store: Store) -> None:
    seed_six_sections(store)

    at_700 = render(store, 700)
    assert "relations" in at_700
    assert "loops" in at_700

    at_500 = render(store, 500)
    assert "relations" not in at_500
    assert "loops" in at_500
    assert "episode" in at_500


# ---- (d) 默认预算 1000（代码默认 + 示例配置） ---------------------------


def test_load_settings_defaults_mind_chars_to_1000(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_QQ=10001\n", encoding="utf-8")

    settings = load_settings(
        env_file=env_file, config_file=tmp_path / "config.yaml"
    )

    assert settings.injection.mind_chars == 1000


def test_missing_key_falls_back_to_1000() -> None:
    """旧配置没有 memory.mind_chars 时，解析回退值也必须是 1000。"""
    assert _build_injection({}, {}).mind_chars == 1000


# ---- QA failure：只有 episode 且预算 0，不抛异常、返回空 ------------------


def test_zero_budget_with_only_episode_returns_empty(store: Store) -> None:
    store.save_episode(
        Episode(
            chat_type=GROUP,
            chat_id=CHAT,
            start_message_id="m1",
            last_message_id="m1",
            topic="",
            summary="情" * 100,
            started_at=100,
        )
    )

    blocks = _mind_blocks(store, GROUP, CHAT, cfg(0))

    assert blocks == []
