"""F3 真实冒烟：一条命令跑完「信封 -> 核心 -> 出站 UIA 发送」并逐步落盘 JSON。

用法（必须用仓库虚环境的 python 跑）::

    .venv/Scripts/python scripts/qa_wechat_smoke.py --self-check
    .venv/Scripts/python scripts/qa_wechat_smoke.py --alias 测试群

前置自检（``--self-check``）只读配置、跑 ``doctor --json``、检查控制端口，
不启动 supervisor，也不会改任何文件。环境没就绪时它用**明确的原因码**退出，
而不是等满超时。

真实运行分两段：

* F3a（全自动）① doctor 8 项 PASS；② supervisor 起来后轮询
  ``/api/health/detail`` 直到 ``channels.wechat.connected == true``；③ 两条
  **文本完全相同**的 ``/inject`` 信封断言入库 2 行且 message_id 不同、新增
  ``is_bot=1`` 行 >= 1、``last_send_at`` 前移；④ 急停后 ``/inject`` 必须 409、
  消息不增、``last_send_at`` 60s 不变；⑤ 删急停文件后 ``resume``，再注入必须
  恢复回复。
* F3b（需要人做**一个发送动作**）：脚本打印提示，等人用第二个微信号在测试群
  发一条 ``@<account_nickname>``；脚本轮询 timeline，断言
  ``poll_seconds + 120s`` 内出现她的回复。

证明力边界（必须保留）：F3a 覆盖的是「信封 -> 核心 -> 出站 UIA 发送」；它
**不**覆盖入站 UIA 观测（``/inject`` 绕过轮询），那只有 F3b 能证明。

安全边界：不在锁屏 / RDP 会话里跑；不在日常号上跑；``allow_test_inject`` 只
在脚本运行期间为 ``true``，退出时恢复，绝不留着。

退出码::

    0 通过        1 断言失败        2 no_wechat     3 locked
    4 port_busy   5 driver_missing  6 config_missing
    7 token_missing                 8 timeout
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

EXIT_OK = 0
EXIT_ASSERT = 1
EXIT_NO_WECHAT = 2
EXIT_LOCKED = 3
EXIT_PORT_BUSY = 4
EXIT_DRIVER_MISSING = 5
EXIT_CONFIG_MISSING = 6
EXIT_TOKEN_MISSING = 7
EXIT_TIMEOUT = 8

CODE_EXIT = {
    "ok": EXIT_OK,
    "assert_failed": EXIT_ASSERT,
    "no_wechat": EXIT_NO_WECHAT,
    "locked": EXIT_LOCKED,
    "port_busy": EXIT_PORT_BUSY,
    "driver_missing": EXIT_DRIVER_MISSING,
    "config_missing": EXIT_CONFIG_MISSING,
    "token_missing": EXIT_TOKEN_MISSING,
    "timeout": EXIT_TIMEOUT,
}

# 同一份检查项可能命中多条原因；固定优先级让自检输出可复现。
CODE_PRIORITY = (
    "config_missing",
    "no_wechat",
    "locked",
    "driver_missing",
    "port_busy",
    "token_missing",
    "timeout",
    "assert_failed",
)

CHECK_CODES = {
    "wechat.process_running": "no_wechat",
    "wechat.version_ok": "no_wechat",
    "wechat.language_ok": "no_wechat",
    "wechat.integrity_match": "no_wechat",
    "wechat.desktop_interactive": "locked",
    "wechat.driver_importable": "driver_missing",
    "wechat.bridge_port_free": "port_busy",
    "wechat.config_ready": "config_missing",
}

WECHAT_PROVIDERS = {"wechat", "both"}
DEFAULT_EVIDENCE_DIR = Path(
    os.environ.get("F3_EVIDENCE_DIR")
    or (REPO_ROOT / ".omo" / "evidence" / "F3-wechat-integration" / "raw")
)
#: 桥的本地身份表（cwd 相对路径，见 `identity.default_peers_path`）。
PEERS_PATH = REPO_ROOT / "data" / "wechat-bridge" / "peers.json"

_ALLOW_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)allow_test_inject[ \t]*:[ \t]*"
    r"(?P<value>true|false|True|False|yes|no|on|off)"
    r"(?P<suffix>[ \t]*(?:#.*)?\r?)$"
)


class SmokeError(Exception):
    """带明确原因码的失败：退出码与原因一一对应。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.exit_code = CODE_EXIT.get(code, EXIT_ASSERT)


@dataclass(frozen=True)
class Problem:
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass
class Deps:
    """自检依赖；测试用假探针替换，避免碰真实环境。"""

    read_text: Callable[[Path], str]
    run_doctor: Callable[[Path, Path], dict[str, Any]]
    port_free: Callable[[str, int], bool]
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class Response:
    status: int
    body: Any
    raw: str


# --------------------------------------------------------------------- 基础


def _read_text(path: Path) -> str:
    # 用 bytes -> decode 而不是 read_text：Windows 上 read_text 会把 CRLF 折成
    # LF，写回时改掉整个文件的换行。自检与恢复都必须逐字节可逆。
    return path.read_bytes().decode("utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _pythonpath() -> str:
    src = str(REPO_ROOT / "src")
    existing = os.environ.get("PYTHONPATH", "")
    return src + (os.pathsep + existing if existing else "")


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = _pythonpath()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _last_json_line(text: str) -> dict[str, Any] | None:
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _run_doctor(config: Path, env_file: Path) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "dafeiyu.cli",
        "doctor",
        "--json",
        "--config",
        str(config),
        "--env-file",
        str(env_file),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=_child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"checks": [], "ok": False, "error": f"doctor 无法执行：{exc}"}
    payload = _last_json_line(proc.stdout or "")
    if payload is None:
        return {
            "checks": [],
            "ok": False,
            "error": f"doctor 没有输出 JSON（exit={proc.returncode}）",
        }
    payload.setdefault("exit_code", proc.returncode)
    return payload


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _default_deps() -> Deps:
    return Deps(
        read_text=_read_text,
        run_doctor=_run_doctor,
        port_free=_port_free,
    )


# ------------------------------------------------------------------- 配置


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_chat_id(allowlist: list[Any], alias_arg: str | None) -> str:
    if alias_arg:
        alias = alias_arg.strip()
    elif allowlist:
        alias = str(allowlist[0]).strip()
    else:
        alias = ""
    if not alias:
        return ""
    return alias if alias.startswith("wxg:") else f"wxg:{alias}"


def peers_chat_id(raw: Any, allowlist: list[Any]) -> str:
    """从 `peers.json` 快照解析 allowlist 显示名对应的 `wxg:<alias>`。

    桥只在首次轮询时铸造随机别名，配置里只能写群显示名；因此冒烟脚本必须
    在桥启动后从身份表取真实 chat_id。同一群有多个存活别名时 fail-closed。
    """
    names = {str(item).strip() for item in allowlist if str(item).strip()}
    if not names:
        return ""
    entries = raw.get("peers") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return ""
    matches: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, dict) or item.get("retired_at") is not None:
            continue
        if item.get("kind") != "group":
            continue
        alias = str(item.get("alias") or "").strip()
        if not alias or alias == "self":
            continue
        display = str(item.get("display_name") or "").strip()
        former = {
            str(name).strip()
            for name in (item.get("former_names") or [])
            if str(name).strip()
        }
        hit = ({display} & names) or (former & names)
        if hit:
            matches[alias] = display or sorted(hit)[0]
    if len(matches) > 1:
        detail = ", ".join(
            f"wxg:{alias}({display})" for alias, display in sorted(matches.items())
        )
        raise SmokeError(
            "assert_failed",
            f"peers.json 里目标群有多个存活别名，拒绝猜：{detail}",
        )
    if not matches:
        return ""
    alias, _display = next(iter(matches.items()))
    return f"wxg:{alias}"


def set_allow_test_inject(text: str, enabled: bool) -> str:
    """只改 ``allow_test_inject`` 这一行，其余字节（含注释、换行）原样保留。"""
    desired = "true" if enabled else "false"
    match = _ALLOW_RE.search(text)
    if match is None:
        raise SmokeError(
            "config_missing",
            "config.yaml 缺少 gateway.wechat.allow_test_inject 一行",
        )
    if match.group("value").lower() == desired:
        return text
    replacement = (
        f"{match.group('indent')}allow_test_inject: {desired}"
        f"{match.group('suffix')}"
    )
    return text[: match.start()] + replacement + text[match.end() :]


def _ensure_disabled(text: str) -> str:
    """退出时保证 allow_test_inject 为 false；原本就是 true 也要按计划关掉。"""
    try:
        return set_allow_test_inject(text, False)
    except SmokeError:
        return text


def _kill_switch_path(wechat: dict[str, Any]) -> Path:
    raw = str(wechat.get("kill_switch_file") or "data/wechat.stop").strip()
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _config_info(
    raw_config: dict[str, Any],
    alias_arg: str | None,
) -> dict[str, Any]:
    gateway = _mapping(raw_config.get("gateway"))
    wechat = _mapping(gateway.get("wechat"))
    supervisor = _mapping(raw_config.get("supervisor"))
    control = _mapping(supervisor.get("control"))
    allowlist = _as_list(wechat.get("group_allowlist"))
    chat_id = resolve_chat_id(allowlist, alias_arg)
    return {
        "provider": str(gateway.get("provider") or ""),
        "wechat": wechat,
        "allowlist": allowlist,
        "chat_id": chat_id,
        "nickname": str(wechat.get("account_nickname") or "").strip(),
        "control_host": str(control.get("host") or "127.0.0.1").strip(),
        "control_port": _as_int(control.get("port"), 8788),
        "poll_seconds": float(wechat.get("poll_seconds") or 30),
        "kill_switch": _kill_switch_path(wechat),
        "whitelist": [str(item) for item in _as_list(_mapping(raw_config.get("groups")).get("whitelist"))],
        "allow_test_inject": _as_bool(wechat.get("allow_test_inject")),
    }


def analyze_config(
    raw_config: dict[str, Any],
    info: dict[str, Any],
) -> list[Problem]:
    problems: list[Problem] = []
    if info["provider"] not in WECHAT_PROVIDERS:
        problems.append(
            Problem(
                "config_missing",
                "gateway.provider 必须是 wechat 或 both，当前 "
                f"{info['provider'] or '(空)'}",
            )
        )
    if not _as_bool(info["wechat"].get("enabled")):
        problems.append(
            Problem("config_missing", "gateway.wechat.enabled 必须为 true")
        )
    if not info["allowlist"]:
        problems.append(
            Problem("config_missing", "gateway.wechat.group_allowlist 不能为空")
        )
    if not info["nickname"]:
        problems.append(
            Problem("config_missing", "gateway.wechat.account_nickname 不能为空")
        )
    if "allow_test_inject" not in info["wechat"]:
        problems.append(
            Problem(
                "config_missing",
                "gateway.wechat.allow_test_inject 缺失（脚本要临时打开它）",
            )
        )
    if info["allowlist"] and not info["chat_id"]:
        problems.append(
            Problem("config_missing", "无法从 group_allowlist 解析测试群 chat_id")
        )
    if info["whitelist"] and info["chat_id"] not in info["whitelist"]:
        problems.append(
            Problem(
                "config_missing",
                f"groups.whitelist 非空但不含 {info['chat_id']}，回复会被白名单拦掉",
            )
        )
    return problems


def analyze_checks(checks: list[Any]) -> list[Problem]:
    problems: list[Problem] = []
    for check in checks:
        item = _mapping(check)
        if str(item.get("status") or "").upper() != "FAIL":
            continue
        check_id = str(item.get("id") or "")
        code = CHECK_CODES.get(check_id, "no_wechat")
        problems.append(
            Problem(code, f"{check_id}: {item.get('detail') or '无详情'}")
        )
    return problems


def _pick_exit(problems: list[Problem]) -> int:
    if not problems:
        return EXIT_OK
    for code in CODE_PRIORITY:
        if any(problem.code == code for problem in problems):
            return CODE_EXIT[code]
    return EXIT_ASSERT


def _first_problem(problems: list[Problem]) -> Problem:
    for code in CODE_PRIORITY:
        for problem in problems:
            if problem.code == code:
                return problem
    return problems[0]


# ------------------------------------------------------------------- 自检


def self_check(args: argparse.Namespace, deps: Deps) -> int:
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "self-check",
        "config": str(args.config),
        "env_file": str(args.env_file),
        "problems": [],
        "checks": [],
        "ready": False,
    }
    problems: list[Problem] = []
    info: dict[str, Any] = {}
    try:
        text = deps.read_text(args.config)
    except OSError as exc:
        problems.append(Problem("config_missing", f"读不到配置：{exc}"))
        text = ""
    if text:
        try:
            raw_config = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            problems.append(Problem("config_missing", f"配置不是合法 YAML：{exc}"))
        else:
            if not isinstance(raw_config, dict):
                problems.append(Problem("config_missing", "配置顶层必须是映射"))
            else:
                info = _config_info(raw_config, args.alias)
                report.update(
                    {
                        "chat_id": info["chat_id"],
                        "nickname": info["nickname"],
                        "control_base": _control_base(info),
                    }
                )
                problems.extend(analyze_config(raw_config, info))

    if not problems:
        doctor = deps.run_doctor(args.config, args.env_file)
        checks = _as_list(doctor.get("checks"))
        report["checks"] = checks
        report["doctor_ok"] = bool(doctor.get("ok"))
        if not checks:
            problems.append(
                Problem(
                    "no_wechat",
                    str(doctor.get("error") or "doctor 没有返回任何微信检查项"),
                )
            )
        problems.extend(analyze_checks(checks))
        if not deps.port_free(info["control_host"], info["control_port"]):
            problems.append(
                Problem(
                    "port_busy",
                    f"控制端口 {info['control_host']}:{info['control_port']} 已被占用",
                )
            )

    for problem in problems:
        detail = problem.detail
        print(f"self-check: {problem.code}: {detail}", file=sys.stderr)
    report["problems"] = [problem.as_dict() for problem in problems]
    report["ready"] = not problems
    report["exit_code"] = _pick_exit(problems)
    try:
        _write_json(args.evidence_dir / "00-self-check.json", report)
    except OSError as exc:
        print(f"self-check: 证据写入失败：{exc}", file=sys.stderr)
    print(
        json.dumps(
            {
                "ready": report["ready"],
                "problems": report["problems"],
                "exit_code": report["exit_code"],
                "chat_id": report.get("chat_id", ""),
            },
            ensure_ascii=False,
        )
    )
    return int(report["exit_code"])


# ------------------------------------------------------------------- HTTP


class ControlClient:
    """bot 控制口的最小同步客户端；每次调用都带 X-Control-Token。"""

    def __init__(self, base: str, token: str, timeout: float = 15.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, payload: Any = None) -> Response:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method=method
        )
        request.add_header("X-Control-Token", self.token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return Response(resp.status, _decode_body(raw), raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return Response(exc.code, _decode_body(raw), raw)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SmokeError("timeout", f"{method} {path} 失败：{exc}") from exc

    def get(self, path: str) -> Response:
        return self._call("GET", path)

    def post(self, path: str, payload: Any = None) -> Response:
        return self._call("POST", path, payload)


def _decode_body(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _control_base(info: dict[str, Any]) -> str:
    return f"http://{info['control_host']}:{info['control_port']}"


def _timeline_path(chat_id: str, limit: int = 200) -> str:
    return (
        f"/api/sessions/group/{quote(chat_id, safe='')}/timeline"
        f"?limit={limit}"
    )


def _wechat_channel(client: ControlClient) -> dict[str, Any]:
    resp = client.get("/api/health/detail")
    if resp.status == 401:
        raise SmokeError("token_missing", "控制口返回 401，token 不对")
    if resp.status != 200:
        raise SmokeError(
            "no_wechat", f"/api/health/detail 返回 HTTP {resp.status}"
        )
    channels = _mapping(_mapping(resp.body).get("channels"))
    return _mapping(channels.get("wechat"))


def _fetch_timeline(client: ControlClient, chat_id: str) -> dict[str, Any]:
    resp = client.get(_timeline_path(chat_id))
    if resp.status != 200:
        raise SmokeError(
            "assert_failed",
            f"timeline 返回 HTTP {resp.status}：{resp.body!r}",
        )
    body = _mapping(resp.body)
    if not isinstance(body.get("messages"), list):
        raise SmokeError("assert_failed", "timeline 缺少 messages 数组")
    return body


def _messages(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    return [_mapping(item) for item in timeline.get("messages") or []]


def _total(timeline: dict[str, Any]) -> int:
    chat = _mapping(timeline.get("chat"))
    return _as_int(chat.get("message_total"), len(_messages(timeline)))


def _rows_with_text(timeline: dict[str, Any], text: str) -> list[dict[str, Any]]:
    return [
        row
        for row in _messages(timeline)
        if row.get("content") == text and not _as_bool(row.get("is_bot"))
    ]


def _bot_ids(timeline: dict[str, Any]) -> set[str]:
    return {
        str(row.get("message_id"))
        for row in _messages(timeline)
        if _as_bool(row.get("is_bot"))
    }


def _new_bot_rows(
    before: set[str], timeline: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        row
        for row in _messages(timeline)
        if _as_bool(row.get("is_bot"))
        and str(row.get("message_id")) not in before
    ]


def _new_inbound_rows(
    before: set[str], timeline: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        row
        for row in _messages(timeline)
        if not _as_bool(row.get("is_bot"))
        and str(row.get("message_id")) not in before
    ]


def _all_ids(timeline: dict[str, Any]) -> set[str]:
    return {str(row.get("message_id")) for row in _messages(timeline)}


def _advanced(current: Any, baseline: Any) -> bool:
    if current is None:
        return False
    if baseline is None:
        return True
    try:
        return datetime.fromisoformat(str(current)) > datetime.fromisoformat(
            str(baseline)
        )
    except ValueError:
        return str(current) != str(baseline)


def _wait_until(
    describe: str,
    probe: Callable[[], Any],
    timeout: float,
    deps: Deps,
    interval: float = 2.0,
) -> Any:
    deadline = deps.monotonic() + float(timeout)
    last_error: Exception | None = None
    while deps.monotonic() < deadline:
        try:
            value = probe()
        except SmokeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 轮询期间的单次失败不算终局
            last_error = exc
            value = None
        if value:
            return value
        deps.sleep(interval)
    suffix = f"（最后一次错误：{last_error}）" if last_error else ""
    raise SmokeError("timeout", f"等待 {describe} 超时（{timeout:.0f}s）{suffix}")


# --------------------------------------------------------------- 真实执行


def _random_text() -> str:
    return f"F3冒烟-{uuid.uuid4().hex[:12]}"


def _envelope(chat_id: str, text: str, nickname: str) -> dict[str, Any]:
    return {
        "v": 1,
        "type": "message",
        "chat_id": chat_id,
        "chat_type": "group",
        "user_id": "wxu:f3-smoke",
        "nickname": nickname,
        "text": text,
        "message_id": f"f3-{uuid.uuid4().hex}",
        "is_bot": False,
        "at_bot": True,
        "ts": int(time.time()),
    }


def _expect(resp: Response, status: int, step: str) -> Response:
    if resp.status == status:
        return resp
    if resp.status == 401:
        raise SmokeError("token_missing", f"{step}: HTTP 401")
    if resp.status == 409:
        raise SmokeError("no_wechat", f"{step}: HTTP 409 {resp.body!r}")
    if resp.status == 404:
        raise SmokeError(
            "config_missing",
            f"{step}: HTTP 404（allow_test_inject 未生效或微信适配器未接线）",
        )
    raise SmokeError(
        "assert_failed", f"{step}: 期望 HTTP {status}，实际 {resp.status} {resp.body!r}"
    )


def _inject(
    client: ControlClient, chat_id: str, text: str, nickname: str
) -> Response:
    return client.post(
        "/api/channels/wechat/inject", _envelope(chat_id, text, nickname)
    )


def _wait_connected(
    client: ControlClient, deps: Deps, timeout: float, want: bool = True
) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        channel = _wechat_channel(client)
        if _as_bool(channel.get("connected")) is want:
            return channel
        return None

    return _wait_until(
        f"wechat.connected == {want}", probe, timeout, deps=deps
    )


def _wait_peer_chat_id(
    info: dict[str, Any], deps: Deps, timeout: float
) -> str:
    """等桥首轮轮询把 allowlist 群写进 peers.json，再取真实别名。"""

    def probe() -> str | None:
        try:
            raw = json.loads(deps.read_text(PEERS_PATH))
        except (OSError, ValueError):
            return None
        return peers_chat_id(raw, info["allowlist"]) or None

    chat_id = _wait_until(
        "桥为 allowlist 群铸造会话别名", probe, timeout, deps
    )
    info["chat_id"] = chat_id
    return chat_id


def _wait_settled(
    client: ControlClient, chat_id: str, deps: Deps, quiet_seconds: float
) -> dict[str, Any]:
    """行数安静 ``quiet_seconds`` 后才把此刻当作 ④ 的基线。

    批次调度器最长会等 ``addressed_max_wait_seconds``（默认 30s）才回；只比较
    相邻两次采样会把「还没回完」误当稳定，导致 ④ 假失败。
    """
    quiet = max(30.0, float(quiet_seconds))
    deadline = deps.monotonic() + quiet * 4
    last_total: int | None = None
    last_change = deps.monotonic()
    timeline = _fetch_timeline(client, chat_id)
    while deps.monotonic() < deadline:
        timeline = _fetch_timeline(client, chat_id)
        total = _total(timeline)
        now = deps.monotonic()
        if total != last_total:
            last_total = total
            last_change = now
        elif now - last_change >= quiet:
            return timeline
        deps.sleep(5.0)
    raise SmokeError("timeout", "timeline 行数一直没有稳定下来")


def _wait_reply(
    client: ControlClient,
    chat_id: str,
    before_bot: set[str],
    deps: Deps,
    timeout: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    def probe() -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        timeline = _fetch_timeline(client, chat_id)
        rows = _new_bot_rows(before_bot, timeline)
        return (timeline, rows) if rows else None

    return _wait_until("新增 is_bot=1 回复", probe, timeout, deps=deps)


def _start_supervisor(log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        [sys.executable, "-m", "dafeiyu.supervisor"],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )


def _stop_supervisor() -> None:
    # 复用仓库自己的停服器：先面板 /api/shutdown，失败再 taskkill /T /F。
    subprocess.run(
        [sys.executable, "-m", "dafeiyu.supervisor.stop"],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _token_from_file() -> str:
    path = REPO_ROOT / "data" / "supervisor.token"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _wait_token(deps: Deps, timeout: float = 30.0) -> str:
    def probe() -> str | None:
        token = _token_from_file()
        return token or None

    return _wait_until(
        "data/supervisor.token 生成", probe, timeout, deps=deps, interval=0.5
    )


def _wait_http_ready(
    base: str,
    proc: subprocess.Popen[bytes],
    deps: Deps,
    timeout: float = 120.0,
) -> None:
    def probe() -> bool:
        if proc.poll() is not None:
            raise SmokeError(
                "no_wechat",
                f"supervisor 提前退出（exit={proc.returncode}），见 supervisor.log",
            )
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=3) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    _wait_until(
        "bot 控制口 /api/health", probe, timeout, deps=deps, interval=1.0
    )


def _step(name: str, evidence_dir: Path, payload: dict[str, Any]) -> None:
    payload = {"step": name, "at": datetime.now().isoformat(timespec="seconds"), **payload}
    _write_json(evidence_dir / f"{name}.json", payload)


def _run_f3a(
    client: ControlClient,
    info: dict[str, Any],
    deps: Deps,
    evidence_dir: Path,
) -> None:
    poll = info["poll_seconds"]
    chat_id = info["chat_id"]
    nickname = info["nickname"]

    channel = _wait_connected(client, deps, 120.0)
    _step("02-health-connected", evidence_dir, {"channel": channel})

    baseline = _fetch_timeline(client, chat_id)
    baseline_send = channel.get("last_send_at")
    text = _random_text()
    first = _inject(client, chat_id, text, nickname)
    second = _inject(client, chat_id, text, nickname)
    _expect(first, 200, "③ 第一条 inject")
    _expect(second, 200, "③ 第二条 inject")

    def probe_rows() -> list[dict[str, Any]] | None:
        timeline = _fetch_timeline(client, chat_id)
        rows = _rows_with_text(timeline, text)
        return rows if len(rows) >= 2 else None

    rows = _wait_until("两条相同文本入库", probe_rows, poll + 60.0, deps)
    ids = [str(row.get("message_id")) for row in rows]
    if len(ids) != len(set(ids)):
        raise SmokeError("assert_failed", f"③ 相同文本入库 message_id 重复：{ids}")

    baseline_bot = _bot_ids(baseline)
    reply_timeline, bot_rows = _wait_reply(
        client, chat_id, baseline_bot, deps, poll + 120.0
    )
    advanced = _wait_until(
        "channels.wechat.last_send_at 前移",
        lambda: (
            _wechat_channel(client).get("last_send_at")
            if _advanced(_wechat_channel(client).get("last_send_at"), baseline_send)
            else None
        ),
        poll + 60.0,
        deps,
    )
    _step(
        "03-inject",
        evidence_dir,
        {
            "text": text,
            "inject": [first.body, second.body],
            "rows": [
                {
                    "message_id": row.get("message_id"),
                    "content": row.get("content"),
                    "is_bot": row.get("is_bot"),
                }
                for row in rows
            ],
            "new_bot_rows": [
                {"message_id": row.get("message_id"), "content": row.get("content")}
                for row in bot_rows
            ],
            "last_send_at_before": baseline_send,
            "last_send_at_after": advanced,
            "reply_total": _total(reply_timeline),
        },
    )

    settled = _wait_settled(client, chat_id, deps, poll)
    before_stop_total = _total(settled)
    before_stop_send = _wechat_channel(client).get("last_send_at")
    stop_resp = client.post("/api/channels/wechat/stop")
    _expect(stop_resp, 200, "④ stop")
    _wait_connected(client, deps, max(30.0, poll * 3), want=False)

    blocked_text = _random_text()
    blocked = _inject(client, chat_id, blocked_text, nickname)
    _expect(blocked, 409, "④ 急停后 inject 必须 409")

    observe = float(info.get("stop_observe_seconds", 60.0))
    deadline = deps.monotonic() + observe
    samples: list[dict[str, Any]] = []
    while deps.monotonic() < deadline:
        timeline = _fetch_timeline(client, chat_id)
        channel = _wechat_channel(client)
        samples.append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "message_total": _total(timeline),
                "last_send_at": channel.get("last_send_at"),
                "connected": _as_bool(channel.get("connected")),
            }
        )
        deps.sleep(5.0)
    if any(sample["message_total"] != before_stop_total for sample in samples):
        raise SmokeError("assert_failed", "④ 急停期间 messages 行数发生变化")
    if any(_as_bool(sample["connected"]) for sample in samples):
        raise SmokeError("assert_failed", "④ 急停期间 channels.wechat.connected 又变回 true")
    if any(_advanced(sample["last_send_at"], before_stop_send) for sample in samples):
        raise SmokeError("assert_failed", "④ 急停期间 last_send_at 前移了")
    _step(
        "04-stop",
        evidence_dir,
        {
            "stop": stop_resp.body,
            "blocked_inject": {"status": blocked.status, "body": blocked.body},
            "baseline_total": before_stop_total,
            "baseline_last_send_at": before_stop_send,
            "samples": samples,
        },
    )

    info["kill_switch"].unlink(missing_ok=True)
    resume_resp = client.post("/api/channels/wechat/resume")
    _expect(resume_resp, 200, "⑤ resume")
    _wait_connected(client, deps, max(60.0, poll * 3))

    baseline5 = _fetch_timeline(client, chat_id)
    baseline5_bot = _bot_ids(baseline5)
    resumed_text = _random_text()
    resumed = _inject(client, chat_id, resumed_text, nickname)
    _expect(resumed, 200, "⑤ resume 后 inject")
    reply5_timeline, reply5_rows = _wait_reply(
        client, chat_id, baseline5_bot, deps, poll + 120.0
    )
    resumed_rows = _rows_with_text(reply5_timeline, resumed_text)
    if len(resumed_rows) != 1:
        raise SmokeError(
            "assert_failed",
            f"⑤ 恢复后注入应入库 1 行，实际 {len(resumed_rows)} 行",
        )
    _step(
        "05-resume",
        evidence_dir,
        {
            "resume": resume_resp.body,
            "text": resumed_text,
            "rows": [
                {"message_id": row.get("message_id"), "content": row.get("content")}
                for row in resumed_rows
            ],
            "new_bot_rows": [
                {"message_id": row.get("message_id"), "content": row.get("content")}
                for row in reply5_rows
            ],
        },
    )


def _run_f3b(
    client: ControlClient,
    info: dict[str, Any],
    deps: Deps,
    evidence_dir: Path,
    human_timeout: float,
) -> None:
    chat_id = info["chat_id"]
    nickname = info["nickname"]
    poll = info["poll_seconds"]
    baseline = _fetch_timeline(client, chat_id)
    baseline_ids = _all_ids(baseline)
    baseline_bot = _bot_ids(baseline)

    prompt = (
        f"请用第二个微信账号在测试群 @{nickname} 发一条文本；群 chat_id = {chat_id}。"
    )
    _step(
        "06-f3b-ready",
        evidence_dir,
        {
            "chat_id": chat_id,
            "nickname": nickname,
            "prompt": prompt,
            "timeout_seconds": human_timeout,
        },
    )
    print(
        "\n=== F3b：需要你做一个发送动作 ===\n"
        f"请用**第二个**微信账号（手机即可）在测试群 @{nickname} 发一条文本，\n"
        f"群 chat_id = {chat_id}。脚本最多等 {human_timeout:.0f}s。\n"
        "这一步只能证明 UIA 真的看见了气泡；/inject 不算。",
        flush=True,
    )

    inbound = _wait_until(
        f"{human_timeout:.0f}s 内出现新的入站消息",
        lambda: (
            _new_inbound_rows(baseline_ids, _fetch_timeline(client, chat_id))
            or None
        ),
        human_timeout,
        deps,
        interval=3.0,
    )
    inbound_row = inbound[-1]
    content = str(inbound_row.get("content") or "")
    if nickname not in content:
        raise SmokeError(
            "assert_failed",
            f"F3b 新入站消息里没有 @{nickname}：{content!r}",
        )
    reply_timeline, reply_rows = _wait_reply(
        client, chat_id, baseline_bot, deps, poll + 120.0
    )
    _step(
        "06-f3b",
        evidence_dir,
        {
            "inbound": {
                "message_id": inbound_row.get("message_id"),
                "content": content,
                "is_bot": inbound_row.get("is_bot"),
            },
            "reply": [
                {"message_id": row.get("message_id"), "content": row.get("content")}
                for row in reply_rows
            ],
            "reply_total": _total(reply_timeline),
            "timeout_seconds": poll + 120.0,
        },
    )


def run_smoke(args: argparse.Namespace, deps: Deps) -> int:
    evidence_dir: Path = args.evidence_dir
    text = ""
    try:
        if not args.config.exists():
            raise SmokeError("config_missing", f"找不到配置：{args.config}")
        text = deps.read_text(args.config)
        try:
            raw_config = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SmokeError("config_missing", f"配置不是合法 YAML：{exc}") from exc
        if not isinstance(raw_config, dict):
            raise SmokeError("config_missing", "配置顶层必须是映射")
        info = _config_info(raw_config, args.alias)
        problems = analyze_config(raw_config, info)
        doctor = deps.run_doctor(args.config, args.env_file)
        checks = _as_list(doctor.get("checks"))
        if not checks:
            problems.append(
                Problem(
                    "no_wechat",
                    str(doctor.get("error") or "doctor 没有返回任何微信检查项"),
                )
            )
        problems.extend(analyze_checks(checks))
        if not deps.port_free(info["control_host"], info["control_port"]):
            problems.append(
                Problem(
                    "port_busy",
                    f"控制端口 {info['control_host']}:{info['control_port']} 已被占用",
                )
            )
        _step("00-self-check", evidence_dir, {"doctor": doctor, "problems": [p.as_dict() for p in problems], "chat_id": info["chat_id"]})
        if problems:
            chosen = _first_problem(problems)
            raise SmokeError(chosen.code, chosen.detail)
        info["stop_observe_seconds"] = args.stop_observe_seconds
        if args.alias:
            info["chat_id"] = resolve_chat_id(info["allowlist"], args.alias)

        original = text
        patched = set_allow_test_inject(original, True)
        if patched != original:
            args.config.write_bytes(patched.encode("utf-8"))
        try:
            proc = _start_supervisor(evidence_dir / "supervisor.log")
            try:
                _wait_http_ready(_control_base(info), proc, deps)
                token = _wait_token(deps)
                client = ControlClient(_control_base(info), token)
                if not args.alias:
                    # 首轮 open_chat + pull_messages(30) 在冷 UIA 上可能十几秒；
                    # 桥每 poll_seconds 才轮一次，60s 余量太紧，给 180s。
                    _wait_peer_chat_id(
                        info, deps, max(180.0, info["poll_seconds"] * 6)
                    )
                    _step(
                        "01-chat-id",
                        evidence_dir,
                        {"chat_id": info["chat_id"], "source": "peers.json"},
                    )
                _run_f3a(client, info, deps, evidence_dir)
                _run_f3b(
                    client,
                    info,
                    deps,
                    evidence_dir,
                    args.human_timeout,
                )
            finally:
                if proc.poll() is None:
                    try:
                        _stop_supervisor()
                    except Exception as exc:  # noqa: BLE001 - 停服失败也要先恢复配置
                        print(f"停止 supervisor 失败：{exc}", file=sys.stderr)
        finally:
            restored = _ensure_disabled(
                args.config.read_bytes().decode("utf-8")
            )
            args.config.write_bytes(restored.encode("utf-8"))
        _step("99-result", evidence_dir, {"ok": True})
        print("F3 真实冒烟通过：F3a ①–⑤ + F3b 全部断言成立")
        return EXIT_OK
    except SmokeError as exc:
        _step(
            "99-result",
            evidence_dir,
            {"ok": False, "code": exc.code, "detail": exc.detail},
        )
        print(f"F3 冒烟失败：{exc.code}: {exc.detail}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        _step("99-result", evidence_dir, {"ok": False, "code": "assert_failed", "detail": str(exc)})
        print(f"F3 冒烟失败：{exc}", file=sys.stderr)
        return EXIT_ASSERT


# ------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qa_wechat_smoke",
        description="F3 微信真实冒烟（信封 -> 核心 -> 出站 UIA 发送）",
    )
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "config.yaml", help="配置文件"
    )
    parser.add_argument(
        "--env-file", type=Path, default=REPO_ROOT / ".env", help="环境变量文件"
    )
    parser.add_argument(
        "--alias", default="", help="测试群 alias；默认取 group_allowlist 第一个"
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="每步 JSON 的落盘目录",
    )
    parser.add_argument(
        "--self-check", action="store_true", help="只做前置自检，不启动任何进程"
    )
    parser.add_argument(
        "--stop-observe-seconds",
        type=float,
        default=60.0,
        help="④ 急停后观察 last_send_at 不变的时长",
    )
    parser.add_argument(
        "--human-timeout",
        type=float,
        default=600.0,
        help="F3b 等待人工发送的最长秒数",
    )
    return parser


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    args.config = args.config.resolve()
    args.env_file = args.env_file.resolve()
    args.evidence_dir = args.evidence_dir.resolve()
    deps = deps or _default_deps()
    if args.self_check:
        return self_check(args, deps)
    return run_smoke(args, deps)


if __name__ == "__main__":
    raise SystemExit(main())
