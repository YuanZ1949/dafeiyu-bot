"""``scripts/qa_wechat_smoke.py`` 的自检与配置改写测试。

真实冒烟需要桌面微信小号，仓库里不可执行；这里只用注入的探针验证
「明确原因码快速失败」与「ready 路径不会误报」。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "qa_wechat_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("qa_wechat_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_module()

CONFIG_TEXT = """\
brain:
  provider: opencode
gateway:
  provider: wechat
  wechat:
    enabled: true
    group_allowlist: ["冒烟群"]
    account_nickname: "小号"
    allow_test_inject: false
supervisor:
  dashboard:
    port: 18787
  control:
    host: 127.0.0.1
    port: 18788
groups:
  whitelist: []
"""


def _doctor(checks: list[dict[str, str]]) -> dict[str, object]:
    return {
        "checks": checks,
        "ok": not any(check["status"] == "FAIL" for check in checks),
    }


def _all_pass() -> list[dict[str, str]]:
    return [
        {"id": check_id, "status": "PASS", "detail": "ok"}
        for check_id in smoke.CHECK_CODES
    ]


def _deps(
    checks: list[dict[str, str]] | None = None,
    *,
    port_free: bool = True,
    doctor_called: list[bool] | None = None,
    text: str | None = CONFIG_TEXT,
):
    def read_text(path: Path) -> str:
        if text is None:
            raise FileNotFoundError(path)
        return text

    def run_doctor(config: Path, env_file: Path) -> dict[str, object]:
        if doctor_called is not None:
            doctor_called.append(True)
        return _doctor(checks if checks is not None else _all_pass())

    return smoke.Deps(
        read_text=read_text,
        run_doctor=run_doctor,
        port_free=lambda host, port: port_free,
    )


def _run(tmp_path: Path, deps, *extra: str) -> tuple[int, dict]:
    config = tmp_path / "config.yaml"
    if not config.exists():
        config.write_text(CONFIG_TEXT, encoding="utf-8")
    evidence = tmp_path / "raw"
    code = smoke.main(
        [
            "--self-check",
            "--config",
            str(config),
            "--evidence-dir",
            str(evidence),
            *extra,
        ],
        deps=deps,
    )
    report = json.loads((evidence / "00-self-check.json").read_text("utf-8"))
    return code, report


def test_self_check_ready_exits_zero(tmp_path: Path) -> None:
    code, report = _run(tmp_path, _deps())
    assert code == 0
    assert report["ready"] is True
    assert report["problems"] == []
    assert report["chat_id"] == "wxg:冒烟群"


def test_self_check_no_wechat_when_process_missing(tmp_path: Path) -> None:
    checks = _all_pass()
    checks[0]["status"] = "FAIL"
    checks[0]["detail"] = "未发现 WeChat.exe 进程"
    code, report = _run(tmp_path, _deps(checks))
    assert code == smoke.EXIT_NO_WECHAT
    assert report["problems"][0]["code"] == "no_wechat"
    assert "WeChat.exe" in report["problems"][0]["detail"]


def test_self_check_locked_when_desktop_not_interactive(tmp_path: Path) -> None:
    checks = _all_pass()
    for check in checks:
        if check["id"] == "wechat.desktop_interactive":
            check["status"] = "FAIL"
            check["detail"] = "会话已锁屏"
    code, report = _run(tmp_path, _deps(checks))
    assert code == smoke.EXIT_LOCKED
    assert report["problems"][0]["code"] == "locked"


def test_self_check_port_busy(tmp_path: Path) -> None:
    code, report = _run(tmp_path, _deps(port_free=False))
    assert code == smoke.EXIT_PORT_BUSY
    assert report["problems"][0]["code"] == "port_busy"


def test_self_check_driver_missing(tmp_path: Path) -> None:
    checks = _all_pass()
    for check in checks:
        if check["id"] == "wechat.driver_importable":
            check["status"] = "FAIL"
    code, report = _run(tmp_path, _deps(checks))
    assert code == smoke.EXIT_DRIVER_MISSING
    assert report["problems"][0]["code"] == "driver_missing"


def test_self_check_config_missing_skips_doctor(tmp_path: Path) -> None:
    called: list[bool] = []
    code, report = _run(
        tmp_path, _deps(text=None, doctor_called=called)
    )
    assert code == smoke.EXIT_CONFIG_MISSING
    assert report["problems"][0]["code"] == "config_missing"
    assert called == []


def test_self_check_disabled_wechat_skips_doctor(tmp_path: Path) -> None:
    called: list[bool] = []
    text = CONFIG_TEXT.replace("enabled: true", "enabled: false")
    code, report = _run(
        tmp_path, _deps(text=text, doctor_called=called)
    )
    assert code == smoke.EXIT_CONFIG_MISSING
    assert report["problems"][0]["code"] == "config_missing"
    assert called == []


def test_self_check_whitelist_mismatch_is_config_missing(tmp_path: Path) -> None:
    text = CONFIG_TEXT.replace(
        "  whitelist: []", '  whitelist: ["wxg:别的群"]'
    )
    code, report = _run(tmp_path, _deps(text=text))
    assert code == smoke.EXIT_CONFIG_MISSING
    assert any("whitelist" in item["detail"] for item in report["problems"])


def test_pick_exit_uses_fixed_priority() -> None:
    problems = [
        smoke.Problem("port_busy", "port"),
        smoke.Problem("locked", "desktop"),
    ]
    assert smoke._pick_exit(problems) == smoke.EXIT_LOCKED


def test_set_allow_test_inject_only_touches_its_line() -> None:
    text = (
        "gateway:\r\n"
        "  wechat:\r\n"
        "    allow_test_inject: false  # 冒烟临时打开\r\n"
        "    enabled: true\r\n"
    )
    patched = smoke.set_allow_test_inject(text, True)
    assert "allow_test_inject: true  # 冒烟临时打开\r\n" in patched
    assert patched.replace("true", "false", 1) == text
    assert patched.count("\r\n") == text.count("\r\n")

    restored = smoke._ensure_disabled(patched)
    assert "allow_test_inject: false" in restored
    # 原本就是 true 时也强制关掉，绝不把 allow_test_inject=true 留在配置里。
    forced = smoke._ensure_disabled(
        "gateway:\n  wechat:\n    allow_test_inject: true\n"
    )
    assert "allow_test_inject: false" in forced


def test_resolve_chat_id_accepts_alias_or_full_id() -> None:
    assert smoke.resolve_chat_id(["测试群"], None) == "wxg:测试群"
    assert smoke.resolve_chat_id(["测试群"], "别的群") == "wxg:别的群"
    assert smoke.resolve_chat_id([], "wxg:已带前缀") == "wxg:已带前缀"
    assert smoke.resolve_chat_id([], None) == ""
