"""出站语音合成（edge-tts）：懒加载、原子落盘、失败/超时退化。

全部离线：``edge_tts`` 用假模块注入 ``sys.modules``；没有任何网络调用。
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dafeiyu.tts as tts_module
from dafeiyu.config import SpeechConfig, load_settings

PAYLOAD = b"ID3fake-mp3"


class FakeCommunicate:
    def __init__(self, owner: FakeEdgeTTS, text: str) -> None:
        self.owner = owner
        self.text = text

    async def save(self, path: str) -> None:
        self.owner.saves.append(path)
        if self.owner.delay:
            await asyncio.sleep(self.owner.delay)
        if self.owner.error is not None:
            raise self.owner.error
        Path(path).write_bytes(self.owner.payload)


class FakeEdgeTTS(SimpleNamespace):
    """最小 edge-tts 替身：只实现 ``Communicate(...).save(path)``。"""

    def __init__(
        self,
        *,
        payload: bytes = PAYLOAD,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        super().__init__()
        self.payload = payload
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, str, str]] = []
        self.saves: list[str] = []

    def Communicate(
        self, text: str, *, voice: str = "", rate: str = "", **_: Any
    ) -> FakeCommunicate:
        self.calls.append((text, voice, rate))
        return FakeCommunicate(self, text)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # asyncio 原语与事件循环绑定；每个测试换一份，避免跨 loop 复用。
    monkeypatch.setattr(tts_module, "_SEMAPHORE", asyncio.Semaphore(1))
    monkeypatch.setattr(tts_module, "_TIMEOUT_SECONDS", 30.0)


def make_cfg(**overrides: Any) -> SpeechConfig:
    return dataclasses.replace(SpeechConfig(), **overrides)


def install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeEdgeTTS) -> None:
    monkeypatch.setitem(sys.modules, "edge_tts", fake)


async def test_synthesize_writes_mp3_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEdgeTTS()
    install_fake(monkeypatch, fake)
    out = tmp_path / "replies" / "voice-1.mp3"

    ok = await tts_module.synthesize("你好呀", out, make_cfg(tts_enabled=True))

    assert ok is True
    assert out.is_file()
    assert out.read_bytes() == PAYLOAD
    # 先写 <out>.tmp 再 os.replace，成功路径不残留临时文件。
    assert not out.with_name("voice-1.mp3.tmp").exists()
    assert fake.calls == [("你好呀", "zh-CN-XiaoxiaoNeural", "+0%")]


async def test_disabled_returns_false_without_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEdgeTTS()
    install_fake(monkeypatch, fake)
    out = tmp_path / "voice.mp3"

    ok = await tts_module.synthesize("你好呀", out, make_cfg(tts_enabled=False))

    assert ok is False
    assert fake.saves == []
    assert fake.calls == []
    assert not out.exists()
    assert not out.with_name("voice.mp3.tmp").exists()


async def test_empty_text_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEdgeTTS()
    install_fake(monkeypatch, fake)
    out = tmp_path / "voice.mp3"

    ok = await tts_module.synthesize("   ", out, make_cfg(tts_enabled=True))

    assert ok is False
    assert fake.saves == []
    assert not out.exists()


async def test_too_long_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEdgeTTS()
    install_fake(monkeypatch, fake)
    out = tmp_path / "voice.mp3"

    ok = await tts_module.synthesize(
        "1234", out, make_cfg(tts_enabled=True, tts_max_chars=3)
    )

    assert ok is False
    assert fake.saves == []
    assert not out.exists()
    assert not out.with_name("voice.mp3.tmp").exists()


async def test_save_error_returns_false_and_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEdgeTTS(error=RuntimeError("boom"))
    install_fake(monkeypatch, fake)
    out = tmp_path / "voice.mp3"

    ok = await tts_module.synthesize("你好呀", out, make_cfg(tts_enabled=True))

    assert ok is False
    assert not out.exists()
    assert not out.with_name("voice.mp3.tmp").exists()


async def test_timeout_returns_false_and_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEdgeTTS(delay=0.5)
    install_fake(monkeypatch, fake)
    monkeypatch.setattr(tts_module, "_TIMEOUT_SECONDS", 0.01)
    out = tmp_path / "voice.mp3"

    ok = await tts_module.synthesize("你好呀", out, make_cfg(tts_enabled=True))

    assert ok is False
    assert not out.exists()
    assert not out.with_name("voice.mp3.tmp").exists()


async def test_missing_package_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # None 在 sys.modules 里会让 ``import edge_tts`` 直接 ImportError。
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    out = tmp_path / "voice.mp3"

    ok = await tts_module.synthesize("你好呀", out, make_cfg(tts_enabled=True))

    assert ok is False
    assert not out.exists()


def test_module_has_no_top_level_edge_tts_import() -> None:
    source = inspect.getsource(tts_module)
    tree = ast.parse(source)
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")

    assert not any(
        name == "edge_tts" or name.startswith("edge_tts.") for name in top_level
    )
    # 依赖仍在函数体内按名字懒加载，而不是被彻底删掉。
    assert 'importlib.import_module("edge_tts")' in source


def _write_config(tmp_path: Path, speech: dict[str, Any] | None = None) -> Path:
    lines = [
        "persona:",
        "  name: 溟月",
        "  aliases: [溟月]",
        "  max_reply_chars: 120",
        "  max_reply_chars_technical: 300",
        "triggers: {}",
        "groups: {}",
        "budget: {}",
        "memory: {}",
        "logging:",
        "  level: INFO",
        "  dir: logs",
    ]
    if speech is not None:
        lines.append("speech:")
        for key, value in speech.items():
            lines.append(f"  {key}: {value}")
    config = tmp_path / "config.yaml"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config


def _write_env(tmp_path: Path) -> Path:
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=sk-test\nBOT_QQ=10001\n", encoding="utf-8")
    return env


def test_load_settings_tts_defaults_off(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    settings = load_settings(
        env_file=_write_env(tmp_path),
        config_file=_write_config(tmp_path),
    )

    assert settings.speech.tts_enabled is False
    assert settings.speech.tts_voice == "zh-CN-XiaoxiaoNeural"
    assert settings.speech.tts_rate == "+0%"
    assert settings.speech.tts_max_chars == 200


def test_load_settings_parses_tts_overrides(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = _write_config(
        tmp_path,
        {
            "tts_enabled": True,
            "tts_voice": "zh-CN-YunxiNeural",
            "tts_rate": "-10%",
            "tts_max_chars": 120,
        },
    )

    settings = load_settings(env_file=_write_env(tmp_path), config_file=config)

    assert settings.speech.tts_enabled is True
    assert settings.speech.tts_voice == "zh-CN-YunxiNeural"
    assert settings.speech.tts_rate == "-10%"
    assert settings.speech.tts_max_chars == 120
