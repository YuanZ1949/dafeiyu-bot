from __future__ import annotations

import ipaddress
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
    # rules：旧的同步投递；model：入库后交给批次调度器决定何时回复。
    mode: str = "rules"
    # 点名消息的硬上限：防抖一直被打断时，最多等这么久也必须回。
    addressed_max_wait_seconds: float = 30.0
    # 非点名消息攒多久再回（批次窗口）。
    batch_seconds: float = 45.0
    # 点名后等群里说完再回；每来一条点名消息重新计时，但不突破上面的硬上限。
    addressed_debounce_seconds: float = 8.0
    # 两次非点名发言之间的最小间隔。
    ambient_min_interval_seconds: float = 90.0


@dataclass(frozen=True)
class GroupsConfig:
    whitelist: frozenset[int]
    blacklist: frozenset[int]
    admins: frozenset[int] = frozenset()


@dataclass(frozen=True)
class BudgetConfig:
    daily_calls: int
    daily_tokens: int
    # 非点名消息可动用的额度比例；点名的回复永远优先。
    ambient_ratio: float = 0.8
    # 全局每日上限（所有会话合计）；0 = 未启用。
    global_daily_calls: int = 0
    global_daily_tokens: int = 0
    # 自然月上限（所有会话合计）；0 = 未启用。
    monthly_calls: int = 0
    monthly_tokens: int = 0
    # 月度费用上限；0 = 不限制。单价按百万 token 估算。
    monthly_cost_limit: float = 0.0
    currency: str = "CNY"
    price_input_per_million: float = 0.0
    price_output_per_million: float = 0.0
    # 额度用到这些比例时发预警；升序去重，每项 0 < r <= 1。
    warning_ratios: tuple[float, ...] = (0.5, 0.8, 1.0)
    # 超限动作：degrade = 只停非点名回复；stop = 全部停用；notify = 只告警。
    on_exceed: str = "degrade"


@dataclass(frozen=True)
class MemoryConfig:
    window_size: int
    impression_topk: int
    extract_every: int
    # 0 = 永久保留；>0 时由 retention_loop 每天清理更早的消息。
    retention_days: int = 0
    # 会话轮换阈值（任一触发）；Task 13 才在 config.yaml 里显式暴露。
    rotation_messages: int = 60
    rotation_days: int = 7
    rotation_tokens: int = 40000


@dataclass(frozen=True)
class InjectionConfig:
    """opencode 分支的注入预算，单位是字符数（不是 token）。"""

    memory_chars: int = 500
    summary_chars: int = 250
    impression_chars: int = 150
    inject_summaries: bool = True
    inject_impressions: bool = True
    # 默认关闭：相关旧记忆会改变 prompt，升级后要显式打开。
    recall_enabled: bool = False
    recall_topk: int = 3
    recall_chars: int = 300


@dataclass(frozen=True)
class WorkspaceConfig:
    """长期记忆工作区。``dir`` 留空 = 关闭注入，并给 session 下全量 deny。"""

    dir: str = "data/bot-workspace"
    # 事后检查（不是硬拦截）：超限保留尾部并在首行插截断注释。
    max_file_bytes: int = 65536

    @property
    def enabled(self) -> bool:
        return bool(self.dir.strip())


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    dir: str


@dataclass(frozen=True)
class BrainConfig:
    provider: str = "local"


@dataclass(frozen=True)
class GatewayConfig:
    provider: str = "qq"


@dataclass(frozen=True)
class OpenCodeConfig:
    bin: str = ""
    agent: str = "deepseek-qq"
    project_dir: str = ""
    timeout_seconds: float = 90.0
    manage_server: bool = False
    server_url: str = ""
    # http：直接调 server API（默认，读结构化消息）；cli：spawn `opencode run`。
    transport: str = "http"
    # server 的 Basic 认证密码，用户名固定 opencode；空则读同名的环境变量。
    server_password: str = ""
    db_path: str = "data/opencode-bot.db"
    host: str = "127.0.0.1"
    port: int = 24310
    startup_timeout_seconds: float = 30.0
    # bot 专用 opencode 配置目录；空 = 沿用启动环境的配置（软回滚）。
    config_home: str = ""


@dataclass(frozen=True)
class LocalModelConfig:
    """任意 OpenAI 兼容端点；默认指向本机 LM Studio。"""

    base_url: str = "http://127.0.0.1:1234/v1"
    model: str = ""
    api_key: str = "local"
    # 本地 Qwen3.x 这类思考型模型会先写一大段 reasoning，预算太低会导致
    # finish_reason=length 且 content 为空，群里只能看到兜底话术。
    max_tokens: int = 2048
    # 本地推理比云端慢，15s 默认超时对思考型模型不够。
    timeout_seconds: float = 60.0
    # 思考型本地模型（如 qwen3.5）会把 token 预算全烧在 reasoning 上，
    # 旁路调用（印象/摘要）600 token 内 content 恒空。置 "none" 关掉思考；
    # 留空 = 不发该字段，保持对旧后端的兼容。
    reasoning_effort: str = ""
    # 切到本地模型时若端点没起，自动用 lms CLI 拉起 LM Studio server。
    autostart: bool = True
    # 留空自动找 ~/.lmstudio/bin/lms.exe 或 PATH 里的 lms。
    lms_path: str = ""
    autostart_timeout_seconds: float = 20.0


@dataclass(frozen=True)
class NapCatConfig:
    """NapCat 快登失败时的密码回退。

    QQ 密码是凭据，默认留空：只有 ``-q <uin>`` 快速登录失败时才用得上。
    值从 ``.env``（已 gitignore）或进程环境读，不要写进被 git 跟踪的
    config.yaml；``_md5`` 是 32 位十六进制，优先于明文。
    """

    quick_password: str = ""
    quick_password_md5: str = ""


@dataclass(frozen=True)
class RestartConfig:
    backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
    max_failures: int = 5


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip().strip("[]").casefold()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    open_browser: bool = True
    # auto：loopback 免登录，非 loopback 强制登录；on：始终登录；off：只允许 loopback。
    auth: str = "auto"
    username: str = "admin"
    password: str = field(default="", repr=False)
    session_hours: int = 7 * 24

    @property
    def auth_required(self) -> bool:
        if self.auth == "on":
            return True
        if self.auth == "off":
            return False
        return not is_loopback_host(self.host)


@dataclass(frozen=True)
class SupervisorConfig:
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    control_host: str = "127.0.0.1"
    control_port: int = 8788
    restart: RestartConfig = field(default_factory=RestartConfig)


@dataclass(frozen=True)
class AlertRulesConfig:
    bot_down_seconds: int = 120
    napcat_down_seconds: int = 180
    opencode_down_seconds: int = 120
    quota_pause: bool = True
    disk_free_percent: int = 10
    reply_error_window_seconds: int = 900
    reply_error_count: int = 3
    budget_warning: bool = True


@dataclass(frozen=True)
class WebhookConfig:
    url: str = ""
    format: str = "generic"


@dataclass(frozen=True)
class AlertsConfig:
    enabled: bool = False
    check_seconds: int = 30
    repeat_seconds: int = 21600
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    qq_admin: int = 0
    rules: AlertRulesConfig = field(default_factory=AlertRulesConfig)


@dataclass(frozen=True)
class BackupConfig:
    enabled: bool = True
    dir: str = "data/backups"
    daily_keep: int = 7
    weekly_keep: int = 4
    schedule_hour: int = 4
    max_total_gb: int = 20
    max_upload_mb: int = 512


@dataclass(frozen=True)
class AuditConfig:
    path: str = "data/audit.jsonl"
    max_bytes: int = 10485760
    backups: int = 3


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
    injection: InjectionConfig = field(default_factory=InjectionConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    local_model: LocalModelConfig = field(default_factory=LocalModelConfig)
    napcat: NapCatConfig = field(default_factory=NapCatConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)


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


def _non_negative_int(value: Any, field: str, default: int) -> int:
    got = _int(default if value is None else value, field)
    if got < 0:
        raise ConfigError(f"{field} 必须大于等于 0")
    return got


def _positive_int(value: Any, field: str, default: int) -> int:
    got = _int(default if value is None else value, field)
    if got < 1:
        raise ConfigError(f"{field} 必须大于等于 1")
    return got


def _build_ambient_ratio(raw: dict[str, Any]) -> float:
    ratio = _float(raw.get("ambient_ratio", 0.8), "budget.ambient_ratio")
    if not 0 < ratio <= 1:
        raise ConfigError("budget.ambient_ratio 必须在 (0, 1] 之间")
    return ratio


def _build_warning_ratios(raw: dict[str, Any]) -> tuple[float, ...]:
    values = raw.get("warning_ratios", [0.5, 0.8, 1.0])
    if not isinstance(values, (list, tuple)) or not values:
        raise ConfigError("budget.warning_ratios 必须是非空列表")
    ratios: list[float] = []
    for value in values:
        ratio = _float(value, "budget.warning_ratios")
        if not 0 < ratio <= 1:
            raise ConfigError("budget.warning_ratios 的每一项都必须在 (0, 1] 之间")
        ratios.append(ratio)
    return tuple(sorted(set(ratios)))


def _build_budget(raw: dict[str, Any]) -> BudgetConfig:
    """预算段：daily 是每群硬上限，其余为 0 表示未启用。"""
    daily_calls = _int(raw.get("daily_calls", 200), "budget.daily_calls")
    if daily_calls < 1:
        raise ConfigError("budget.daily_calls 必须大于 0")
    daily_tokens = _int(raw.get("daily_tokens", 300000), "budget.daily_tokens")
    if daily_tokens < 1:
        raise ConfigError("budget.daily_tokens 必须大于 0")

    global_daily_calls = _non_negative_int(
        raw.get("global_daily_calls"), "budget.global_daily_calls", 0
    )
    global_daily_tokens = _non_negative_int(
        raw.get("global_daily_tokens"), "budget.global_daily_tokens", 0
    )
    monthly_calls = _non_negative_int(raw.get("monthly_calls"), "budget.monthly_calls", 0)
    monthly_tokens = _non_negative_int(
        raw.get("monthly_tokens"), "budget.monthly_tokens", 0
    )

    monthly_cost_limit = _float(
        raw.get("monthly_cost_limit", 0.0), "budget.monthly_cost_limit"
    )
    price_input = _float(
        raw.get("price_input_per_million", 0.0), "budget.price_input_per_million"
    )
    price_output = _float(
        raw.get("price_output_per_million", 0.0), "budget.price_output_per_million"
    )
    if monthly_cost_limit < 0:
        raise ConfigError("budget.monthly_cost_limit 必须大于等于 0")
    if price_input < 0:
        raise ConfigError("budget.price_input_per_million 必须大于等于 0")
    if price_output < 0:
        raise ConfigError("budget.price_output_per_million 必须大于等于 0")
    if monthly_cost_limit > 0 and price_input <= 0 and price_output <= 0:
        raise ConfigError(
            "budget.monthly_cost_limit 大于 0 时，"
            "price_input_per_million 与 price_output_per_million 至少要有一个大于 0"
        )

    on_exceed = str(raw.get("on_exceed", "degrade") or "").strip().casefold()
    if on_exceed not in ("degrade", "stop", "notify"):
        raise ConfigError("budget.on_exceed 只能是 degrade、stop 或 notify")

    currency = str(raw.get("currency", "CNY") or "").strip()
    if not currency:
        raise ConfigError("budget.currency 不能为空")

    return BudgetConfig(
        daily_calls=daily_calls,
        daily_tokens=daily_tokens,
        ambient_ratio=_build_ambient_ratio(raw),
        global_daily_calls=global_daily_calls,
        global_daily_tokens=global_daily_tokens,
        monthly_calls=monthly_calls,
        monthly_tokens=monthly_tokens,
        monthly_cost_limit=monthly_cost_limit,
        currency=currency,
        price_input_per_million=price_input,
        price_output_per_million=price_output,
        warning_ratios=_build_warning_ratios(raw),
        on_exceed=on_exceed,
    )


def _build_alerts(raw: dict[str, Any]) -> AlertsConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 的 alerts 必须是映射")
    enabled = _bool(raw.get("enabled", False), "alerts.enabled")
    check_seconds = _int(raw.get("check_seconds", 30), "alerts.check_seconds")
    if not 5 <= check_seconds <= 3600:
        raise ConfigError("alerts.check_seconds 必须在 5-3600 之间")
    repeat_seconds = _int(raw.get("repeat_seconds", 21600), "alerts.repeat_seconds")
    if repeat_seconds < 300:
        raise ConfigError("alerts.repeat_seconds 必须大于等于 300")

    webhook_raw = raw.get("webhook") or {}
    if not isinstance(webhook_raw, dict):
        raise ConfigError("alerts.webhook 必须是映射")
    webhook_format = str(webhook_raw.get("format") or "generic").strip().casefold()
    if webhook_format not in ("generic", "ntfy", "wecom", "dingtalk"):
        raise ConfigError(
            "alerts.webhook.format 只能是 generic、ntfy、wecom 或 dingtalk"
        )
    webhook = WebhookConfig(
        url=str(webhook_raw.get("url") or "").strip(),
        format=webhook_format,
    )

    qq_admin = _non_negative_int(raw.get("qq_admin"), "alerts.qq_admin", 0)
    rules_raw = raw.get("rules") or {}
    if not isinstance(rules_raw, dict):
        raise ConfigError("alerts.rules 必须是映射")
    disk_free_percent = _int(
        rules_raw.get("disk_free_percent", 10), "alerts.rules.disk_free_percent"
    )
    if not 1 <= disk_free_percent <= 50:
        raise ConfigError("alerts.rules.disk_free_percent 必须在 1-50 之间")
    reply_error_window = _positive_int(
        rules_raw.get("reply_error_window_seconds"),
        "alerts.rules.reply_error_window_seconds",
        900,
    )
    if reply_error_window < 60:
        raise ConfigError("alerts.rules.reply_error_window_seconds 必须大于等于 60")
    rules = AlertRulesConfig(
        bot_down_seconds=_positive_int(
            rules_raw.get("bot_down_seconds"), "alerts.rules.bot_down_seconds", 120
        ),
        napcat_down_seconds=_positive_int(
            rules_raw.get("napcat_down_seconds"), "alerts.rules.napcat_down_seconds", 180
        ),
        opencode_down_seconds=_positive_int(
            rules_raw.get("opencode_down_seconds"),
            "alerts.rules.opencode_down_seconds",
            120,
        ),
        quota_pause=_bool(
            rules_raw.get("quota_pause", True), "alerts.rules.quota_pause"
        ),
        disk_free_percent=disk_free_percent,
        reply_error_window_seconds=reply_error_window,
        reply_error_count=_positive_int(
            rules_raw.get("reply_error_count"), "alerts.rules.reply_error_count", 3
        ),
        budget_warning=_bool(
            rules_raw.get("budget_warning", True), "alerts.rules.budget_warning"
        ),
    )
    return AlertsConfig(
        enabled=enabled,
        check_seconds=check_seconds,
        repeat_seconds=repeat_seconds,
        webhook=webhook,
        qq_admin=qq_admin,
        rules=rules,
    )


def _build_backup(raw: dict[str, Any]) -> BackupConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 的 backup 必须是映射")
    backup_dir = str(raw.get("dir", "data/backups") or "").strip()
    if not backup_dir:
        raise ConfigError("backup.dir 不能为空")
    schedule_hour = _int(raw.get("schedule_hour", 4), "backup.schedule_hour")
    if not 0 <= schedule_hour <= 23:
        raise ConfigError("backup.schedule_hour 必须在 0-23 之间")
    max_total_gb = _int(raw.get("max_total_gb", 20), "backup.max_total_gb")
    if not 1 <= max_total_gb <= 1024:
        raise ConfigError("backup.max_total_gb 必须在 1-1024 之间")
    max_upload_mb = _int(raw.get("max_upload_mb", 512), "backup.max_upload_mb")
    if not 1 <= max_upload_mb <= 4096:
        raise ConfigError("backup.max_upload_mb 必须在 1-4096 之间")
    return BackupConfig(
        enabled=_bool(raw.get("enabled", True), "backup.enabled"),
        dir=backup_dir,
        daily_keep=_non_negative_int(
            raw.get("daily_keep"), "backup.daily_keep", 7
        ),
        weekly_keep=_non_negative_int(
            raw.get("weekly_keep"), "backup.weekly_keep", 4
        ),
        schedule_hour=schedule_hour,
        max_total_gb=max_total_gb,
        max_upload_mb=max_upload_mb,
    )


def _build_audit(raw: dict[str, Any]) -> AuditConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 的 audit 必须是映射")
    path = str(raw.get("path", "data/audit.jsonl") or "").strip()
    if not path:
        raise ConfigError("audit.path 不能为空")
    max_bytes = _int(raw.get("max_bytes", 10485760), "audit.max_bytes")
    if max_bytes < 1048576:
        raise ConfigError("audit.max_bytes 必须大于等于 1048576")
    backups = _int(raw.get("backups", 3), "audit.backups")
    if backups < 1:
        raise ConfigError("audit.backups 必须大于等于 1")
    return AuditConfig(path=path, max_bytes=max_bytes, backups=backups)


def _build_workspace(raw: dict[str, Any]) -> WorkspaceConfig:
    return WorkspaceConfig(
        dir=str(raw.get("dir", "data/bot-workspace") or "").strip(),
        max_file_bytes=_positive_int(
            raw.get("max_file_bytes"), "workspace.max_file_bytes", 65536
        ),
    )


def _build_injection(
    workspace_raw: dict[str, Any], memory_raw: dict[str, Any]
) -> InjectionConfig:
    """字符预算在 workspace 段（spec §12），开关在 memory 段。"""
    return InjectionConfig(
        memory_chars=_positive_int(
            workspace_raw.get("memory_chars"), "workspace.memory_chars", 500
        ),
        summary_chars=_positive_int(
            workspace_raw.get("summary_chars"), "workspace.summary_chars", 250
        ),
        impression_chars=_positive_int(
            workspace_raw.get("impression_chars"), "workspace.impression_chars", 150
        ),
        inject_summaries=_bool(memory_raw.get("inject_summaries", True), "memory.inject_summaries"),
        inject_impressions=_bool(
            memory_raw.get("inject_impressions", True), "memory.inject_impressions"
        ),
        recall_enabled=_bool(
            memory_raw.get("recall_enabled", False), "memory.recall_enabled"
        ),
        recall_topk=_positive_int(
            memory_raw.get("recall_topk"), "memory.recall_topk", 3
        ),
        recall_chars=_positive_int(
            memory_raw.get("recall_chars"), "memory.recall_chars", 300
        ),
    )


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field} 必须是布尔值")
    return value


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
    mode = str(raw.get("mode") or "rules").strip().casefold()
    if mode not in ("rules", "model"):
        raise ConfigError("triggers.mode 只能是 rules 或 model")
    batch_seconds = _float(raw.get("batch_seconds", 45), "triggers.batch_seconds")
    addressed_debounce = _float(
        raw.get("addressed_debounce_seconds", 8),
        "triggers.addressed_debounce_seconds",
    )
    addressed_max_wait = _float(
        raw.get("addressed_max_wait_seconds", 30),
        "triggers.addressed_max_wait_seconds",
    )
    ambient_min_interval = _float(
        raw.get("ambient_min_interval_seconds", 90),
        "triggers.ambient_min_interval_seconds",
    )
    for name, value in (
        ("triggers.batch_seconds", batch_seconds),
        ("triggers.addressed_debounce_seconds", addressed_debounce),
        ("triggers.addressed_max_wait_seconds", addressed_max_wait),
        ("triggers.ambient_min_interval_seconds", ambient_min_interval),
    ):
        if value <= 0:
            raise ConfigError(f"{name} 必须大于 0")
    return TriggerConfig(
        cooldown_seconds=_int(raw.get("cooldown_seconds", 60), "triggers.cooldown_seconds"),
        sample_rate=sample_rate,
        quiet_hours=(str(quiet[0]), str(quiet[1])),
        keywords=tuple(str(k) for k in keywords),
        mode=mode,
        batch_seconds=batch_seconds,
        addressed_debounce_seconds=addressed_debounce,
        addressed_max_wait_seconds=addressed_max_wait,
        ambient_min_interval_seconds=ambient_min_interval,
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

    gateway_raw = raw.get("gateway") or {}
    if not isinstance(gateway_raw, dict):
        raise ConfigError("config.yaml 的 gateway 必须是映射")
    gateway_provider = str(gateway_raw.get("provider") or "qq").strip().casefold()
    if gateway_provider not in ("qq", "wechat"):
        raise ConfigError("gateway.provider 只能是 qq 或 wechat")
    if gateway_provider == "wechat":
        raise ConfigError(
            "gateway.provider=wechat 暂不可用：微信接入未实现。可选路径只有三条："
            "1) WeChatPadPro（付费 iPad 协议，LangBot 内置 wechatpad_api）；"
            "2) wechatauto-replica / wxauto（Windows 桌面 RPA，依赖微信窗口常驻）；"
            "3) 继续用 QQ（gateway.provider=qq）。"
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
        max_tokens=_int(local_model_raw.get("max_tokens", 2048), "local_model.max_tokens"),
        timeout_seconds=_float(
            local_model_raw.get("timeout_seconds", 60.0), "local_model.timeout_seconds"
        ),
        reasoning_effort=str(local_model_raw.get("reasoning_effort") or "").strip(),
        autostart=bool(local_model_raw.get("autostart", True)),
        lms_path=str(local_model_raw.get("lms_path") or "").strip(),
        autostart_timeout_seconds=_float(
            local_model_raw.get("autostart_timeout_seconds", 20.0),
            "local_model.autostart_timeout_seconds",
        ),
    )
    if not local_model.base_url:
        raise ConfigError("local_model.base_url 不能为空")
    if local_model.max_tokens < 256:
        raise ConfigError("local_model.max_tokens 必须大于等于 256")
    if local_model.timeout_seconds <= 0:
        raise ConfigError("local_model.timeout_seconds 必须大于 0")
    if local_model.autostart_timeout_seconds <= 0:
        raise ConfigError("local_model.autostart_timeout_seconds 必须大于 0")

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
    opencode_transport = (
        str(opencode_raw.get("transport") or "http").strip().casefold()
    )
    if opencode_transport not in ("http", "cli"):
        raise ConfigError("opencode.transport 只能是 http 或 cli")
    server_password = str(opencode_raw.get("server_password") or "").strip()
    if not server_password:
        server_password = str(env.get("OPENCODE_SERVER_PASSWORD", "")).strip()
    db_path = str(opencode_raw.get("db_path") or "data/opencode-bot.db").strip()
    host = str(opencode_raw.get("host") or "127.0.0.1").strip()
    port = _int(opencode_raw.get("port", 24310), "opencode.port")
    startup_timeout = _float(
        opencode_raw.get("startup_timeout_seconds", 30.0),
        "opencode.startup_timeout_seconds",
    )
    config_home = str(opencode_raw.get("config_home") or "").strip()
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

    napcat_raw = raw.get("napcat") or {}
    if not isinstance(napcat_raw, dict):
        raise ConfigError("config.yaml 的 napcat 必须是映射")
    quick_password = str(napcat_raw.get("quick_password") or "").strip() or str(
        env.get("NAPCAT_QUICK_PASSWORD", "")
    ).strip()
    quick_password_md5 = str(
        napcat_raw.get("quick_password_md5") or ""
    ).strip() or str(env.get("NAPCAT_QUICK_PASSWORD_MD5", "")).strip()
    if quick_password_md5 and (
        len(quick_password_md5) != 32
        or any(c not in "0123456789abcdefABCDEF" for c in quick_password_md5)
    ):
        raise ConfigError("napcat.quick_password_md5 必须是 32 位十六进制")

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
    dash_host = str(dash_raw.get("host") or "127.0.0.1").strip()
    dash_port = _int(dash_raw.get("port", 8787), "supervisor.dashboard.port")
    open_browser = dash_raw.get("open_browser", True)
    if not isinstance(open_browser, bool):
        raise ConfigError("supervisor.dashboard.open_browser 必须是布尔值")
    dash_auth_raw = dash_raw.get("auth", "auto")
    if isinstance(dash_auth_raw, bool):
        # YAML 1.1 会把裸 on/off 解析成布尔值。
        dash_auth_raw = "on" if dash_auth_raw else "off"
    dash_auth = str(dash_auth_raw or "auto").strip().casefold()
    if dash_auth not in ("auto", "on", "off"):
        raise ConfigError("supervisor.dashboard.auth 只能是 auto、on 或 off")
    dash_user = str(env.get("DAFEIYU_DASHBOARD_USER", "admin")).strip() or "admin"
    dash_password = str(env.get("DAFEIYU_DASHBOARD_PASSWORD", "")).strip()
    dash_session_hours = _int(
        env.get("DAFEIYU_DASHBOARD_SESSION_HOURS", 7 * 24),
        "DAFEIYU_DASHBOARD_SESSION_HOURS",
    )
    if not 1 <= dash_session_hours <= 24 * 30:
        raise ConfigError("DAFEIYU_DASHBOARD_SESSION_HOURS 必须在 1-720 之间")
    if dash_auth == "off" and not is_loopback_host(dash_host):
        raise ConfigError(
            "supervisor.dashboard.auth=off 只允许绑定 loopback；"
            "非 loopback 必须开启登录"
        )
    dash_auth_required = dash_auth == "on" or (
        dash_auth == "auto" and not is_loopback_host(dash_host)
    )
    if dash_auth_required and len(dash_password) < 12:
        raise ConfigError(
            "开启 dashboard 登录时 DAFEIYU_DASHBOARD_PASSWORD 至少 12 位"
        )
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

    workspace_raw = raw.get("workspace") or {}
    if not isinstance(workspace_raw, dict):
        raise ConfigError("config.yaml 的 workspace 必须是映射")

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
        budget=_build_budget(budget_raw),
        memory=MemoryConfig(
            window_size=window_size,
            impression_topk=_int(memory_raw.get("impression_topk", 5), "memory.impression_topk"),
            extract_every=extract_every,
            retention_days=_non_negative_int(
                memory_raw.get("retention_days"), "memory.retention_days", 0
            ),
            rotation_messages=_positive_int(
                memory_raw.get("rotation_messages"), "memory.rotation_messages", 60
            ),
            rotation_days=_positive_int(
                memory_raw.get("rotation_days"), "memory.rotation_days", 7
            ),
            rotation_tokens=_positive_int(
                memory_raw.get("rotation_tokens"), "memory.rotation_tokens", 40000
            ),
        ),
        injection=_build_injection(workspace_raw, memory_raw),
        workspace=_build_workspace(workspace_raw),
        logging=LoggingConfig(
            level=str(logging_raw.get("level", "INFO")),
            dir=str(logging_raw.get("dir", "logs")),
        ),
        brain=BrainConfig(provider=provider),
        gateway=GatewayConfig(provider=gateway_provider),
        opencode=OpenCodeConfig(
            bin=str(opencode_raw.get("bin") or "").strip(),
            agent=opencode_agent,
            project_dir=str(opencode_raw.get("project_dir") or "").strip(),
            timeout_seconds=opencode_timeout,
            manage_server=manage_server,
            server_url=server_url,
            transport=opencode_transport,
            server_password=server_password,
            db_path=db_path,
            host=host,
            port=port,
            startup_timeout_seconds=startup_timeout,
            config_home=config_home,
        ),
        local_model=local_model,
        napcat=NapCatConfig(
            quick_password=quick_password,
            quick_password_md5=quick_password_md5,
        ),
        supervisor=SupervisorConfig(
            dashboard=DashboardConfig(
                host=dash_host,
                port=dash_port,
                open_browser=open_browser,
                auth=dash_auth,
                username=dash_user,
                password=dash_password,
                session_hours=dash_session_hours,
            ),
            control_host=str(ctrl_raw.get("host") or "127.0.0.1").strip(),
            control_port=ctrl_port,
            restart=RestartConfig(
                backoff_seconds=backoff, max_failures=max_failures
            ),
        ),
        alerts=_build_alerts(raw.get("alerts") or {}),
        backup=_build_backup(raw.get("backup") or {}),
        audit=_build_audit(raw.get("audit") or {}),
    )
