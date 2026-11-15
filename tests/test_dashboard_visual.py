"""面板视觉不变量回归（hotchat-lifetime-panel-redesign todo 22）。

输入契约就是 ``scripts/qa_dashboard_shots.py`` 产出的 ``metrics.json``：
桌面 / 手机两档的 ``content``（``.timeline-body`` 内容左右边缘与宽度）、
``bubbles``（每条 ``.timeline-msg`` 的 bot 标记、宽度、左右边缘、``maxWidth``、
``data-channel``、``border-top-color``）、``dates``（日期分隔条宽度）、``page``。

判定原则：**不靠人眼看图**，全部用像素数值 + ±1px 容差；面板没跑就 ``skip``
并写明原因，绝不当成「通过」。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "qa_dashboard_shots.py"
BASE_URL = os.environ.get("DAFEIYU_DASHBOARD_URL", "http://127.0.0.1:8787").rstrip("/")

#: 像素容差：布局取整 + 子像素抗锯齿，1px 内的差不算错位。
TOL = 1.0

#: ``.timeline-msg`` 的计算值形如 ``min(543.375px, 88%)``：上限取两者较小者。
_MAX_WIDTH_RE = re.compile(r"min\((?P<cap>[\d.]+)px,\s*(?P<pct>[\d.]+)%\)")

#: 没有会话 / 会话没有消息时没法度量几何：明确 skip，不静默通过。
_NO_GEOMETRY_REASONS = {
    "no-session": "面板里没有任何会话",
    "no-messages": "打开时间线后没有任何消息",
    "no-timeline-button": "会话行里找不到「查看时间线」按钮",
}


def _panel_alive(base_url: str, timeout: float = 2.0) -> bool:
    """探活面板：任何 HTTP 状态码都算活着（401 = 需要登录，不是不可达）。"""
    try:
        with urllib.request.urlopen(
            f"{base_url}/api/auth/session", timeout=timeout
        ) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


@pytest.fixture(scope="module")
def visual_metrics(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """跑一遍视觉验收脚本，返回两档 metrics（面板不可达时 skip）。"""
    if not _panel_alive(BASE_URL):
        pytest.skip(f"面板未运行（{BASE_URL}），无法做视觉度量")
    out_dir = tmp_path_factory.mktemp("dashboard-visual")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base-url",
            BASE_URL,
            "--out-dir",
            str(out_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode == 2:
        pytest.skip(f"面板不可达：{BASE_URL}（{result.stderr.strip()}）")
    if result.returncode == 3:
        pytest.skip(
            "面板需要登录但没有口令：设置 DAFEIYU_DASHBOARD_PASSWORD "
            "或 data/dashboard.password 后再跑"
        )
    assert result.returncode == 0, (
        f"视觉验收脚本失败（exit={result.returncode}）：{result.stderr.strip()}"
    )
    metrics_path = out_dir / "metrics.json"
    assert metrics_path.is_file(), f"脚本没有产出 metrics.json：{out_dir}"
    data = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and {"desktop", "mobile"} <= data.keys(), (
        f"metrics.json 结构不对：{sorted(data) if isinstance(data, dict) else data!r}"
    )
    for name in ("desktop", "mobile"):
        reason = data[name].get("reason") or ""
        if reason:
            pytest.skip(f"{name} 档无法度量：{_NO_GEOMETRY_REASONS.get(reason, reason)}")
    return data


def _content(view: dict[str, Any]) -> dict[str, float]:
    return view["content"]


def _bots(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in view["bubbles"] if item["bot"]]


def _humans(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in view["bubbles"] if not item["bot"]]


def _max_width_px(bubble: dict[str, Any], content_width: float) -> float:
    """把 ``min(<cap>px, <pct>%)`` 换算成像素上限（与浏览器同一口径）。"""
    match = _MAX_WIDTH_RE.fullmatch(str(bubble["maxWidth"]).strip())
    assert match is not None, f"未预期的 max-width：{bubble['maxWidth']!r}"
    cap = float(match.group("cap"))
    percent = float(match.group("pct")) / 100.0
    return min(cap, percent * content_width)


def test_desktop_bubbles_align_to_content_edges(visual_metrics: dict[str, Any]) -> None:
    """桌面：bot 右边缘彼此相等且贴内容右边缘，人左边缘贴内容左边缘。"""
    view = visual_metrics["desktop"]
    content = _content(view)
    bots = _bots(view)
    humans = _humans(view)
    assert bots, "桌面档没有 bot 气泡，度量不成立"
    assert humans, "桌面档没有人气泡，度量不成立"

    bot_rights = [item["right"] for item in bots]
    assert max(bot_rights) - min(bot_rights) <= TOL, (
        f"bot 右边缘不一致：{sorted({round(value, 2) for value in bot_rights})}"
    )
    assert abs(bot_rights[0] - content["right"]) <= TOL, (
        f"bot 右边缘 {bot_rights[0]:.2f} != 内容右边缘 {content['right']:.2f}"
    )

    human_lefts = [item["left"] for item in humans]
    assert max(human_lefts) - min(human_lefts) <= TOL, (
        f"人左边缘不一致：{sorted({round(value, 2) for value in human_lefts})}"
    )
    assert abs(human_lefts[0] - content["left"]) <= TOL, (
        f"人左边缘 {human_lefts[0]:.2f} != 内容左边缘 {content['left']:.2f}"
    )


def test_mobile_bubbles_align_to_content_edges(visual_metrics: dict[str, Any]) -> None:
    """手机：同一条左右基准必须成立（证明 `max-width:100%` 覆盖已删除）。"""
    view = visual_metrics["mobile"]
    content = _content(view)
    bots = _bots(view)
    humans = _humans(view)
    assert bots and humans, "手机档气泡不足，度量不成立"
    assert view["page"]["innerWidth"] == 390, (
        f"手机档视口不是 390：innerWidth={view['page']['innerWidth']}"
    )
    assert abs(max(item["right"] for item in bots) - content["right"]) <= TOL, (
        f"bot 右边缘没贴内容右边缘：{content['right']:.2f}"
    )
    assert abs(min(item["left"] for item in humans) - content["left"]) <= TOL, (
        f"人左边缘没贴内容左边缘：{content['left']:.2f}"
    )


def test_desktop_width_follows_content(visual_metrics: dict[str, Any]) -> None:
    """桌面：宽度按内容定——短句收窄、长句封顶、1 字最窄，不再被拉伸撑满。"""
    view = visual_metrics["desktop"]
    content_width = _content(view)["width"]
    bubbles = view["bubbles"]
    assert bubbles, "桌面档没有气泡，度量不成立"

    caps = {bubble["maxWidth"] for bubble in bubbles}
    assert len(caps) == 1, f"气泡 max-width 不统一：{sorted(caps)}"
    cap_px = _max_width_px(bubbles[0], content_width)
    assert cap_px <= 0.88 * content_width + TOL, (
        f"上限 {cap_px:.2f} 超过容器 88%（{0.88 * content_width:.2f}）"
    )
    for bubble in bubbles:
        assert bubble["width"] <= cap_px + TOL, (
            f"气泡超上限：width={bubble['width']:.2f} cap={cap_px:.2f}"
        )

    short = [item for item in bubbles if item["textLength"] <= 10]
    long = [item for item in bubbles if item["textLength"] > 10]
    assert short, "没有 ≤10 字的短消息，短句规则未被覆盖"
    assert long, "没有 >10 字的长消息，长句规则未被覆盖"
    for bubble in short:
        assert bubble["width"] <= 0.5 * content_width + TOL, (
            f"短消息被撑宽：{bubble['textLength']} 字 / {bubble['width']:.2f}px"
        )
    for bubble in long:
        assert bubble["width"] <= 0.88 * content_width + TOL, (
            f"长消息超 88%：{bubble['textLength']} 字 / {bubble['width']:.2f}px"
        )

    humans = _humans(view)
    assert min(item["width"] for item in humans) < content_width - TOL, (
        "所有人气泡都撑满容器：宽度仍是被拉伸的"
    )


def test_desktop_probe_bubbles_are_content_sized(
    visual_metrics: dict[str, Any],
) -> None:
    """1 字 < 10% / 10 字 ≤ 50% 容器：用同一组件做离屏探针，证明不是固定宽度。"""
    view = visual_metrics["desktop"]
    content_width = _content(view)["width"]
    probes = {int(item["chars"]): item["width"] for item in view["probes"]}
    assert set(probes) == {1, 10}, f"探针不全：{probes}"
    assert probes[1] < 0.1 * content_width, (
        f"1 字探针 {probes[1]:.2f}px 未小于容器 10%（{0.1 * content_width:.2f}）"
    )
    assert probes[10] <= 0.5 * content_width + TOL, (
        f"10 字探针 {probes[10]:.2f}px 超过容器 50%（{0.5 * content_width:.2f}）"
    )
    assert probes[1] < probes[10], "1 字与 10 字探针等宽：宽度不是按内容算的"
    assert probes[10] < content_width, "10 字探针撑满容器"


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
def test_date_separator_spans_content(
    visual_metrics: dict[str, Any], viewport: str
) -> None:
    """日期分隔条跨满整行（todo 9 的 `grid-column: 1 / -1` 生效证据）。"""
    view = visual_metrics[viewport]
    content_width = _content(view)["width"]
    dates = view["dates"]
    assert dates, f"{viewport} 档没有日期分隔条，度量不成立"
    for item in dates:
        assert item["gridColumn"] == "1 / -1", (
            f"{viewport} 档日期条 grid-column={item['gridColumn']!r}"
        )
        assert abs(item["width"] - content_width) <= TOL, (
            f"{viewport} 档日期条宽度 {item['width']:.2f} != 内容宽度 {content_width:.2f}"
        )


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
def test_no_horizontal_overflow(
    visual_metrics: dict[str, Any], viewport: str
) -> None:
    """整页不许横向溢出（窄屏下 grid auto 轨道撑宽曾把抽屉推出屏幕）。"""
    page = visual_metrics[viewport]["page"]
    assert page["overflowX"] is False, (
        f"{viewport} 档横向溢出：scrollWidth={page['scrollWidth']} "
        f"clientWidth={page['clientWidth']}"
    )


def test_channel_marks_and_colors(visual_metrics: dict[str, Any]) -> None:
    """通道标记存在；两种通道都在时上边框颜色必须不同（单通道则只断言有标记）。"""
    colors: dict[str, set[str]] = {}
    for view in visual_metrics.values():
        for bubble in view["bubbles"]:
            channel = bubble["channel"]
            assert channel in {"qq", "wechat", ""}, f"未预期的 data-channel={channel!r}"
            if channel:
                colors.setdefault(channel, set()).add(bubble["borderTopColor"])
    assert colors, "没有任何气泡被标记 data-channel"
    if {"qq", "wechat"} <= colors.keys():
        assert colors["qq"] != colors["wechat"], (
            f"QQ 与微信通道配色相同：{sorted(colors['qq'] | colors['wechat'])}"
        )
