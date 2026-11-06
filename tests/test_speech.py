"""语音转写：SILK/AMR 解码与 faster-whisper 接线。

全部离线：``pysilk`` / ``faster_whisper`` 用假模块注入，ffmpeg 用假的
``subprocess.run`` 代替；只有可选的往返测试会调真实依赖（缺就 skip）。
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import dafeiyu.speech as speech_module
from dafeiyu.config import Settings

SILK = b"#!SILK_V3" + b"0" * 64
TENCENT_SILK = b"\x02#!SILK_V" + b"3" + b"0" * 64
AMR = b"#!AMR\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    speech_module._MODEL_CACHE.clear()
    speech_module._SEMAPHORES.clear()
    monkeypatch.delenv("DAFEIYU_SPEECH_DISABLE", raising=False)


def make_settings(
    tmp_path: Path, *, workspace: bool = True, **overrides: Any
) -> Settings:
    speech = SimpleNamespace(
        enabled=True,
        model="base",
        language="zh",
        device="cpu",
        compute_type="int8",
        beam_size=1,
        max_seconds=60,
        timeout_seconds=5.0,
        concurrency=1,
        max_bytes=20 * 1024 * 1024,
        initial_prompt="以下是普通话的句子，请用简体中文转写。",
        hf_endpoint="",
    )
    for name, value in overrides.items():
        setattr(speech, name, value)
    return cast(
        Settings,
        cast(
            object,
            SimpleNamespace(
                workspace=SimpleNamespace(
                    enabled=workspace, dir=str(tmp_path / "ws")
                ),
                speech=speech,
            ),
        ),
    )


class FakePysilk:
    def __init__(self) -> None:
        self.sample_rates: list[int] = []

    def decode(
        self, in_handle: Any, out_handle: Any, sample_rate: int, **_kwargs: Any
    ) -> None:
        self.sample_rates.append(sample_rate)
        out_handle.write(b"\x00\x00" * 800)


def patch_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> list[list[str]]:
    """替换 subprocess.run：记录参数并伪造输出 wav。"""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        calls.append(list(args))
        if fail:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(speech_module, "_locate", lambda _name: "ffmpeg")
    monkeypatch.setattr(speech_module.subprocess, "run", fake_run)
    return calls


class FakeWhisperModel:
    instances: list["FakeWhisperModel"] = []
    texts = [" 你好", "世界 "]

    def __init__(self, model_size: str, device: str = "cpu", compute_type: str = "int8") -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.calls: list[dict[str, Any]] = []
        type(self).instances.append(self)

    def transcribe(self, audio: str, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append({"audio": audio, **kwargs})
        # 惰性生成器：调用方必须消费完才有结果。
        return (SimpleNamespace(text=text) for text in self.texts), SimpleNamespace()


def patch_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWhisperModel.instances.clear()
    fake_module = SimpleNamespace(WhisperModel=FakeWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)


# ---------------------------------------------------------------- decode_to_wav
def test_silk_decode_calls_pysilk_and_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.silk"
    source.write_bytes(SILK)
    target = tmp_path / "out.wav"
    pysilk = FakePysilk()
    monkeypatch.setitem(sys.modules, "pysilk", pysilk)
    calls = patch_ffmpeg(monkeypatch)

    assert speech_module.decode_to_wav(source, target) is True
    assert pysilk.sample_rates == [24000]
    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "ffmpeg"
    assert "-f" in args and args[args.index("-f") + 1] == "s16le"
    assert args[-1] == str(target)
    assert not (tmp_path / "out-pcm.tmp").exists()


def test_tencent_silk_prefix_is_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.silk"
    source.write_bytes(TENCENT_SILK)
    pysilk = FakePysilk()
    monkeypatch.setitem(sys.modules, "pysilk", pysilk)
    patch_ffmpeg(monkeypatch)

    assert speech_module.decode_to_wav(source, tmp_path / "out.wav") is True
    assert pysilk.sample_rates == [24000]


def test_amr_decode_does_not_touch_pysilk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)
    pysilk = FakePysilk()
    monkeypatch.setitem(sys.modules, "pysilk", pysilk)
    calls = patch_ffmpeg(monkeypatch)

    assert speech_module.decode_to_wav(source, tmp_path / "out.wav") is True
    assert pysilk.sample_rates == []
    assert "s16le" not in calls[0]


def test_other_audio_goes_straight_to_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.wav"
    source.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32)
    pysilk = FakePysilk()
    monkeypatch.setitem(sys.modules, "pysilk", pysilk)
    calls = patch_ffmpeg(monkeypatch)

    assert speech_module.decode_to_wav(source, tmp_path / "out.wav") is True
    assert pysilk.sample_rates == []
    assert len(calls) == 1


def test_missing_pysilk_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.silk"
    source.write_bytes(SILK)
    monkeypatch.setitem(sys.modules, "pysilk", None)
    calls = patch_ffmpeg(monkeypatch)

    assert speech_module.decode_to_wav(source, tmp_path / "out.wav") is False
    assert calls == []


def test_missing_ffmpeg_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)
    monkeypatch.setattr(speech_module, "_locate", lambda _name: None)

    assert speech_module.decode_to_wav(source, tmp_path / "out.wav") is False


def test_ffmpeg_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)
    patch_ffmpeg(monkeypatch, fail=True)

    assert speech_module.decode_to_wav(source, tmp_path / "out.wav") is False


def test_missing_source_returns_false(tmp_path: Path) -> None:
    assert (
        speech_module.decode_to_wav(tmp_path / "nope.silk", tmp_path / "out.wav")
        is False
    )


# ----------------------------------------------------------------- transcribe
async def test_transcribe_consumes_segments_and_passes_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)
    patch_ffmpeg(monkeypatch)
    patch_whisper(monkeypatch)
    settings = make_settings(tmp_path)

    text = await speech_module.transcribe(source, settings)

    assert text == "你好世界"
    model = FakeWhisperModel.instances[0]
    assert model.device == "cpu"
    assert model.compute_type == "int8"
    call = model.calls[0]
    assert call["language"] == "zh"
    assert call["beam_size"] == 1
    assert call["vad_filter"] is True
    assert call["initial_prompt"] == "以下是普通话的句子，请用简体中文转写。"
    assert Path(call["audio"]).suffix == ".wav"


async def test_transcribe_reuses_model_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)
    patch_ffmpeg(monkeypatch)
    patch_whisper(monkeypatch)
    settings = make_settings(tmp_path)

    await speech_module.transcribe(source, settings)
    await speech_module.transcribe(source, settings)

    assert len(FakeWhisperModel.instances) == 1


async def test_empty_initial_prompt_is_not_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)
    patch_ffmpeg(monkeypatch)
    patch_whisper(monkeypatch)
    settings = make_settings(tmp_path, initial_prompt="   ")

    await speech_module.transcribe(source, settings)

    assert "initial_prompt" not in FakeWhisperModel.instances[0].calls[0]


async def test_transcribe_missing_faster_whisper_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)
    patch_ffmpeg(monkeypatch)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    assert await speech_module.transcribe(source, make_settings(tmp_path)) == ""


async def test_transcribe_disabled_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)

    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("enabled=false 时不允许转写")

    monkeypatch.setattr(speech_module, "_transcribe_sync", boom)

    assert await speech_module.transcribe(
        source, make_settings(tmp_path, enabled=False)
    ) == ""


async def test_transcribe_env_disable_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)
    monkeypatch.setenv("DAFEIYU_SPEECH_DISABLE", "1")

    assert await speech_module.transcribe(source, make_settings(tmp_path)) == ""


async def test_transcribe_workspace_disabled_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)
    settings = make_settings(tmp_path, workspace=False)

    assert await speech_module.transcribe(source, settings) == ""


async def test_transcribe_oversize_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)

    assert await speech_module.transcribe(
        source, make_settings(tmp_path, max_bytes=4)
    ) == ""


async def test_transcribe_timeout_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "voice.amr"
    source.write_bytes(AMR)

    def slow(*_args: Any, **_kwargs: Any) -> str:
        time.sleep(0.3)
        return "太慢了"

    monkeypatch.setattr(speech_module, "_transcribe_sync", slow)

    assert await speech_module.transcribe(
        source, make_settings(tmp_path, timeout_seconds=0.05)
    ) == ""


async def test_transcribe_missing_file_returns_empty(tmp_path: Path) -> None:
    assert (
        await speech_module.transcribe(
            tmp_path / "nope.amr", make_settings(tmp_path)
        )
        == ""
    )


# ------------------------------------------------------- 可选：真实依赖往返测试
def test_real_silk_roundtrip(tmp_path: Path) -> None:
    if importlib.util.find_spec("pysilk") is None:
        pytest.skip("pysilk 未安装")
    ffmpeg = speech_module._locate("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg 不在 PATH / DAFEIYU_FFMPEG")

    pcm = tmp_path / "silence.pcm"
    pcm.write_bytes(b"\x00\x00" * 24000)  # 1 秒 24kHz 静音
    silk = tmp_path / "silence.silk"
    try:
        import pysilk

        with pcm.open("rb") as in_handle, silk.open("wb") as out_handle:
            pysilk.encode(in_handle, out_handle, 24000, 24000)
    except Exception as exc:  # pragma: no cover - 环境相关
        pytest.skip(f"pysilk.encode 不可用：{exc}")

    target = tmp_path / "out.wav"
    assert speech_module.decode_to_wav(silk, target) is True
    assert target.stat().st_size > 44
