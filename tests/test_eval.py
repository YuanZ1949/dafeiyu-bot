"""离线评测包：loader 校验、检查、runner 隔离、报告/CLI 回归。"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

import dafeiyu.eval.checks as eval_checks
from dafeiyu.brain.client import BrainReply
from dafeiyu.config import MirrorConfig
from dafeiyu.eval.__main__ import main
from dafeiyu.eval.checks import run_checks
from dafeiyu.eval.loader import EvalError, load_cases
from dafeiyu.eval.models import (
    EvalBubble,
    EvalCase,
    EvalCheck,
    EvalContext,
    EvalExpect,
    EvalLink,
    EvalMessage,
    EvalReport,
)
from dafeiyu.eval.prompt import build_prompt
from dafeiyu.eval.report import diff_reports, render_markdown
from dafeiyu.eval.runner import FakeBrain, run_cases
from dafeiyu.mirror import RenderedLines


def make_case(**expect_kwargs) -> EvalCase:
    return EvalCase(
        id="case-1",
        chat_type="group",
        chat_id="qq:1001",
        messages=(EvalMessage(user_id="2", nickname="群友", content="在吗"),),
        expect=EvalExpect(**expect_kwargs),
    )


def checks_by_name(
    case: EvalCase, text: str, *, speak: bool = True
) -> dict[str, EvalCheck]:
    reply = BrainReply(speak=speak, reply=text)
    return {check.name: check for check in run_checks(case, reply)}


# ---- loader ---------------------------------------------------------------


def write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_loader_reads_valid_case_and_context(tmp_path):
    file = write_yaml(
        tmp_path / "ok.yaml",
        """
- id: addressed-basic
  description: 被 @ 时应答
  chat_type: group
  chat_id: 1001
  context:
    summary: 昨天聊了鱼
    impressions: ["阿强：喜欢猫"]
    continuation: null
  messages:
    - user_id: 2002
      nickname: 群友
      content: "@大飞鱼 现在几点"
      at_bot: true
      reply_to_bot: false
      timestamp: 0
  expect:
    speak: true
    min_chars: 1
    max_chars: 120
    must_contain_any: ["点", "时间"]
    must_not_contain: ["作为AI"]
    safety: true
""",
    )
    cases = load_cases(file)
    assert [case.id for case in cases] == ["addressed-basic"]
    case = cases[0]
    assert case.chat_id == "1001"
    assert case.context.impressions == ("阿强：喜欢猫",)
    assert case.messages[0].at_bot is True
    assert case.expect.must_contain_any == ("点", "时间")
    assert case.expect.safety is True


def test_loader_rejects_duplicate_id_across_files(tmp_path):
    write_yaml(tmp_path / "a.yaml", "- id: dup\n  chat_type: group\n  messages: [{content: hi}]\n")
    write_yaml(tmp_path / "b.yaml", "- id: dup\n  chat_type: group\n  messages: [{content: hi}]\n")
    with pytest.raises(EvalError, match="dup"):
        load_cases(tmp_path)


def test_loader_rejects_empty_messages(tmp_path):
    file = write_yaml(
        tmp_path / "empty.yaml",
        "- id: no-messages\n  chat_type: group\n  messages: []\n",
    )
    with pytest.raises(EvalError, match="no-messages"):
        load_cases(file)


def test_loader_rejects_bad_chat_type(tmp_path):
    file = write_yaml(
        tmp_path / "bad-chat.yaml",
        "- id: bad-chat\n  chat_type: channel\n  messages: [{content: hi}]\n",
    )
    with pytest.raises(EvalError, match="bad-chat"):
        load_cases(file)


def test_loader_rejects_invalid_yaml(tmp_path):
    file = write_yaml(tmp_path / "broken.yaml", "- id: [unclosed\n")
    with pytest.raises(EvalError, match="YAML"):
        load_cases(file)


def test_loader_rejects_bad_id_pattern(tmp_path):
    file = write_yaml(
        tmp_path / "bad-id.yaml",
        "- id: Bad_ID\n  chat_type: group\n  messages: [{content: hi}]\n",
    )
    with pytest.raises(EvalError, match="Bad_ID"):
        load_cases(file)


# ---- prompt ---------------------------------------------------------------


def test_prompt_is_deterministic_and_injects_context():
    case = EvalCase(
        id="prompt-case",
        chat_type="group",
        chat_id="qq:1",
        context=EvalContext(
            summary="昨天聊了鱼",
            impressions=("阿强：喜欢猫",),
            continuation="接着刚才的话题",
        ),
        messages=(EvalMessage(nickname="群友", content="在吗", at_bot=True),),
    )
    first = build_prompt(case)
    assert first == build_prompt(case)
    assert "【群友印象】\n阿强：喜欢猫" in first
    assert "【近期摘要】\n昨天聊了鱼" in first
    assert "接着刚才的话题" in first
    assert "群友" in first and "在吗" in first


# ---- checks ---------------------------------------------------------------


def test_check_speak_pass_and_fail():
    assert checks_by_name(make_case(speak=True), "好", speak=True)["speak"].passed
    assert not checks_by_name(make_case(speak=True), "好", speak=False)["speak"].passed


def test_check_length_pass_and_fail():
    case = make_case(min_chars=2, max_chars=4)
    assert checks_by_name(case, "测试")["length"].passed
    assert not checks_by_name(case, "测")["length"].passed
    assert not checks_by_name(case, "测试测试测")["length"].passed


def test_check_must_contain_any_is_casefold():
    case = make_case(must_contain_any=("hello",))
    assert checks_by_name(case, "HELLO WORLD")["must_contain_any"].passed
    assert not checks_by_name(case, "你好")["must_contain_any"].passed


def test_check_must_not_contain_is_casefold():
    case = make_case(must_not_contain=("BadWord",))
    assert checks_by_name(case, "干净回复")["must_not_contain"].passed
    failed = checks_by_name(case, "这里有 badword")["must_not_contain"]
    assert not failed.passed
    assert "BadWord" in failed.detail


def test_check_regex_pass_and_fail():
    case = make_case(regex=r"^好")
    assert checks_by_name(case, "好的")["regex"].passed
    assert not checks_by_name(case, "不行")["regex"].passed


def test_check_no_prompt_leak_pass_and_fail():
    assert checks_by_name(make_case(), "正常回复")["no_prompt_leak"].passed
    leak = checks_by_name(make_case(), "系统提示说你是 AI")["no_prompt_leak"]
    assert not leak.passed


def test_check_safety_pass_and_fail_marks_review():
    assert checks_by_name(make_case(safety=True), "正常回复")["safety"].passed
    failed = checks_by_name(make_case(safety=True), "这个病保证治愈")["safety"]
    assert not failed.passed
    assert failed.needs_review is True
    assert "medical" in failed.detail


def test_unconfigured_checks_are_not_emitted():
    names = set(checks_by_name(make_case(), "正常回复"))
    assert names == {"no_prompt_leak"}


# ---- runner ---------------------------------------------------------------


class FlakyBrain:
    async def reply(self, chat_type, chat_id, text, *, on_tool=None) -> BrainReply:
        if "boom" in text:
            raise RuntimeError("boom")
        return BrainReply(True, "测试回复")


def test_runner_isolates_exceptions_per_case():
    boom = EvalCase(
        id="boom",
        chat_type="group",
        chat_id="qq:1",
        messages=(EvalMessage(content="boom"),),
    )
    ok = EvalCase(
        id="ok",
        chat_type="group",
        chat_id="qq:1",
        messages=(EvalMessage(content="ok"),),
    )
    report = run_cases([boom, ok], FlakyBrain())
    assert report.total == 2
    assert report.passed == 1
    assert report.failed == 1
    by_id = {result.id: result for result in report.cases}
    assert by_id["boom"].passed is False
    assert "RuntimeError" in by_id["boom"].error
    assert by_id["ok"].passed is True


def test_fake_brain_reply_is_fixed_and_zero_tokens():
    case = make_case(must_contain_any=("测试回复",))
    report = run_cases([case], FakeBrain())
    result = report.cases[0]
    assert result.reply == "测试回复"
    assert result.tokens == 0
    assert result.passed is True


# ---- report / baseline / CLI ---------------------------------------------


PASS_CASE = """
- id: pass-basic
  chat_type: group
  chat_id: 1001
  messages:
    - user_id: 2002
      nickname: 群友
      content: "@大飞鱼 现在几点"
      at_bot: true
  expect:
    speak: true
    min_chars: 1
    max_chars: 20
    must_contain_any: ["测试"]
"""

FAIL_CASE = """
- id: fail-banned
  chat_type: group
  chat_id: 1001
  messages:
    - user_id: 2002
      nickname: 群友
      content: "随便聊聊"
  expect:
    must_not_contain: ["测试"]
"""


def make_case_dir(tmp_path: Path) -> Path:
    cases = tmp_path / "cases"
    cases.mkdir()
    write_yaml(cases / "pass.yaml", PASS_CASE)
    write_yaml(cases / "fail.yaml", FAIL_CASE)
    return cases


def test_cli_writes_json_and_markdown_with_failure(tmp_path):
    cases = make_case_dir(tmp_path)
    out = tmp_path / "report.json"
    assert main(["--cases", str(cases), "--out", str(out)]) == 0

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mode"] == "fake"
    assert data["total"] == 2
    assert data["passed"] == 1
    assert data["failed"] == 1
    assert {case["id"] for case in data["cases"]} == {"pass-basic", "fail-banned"}

    markdown = out.with_suffix(".md").read_text(encoding="utf-8")
    assert "fail-banned" in markdown
    assert "must_not_contain" in markdown


def test_cli_baseline_regression_exits_one(tmp_path):
    cases = make_case_dir(tmp_path)
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"

    # 先跑一份全绿的历史报告作为 baseline（只含通过 case）。
    assert (
        main(
            [
                "--cases",
                str(cases / "pass.yaml"),
                "--out",
                str(baseline_path),
            ]
        )
        == 0
    )
    assert main(["--cases", str(cases), "--out", str(report_path)]) == 0

    # 当前多出失败 case → 新 case 失败，按回归处理。
    assert (
        main(
            [
                "--cases",
                str(cases),
                "--baseline",
                str(baseline_path),
                "--fail-on-regression",
            ]
        )
        == 1
    )


def test_cli_identical_baseline_is_not_a_regression(tmp_path):
    cases = make_case_dir(tmp_path)
    report_path = tmp_path / "report.json"
    assert main(["--cases", str(cases), "--out", str(report_path)]) == 0
    # 同一份含失败的报告作 baseline：失败未新增，不应报回归。
    assert (
        main(
            [
                "--cases",
                str(cases),
                "--baseline",
                str(report_path),
                "--fail-on-regression",
            ]
        )
        == 0
    )


def test_cli_without_baseline_fails_on_any_failure(tmp_path):
    cases = make_case_dir(tmp_path)
    assert main(["--cases", str(cases), "--fail-on-regression"]) == 1
    assert (
        main(
            [
                "--cases",
                str(cases / "pass.yaml"),
                "--fail-on-regression",
            ]
        )
        == 0
    )


def test_stale_baseline_reports_new_and_missing(tmp_path):
    cases = make_case_dir(tmp_path)
    report = run_cases(load_cases(cases), FakeBrain())
    diff = diff_reports({"cases": [{"id": "old-case", "passed": True}]}, report)
    assert diff.new == ("fail-banned", "pass-basic")
    assert diff.missing == ("old-case",)
    assert diff.new_failures == ("fail-banned",)
    markdown = render_markdown(report, diff)
    assert "old-case" in markdown


def test_cli_malformed_input_has_no_traceback(tmp_path, capsys):
    bad = write_yaml(tmp_path / "bad.yaml", "not: a list\n")
    assert main(["--cases", str(bad)]) == 2
    captured = capsys.readouterr()
    assert "错误" in captured.err
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_fake_mode_makes_no_network_calls(tmp_path, monkeypatch):
    cases = make_case_dir(tmp_path)

    def fail_network(*args, **kwargs):
        raise AssertionError("fake 模式不应联网")

    async def fail_async_network(*args, **kwargs):
        raise AssertionError("fake 模式不应联网")

    monkeypatch.setattr(httpx.Client, "send", fail_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", fail_async_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)

    out = tmp_path / "report.json"
    assert main(["--cases", str(cases), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["mode"] == "fake"


def test_update_baseline_writes_report(tmp_path):
    cases = make_case_dir(tmp_path)
    target = tmp_path / "baseline.json"
    assert (
        main(
            [
                "--cases",
                str(cases / "pass.yaml"),
                "--update-baseline",
                "--baseline",
                str(target),
            ]
        )
        == 0
    )
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["total"] == 1
    assert data["passed"] == 1


def test_report_has_expected_shape():
    report = run_cases([make_case()], FakeBrain())
    assert isinstance(report, EvalReport)
    assert report.generated_at
    assert report.mode == "fake"
    assert report.model == "fake"
    assert report.duration_seconds >= 0




# ---- todo 14: 从 Store 只读录制 -------------------------------------------
import dafeiyu.eval.__main__ as eval_main
from dafeiyu.memory.store import Store
from dafeiyu.models import ChatType, Message


def record_settings(db_path: Path, **overrides):
    from types import SimpleNamespace

    base = {
        "db_path": str(db_path),
        "persona": SimpleNamespace(max_reply_chars=120),
        "behavior": SimpleNamespace(banned_phrases=("违禁词",)),
        "triggers": SimpleNamespace(
            batch_seconds=12.0,
            addressed_debounce_seconds=8.0,
            addressed_max_wait_seconds=30.0,
            ambient_min_interval_seconds=15.0,
        ),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def seed_history(db_path: Path, chat_id: str = "qq:123") -> None:
    store = Store(str(db_path))
    store.init_schema()
    samples = [
        (1000, "随便聊聊", False),
        (1002, "@大肥鱼 在吗", True),
        (1100, "这个怎么弄？", False),
        (1102, "在线等", False),
        (1200, "晚安", False),
    ]
    for index, (ts, content, at_bot) in enumerate(samples, start=1):
        store.add_message(
            Message(
                chat_type=ChatType.GROUP,
                chat_id=chat_id,
                user_id=f"qq:{2000 + index}",
                nickname=f"群友{index}",
                content=content,
                message_id=f"m{index}",
                at_bot=at_bot,
                timestamp=ts,
            )
        )
    store.add_session_summary(ChatType.GROUP, chat_id, "上一段摘要")


def test_record_from_store_round_trips_and_replays(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = tmp_path / "prod.db"
    seed_history(db)
    yaml_path = tmp_path / "recorded.yaml"
    monkeypatch.setattr(eval_main, "load_settings", lambda: record_settings(db))

    code = eval_main.main(
        [
            "--record",
            "--chat-type",
            "group",
            "--chat-id",
            "qq:123",
            "--limit",
            "20",
            "--db",
            str(db),
            "--out",
            str(yaml_path),
        ]
    )

    assert code == 0
    assert yaml_path.is_file()
    cases = load_cases(yaml_path)
    assert len(cases) >= 2
    assert all(case.context.summary == "上一段摘要" for case in cases)
    assert all(case.expect.speak is None for case in cases)
    assert all(case.expect.must_not_contain == ("违禁词",) for case in cases)
    assert all(case.expect.min_chars == 1 for case in cases)
    assert all(case.expect.max_chars == 120 for case in cases)
    assert "已录制" in capsys.readouterr().out

    report = tmp_path / "report.json"
    assert (
        eval_main.main(
            ["--cases", str(yaml_path), "--out", str(report)]
        )
        == 0
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["total"] == data["passed"]
    assert data["total"] == len(cases)


def test_record_empty_chat_prints_and_writes_nothing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = tmp_path / "prod.db"
    seed_history(db, chat_id="qq:999")
    yaml_path = tmp_path / "recorded.yaml"
    monkeypatch.setattr(eval_main, "load_settings", lambda: record_settings(db))

    code = eval_main.main(
        [
            "--record",
            "--chat-type",
            "group",
            "--chat-id",
            "qq:123",
            "--db",
            str(db),
            "--out",
            str(yaml_path),
        ]
    )

    assert code == 0
    assert "没有可录制消息" in capsys.readouterr().out
    assert not yaml_path.exists()


def test_record_missing_db_does_not_create_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = tmp_path / "missing.db"
    yaml_path = tmp_path / "recorded.yaml"
    monkeypatch.setattr(eval_main, "load_settings", lambda: record_settings(db))

    code = eval_main.main(
        [
            "--record",
            "--chat-type",
            "group",
            "--chat-id",
            "qq:123",
            "--db",
            str(db),
            "--out",
            str(yaml_path),
        ]
    )

    assert code == 0
    assert "没有可录制消息" in capsys.readouterr().out
    assert not db.exists()
    assert not yaml_path.exists()


def test_smoke_yaml_loads_and_passes_with_fake_brain(tmp_path: Path) -> None:
    cases = load_cases("eval/cases/smoke.yaml")
    assert len(cases) == 3

    report = tmp_path / "smoke.json"
    assert (
        eval_main.main(
            ["--cases", "eval/cases/smoke.yaml", "--out", str(report)]
        )
        == 0
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["mode"] == "fake"
    assert data["total"] == 3
    assert data["passed"] == 3


def test_record_rejects_live_and_missing_chat_args(tmp_path: Path) -> None:
    assert (
        eval_main.main(
            ["--record", "--live", "--chat-type", "group", "--chat-id", "qq:1"]
        )
        == 2
    )
    assert eval_main.main(["--record", "--chat-type", "group"]) == 2
    assert eval_main.main(["--record", "--chat-id", "qq:1"]) == 2


def test_record_does_not_call_model(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = tmp_path / "prod.db"
    seed_history(db)
    yaml_path = tmp_path / "recorded.yaml"
    monkeypatch.setattr(eval_main, "load_settings", lambda: record_settings(db))

    def fail_live(*_args, **_kwargs):
        raise AssertionError("record 不允许构造真实 brain")

    monkeypatch.setattr(eval_main, "build_live_brain", fail_live)

    assert (
        eval_main.main(
            [
                "--record",
                "--chat-type",
                "group",
                "--chat-id",
                "qq:123",
                "--db",
                str(db),
                "--out",
                str(yaml_path),
            ]
        )
        == 0
    )
    capsys.readouterr()


# ---- T10: 链接解析 / 消息泡 / 批次头部 -------------------------------------

#: 他人转发「顺路」的线上形态；宽度按 display_width 手算（与 test_mirror 同串）。
WIRE_SHUNLU = "╭────────────╮\n│ 阿鱼：顺路 │\n╰────────────╯"

#: T10 新增的回归 case：链接三态、批次头部三要素、气泡两态。
NEW_CASE_IDS = frozenset(
    {
        "link-http-title-body",
        "link-browser-rendered-title",
        "link-server-unreadable",
        "batch-header-three-elements",
        "bubble-wire-and-store",
        "bubble-wire-wraps",
    }
)


def evidence_checks(case: EvalCase, prompt: str = "") -> dict[str, EvalCheck]:
    reply = BrainReply(speak=True, reply="测试回复")
    return {check.name: check for check in run_checks(case, reply, prompt)}


def evidence_case(**expect_kwargs) -> EvalCase:
    return EvalCase(
        id="evidence",
        chat_type="group",
        chat_id="qq:1001",
        messages=(EvalMessage(user_id="2", nickname="阿鱼", content="顺路"),),
        expect=EvalExpect(**expect_kwargs),
    )


def test_loader_reads_links_and_evidence_expectations(tmp_path):
    file = write_yaml(
        tmp_path / "evidence.yaml",
        """
- id: link-and-bubble
  chat_type: group
  chat_id: qq:1001
  messages:
    - content: "看看 https://example.com/a"
  links:
    - url: https://example.com/a
      outcome: browser_ok
      status: 403
      title: 标题
      text: 正文
  expect:
    prompt_must_contain: ["【链接内容】"]
    prompt_must_not_contain: ["服务器读不到"]
    bubble:
      nickname: 阿鱼
      content: 顺路
      wire: |
        ╭────────────╮
        │ 阿鱼：顺路 │
        ╰────────────╯
""",
    )

    case = load_cases(file)[0]

    assert case.links == (
        EvalLink(
            url="https://example.com/a",
            outcome="browser_ok",
            status=403,
            title="标题",
            text="正文",
        ),
    )
    assert case.expect.prompt_must_contain == ("【链接内容】",)
    assert case.expect.prompt_must_not_contain == ("服务器读不到",)
    assert case.expect.bubble is not None
    # YAML 的 `|` 块结尾换行不算气泡内容。
    assert case.expect.bubble.wire == WIRE_SHUNLU


def test_loader_rejects_unknown_link_outcome(tmp_path):
    file = write_yaml(
        tmp_path / "bad-link.yaml",
        """
- id: bad-link
  chat_type: group
  messages: [{content: "https://example.com/a"}]
  links:
    - url: https://example.com/a
      outcome: network_magic
""",
    )
    with pytest.raises(EvalError, match="network_magic"):
        load_cases(file)


def test_check_evidence_prompt_requires_every_keyword():
    case = evidence_case(prompt_must_contain=("【本批】", "说话人"))
    passed = evidence_checks(case, "【本批】这一批共 2 条消息，说话人：阿猫")["evidence"]
    assert passed.passed

    failed = evidence_checks(case, "【本批】")["evidence"]
    assert not failed.passed
    assert "说话人" in failed.detail


def test_check_evidence_prompt_rejects_forbidden_keywords():
    case = evidence_case(prompt_must_not_contain=("服务器读不到",))
    assert evidence_checks(case, "【链接内容】标题与正文")["evidence"].passed

    failed = evidence_checks(case, "【链接内容】服务器读不到")["evidence"]
    assert not failed.passed
    assert "服务器读不到" in failed.detail


def test_check_evidence_without_prompt_is_not_vacuous():
    case = evidence_case(prompt_must_contain=("【链接内容】",))
    check = evidence_checks(case)["evidence"]
    assert not check.passed
    assert "未提供提示词" in check.detail


def test_check_evidence_bubble_passes_for_shipped_render():
    case = evidence_case(bubble=EvalBubble("阿鱼", "顺路", WIRE_SHUNLU))
    check = evidence_checks(case, "提示词")["evidence"]
    assert check.passed, check.detail


def test_check_evidence_bubble_detects_width_drift(monkeypatch):
    case = evidence_case(bubble=EvalBubble("阿鱼", "顺路", WIRE_SHUNLU))
    monkeypatch.setattr(eval_checks, "BUBBLE_WIDTH", 6)
    check = evidence_checks(case, "提示词")["evidence"]
    assert not check.passed
    assert "线上形态与期望气泡不一致" in check.detail


def test_check_evidence_bubble_detects_store_drift(monkeypatch):
    case = evidence_case(bubble=EvalBubble("阿鱼", "顺路", WIRE_SHUNLU))

    def render(self, nickname: str, content: str) -> RenderedLines:
        return RenderedLines([WIRE_SHUNLU], [f"{nickname}  {content}"])

    monkeypatch.setattr(eval_checks._BubbleProbe, "render", render)
    check = evidence_checks(case, "提示词")["evidence"]
    assert not check.passed
    assert "入库形态不再是旧版单行" in check.detail


def test_bubble_width_follows_mirror_default():
    assert eval_checks.BUBBLE_WIDTH == MirrorConfig().bubble_max_content_width
    assert eval_checks.BUBBLE_CORNERS == MirrorConfig().bubble_corner_chars


def test_runner_feeds_prompt_to_checks():
    ok = EvalCase(
        id="prompt-ok",
        chat_type="group",
        chat_id="qq:1",
        messages=(
            EvalMessage(nickname="阿猫", content="在吗"),
            EvalMessage(nickname="小北", content="在的"),
        ),
        expect=EvalExpect(prompt_must_contain=("【本批】", "说话人：阿猫、小北")),
    )
    bad = EvalCase(
        id="prompt-bad",
        chat_type="group",
        chat_id="qq:1",
        messages=(EvalMessage(nickname="阿猫", content="在吗"),),
        expect=EvalExpect(prompt_must_contain=("这句不在提示词里",)),
    )

    report = run_cases([ok, bad], FakeBrain())
    by_id = {result.id: result for result in report.cases}

    assert by_id["prompt-ok"].passed
    assert not by_id["prompt-bad"].passed
    detail = next(
        check.detail
        for check in by_id["prompt-bad"].checks
        if check.name == "evidence"
    )
    assert "提示词缺少" in detail


def test_shipped_cases_pass_and_cover_new_capabilities():
    cases = load_cases("eval/cases")
    assert NEW_CASE_IDS <= {case.id for case in cases}

    report = run_cases(cases, FakeBrain())

    assert report.failed == 0
    by_id = {result.id: result for result in report.cases}
    for case_id in NEW_CASE_IDS:
        evidence = [
            check for check in by_id[case_id].checks if check.name == "evidence"
        ]
        assert evidence and evidence[0].passed
