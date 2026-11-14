"""todo 34：ffmpeg 探测尺寸与降采样。

覆盖：开关/依赖缺失的快路径、尺寸判断、临时文件命名不变量（C2）、
失败清理，以及本机真有 ffmpeg 时必须真跑的集成用例。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dafeiyu import imaging


def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("不应调用 subprocess")

    monkeypatch.setattr(imaging.subprocess, "run", boom)


def test_max_edge_zero_skips_without_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.png"
    source.write_bytes(b"old")
    _no_subprocess(monkeypatch)

    assert imaging.downscale(source, 0) is False
    assert source.read_bytes() == b"old"


def test_missing_ffmpeg_skips_without_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.png"
    source.write_bytes(b"old")
    monkeypatch.setattr(imaging, "locate", lambda _program: None)
    _no_subprocess(monkeypatch)

    assert imaging.downscale(source, 1280) is False


def test_small_image_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.png"
    source.write_bytes(b"old")
    monkeypatch.setattr(imaging, "locate", lambda _program: "ffmpeg")
    monkeypatch.setattr(imaging, "_locate_ffprobe", lambda _ffmpeg: "ffprobe")
    monkeypatch.setattr(imaging, "probe_size", lambda _path, _probe: (800, 600))
    _no_subprocess(monkeypatch)

    assert imaging.downscale(source, 1280) is False


def test_gif_is_never_downscaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.gif"
    source.write_bytes(b"GIF89a")
    _no_subprocess(monkeypatch)

    assert imaging.downscale(source, 1280) is False


def test_oversized_image_is_replaced_and_tmp_name_keeps_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.png"
    source.write_bytes(b"old")
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append([str(item) for item in args])
        Path(args[-1]).write_bytes(b"new")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(imaging, "locate", lambda _program: "ffmpeg")
    monkeypatch.setattr(imaging, "_locate_ffprobe", lambda _ffmpeg: "ffprobe")
    monkeypatch.setattr(imaging, "probe_size", lambda _path, _probe: (4000, 3000))
    monkeypatch.setattr(imaging.subprocess, "run", fake_run)

    assert imaging.downscale(source, 1280) is True
    assert source.read_bytes() == b"new"
    assert calls and "-vf" in calls[0]
    tmp = Path(calls[0][-1])
    # C2 #4：临时文件最后一个后缀必须是真实图片后缀，ffmpeg 才能选 muxer。
    assert tmp.suffix == source.suffix
    assert tmp.name == "a.tmp.png"
    assert tmp.parent == source.parent
    assert not list(tmp_path.glob("*.tmp.*"))


def test_ffmpeg_failure_keeps_original_and_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.png"
    source.write_bytes(b"old")

    def fake_run(args, **_kwargs):
        Path(args[-1]).write_bytes(b"partial")
        return SimpleNamespace(returncode=-22, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(imaging, "locate", lambda _program: "ffmpeg")
    monkeypatch.setattr(imaging, "_locate_ffprobe", lambda _ffmpeg: "ffprobe")
    monkeypatch.setattr(imaging, "probe_size", lambda _path, _probe: (4000, 3000))
    monkeypatch.setattr(imaging.subprocess, "run", fake_run)

    assert imaging.downscale(source, 1280) is False
    assert source.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.tmp.*"))


def _capture_downscale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> list[list[str]]:
    source = tmp_path / name
    source.write_bytes(b"old")
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        Path(args[-1]).write_bytes(b"new")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(imaging, "locate", lambda _program: "ffmpeg")
    monkeypatch.setattr(imaging, "_locate_ffprobe", lambda _ffmpeg: "ffprobe")
    monkeypatch.setattr(imaging, "probe_size", lambda _path, _probe: (4000, 3000))
    monkeypatch.setattr(imaging.subprocess, "run", fake_run)

    assert imaging.downscale(source, 1280) is True
    return calls


def test_jpeg_downscale_pins_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JPEG 二次编码必须显式给高质量，否则 1280px 也会肉眼发糊。"""
    calls = _capture_downscale(tmp_path, monkeypatch, "a.jpg")

    assert calls and "-q:v" in calls[0]
    assert calls[0][calls[0].index("-q:v") + 1] == "3"


def test_png_downscale_keeps_encoder_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_downscale(tmp_path, monkeypatch, "a.png")

    assert calls and "-q:v" not in calls[0]


@pytest.mark.parametrize("name", ["a.jpg", "a.jpeg", "a.png", "a.bmp"])
def test_static_suffixes_pin_single_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """静态容器只留首帧：原有语义不能丢。"""
    calls = _capture_downscale(tmp_path, monkeypatch, name)

    assert calls and "-frames:v" in calls[0]
    assert calls[0][calls[0].index("-frames:v") + 1] == "1"


def test_webp_does_not_pin_single_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WEBP 可能是多帧动图：再压 ``-frames:v 1`` 等于丢动画（I-1）。"""
    calls = _capture_downscale(tmp_path, monkeypatch, "a.webp")

    assert calls and "-frames:v" not in calls[0]


def test_empty_output_keeps_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.png"
    source.write_bytes(b"old")

    def fake_run(args, **_kwargs):
        Path(args[-1]).write_bytes(b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(imaging, "locate", lambda _program: "ffmpeg")
    monkeypatch.setattr(imaging, "_locate_ffprobe", lambda _ffmpeg: "ffprobe")
    monkeypatch.setattr(imaging, "probe_size", lambda _path, _probe: (4000, 3000))
    monkeypatch.setattr(imaging.subprocess, "run", fake_run)

    assert imaging.downscale(source, 1280) is False
    assert source.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.tmp.*"))


@pytest.mark.parametrize(
    "stdout",
    [b"4000,3000\n", b"  4000 , 3000 \r\n", b"\n4000,3000\n640,480\n"],
)
def test_probe_size_parses_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    monkeypatch.setattr(
        imaging.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout=stdout, stderr=b""
        ),
    )

    assert imaging.probe_size(tmp_path / "a.png", "ffprobe") == (4000, 3000)


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=1, stdout=b"4000,3000\n", stderr=b"err"),
        SimpleNamespace(returncode=0, stdout=b"abc,def\n", stderr=b""),
        SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
        SimpleNamespace(returncode=0, stdout=b"0,0\n", stderr=b""),
    ],
)
def test_probe_size_bad_output_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result
) -> None:
    monkeypatch.setattr(imaging.subprocess, "run", lambda *_a, **_k: result)

    assert imaging.probe_size(tmp_path / "a.png", "ffprobe") is None


def test_probe_size_swallows_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("missing")

    monkeypatch.setattr(imaging.subprocess, "run", boom)

    assert imaging.probe_size(tmp_path / "a.png", "ffprobe") is None


def test_locate_honours_dafeiyu_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAFEIYU_FFMPEG", "  /opt/ffmpeg  ")
    assert imaging.locate("ffmpeg") == "/opt/ffmpeg"

    monkeypatch.delenv("DAFEIYU_FFMPEG")
    monkeypatch.setattr(imaging.shutil, "which", lambda name: f"/bin/{name}")
    assert imaging.locate("ffprobe") == "/bin/ffprobe"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="本机没有 ffmpeg"
)
def test_real_ffmpeg_downscales_oversized_png(tmp_path: Path) -> None:
    """C2 #3：ffmpeg 存在时必须真跑，不允许 skip。"""
    ffmpeg = imaging.locate("ffmpeg")
    assert ffmpeg
    source = tmp_path / "big.png"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=2000x1500",
            "-frames:v",
            "1",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    ffprobe = imaging._locate_ffprobe(ffmpeg)
    assert ffprobe
    before = imaging.probe_size(source, ffprobe)
    assert before is not None and max(before) == 2000

    assert imaging.downscale(source, 1280) is True

    after = imaging.probe_size(source, ffprobe)
    assert after is not None
    assert max(after) <= 1280
    assert not list(tmp_path.glob("*.tmp.*"))


def _frame_count(path: Path) -> int | None:
    """用 ffprobe 数实际解出的帧；读不出（老版本 WEBP demuxer）返回 ``None``。"""
    ffmpeg = imaging.locate("ffmpeg")
    if not ffmpeg:
        return None
    ffprobe = imaging._locate_ffprobe(ffmpeg)
    if not ffprobe:
        return None
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    text = (completed.stdout or b"").decode("ascii", "ignore").strip()
    try:
        return int(text.splitlines()[0])
    except (ValueError, IndexError):
        return None


def _write_animated_webp(path: Path, frames: int = 6) -> bool:
    """用本机 ffmpeg 造一段多帧 WEBP；没有 libwebp_anim 编码器时返回 ``False``。"""
    ffmpeg = imaging.locate("ffmpeg")
    if not ffmpeg:
        return False
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x240:rate=5:duration={frames / 5:.1f}",
            "-loop",
            "0",
            "-c:v",
            "libwebp_anim",
            "-q:v",
            "60",
            str(path),
        ],
        check=False,
        capture_output=True,
    )
    return (
        completed.returncode == 0
        and path.is_file()
        and path.stat().st_size > 0
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机没有 ffmpeg")
def test_real_ffmpeg_keeps_animated_webp_frames(tmp_path: Path) -> None:
    """多帧 WEBP 经 downscale 后帧数不变（I-1 / G5）。

    ffprobe 读不出动画帧数的老构建（如某些 Windows 版 demuxer 不会解析
    ANMF 块）只能跳过：没有可用的观测手段，绝不伪造成功。
    """
    source = tmp_path / "anim.webp"
    if not _write_animated_webp(source):
        pytest.skip("本机 ffmpeg 没有 libwebp_anim 编码器")
    before = _frame_count(source)
    if before is None or before < 2:
        pytest.skip("本机 ffmpeg/ffprobe 读不出 WEBP 动画帧数")

    imaging.downscale(source, 200)

    assert _frame_count(source) == before


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机没有 ffmpeg")
def test_real_ffmpeg_static_jpeg_stays_single_frame(tmp_path: Path) -> None:
    """静态 JPEG 仍只产出单帧（``-frames:v 1`` 的旧语义不能丢）。"""
    ffmpeg = imaging.locate("ffmpeg")
    assert ffmpeg
    source = tmp_path / "big.jpg"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=2000x1500",
            "-frames:v",
            "1",
            str(source),
        ],
        check=True,
        capture_output=True,
    )

    assert imaging.downscale(source, 1280) is True

    assert _frame_count(source) == 1
