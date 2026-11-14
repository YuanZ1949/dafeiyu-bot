"""诊断包收集器：版本、依赖、端口、健康、日志与脱敏配置。

脱敏统一复用 :func:`dafeiyu.supervisor.redact.redact`，这里只补两层兜底：
YAML 行级敏感键名（覆盖带空格的带引号值）与 ``sk-`` 形态的裸值。
脚本 ``scripts/diag.ps1`` 只是本模块的薄包装，避免在 PowerShell 里重写正则。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .version import get_version

# 任务约定的六个端口：面板、控制口、微信桥、微信 hook、NapCat、opencode。
# hook 端口以 config.yaml 的 gateway.wechat.hook_url 为准，这里只是诊断展示。
PORTS: tuple[tuple[int, str], ...] = (
    (8787, "supervisor 面板"),
    (8788, "bot 控制口"),
    (8790, "微信桥"),
    (30012, "微信 hook"),
    (8080, "NapCat 反向 WS"),
    (24310, "opencode serve"),
)

# 只探本机公开健康口：控制口里 /api/health 是唯一免 token 的路径，
# /api/health/detail 及其余接口都要 X-Control-Token。
HEALTH_TARGETS: tuple[tuple[int, str], ...] = (
    (8788, "/api/health"),
    (8788, "/health"),
    (8787, "/api/health"),
    (8787, "/health"),
    (8790, "/health"),
)

# 打包后仍能扫到的凭据形态；命中即再打码，避免把密钥带出去。
_SECRET_RE = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]+)")
_SENSITIVE_KEY_RE = re.compile(r"(?i)(?:key|token|password|passwd|secret)")
_YAML_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_.-]+)[ \t]*:[ \t]*(?P<value>.*?)[ \t]*$"
)


@dataclass(frozen=True)
class DiagResult:
    """一次诊断包生成的结果；方便测试与脚本打印。"""

    out: Path
    entries: tuple[str, ...]
    skipped: tuple[str, ...]
    warnings: tuple[str, ...]


def redact_text(text: str, *, yaml_mode: bool = False) -> str:
    """先走公共脱敏，再补 YAML 敏感键名与裸 ``sk-`` 值。"""
    from .supervisor.redact import redact

    result = redact(text)
    if yaml_mode:
        result = _redact_yaml_keys(result)
    return _mask_bare_secrets(result)


def _redact_yaml_keys(text: str) -> str:
    """按行把含 key/token/password/secret 的 YAML 字段值整段替换为 ***。"""
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        match = _YAML_LINE_RE.match(body)
        if match is not None and _SENSITIVE_KEY_RE.search(match.group("key")):
            ending = line[len(body) :]
            lines.append(f"{match.group('indent')}{match.group('key')}: ***{ending}")
        else:
            lines.append(line)
    return "".join(lines)


def _mask_bare_secrets(text: str) -> str:
    """裸值兜底：``sk-`` 前缀与 Bearer 令牌，即使前面没有键名也打码。"""

    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        if value.lower().startswith("sk-"):
            return "sk-***"
        return "Bearer ***"

    return _SECRET_RE.sub(replace, text)


def _is_denied(name: str) -> bool:
    """拒绝清单：运行数据与密钥永远不进 zip。"""
    normalized = name.replace("\\", "/").lower()
    pure = PurePosixPath(normalized)
    if pure.name == ".env":
        return True
    if normalized == "config.yaml":
        return True
    # 整个 data/ 都拒绝：数据库、secrets、supervisor.token、面板口令全在里面。
    if normalized.startswith("data/"):
        return True
    if pure.suffix in {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm"}:
        return True
    return False


def _add_entry(
    archive: zipfile.ZipFile,
    name: str,
    text: str,
    *,
    entries: list[str],
    skipped: list[str],
    warnings: list[str],
    yaml_mode: bool = False,
) -> None:
    """脱敏后写入一个文本条目；仍命中密钥形态就整条跳过。"""
    normalized = PurePosixPath(name.replace("\\", "/")).as_posix()
    if _is_denied(normalized):
        skipped.append(normalized)
        return
    safe = redact_text(text, yaml_mode=yaml_mode)
    if _SECRET_RE.search(safe):
        warnings.append(f"{normalized} 仍含疑似密钥，已跳过")
        skipped.append(normalized)
        return
    archive.writestr(normalized, safe)
    entries.append(normalized)


def _one_line(text: str) -> str:
    """把多行命令输出压成一行，方便写进 version.txt。"""
    parts = [part.strip() for part in text.splitlines() if part.strip()]
    return " / ".join(parts) if parts else "（无输出）"


def _run_text(args: list[str], *, cwd: Path, timeout: float) -> str:
    """跑一个只读子命令，失败也返回可读文本，不抛异常。"""
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"（执行失败：{exc}）"
    output = (completed.stdout or "") + (completed.stderr or "")
    return f"退出码 {completed.returncode}\n{output.strip() or '（无输出）'}"


def _opencode_version(root: Path) -> str:
    candidates = (
        root / "vendor" / "opencode" / "opencode.exe",
        root / "vendor" / "opencode" / "opencode",
    )
    found = next((path for path in candidates if path.is_file()), None)
    if found is None:
        which = shutil.which("opencode")
        if which:
            found = Path(which)
    if found is None:
        return "未找到"
    return _one_line(_run_text([str(found), "--version"], cwd=root, timeout=10))


def _napcat_version(root: Path) -> str:
    package = root / "vendor" / "napcat" / "napcat" / "package.json"
    if not package.is_file():
        return "未找到"
    try:
        payload = json.loads(package.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return "读取失败"
    version = payload.get("version") if isinstance(payload, dict) else None
    return str(version) if version else "未知"


def _version_report(root: Path) -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        f"生成时间: {now}",
        f"dafeiyu: {get_version()}",
        f"Python: {platform.python_version()} ({sys.executable})",
        f"平台: {platform.platform()}",
        f"opencode: {_opencode_version(root)}",
        f"NapCat: {_napcat_version(root)}",
    ]
    return "\n".join(lines) + "\n"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _ports_report() -> str:
    lines = ["端口\t用途\t状态"]
    for port, label in PORTS:
        state = "可连接" if _port_open(port) else "未监听"
        lines.append(f"{port}\t{label}\t{state}")
    return "\n".join(lines) + "\n"


def _health_report() -> str:
    lines: list[str] = []
    for port, path in HEALTH_TARGETS:
        url = f"http://127.0.0.1:{port}{path}"
        lines.append(f"== {url} ==")
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310 - 只探本机
                body = response.read(65536).decode("utf-8", errors="replace")
                lines.append(f"HTTP {response.status}")
                lines.append(body.strip() or "（空响应）")
        except HTTPError as exc:
            body = exc.read(65536).decode("utf-8", errors="replace")
            lines.append(f"HTTP {exc.code}")
            lines.append(body.strip() or "（空响应）")
        except (URLError, OSError, ValueError) as exc:
            lines.append(f"请求失败：{exc}")
        lines.append("")
    return "\n".join(lines)


def _pip_freeze(root: Path) -> str:
    return _run_text(
        [sys.executable, "-m", "pip", "freeze"], cwd=root, timeout=60
    )


def _doctor_report(root: Path) -> str:
    return _run_text(
        [sys.executable, "-m", "dafeiyu.cli", "doctor"], cwd=root, timeout=120
    )


def _env_keys(root: Path) -> str:
    """只列 .env / .env.example 的键名，绝不输出值。"""
    lines: list[str] = []
    for name in (".env", ".env.example"):
        path = root / name
        if not path.is_file():
            continue
        lines.append(f"== {name}（仅键名）==")
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key:
                lines.append(key)
        lines.append("")
    return "\n".join(lines) or "（未找到 .env / .env.example）\n"


def _tail_lines(path: Path, count: int) -> str:
    """从文件末尾按块倒读，避免把整份日志载入内存。"""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        remaining = handle.tell()
        data = b""
        while remaining > 0 and data.count(b"\n") <= count:
            size = min(8192, remaining)
            remaining -= size
            handle.seek(remaining)
            data = handle.read(size) + data
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-count:])


def _log_files(root: Path) -> list[Path]:
    logs = root / "logs"
    if not logs.is_dir():
        return []
    return sorted(
        (path for path in logs.rglob("*.log") if path.is_file()),
        key=lambda path: path.as_posix(),
    )


def default_out(root: Path) -> Path:
    """默认输出到仓库 data/diagnostics/diag-<时间戳>.zip。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return root / "data" / "diagnostics" / f"diag-{stamp}.zip"


def create_diagnostics(
    *,
    root: Path,
    out: Path,
    log_lines: int = 200,
    include_doctor: bool = True,
) -> DiagResult:
    """收集并打包诊断信息；返回实际写入了哪些条目。"""
    root = Path(root).resolve()
    out = Path(out)
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    entries: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        _add_entry(
            archive,
            "version.txt",
            _version_report(root),
            entries=entries,
            skipped=skipped,
            warnings=warnings,
        )
        _add_entry(
            archive,
            "pip-freeze.txt",
            _pip_freeze(root),
            entries=entries,
            skipped=skipped,
            warnings=warnings,
        )
        _add_entry(
            archive,
            "ports.txt",
            _ports_report(),
            entries=entries,
            skipped=skipped,
            warnings=warnings,
        )
        _add_entry(
            archive,
            "health.txt",
            _health_report(),
            entries=entries,
            skipped=skipped,
            warnings=warnings,
        )
        _add_entry(
            archive,
            "env.keys.txt",
            _env_keys(root),
            entries=entries,
            skipped=skipped,
            warnings=warnings,
        )
        if include_doctor:
            _add_entry(
                archive,
                "doctor.txt",
                _doctor_report(root),
                entries=entries,
                skipped=skipped,
                warnings=warnings,
            )

        config = root / "config.yaml"
        if config.is_file():
            config_text = config.read_text(encoding="utf-8", errors="replace")
        else:
            config_text = "（未找到 config.yaml）\n"
        _add_entry(
            archive,
            "config.redacted.yaml",
            config_text,
            entries=entries,
            skipped=skipped,
            warnings=warnings,
            yaml_mode=True,
        )

        for path in _log_files(root):
            relative = path.relative_to(root).as_posix()
            _add_entry(
                archive,
                relative,
                _tail_lines(path, log_lines),
                entries=entries,
                skipped=skipped,
                warnings=warnings,
            )

        manifest = "\n".join(
            [
                f"generated_at: {datetime.now().astimezone().isoformat(timespec='seconds')}",
                f"root: {root}",
                f"log_lines_per_file: {log_lines}",
                "entries:",
                *[f"  - {name}" for name in entries],
                "skipped:",
                *[f"  - {name}" for name in skipped],
                "warnings:",
                *[f"  - {message}" for message in warnings],
                "",
            ]
        )
        archive.writestr("manifest.txt", manifest)
        entries.append("manifest.txt")

    return DiagResult(
        out=out,
        entries=tuple(sorted(entries)),
        skipped=tuple(sorted(skipped)),
        warnings=tuple(warnings),
    )


def _print_plan(
    root: Path, out: Path, log_lines: int, *, include_doctor: bool
) -> None:
    print("[dry-run] 诊断包计划（不写任何文件，不执行 pip / doctor）：")
    print(f"  仓库根目录：{root}")
    print(f"  输出路径：{out}")
    print(f"  每个日志文件保留最后 {log_lines} 行")
    extras = "、doctor.txt" if include_doctor else ""
    print(
        "  将包含：manifest.txt、version.txt、pip-freeze.txt、ports.txt、"
        f"health.txt、env.keys.txt{extras}"
    )
    print("           config.redacted.yaml（脱敏）、logs/**/*.log（脱敏）")
    print("  端口：" + "、".join(str(port) for port, _ in PORTS))
    logs = _log_files(root)
    if logs:
        print("  日志文件：")
        for path in logs:
            print(f"    - {path.relative_to(root).as_posix()}")
    else:
        print("  日志文件：（未找到）")
    print(
        "  拒绝清单：data/、.env、config.yaml 原文、*.db*、"
        "data/secrets、data/supervisor.token"
    )
    print("[dry-run] 未写入任何文件。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dafeiyu.diag", description="收集脱敏诊断包（zip）"
    )
    parser.add_argument("--root", default="", help="仓库根目录，默认当前目录")
    parser.add_argument("--out", default="", help="zip 输出路径")
    parser.add_argument(
        "--log-lines", type=int, default=200, help="每个日志文件保留的行数"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印计划，不写文件"
    )
    parser.add_argument("--no-doctor", action="store_true", help="跳过 doctor 输出")
    args = parser.parse_args(argv)

    if args.log_lines < 1:
        parser.error("--log-lines 必须 >= 1")

    root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    repo_markers = (
        root / "pyproject.toml",
        root / "src" / "dafeiyu",
        root / "config.yaml",
    )
    if not any(marker.exists() for marker in repo_markers):
        print(f"诊断包生成失败：{root} 不像 dafeiyu 仓库根目录", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else default_out(root)
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()

    if args.dry_run:
        _print_plan(root, out, args.log_lines, include_doctor=not args.no_doctor)
        return 0

    try:
        result = create_diagnostics(
            root=root,
            out=out,
            log_lines=args.log_lines,
            include_doctor=not args.no_doctor,
        )
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"诊断包生成失败：{exc}", file=sys.stderr)
        return 1

    print(f"已生成诊断包：{result.out}（{len(result.entries)} 个文件）")
    if result.skipped:
        print("已按拒绝清单跳过：" + "、".join(result.skipped))
    for warning in result.warnings:
        print("警告：" + warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
