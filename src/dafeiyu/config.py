from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values


class ConfigError(Exception):
    """配置缺失或非法。消息只提字段名，不回显字段值。"""


@dataclass(frozen=True)
class PersonaConfig:
    name: str
    aliases: tuple[str, ...]
    max_reply_chars: int
    max_reply_chars_technical: int
    os_probability: float
    prompt_file: str = ""


@dataclass(frozen=True)
class TriggerConfig:
    cooldown_seconds: int
    sample_rate: float
    quiet_hours: tuple[str, str]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class GroupsConfig:
    whitelist: frozenset[int]
    blacklist: frozenset[int]
    admins: frozenset[int] = frozenset()


@dataclass(frozen=True)
class BudgetConfig:
    daily_calls: int
    daily_tokens: int


@dataclass(frozen=True)
class MemoryConfig:
    window_size: int
    impression_topk: int
    extract_every: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    dir: str


@dataclass(frozen=True)
class BrainConfig:
    provider: str = "local"


@dataclass(frozen=True)
class OpenCodeConfig:
    bin: str = ""
    agent: str = "deepseek-qq"
    project_dir: str = ""
    timeout_seconds: float = 90.0
    manage_server: bool = False
    server_url: str = ""
    db_path: str = "data/opencode-bot.db"
    host: str = "127.0.0.1"
    port: int = 24310
    startup_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class LocalModelConfig:
    """任意 OpenAI 兼容端点；默认指向本机 LM Studio。"""

    base_url: str = "http://127.0.0.1:1234/v1"
    model: str = ""
    api_key: str = "local"


@dataclass(frozen=True)
class RestartConfig:
    backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
    max_failures: int = 5


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    open_browser: bool = True


@dataclass(frozen=True)
class SupervisorConfig:
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    control_host: str = "127.0.0.1"
    control_port: int = 8788
    restart: RestartConfig = field(default_factory=RestartConfig)


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    bot_qq: int
    ws_host: str
    ws_port: int
    ws_token: str
    db_path: str
    persona: PersonaConfig
    triggers: TriggerConfig
    groups: GroupsConfig
    budget: BudgetConfig
    memory: MemoryConfig
    logging: LoggingConfig
    reasoning_effort: str = ""
    brain: BrainConfig = field(default_factory=BrainConfig)
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    local_model: LocalModelConfig = field(default_factory=LocalModelConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"config.yaml 缺少配置段：{name}")
    return value


def _int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} 必须是整数") from exc


def _float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} 必须是数字") from exc


def _int_set(value: Any, field: str) -> frozenset[int]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ConfigError(f"{field} 必须是列表")
    return frozenset(_int(v, field) for v in value)


def _build_persona(raw: dict[str, Any]) -> PersonaConfig:
    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list) or not aliases:
        raise ConfigError("persona.aliases 必须是非空列表")
    max_chars = _int(raw.get("max_reply_chars", 120), "persona.max_reply_chars")
    max_tech = _int(raw.get("max_reply_chars_technical", 300), "persona.max_reply_chars_technical")
    if max_chars < 1 or max_tech < 1:
        raise ConfigError("persona 回复长度上限必须大于 0")
    return PersonaConfig(
        name=str(raw.get("name", "溟月")),
        aliases=tuple(str(a) for a in aliases),
        max_reply_chars=max_chars,
        max_reply_chars_technical=max_tech,
        os_probability=_float(raw.get("os_probability", 0.05), "persona.os_probability"),
        prompt_file=str(raw.get("prompt_file") or "").strip(),
    )


def _build_triggers(raw: dict[str, Any]) -> TriggerConfig:
    sample_rate = _float(raw.get("sample_rate", 0.05), "triggers.sample_rate")
    if not 0 < sample_rate <= 1:
        raise ConfigError("triggers.sample_rate 必须在 (0, 1] 之间")
    quiet = raw.get("quiet_hours", ["01:00", "07:00"])
    if not isinstance(quiet, list) or len(quiet) != 2:
        raise ConfigError("triggers.quiet_hours 必须是长度为 2 的列表，如 [\"01:00\", \"07:00\"]")
    keywords = raw.get("keywords", [])
    if not isinstance(keywords, list):
        raise ConfigError("triggers.keywords 必须是列表")
    return TriggerConfig(
        cooldown_seconds=_int(raw.get("cooldown_seconds", 60), "triggers.cooldown_seconds"),
        sample_rate=sample_rate,
        quiet_hours=(str(quiet[0]), str(quiet[1])),
        keywords=tuple(str(k) for k in keywords),
    )


def load_settings(
    env_file: Path = Path(".env"),
    config_file: Path = Path("config.yaml"),
) -> Settings:
    if not config_file.exists():
        raise ConfigError(f"找不到配置文件：{config_file}（可复制 config.yaml 示例后修改）")

    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml 格式错误：{exc.__class__.__name__}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 顶层必须是映射")

    env: dict[str, str] = {}
    env.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    env.update(os.environ)

    brain_raw = raw.get("brain") or {}
    if not isinstance(brain_raw, dict):
        raise ConfigError("config.yaml 的 brain 必须是映射")
    raw_provider = str(brain_raw.get("provider", "token")).strip().casefold()
    provider = "token" if raw_provider == "local" else raw_provider
    if provider not in ("token", "opencode", "local_model"):
        raise ConfigError(
            "brain.provider 只能是 opencode、token 或 local_model（local 为 token 的别名）"
        )

    local_model_raw = raw.get("local_model") or {}
    if not isinstance(local_model_raw, dict):
        raise ConfigError("config.yaml 的 local_model 必须是映射")
    local_model = LocalModelConfig(
        base_url=str(
            local_model_raw.get("base_url") or "http://127.0.0.1:1234/v1"
        ).strip(),
        model=str(local_model_raw.get("model") or "").strip(),
        api_key=str(local_model_raw.get("api_key") or "local").strip(),
    )
    if not local_model.base_url:
        raise ConfigError("local_model.base_url 不能为空")

    opencode_raw = raw.get("opencode") or {}
    if not isinstance(opencode_raw, dict):
        raise ConfigError("config.yaml 的 opencode 必须是映射")
    opencode_agent = str(opencode_raw.get("agent") or "deepseek-qq").strip()
    if not opencode_agent:
        raise ConfigError("opencode.agent 不能为空")
    opencode_timeout = _float(
        opencode_raw.get("timeout_seconds", 90), "opencode.timeout_seconds"
    )
    if opencode_timeout <= 0:
        raise ConfigError("opencode.timeout_seconds 必须大于 0")

    manage_server = opencode_raw.get("manage_server", False)
    if not isinstance(manage_server, bool):
        raise ConfigError("opencode.manage_server 必须是布尔值")
    server_url = str(opencode_raw.get("server_url") or "").strip()
    db_path = str(opencode_raw.get("db_path") or "data/opencode-bot.db").strip()
    host = str(opencode_raw.get("host") or "127.0.0.1").strip()
    port = _int(opencode_raw.get("port", 24310), "opencode.port")
    startup_timeout = _float(
        opencode_raw.get("startup_timeout_seconds", 30.0),
        "opencode.startup_timeout_seconds",
    )
    if manage_server and server_url:
        raise ConfigError(
            "opencode.manage_server 为 true 时 server_url 必须为空"
        )
    if provider == "opencode" and not manage_server and not server_url:
        raise ConfigError(
            "brain.provider 为 opencode 时必须二选一："
            "opencode.manage_server: true，或填写 opencode.server_url；"
            "两者都空会回落到个人 OpenCode 服务，把机器人会话写进个人库"
        )
    if manage_server and not db_path:
        raise ConfigError("opencode.manage_server 为 true 时 db_path 不能为空")
    if not 1 <= port <= 65535:
        raise ConfigError("opencode.port 必须在 1-65535 之间")
    if startup_timeout <= 0:
        raise ConfigError("opencode.startup_timeout_seconds 必须大于 0")

    api_key = env.get("DEEPSEEK_API_KEY", "").strip()
    if provider == "token" and not api_key:
        raise ConfigError("缺少环境变量 DEEPSEEK_API_KEY")
    if "BOT_QQ" not in env or not str(env["BOT_QQ"]).strip():
        raise ConfigError("缺少环境变量 BOT_QQ")

    ws_port = _int(env.get("NAPCAT_WS_PORT", 8080), "NAPCAT_WS_PORT")
    if not 1 <= ws_port <= 65535:
        raise ConfigError("NAPCAT_WS_PORT 必须在 1–65535 之间")

    budget_raw = _section(raw, "budget")
    memory_raw = _section(raw, "memory")
    logging_raw = _section(raw, "logging")

    supervisor_raw = raw.get("supervisor") or {}
    if not isinstance(supervisor_raw, dict):
        raise ConfigError("config.yaml 的 supervisor 必须是映射")
    dash_raw = supervisor_raw.get("dashboard") or {}
    if not isinstance(dash_raw, dict):
        raise ConfigError("supervisor.dashboard 必须是映射")
    dash_port = _int(dash_raw.get("port", 8787), "supervisor.dashboard.port")
    open_browser = dash_raw.get("open_browser", True)
    if not isinstance(open_browser, bool):
        raise ConfigError("supervisor.dashboard.open_browser 必须是布尔值")
    ctrl_raw = supervisor_raw.get("control") or {}
    if not isinstance(ctrl_raw, dict):
        raise ConfigError("supervisor.control 必须是映射")
    env_ctrl_port = env.get("DAFEIYU_CONTROL_PORT", "").strip()
    ctrl_port = _int(
        env_ctrl_port or ctrl_raw.get("port", 8788), "supervisor.control.port"
    )
    restart_raw = supervisor_raw.get("restart") or {}
    if not isinstance(restart_raw, dict):
        raise ConfigError("supervisor.restart 必须是映射")
    backoff_raw = restart_raw.get("backoff_seconds", [1, 2, 4, 8, 16, 30])
    if not isinstance(backoff_raw, list) or not backoff_raw:
        raise ConfigError("supervisor.restart.backoff_seconds 必须是非空列表")
    backoff = tuple(
        _float(v, "supervisor.restart.backoff_seconds") for v in backoff_raw
    )
    if any(v <= 0 for v in backoff):
        raise ConfigError("supervisor.restart.backoff_seconds 必须全部大于 0")
    max_failures = _int(
        restart_raw.get("max_failures", 5), "supervisor.restart.max_failures"
    )
    if max_failures < 1:
        raise ConfigError("supervisor.restart.max_failures 必须大于等于 1")
    for name, value in (
        ("supervisor.dashboard.port", dash_port),
        ("supervisor.control.port", ctrl_port),
    ):
        if not 1 <= value <= 65535:
            raise ConfigError(f"{name} 必须在 1-65535 之间")
    if dash_port == ctrl_port:
        raise ConfigError("supervisor.dashboard.port 与 supervisor.control.port 不能相同")
    if dash_port == port or ctrl_port == port:
        raise ConfigError("supervisor 端口不能与 opencode.port 相同")

    window_size = _int(memory_raw.get("window_size", 20), "memory.window_size")
    if window_size < 1:
        raise ConfigError("memory.window_size 必须大于等于 1")
    extract_every = _int(memory_raw.get("extract_every", 20), "memory.extract_every")
    if extract_every < 1:
        raise ConfigError("memory.extract_every 必须大于等于 1")

    return Settings(
        api_key=api_key,
        base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        model=env.get("DEEPSEEK_MODEL", "deepseek-chat").strip(),
        reasoning_effort=env.get("DEEPSEEK_REASONING_EFFORT", "").strip(),
        bot_qq=_int(env["BOT_QQ"], "BOT_QQ"),
        ws_host=env.get("NAPCAT_WS_HOST", "127.0.0.1"),
        ws_port=ws_port,
        ws_token=env.get("NAPCAT_WS_TOKEN", ""),
        db_path=env.get("DAFEIYU_DB_PATH", "data/dafeiyu.db"),
        persona=_build_persona(_section(raw, "persona")),
        triggers=_build_triggers(_section(raw, "triggers")),
        groups=GroupsConfig(
            whitelist=_int_set(_section(raw, "groups").get("whitelist"), "groups.whitelist"),
            blacklist=_int_set(_section(raw, "groups").get("blacklist"), "groups.blacklist"),
            admins=_int_set(_section(raw, "groups").get("admins"), "groups.admins"),
        ),
        budget=BudgetConfig(
            daily_calls=_int(budget_raw.get("daily_calls", 200), "budget.daily_calls"),
            daily_tokens=_int(budget_raw.get("daily_tokens", 300000), "budget.daily_tokens"),
        ),
        memory=MemoryConfig(
            window_size=window_size,
            impression_topk=_int(memory_raw.get("impression_topk", 5), "memory.impression_topk"),
            extract_every=extract_every,
        ),
        logging=LoggingConfig(
            level=str(logging_raw.get("level", "INFO")),
            dir=str(logging_raw.get("dir", "logs")),
        ),
        brain=BrainConfig(provider=provider),
        opencode=OpenCodeConfig(
            bin=str(opencode_raw.get("bin") or "").strip(),
            agent=opencode_agent,
            project_dir=str(opencode_raw.get("project_dir") or "").strip(),
            timeout_seconds=opencode_timeout,
            manage_server=manage_server,
            server_url=server_url,
            db_path=db_path,
            host=host,
            port=port,
            startup_timeout_seconds=startup_timeout,
        ),
        local_model=local_model,
        supervisor=SupervisorConfig(
            dashboard=DashboardConfig(
                host=str(dash_raw.get("host") or "127.0.0.1").strip(),
                port=dash_port,
                open_browser=open_browser,
            ),
            control_host=str(ctrl_raw.get("host") or "127.0.0.1").strip(),
            control_port=ctrl_port,
            restart=RestartConfig(
                backoff_seconds=backoff, max_failures=max_failures
            ),
        ),
    )
