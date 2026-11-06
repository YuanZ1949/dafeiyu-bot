from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from ..brain.client import BrainError
from ..brain.opencode import resolve_opencode_bin
from ..brain.opencode_auth import PASSWORD_ENV, password_env, resolve_password
from ..brain.provider_config import ensure_agent_files
from ..brain.runtime_config import server_env
from ..config import ConfigError, Settings, load_settings
from ..gateway.channels import qq_enabled_for, wechat_enabled_for
from ..timeutil import now_shanghai
from .alerts import AlertChannel, AlertManager, QQAdminChannel, WebhookChannel
from .audit import AuditLog
from .backup import BackupManager
from .control_token import TOKEN_PATH, ControlToken
from .dashboard import serve
from .health import HealthMonitor
from .lan_access import PASSWORD_PATH as DASHBOARD_PASSWORD_PATH
from .lan_access import dashboard_urls, ensure_dashboard_password
from .logs import LogHub
from .manager import ProcSpec, Supervisor, pid_alive, port_owner
from .napcat_login import NapCatLogin, load_webui_auth
from .ops import OpsServices
from .restore import RestoreService

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
QR_PATH = REPO_ROOT / "vendor" / "napcat" / "napcat" / "cache" / "qrcode.png"
NAPCAT_DIR = REPO_ROOT / "vendor" / "napcat"
PID_PATH = REPO_ROOT / "data" / "supervisor.pid"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 桥进程只认这个环境变量（config.py 的 `_build_wechat` 同源读取），
# 绝不能把 token 放进 argv 或 ProcSpec 的 repr。
WECHAT_BRIDGE_TOKEN_ENV = "DAFEIYU_WECHAT_BRIDGE_TOKEN"
# 默认 30s 不够：桥要先建 WS 服务，再等 bot 冷启动 + WeChatAdapter 连上，
# 开机/升级后常见 30-60s。这里显式给 90s，避免误判 FAILED 把桥杀掉。
WECHAT_BRIDGE_READY_TIMEOUT = 90.0


class _HubHandler(logging.Handler):
    """把 supervisor 自己的日志也塞进同一套环形缓冲。"""

    def __init__(self, hub: LogHub) -> None:
        super().__init__()
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.hub.write("supervisor", self.format(record))
        # 日志系统本身不能把主流程带崩
        except Exception:  # noqa: BLE001, S110
            pass


def setup_logging(settings: Settings, logs: LogHub) -> None:
    level = getattr(logging, settings.logging.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = []
    if sys.stdout is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        handlers.append(stream)
    hub = _HubHandler(logs)
    hub.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handlers.append(hub)
    root.handlers[:] = handlers


def load_or_create_token(path: Path = TOKEN_PATH) -> str:
    """兼容包装：内部统一走 ControlToken，已有文件也会加固 ACL。"""
    return ControlToken(path).load_or_create()


def _console_python() -> str:
    """supervisor 用 pythonw 跑；bot 用 python.exe 才能把 stdout 交给管道。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _eprint(message: str) -> None:
    if sys.stderr is not None:
        print(message, file=sys.stderr)


def find_napcat_pid(directory: Path = NAPCAT_DIR) -> int | None:
    """NapCat 是 node.exe，用命令行是否包含安装目录来认，端口认不出来。"""
    pids = find_napcat_pids(directory)
    return pids[0] if pids else None


def find_napcat_pids(directory: Path = NAPCAT_DIR) -> list[int]:
    """列出安装目录下的所有 node 进程（主进程 + worker）。

    主进程崩溃后 worker 会留下来继续占着 QQ 登录态，只认「第一个」不够用。
    """
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{directory}') }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _wechat_bridge_ready_probe(control_port: int, token: str) -> Callable[[], bool]:
    """微信桥的就绪判据：bot 的 `/api/health/detail` 里微信通道已连接。

    桥自己只跑 WS 服务端、没有 HTTP 端点，因此 `ready_url` 留空，改由这条
    同步探针问 bot。`/api/health` 只有 `{"ok": true}` 且无 `channels`，不能用；
    `/api/health/detail` 需要 token，所以控制端口与口令都从 supervisor 传进来。
    """

    url = f"http://127.0.0.1:{control_port}/api/health/detail"

    def probe() -> bool:
        headers = {"X-Control-Token": token} if token else {}
        request = urllib.request.Request(url, headers=headers)
        # 2s 上限：探针跑在线程池里，超时只影响本次判定，不会卡住事件循环。
        try:
            with urllib.request.urlopen(request, timeout=2.0) as response:
                if getattr(response, "status", 200) != 200:
                    return False
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - 401/超时/非 JSON 一律视为未就绪
            return False
        if not isinstance(payload, dict):
            return False
        channels = payload.get("channels")
        if not isinstance(channels, dict):
            return False
        wechat = channels.get("wechat")
        return isinstance(wechat, dict) and wechat.get("connected") is True

    return probe


def build_specs(settings: Settings, token: str) -> dict[str, ProcSpec]:
    try:
        opencode_bin = resolve_opencode_bin(settings.opencode.bin)
    except BrainError as exc:
        raise ConfigError(
            f"找不到 OpenCode 可执行文件：{exc}。请先 npm i -g opencode-ai"
        ) from exc

    db_path = Path(settings.opencode.db_path)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    control_host = settings.supervisor.control_host
    control_port = settings.supervisor.control_port

    specs: dict[str, ProcSpec] = {}
    # P7：opencode v2 的 serve 强制 Basic 认证（用户名固定 opencode，密码由
    # OPENCODE_SERVER_PASSWORD 决定）。真正拉起 serve 的是 supervisor（bot 侧
    # manage_server=false），所以口令必须在这里注入：只靠 runtime-launcher.ps1
    # 手动 set 的话，绕过启动器直接跑 start-all/start.bat 就会让 serve 随机生成
    # 口令、bot 全 401。同一个口令同时给 opencode 与 bot 两个子进程；配置留空时
    # 每次启动生成一个新的 32 位十六进制口令（只放进程环境，不落盘、不写日志）。
    server_password = resolve_password(settings.opencode.server_password)
    # 配置隔离同样必须在这里注入；留空时 server_env 返回 None，保持软回滚。
    opencode_env: dict[str, str] = {"OPENCODE_DB": str(db_path), "NO_COLOR": "1"}
    opencode_env.update(server_env(settings.opencode.config_home) or {})
    opencode_env.update(password_env(server_password))
    specs["opencode"] = ProcSpec(
        name="opencode",
        command=[
            opencode_bin,
            "serve",
            "--hostname",
            settings.opencode.host,
            "--port",
            str(settings.opencode.port),
        ],
        cwd=str(REPO_ROOT),
        env=opencode_env,
        ready_url=f"http://{settings.opencode.host}:{settings.opencode.port}/api/info",
        creationflags=CREATE_NO_WINDOW,
    )
    specs["bot"] = ProcSpec(
        name="bot",
        command=[_console_python(), "-m", "dafeiyu"],
        cwd=str(REPO_ROOT),
        env={
            "DAFEIYU_CONTROL_HOST": control_host,
            "DAFEIYU_CONTROL_PORT": str(control_port),
            "DAFEIYU_CONTROL_TOKEN": token,
            # P7：bot 靠这条环境变量读到和 serve 完全一致的口令（config.py 缺省
            # 就是回退同名环境变量）。显式注入而不是依赖继承：启动器没 set 时，
            # supervisor 生成的口令只有这样才能传到 bot。
            PASSWORD_ENV: server_password,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        },
        ready_url=f"http://127.0.0.1:{control_port}/api/health",
        creationflags=CREATE_NO_WINDOW,
    )
    # 插入顺序即启动顺序（Supervisor.start_all 按 dict 顺序）：桥必须排在 bot
    # 之后，否则它的 ready_probe（问 bot 的 /api/health/detail）会一直空转。
    # 只有 provider 真正选中微信时才注册：provider=qq + enabled=true 时
    # task 20 不会构造 WeChatAdapter，拉起桥只会得到一个永远不就绪的进程。
    if wechat_enabled_for(settings):
        specs["wechat_bridge"] = ProcSpec(
            name="wechat_bridge",
            command=[sys.executable, "-m", "dafeiyu.gateway.wechat_bridge"],
            cwd=str(REPO_ROOT),
            env={
                WECHAT_BRIDGE_TOKEN_ENV: settings.wechat.bridge_token,
                # 桥的日志直接由 supervisor 落盘；不强制 UTF-8 时中文会变成 "?"。
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            },
            # 桥是出站 WS 客户端，没有可探的 HTTP 端点。
            ready_url="",
            ready_probe=_wechat_bridge_ready_probe(control_port, token),
            ready_timeout=WECHAT_BRIDGE_READY_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    # NapCat 只为 QQ 通道服务：provider=wechat 且微信启用时 bot 不监听 QQ 的 WS
    # 端口，拉起 NapCat 只会每 5 秒刷一条 `ECONNREFUSED 127.0.0.1:8080`。
    # 反过来，纯微信部署也不该因为「没装 NapCat」而起不来。
    if qq_enabled_for(settings):
        node = NAPCAT_DIR / "node.exe"
        if not node.exists():
            raise ConfigError(
                f"NapCat 未安装：{node} 不存在。请先运行 scripts/setup-napcat.ps1"
            )
        # -q <uin> 让 NapCat 用机器人号已有的本地票据自动登录，冷启动不必再扫码。
        # 必须显式给 uin：本机快速登录列表里还有用户的个人号 504567327，
        # 不给 -q 会按列表顺序登错号。票据失效时 NapCat 自己回落到二维码，不会卡死。
        # 配了密码时，快登失败会自动走一次密码回退（napcat.mjs:83224）。
        napcat_env: dict[str, str] = {}
        if settings.napcat.quick_password:
            napcat_env["NAPCAT_QUICK_PASSWORD"] = settings.napcat.quick_password
        if settings.napcat.quick_password_md5:
            napcat_env["NAPCAT_QUICK_PASSWORD_MD5"] = settings.napcat.quick_password_md5
        specs["napcat"] = ProcSpec(
            name="napcat",
            command=[str(node), "index.js", "-q", str(settings.bot_qq)],
            cwd=str(NAPCAT_DIR),
            env=napcat_env or None,
            creationflags=CREATE_NO_WINDOW,
            # 主进程被 NapCat 自己的 kill EPERM 打崩后，worker 会继续占着 QQ 登录态；
            # 不先清掉，新实例只会报「当前账号已登录，无法重复登录」然后卡住。
            sweep_pids=lambda: find_napcat_pids(NAPCAT_DIR),
            # 真出现这句时说明已经有残留占着登录态：清干净再重启一次。
            recover_patterns=("无法重复登录",),
        )
    return specs


def _repo_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return REPO_ROOT / path


async def _bot_ready(bot_url: str, token: str, timeout: float = 5.0) -> bool:
    """恢复后的就绪判据与 health 卡一致：detail 返回 200 且 ok=True。"""
    url = f"{bot_url.rstrip('/')}/api/health/detail"
    headers = {"X-Control-Token": token} if token else {}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session, session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return False
            data = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return False
    return isinstance(data, dict) and data.get("ok") is True


def build_ops(
    settings: Settings,
    supervisor: Supervisor,
    logs: LogHub,
    bot_url: str,
    token: str | Callable[[], str],
    napcat_login: NapCatLogin | None = None,
) -> OpsServices:
    """按 audit → backup → restore → health → alerts 的顺序装配运维服务。"""

    def _current_token() -> str:
        value = token() if callable(token) else token
        return value or ""

    initial_token = _current_token()
    audit = AuditLog(
        _repo_path(settings.audit.path),
        max_bytes=settings.audit.max_bytes,
        backups=settings.audit.backups,
    )
    db_path = _repo_path(settings.db_path)
    backups = BackupManager(
        db_path,
        _repo_path(settings.backup.dir),
        daily_keep=settings.backup.daily_keep,
        weekly_keep=settings.backup.weekly_keep,
        max_total_gb=settings.backup.max_total_gb,
        audit=audit,
        # 长期记忆（MEMORY.md、chats/*.md）随备份一起打包；未启用工作区时为 None。
        workspace_dir=(
            _repo_path(settings.workspace.dir) if settings.workspace.enabled else None
        ),
    )

    async def ready_check() -> bool:
        # 恢复流程会在轮换后继续跑，必须每次探测都取当前 token。
        return await _bot_ready(bot_url, _current_token())

    restore = RestoreService(
        db_path=db_path,
        backups=backups,
        supervisor=supervisor,
        audit=audit,
        ready_check=ready_check,
        ready_timeout=float(settings.opencode.startup_timeout_seconds),
        # 工作区快照恢复（R18）；未启用工作区时为 None。
        workspace_dir=(
            _repo_path(settings.workspace.dir) if settings.workspace.enabled else None
        ),
    )
    health = HealthMonitor(
        supervisor=supervisor,
        settings=settings,
        bot_url=bot_url,
        token=initial_token,
        backups=backups,
        napcat_login=napcat_login,
    )

    alerts: AlertManager | None = None
    if settings.alerts.enabled:
        channels: list[AlertChannel] = []
        if settings.alerts.qq_admin:
            # 传原始 token：字符串按旧行为冻结，callable 则在每次发送时解析。
            channels.append(QQAdminChannel(bot_url, token))
        webhook = settings.alerts.webhook
        if webhook.url:
            channels.append(WebhookChannel(webhook.url, webhook.format))
        alerts = AlertManager(
            settings.alerts,
            _repo_path("data/alert-state.json"),
            channels,
            audit=audit,
        )
        logs.write("supervisor", f"告警已启用：{len(channels)} 个通道")
    else:
        logs.write("supervisor", "告警未启用（alerts.enabled=false）")
    logs.write("supervisor", f"备份目录：{backups.backup_dir}")

    return OpsServices(
        health=health,
        alerts=alerts,
        backups=backups,
        restore=restore,
        audit=audit,
    )


@dataclass
class StopOrigin:
    """进程内停止来源登记：所有停机入口都先在这里留下是谁/为什么。"""

    reason: str = "unknown"
    actor: str = "system"
    signal: str | None = None


def record_supervisor_stop(
    origin: StopOrigin,
    audit: AuditLog | None,
    *,
    reason: str,
    actor: str = "system",
    signal_name: str | None = None,
    uptime_seconds: float | None = None,
) -> None:
    """登记停机来源；审计日志可用时落一条 ``supervisor.stop``。"""
    origin.reason = reason
    origin.actor = actor
    origin.signal = signal_name
    if audit is None:
        return
    detail: dict[str, Any] = {"reason": reason, "pid": os.getpid()}
    if signal_name is not None:
        detail["signal"] = signal_name
    if uptime_seconds is not None:
        detail["uptime_seconds"] = round(uptime_seconds, 1)
    audit.write("supervisor.stop", actor=actor, target="supervisor", detail=detail)


def signal_stop(
    stop: asyncio.Event,
    origin: StopOrigin,
    audit: AuditLog | None,
    signal_name: str,
    *,
    uptime_seconds: float | None = None,
) -> None:
    """SIGINT/SIGTERM 触发的停机：登记来源（system/signal）后置 stop。"""
    record_supervisor_stop(
        origin,
        audit,
        reason="signal",
        actor="system",
        signal_name=signal_name,
        uptime_seconds=uptime_seconds,
    )
    stop.set()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
    except TimeoutError:
        pass


async def alert_loop(
    ops: OpsServices,
    settings: Settings,
    stop: asyncio.Event,
    *,
    interval: float | None = None,
) -> None:
    if ops.alerts is None:
        return
    delay = float(settings.alerts.check_seconds if interval is None else interval)
    while not stop.is_set():
        try:
            snapshot = await ops.health.snapshot()
            await ops.alerts.evaluate(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 巡检失败不能把 supervisor 带崩
            log.warning("告警巡检失败：%s", exc)
        await _sleep_or_stop(stop, delay)


def backup_due(
    now: datetime, schedule_hour: int, last_scheduled_date: str | None
) -> bool:
    return (
        now.hour == int(schedule_hour)
        and last_scheduled_date != now.date().isoformat()
    )


async def backup_loop(
    ops: OpsServices,
    settings: Settings,
    stop: asyncio.Event,
    *,
    interval: float = 60.0,
    clock: Callable[[], datetime] | None = None,
) -> None:
    if ops.backups is None:
        return
    tick = clock or now_shanghai
    while not stop.is_set():
        now = tick()
        last_date = ops.backups.last_scheduled_date()
        if backup_due(now, settings.backup.schedule_hour, last_date):
            try:
                manifest = await ops.backups.create("scheduled")
                log.info("定时备份完成：%s", manifest.filename)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 失败由告警规则接管
                log.warning("定时备份失败：%s", exc)
        await _sleep_or_stop(stop, interval)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop: asyncio.Event,
    on_signal: Callable[[str], None] | None = None,
) -> None:
    def request(sig_name: str = "") -> None:
        if on_signal is not None and sig_name:
            on_signal(sig_name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        name = signal.Signals(sig).name
        try:
            loop.add_signal_handler(sig, request, name)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(
                    sig,
                    lambda *_, _name=name: loop.call_soon_threadsafe(request, _name),
                )
            except (ValueError, OSError):
                pass


def _write_pid() -> None:
    try:
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _read_pid() -> int | None:
    try:
        raw = PID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _clear_pid() -> None:
    with contextlib.suppress(OSError):
        PID_PATH.unlink()


def selfheal_agent_files(settings: Settings, logs: LogHub) -> list[str]:
    """启动子进程前按当前档位对齐 agent 文件，返回本次改写的路径。

    自愈失败只写日志、返回空列表：配置文件坏了不该让机器人起不来，
    但要留下可查的痕迹（下次看日志能直接看到是哪个异常）。
    """
    try:
        rewritten = ensure_agent_files(settings)
    except Exception as exc:  # noqa: BLE001 - 自愈失败不该阻断启动
        logs.write("supervisor", f"agent 文件自愈失败（{type(exc).__name__}）：{exc}")
        return []
    if rewritten:
        logs.write("supervisor", "agent 文件已按当前档位刷新：" + "、".join(rewritten))
    return rewritten


async def run() -> int:
    settings = load_settings()
    logs = LogHub(Path(settings.logging.dir) / "supervisor")
    setup_logging(settings, logs)

    holder = ControlToken(TOKEN_PATH)
    holder.load_or_create()

    # 面板默认对局域网开放：没有显式密码时生成并落盘，不能因此起不来。
    password_file_existed = DASHBOARD_PASSWORD_PATH.exists()
    dashboard_password = ensure_dashboard_password(settings.supervisor.dashboard)
    if dashboard_password and dashboard_password != settings.supervisor.dashboard.password:
        settings = replace(
            settings,
            supervisor=replace(
                settings.supervisor,
                dashboard=replace(
                    settings.supervisor.dashboard, password=dashboard_password
                ),
            ),
        )
        if password_file_existed:
            logs.write("supervisor", f"面板口令读取自 {DASHBOARD_PASSWORD_PATH}")
        elif DASHBOARD_PASSWORD_PATH.exists():
            logs.write(
                "supervisor",
                f"面板口令已生成：{dashboard_password}"
                f"（存于 {DASHBOARD_PASSWORD_PATH}，可用 .env 覆盖）",
            )
        else:
            logs.write(
                "supervisor",
                f"面板口令已生成：{dashboard_password}（写盘失败，仅本次生效）",
            )

    try:
        specs = build_specs(settings, holder.value)
    except ConfigError as exc:
        logs.write("supervisor", f"启动失败：{exc}")
        _eprint(f"[大肥鱼] {exc}")
        return 1

    supervisor = Supervisor(
        specs,
        logs,
        settings.supervisor.restart,
        ready_timeout=float(settings.opencode.startup_timeout_seconds),
    )

    if "napcat" in specs:
        owner = find_napcat_pid()
        if owner is not None:
            supervisor.mark_external("napcat", owner)
            logs.write("supervisor", f"NapCat 已在外运行（pid={owner}），接管为外部实例")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    started_at = time.monotonic()
    # 停止来源登记：ops（含 audit）要到 build_ops 之后才可用，先用 holder 持有，
    # 闭包里每次现读，避免时序问题导致审计丢失。
    stop_origin = StopOrigin()
    audit_holder: list[AuditLog | None] = [None]

    def uptime_seconds() -> float:
        return time.monotonic() - started_at

    def on_signal(name: str) -> None:
        signal_stop(
            stop,
            stop_origin,
            audit_holder[0],
            name,
            uptime_seconds=uptime_seconds(),
        )

    _install_signal_handlers(loop, stop, on_signal)

    # 双开拦截：pid 文件 + dashboard 端口占用。Windows 的 SO_REUSEADDR
    # 会让两个进程同时绑定同一端口，不能只靠 bind 失败来发现。
    running_pid = _read_pid()
    if running_pid is not None and running_pid != os.getpid() and pid_alive(running_pid):
        logs.write("supervisor", f"已有 supervisor 在运行（pid={running_pid}），本次退出")
        _eprint(f"[大肥鱼] 已有 supervisor 在运行（pid={running_pid}）")
        return 1
    dashboard_port = settings.supervisor.dashboard.port
    busy = port_owner(dashboard_port)
    if busy is not None:
        logs.write(
            "supervisor",
            f"dashboard 端口 {dashboard_port} 已被 pid={busy} 占用，本次退出",
        )
        _eprint(f"[大肥鱼] dashboard 端口 {dashboard_port} 已被占用（pid={busy}）")
        return 1

    _write_pid()

    bot_url = f"http://{settings.supervisor.control_host}:{settings.supervisor.control_port}"

    async def request_shutdown(
        reason: str = "unknown",
        actor: str = "system",
    ) -> None:
        record_supervisor_stop(
            stop_origin,
            audit_holder[0],
            reason=reason,
            actor=actor,
            uptime_seconds=uptime_seconds(),
        )
        stop.set()

    napcat_login = NapCatLogin(load_webui_auth())
    try:
        ops = build_ops(
            settings,
            supervisor,
            logs,
            bot_url,
            lambda: holder.value,
            napcat_login=napcat_login,
        )
    except OSError as exc:
        logs.write("supervisor", f"运维服务初始化失败：{exc}")
        _eprint(f"[大肥鱼] 运维服务初始化失败：{exc}")
        _clear_pid()
        return 1
    audit_holder[0] = ops.audit

    runner = await serve(
        supervisor,
        settings,
        logs,
        QR_PATH,
        bot_url,
        token=lambda: holder.value,
        on_shutdown=request_shutdown,
        napcat_login=napcat_login,
        ops=ops,
        control_token=holder,
    )

    urls = dashboard_urls(
        settings.supervisor.dashboard.host, settings.supervisor.dashboard.port
    )
    for url in urls:
        logs.write("supervisor", f"dashboard: {url}")
    if settings.supervisor.dashboard.open_browser:
        with contextlib.suppress(Exception):
            loop.run_in_executor(None, webbrowser.open, urls[0])

    tasks: list[asyncio.Task[None]] = []
    if settings.alerts.enabled:
        tasks.append(asyncio.create_task(alert_loop(ops, settings, stop), name="alert-loop"))
    if settings.backup.enabled:
        tasks.append(asyncio.create_task(backup_loop(ops, settings, stop), name="backup-loop"))

    clean = True
    try:
        # opencode 先起、bot 后起：agent 文件若还钉着上一个档位的模型，整个
        # 会话都会用错模型（聊天错档、记忆旁路撞免费额度）。启动前按当前
        # 档位重渲一次；失败只记日志，绝不能把机器人挡在门外。
        selfheal_agent_files(settings, logs)
        await supervisor.start_all()
        await stop.wait()
    except BaseException:
        # 异常路径同样要收摊；clean 标记为不干净，但退出顺序保持不变。
        clean = False
        raise
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logs.write("supervisor", "正在关闭所有进程…")
        try:
            await asyncio.wait_for(supervisor.shutdown(), timeout=45)
        except TimeoutError:
            clean = False
            logs.write("supervisor", "停止子进程超时，强制退出")
        logs.write("supervisor", "子进程已停止，关闭 dashboard")
        try:
            await asyncio.wait_for(runner.cleanup(), timeout=15)
        except TimeoutError:
            clean = False
            logs.write("supervisor", "dashboard 关闭超时，强制退出")
        with contextlib.suppress(Exception):
            await ops.health.close()
        _clear_pid()
        logs.write("supervisor", "已退出")
        audit = audit_holder[0]
        if audit is not None:
            audit.write(
                "supervisor.exit",
                actor=stop_origin.actor,
                target="supervisor",
                detail={
                    "reason": stop_origin.reason,
                    "actor": stop_origin.actor,
                    "uptime_seconds": round(uptime_seconds(), 1),
                    "clean": clean,
                },
            )
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except ConfigError as exc:
        _eprint(f"[大肥鱼] 配置错误：{exc}")
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
