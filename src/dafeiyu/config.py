from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from .behavior import BehaviorError, BehaviorSettings, parse_behavior
from .settings_local import (
    DEFAULT_PATH,
    VALID_MODES,
    OverrideError,
    normalize_mode,
    read_overrides,
)

# 仪表盘写下的覆盖层；测试与嵌入方可以改写这个模块级路径。
OVERRIDES_PATH = DEFAULT_PATH

# 配置侧的告警只走日志：doctor 需要能加载一份「不完整」的配置来出报告，
# 所以加载阶段不允许因为微信就绪度缺失而抛错（拒绝在 Adapter.start()）。
logger = logging.getLogger(__name__)


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
    # 拆成多条短消息时，每条之间的停顿（秒）。
    segment_delay_seconds: float = 0.8
    # 重复守卫：同一句话最近说过就改写/换中性短句/不说。
    repeat_guard: bool = True
    # 机会检测（规则版）：纯热聊/纯表情批次先攒着不投，等点名或问句再一起送。
    opportunity_guard: bool = True
    # 引用回复：被点名/被回复时把回复挂在原消息下；关掉后发送参数与无引用时一致。
    quote_reply: bool = True
    # 长度安全阀：软目标（max_reply_chars*）只写进提示词，真正发送前只守这两条
    # 硬边界——整轮总字数与单条字数。超出才截断，不再按软目标砍尾。
    reply_hard_total: int = 2000
    reply_hard_part: int = 500


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
    # 非点名消息攒多久再回（批次窗口）。12s：再长就像她没在听。
    batch_seconds: float = 12.0
    # 点名后等群里说完再回；每来一条点名消息重新计时，但不突破上面的硬上限。
    addressed_debounce_seconds: float = 8.0
    # 两次非点名发言之间的最小间隔。
    ambient_min_interval_seconds: float = 15.0
    # 同一话题里连续 ambient 发言达到这个次数就收尾，等被点名再回来。
    ambient_max_turns: int = 2
    # bot 说完后这么久没人接，视为话题结束。
    thread_idle_seconds: int = 300
    # 话题结束后，这么久内不再主动加入；被点名会立刻重开。
    thread_closed_seconds: int = 600
    # 已废弃：话题收尾不再抽样放行（全部交给模型判断）。
    # 字段保留只为兼容老配置；解析与校验不变，运行链路不再读取它。
    closed_sample_rate: float = 0.05


@dataclass(frozen=True)
class GroupsConfig:
    # 跨通道的会话/用户 id 一律带通道前缀（`qq:123456` / `wxg:0d5c...`）。
    whitelist: frozenset[str]
    blacklist: frozenset[str]
    admins: frozenset[str] = frozenset()


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
    retention_days: int = 180
    # 会话轮换阈值（任一触发）；Task 13 才在 config.yaml 里显式暴露。
    rotation_messages: int = 60
    rotation_days: int = 7
    rotation_tokens: int = 40000


@dataclass(frozen=True)
class MindConfig:
    """心智层总闸（spec §10）：默认全关，灰度打开即软回滚。

    ``mind:`` 段是可选的：老配置没有这一段也要能启动，所以用
    ``raw.get("mind") or {}`` 读取，不能走 ``_section``。
    """

    enabled: bool = False
    state_enabled: bool = True
    episodes_enabled: bool = True
    # Observer/Reflector 需要能调模型；统一复用当前主脑（dashboard 切档后跟随）。
    observer_enabled: bool = False
    observer_interval_seconds: int = 600
    observer_min_messages: int = 8
    # 单群每天观察/自省总调用上限，与 reply 预算分开。
    observer_daily_calls: int = 24
    reflection_hour: int = 5
    # QQ 表情回应：默认关闭；打开后仅在发送成功后尝试调用，失败只记日志。
    # emoji id 的具体取值需按 NapCat/QQ 版本验证，先留成配置项。
    emoji_reactions: bool = False
    emoji_reaction_id: str = "128"
    # 跨群立场（P3）：Reflector 每晚写入，注入还受 mind.enabled 总闸控制。
    stances_enabled: bool = True
    # 入站语音/图片先贴个表情回应（P3）：默认关闭，避免对每条媒体消息都贴。
    inbound_media_reactions: bool = False
    # 同一会话两次入站反应的最小间隔（秒），防止刷屏。
    inbound_media_reaction_cooldown_seconds: int = 600
    # 承诺跟进（P3）：对「答应过但没下文」的 promise loop 主动追问一句。
    # 默认关闭；打开后仍有每日上限与静音/预算门控，且会话里已有新的 bot
    # 发言时不打断（对话还在继续）。
    proactive_followup: bool = False
    # 只有创建超过这么多小时、且之后没有 bot 发言的 loop 才会被跟进。
    followup_after_hours: int = 24
    # 同一会话每天最多追问几次，按上海日期计。
    followup_daily_cap: int = 3


@dataclass(frozen=True)
class SocialConfig:
    """社交动作（戳一戳/进群欢迎）：短句与模板都是确定性的，不调用模型。

    ``social:`` 段是可选的：老配置没有这一段也要能启动，所以用
    ``raw.get("social") or {}`` 读取，不能走 ``_section``。
    """

    # 被戳时回一句固定短句；默认关闭，避免升级后突然开始主动说话。
    poke_enabled: bool = False
    # 同一会话两次回应之间的最小间隔（秒）。
    poke_cooldown_seconds: int = 300
    # 同一会话每日最多回应次数，按上海日期计。
    poke_daily_cap: int = 20
    # 进群欢迎：模板为空串时即使开关打开也不发。
    join_welcome_enabled: bool = False
    join_welcome: str = ""
    # 同一会话每日最多欢迎次数。
    join_welcome_daily_cap: int = 10


@dataclass(frozen=True)
class InjectionConfig:
    """opencode 分支的注入预算，单位是字符数（不是 token）。"""

    memory_chars: int = 500
    summary_chars: int = 375
    impression_chars: int = 150
    inject_summaries: bool = True
    inject_impressions: bool = True
    # 「此刻」：时间感 + 群名 + 距上次开口 + 上一句是谁在说。默认开。
    scene_enabled: bool = True
    # 默认关闭：相关旧记忆会改变 prompt，升级后要显式打开。
    recall_enabled: bool = False
    recall_topk: int = 3
    recall_chars: int = 300
    # 「没有下文的事」/「正在聊的事」：数据由 Observer 写，注入默认关，
    # 等 Observer 真跑起来再开，免得空表也占一段 prompt。
    inject_open_loops: bool = False
    inject_episodes: bool = False
    open_loop_topk: int = 3
    open_loop_chars: int = 200
    episode_chars: int = 220
    # 「正在聊的事 + 没有下文的事 + 你和他 + 本鱼的近况 + 我的立场 + 群友印象」
    # 六段合计的字符预算（默认 1000，装得下六段上限 990）；超了按
    # 「印象 → 立场 → 近况 → 关系卡 → 未了事」整段丢弃，正在聊的事永不主动丢，
    # 只在最后截断（spec §5.5）。
    mind_chars: int = 1000
    # 「本鱼的近况」（bot_state）与「你和他」（关系卡）：只有 mind.enabled
    # 打开后才由 _build_injection 置 true，直接构造 InjectionConfig 时默认关。
    inject_state: bool = False
    state_chars: int = 100
    inject_relations: bool = False
    relation_chars: int = 160
    # 「我的立场」（跨群共享，P3）：只有 mind 总闸 + stances_enabled 同时打开才注入。
    inject_stances: bool = False
    stances_topk: int = 3
    stances_chars: int = 160
    # 群气氛：确定性统计出来的一行观察，拼进「此刻」；默认关，先灰度。
    atmosphere_enabled: bool = False


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
class ImagesConfig:
    """QQ 图片消息物化：落盘到 workspace.dir/images 并把路径写进 content。

    ``retention_days`` 为 0 表示不清理（与 memory.retention_days 语义一致）。
    """

    enabled: bool = True
    max_bytes: int = 10 * 1024 * 1024
    timeout_seconds: float = 8.0
    retention_days: int = 7
    #: 入站图片最长边上限；超过则用 ffmpeg 等比缩小。0 = 不降采样。
    max_edge: int = 1280


@dataclass(frozen=True)
class SpeechConfig:
    """入站语音转写（faster-whisper）。默认关闭，关掉时不下载、不转写。"""

    enabled: bool = False
    model: str = "base"
    language: str = "zh"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1
    max_seconds: int = 60
    timeout_seconds: float = 90.0
    concurrency: int = 1
    max_bytes: int = 20 * 1024 * 1024
    #: faster-whisper 的 initial_prompt：引导输出简体中文；空串 = 不传。
    initial_prompt: str = "以下是普通话的句子，请用简体中文转写。"
    #: 空 = 不设置 HF_ENDPOINT（直连 huggingface.co）；国内建议填镜像。
    hf_endpoint: str = ""
    #: 出站语音合成（edge-tts）。默认关闭：启用后回复文本会发往云端 TTS。
    tts_enabled: bool = False
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_rate: str = "+0%"
    tts_max_chars: int = 200


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    dir: str


@dataclass(frozen=True)
class BrainConfig:
    provider: str = "local"
    # 用户可见的三档：free / key / local_model；provider 是底层实现。
    mode: str = "free"
    # key 档的端点与模型；空 = 未配置，由后续 provider 生成逻辑兜底。
    key_base_url: str = ""
    key_model: str = ""
    # 覆盖层显式给了 brain.mode 时为 True：runtime.json 里的旧 provider 不再生效。
    mode_from_overrides: bool = False


@dataclass(frozen=True)
class GatewayConfig:
    provider: str = "qq"


@dataclass(frozen=True)
class WeChatMediaConfig:
    """微信入站媒体落盘（todo 11/12）。默认关闭，关闭时本地库行完全不变。"""

    enabled: bool = False
    max_bytes: int = 10 * 1024 * 1024
    #: 空 = 自动探测（本地库镜像的 db_root）；配置后只接受该根目录内的文件。
    storage_root: str = ""


@dataclass(frozen=True)
class WeChatConfig:
    """桌面微信小号通道（UIA 桥）的配置。

    注意这里只有人可读的 `account_nickname`（小号在群里的微信显示昵称），
    **没有**会话别名：alias 由桥本地铸造（task 14），两者混用会让 `at_bot`
    退化成「任何 @ 都算」。`bridge_token` 只从 env 读，且不进 repr：配置对象
    常被打印进日志/异常文本。
    """

    enabled: bool = False
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8790
    bridge_token: str = field(default="", repr=False)
    account_nickname: str = ""
    # 机器人自己的微信 wxid（对应 helper 的 wechat_account / --account）。
    # 空 = 不绑定：仅登录一个微信时 helper 能自动识别；同时登录多个账号
    # 必须显式绑定，否则 hook 启动报「多个微信账号同时登录」。
    account: str = ""
    poll_seconds: float = 8.0
    # 未读门控：先用侧边栏红点判断「有没有新消息」，只打开有未读的白名单群。
    # 免打扰会话不计红点，所以每 full_sweep_seconds 仍做一次全量轮询兜底。
    unread_gate: bool = True
    full_sweep_seconds: float = 120.0
    # 本地消息库（只读解密镜像）：开启后入站直接读微信自己的 SQLite
    # （contact.db + message_N.db），不再靠 UIA 开窗抓屏；发送仍然走 UIA。
    # 失败一律 fail-open 回落到 UIA 轮询，不会让入站彻底停摆。
    local_db_enabled: bool = False
    # 空 = 自动探测 %USERPROFILE%\Documents\xwechat_files\*\db_storage。
    local_db_root: str = ""
    # 镜像同步的最小间隔（秒）；0 = 每轮都同步。
    local_db_refresh_seconds: float = 1.0
    # 微信私聊：默认关闭。开启后本地库会把好友私聊也纳入入站轮询
    # （`private_enabled`），`private_reply` 再决定 bot 要不要自动回。
    # 两个开关分开是为了支持「先只看不回」的灰度阶段。
    private_enabled: bool = False
    private_reply: bool = False
    # 私聊白/黑名单（P1-5）：元素是 `wxg:<alias>` 稳定会话 id；deny 优先于
    # allow，allow 为空 = 不限制（仍受 private_enabled 与 groups.whitelist 约束）。
    private_allow: frozenset[str] = frozenset()
    private_deny: frozenset[str] = frozenset()
    # 出站后端：uia = 桌面自动化（现状，锁屏不可用）；hook = HTTP 注入（不依赖桌面，
    # 需配 hook_url，群 username 从本地库解析）；auto = 先 hook，只有明确「没发出去」
    # 时才回落 UIA（HTTP 超时这类结果不明的情况不回落，避免重复发送）。
    outbound: str = "uia"
    # hook 后端地址（POST JSON）；outbound=hook/auto 时必填，启动时校验。
    hook_url: str = ""
    # 可选的 @ / 媒体端点；留空时按 hook_url 的末段推导（.../send_text →
    # .../send_at、.../send_media），推导不出来就明确回 unsupported_at/media。
    hook_at_url: str = ""
    hook_media_url: str = ""
    hook_timeout_seconds: float = 5.0
    # 发送后回读本地库里自己刚发的消息做真实送达确认（需要 local_db_enabled）。
    # 未确认只把回执降级为 typed + receipt_unverified，绝不诱发客户端重发。
    send_verify: bool = True
    send_verify_timeout_seconds: float = 8.0
    send_verify_interval_seconds: float = 0.5
    send_min_interval_seconds: float = 0.0
    send_max_per_hour: int = 0
    send_max_per_day: int = 0
    quiet_hours: tuple[str, str] = ("00:00", "00:00")
    typing_delay_seconds: tuple[float, float] = (0.6, 1.2)
    # 严格回执：true 时未在本地库观察到发送结果会标记 `receipt_unverified`
    # （仍 ok=True，避免客户端换 id 重发造成重复消息）；false 时只写日志，
    # 回执保持普通 typed。
    ack_requires_observed: bool = True
    echo_window_seconds: float = 120.0
    keepalive: bool = False
    require_foreground_idle: bool = True
    kill_switch_file: str = "data/wechat.stop"
    version_min: str = "4.1.6"
    language: str = "zh-CN"
    allow_test_inject: bool = False
    # 追加在最后：位置参数构造与旧测试保持不变。
    media: WeChatMediaConfig = field(default_factory=WeChatMediaConfig)
    # bot 自己的其他昵称（群昵称、别名）：@ 与引用回复判定用。启动时会把
    # `persona.aliases` 并进来，所以这里通常不用手写全。
    self_nicknames: tuple[str, ...] = ()

    @property
    def outbound_requires_desktop(self) -> bool:
        """出站是否要求桌面可交互（客户端发送闸门用）。

        ``hook`` / ``auto`` 的后端不依赖 UIA，锁屏时仍应允许下发；实际失败由桥
        带回 error_code，而不是在客户端被 ``desktop != interactive`` 直接拦掉。
        """
        return self.outbound == "uia"


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
    # 面板写入的上下文窗口；0 = 未覆盖，由调用点用自己的默认值。
    context: int = 0
    # 精简模式（少注入、少旁路调用）；False = 未覆盖。
    lean: bool = False
    # 本地推理比云端慢，15s 默认超时对思考型模型不够。
    timeout_seconds: float = 60.0
    # 思考型本地模型（如 qwen3.5）会把 token 预算全烧在 reasoning 上，
    # 旁路调用（印象/摘要）600 token 内 content 恒空。置 "none" 关掉思考；
    # 留空 = 不发该字段，保持对旧后端的兼容。
    reasoning_effort: str = ""
    # 切到本地模型时若端点没起，自动用 lms CLI 拉起 LM Studio server。
    autostart: bool = True
    # 切走本地模型后自动停掉 LM Studio（lms server stop），释放显存/内存。
    stop_when_unused: bool = True
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
    # 每 N 秒给机器人自己发一条私聊做保活；0 = 关闭。
    keepalive_seconds: float = 300.0


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
    # 默认对局域网开放；非 loopback 绑定会强制登录（见 auth_required）。
    host: str = "0.0.0.0"
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
    wechat_down_seconds: int = 180
    opencode_down_seconds: int = 120
    quota_pause: bool = True
    disk_free_percent: int = 10
    reply_error_window_seconds: int = 900
    reply_error_count: int = 3
    budget_warning: bool = True
    # 备份超过这么多小时没有成功一次就告警；backup.enabled=false 时豁免。
    backup_stale_hours: int = 25
    # 记忆摘要缺口达到这么多天（pending_count）就告警；额度熔断时豁免。
    memory_gap_days: int = 2
    # 旁路任务（摘要/印象/观察/反思）在窗口内失败这么多次就告警；额度熔断时豁免。
    memory_error_count: int = 3
    memory_error_window_seconds: int = 1800


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
    # 异地镜像目录：非空时每份成功备份再复制一份过去（空 = 关闭）。
    mirror_dir: str = ""


@dataclass(frozen=True)
class AuditConfig:
    path: str = "data/audit.jsonl"
    max_bytes: int = 10485760
    backups: int = 3


@dataclass(frozen=True)
class MirrorPairConfig:
    """一对需要双向镜像的群：QQ 群 id 与微信会话 alias。"""

    qq: str
    wechat: str


@dataclass(frozen=True)
class MirrorConfig:
    """跨群镜像开关。默认关闭；启用时必须恰好一对群且微信通道已去节流。"""

    enabled: bool = False
    pairs: tuple[MirrorPairConfig, ...] = ()


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
    images: ImagesConfig = field(default_factory=ImagesConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    wechat: WeChatConfig = field(default_factory=WeChatConfig)
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    local_model: LocalModelConfig = field(default_factory=LocalModelConfig)
    napcat: NapCatConfig = field(default_factory=NapCatConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    # 面板可改的运行时行为；默认值等价于当前线上行为。
    behavior: BehaviorSettings = field(default_factory=BehaviorSettings)
    # 心智层（Observer/Reflector/状态/关系卡）总闸；默认全关，老配置无感。
    mind: MindConfig = field(default_factory=MindConfig)
    # 社交动作（戳一戳/进群欢迎）：确定性的短句与模板，默认全关。
    social: SocialConfig = field(default_factory=SocialConfig)
    # 跨群镜像（QQ ↔ 微信群）：默认关闭。追加在最后，保持位置参数兼容。
    mirror: MirrorConfig = field(default_factory=MirrorConfig)


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
        wechat_down_seconds=_positive_int(
            rules_raw.get("wechat_down_seconds"), "alerts.rules.wechat_down_seconds", 180
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
        backup_stale_hours=_positive_int(
            rules_raw.get("backup_stale_hours"),
            "alerts.rules.backup_stale_hours",
            25,
        ),
        memory_gap_days=_positive_int(
            rules_raw.get("memory_gap_days"), "alerts.rules.memory_gap_days", 2
        ),
        memory_error_count=_positive_int(
            rules_raw.get("memory_error_count"),
            "alerts.rules.memory_error_count",
            3,
        ),
        memory_error_window_seconds=_positive_int(
            rules_raw.get("memory_error_window_seconds"),
            "alerts.rules.memory_error_window_seconds",
            1800,
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
        mirror_dir=str(raw.get("mirror_dir", "") or "").strip(),
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


def _build_images(raw: dict[str, Any]) -> ImagesConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 的 images 必须是映射")
    timeout = _float(raw.get("timeout_seconds", 8.0), "images.timeout_seconds")
    if timeout <= 0:
        raise ConfigError("images.timeout_seconds 必须大于 0")
    return ImagesConfig(
        enabled=bool(raw.get("enabled", True)),
        max_bytes=_positive_int(
            raw.get("max_bytes"), "images.max_bytes", 10 * 1024 * 1024
        ),
        timeout_seconds=timeout,
        retention_days=_non_negative_int(
            raw.get("retention_days"), "images.retention_days", 7
        ),
        max_edge=_non_negative_int(
            raw.get("max_edge"), "images.max_edge", 1280
        ),
    )


def _speech_text(
    raw: dict[str, Any], name: str, default: str
) -> str:
    """字符串字段：strip 后为空就用默认值。"""
    value = raw.get(name, default)
    text = str(value).strip() if value is not None else ""
    return text or default


def _build_speech(raw: dict[str, Any]) -> SpeechConfig:
    """解析可选的 ``speech:`` 段；缺段 = 全默认（enabled=false）。"""
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 的 speech 必须是映射")
    timeout = _float(raw.get("timeout_seconds", 90.0), "speech.timeout_seconds")
    if timeout <= 0:
        raise ConfigError("speech.timeout_seconds 必须大于 0")
    return SpeechConfig(
        enabled=_bool(raw.get("enabled", False), "speech.enabled"),
        model=_speech_text(raw, "model", "base"),
        language=_speech_text(raw, "language", "zh"),
        device=_speech_text(raw, "device", "cpu"),
        compute_type=_speech_text(raw, "compute_type", "int8"),
        beam_size=_positive_int(raw.get("beam_size"), "speech.beam_size", 1),
        max_seconds=_positive_int(raw.get("max_seconds"), "speech.max_seconds", 60),
        timeout_seconds=timeout,
        concurrency=_positive_int(raw.get("concurrency"), "speech.concurrency", 1),
        max_bytes=_positive_int(
            raw.get("max_bytes"), "speech.max_bytes", 20 * 1024 * 1024
        ),
        initial_prompt=_speech_text(
            raw,
            "initial_prompt",
            "以下是普通话的句子，请用简体中文转写。",
        ),
        hf_endpoint=_speech_text(raw, "hf_endpoint", ""),
        tts_enabled=_bool(raw.get("tts_enabled", False), "speech.tts_enabled"),
        tts_voice=_speech_text(raw, "tts_voice", "zh-CN-XiaoxiaoNeural"),
        tts_rate=_speech_text(raw, "tts_rate", "+0%"),
        tts_max_chars=_positive_int(
            raw.get("tts_max_chars"), "speech.tts_max_chars", 200
        ),
    )


def _build_mind(raw: dict[str, Any]) -> MindConfig:
    """解析可选的 ``mind:`` 段；缺段 = 全默认（enabled=false），老配置不受影响。"""
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 的 mind 必须是映射")
    if "observer_provider" in raw:
        # 旧配置可能还留着这个键：统一走主脑后它不再有任何作用。
        logger.warning(
            "mind.observer_provider 已废弃并被忽略：Observer/Reflector 统一使用当前主脑"
        )
    hour = _int(raw.get("reflection_hour", 5), "mind.reflection_hour")
    if not 0 <= hour <= 23:
        raise ConfigError("mind.reflection_hour 必须在 0-23 之间")
    return MindConfig(
        enabled=_bool(raw.get("enabled", False), "mind.enabled"),
        state_enabled=_bool(raw.get("state_enabled", True), "mind.state_enabled"),
        episodes_enabled=_bool(
            raw.get("episodes_enabled", True), "mind.episodes_enabled"
        ),
        observer_enabled=_bool(
            raw.get("observer_enabled", False), "mind.observer_enabled"
        ),
        observer_interval_seconds=_positive_int(
            raw.get("observer_interval_seconds"),
            "mind.observer_interval_seconds",
            600,
        ),
        observer_min_messages=_positive_int(
            raw.get("observer_min_messages"), "mind.observer_min_messages", 8
        ),
        observer_daily_calls=_non_negative_int(
            raw.get("observer_daily_calls"), "mind.observer_daily_calls", 24
        ),
        reflection_hour=hour,
        emoji_reactions=_bool(
            raw.get("emoji_reactions", False), "mind.emoji_reactions"
        ),
        emoji_reaction_id=str(raw.get("emoji_reaction_id") or "128").strip() or "128",
        stances_enabled=_bool(
            raw.get("stances_enabled", True), "mind.stances_enabled"
        ),
        inbound_media_reactions=_bool(
            raw.get("inbound_media_reactions", False),
            "mind.inbound_media_reactions",
        ),
        inbound_media_reaction_cooldown_seconds=_non_negative_int(
            raw.get("inbound_media_reaction_cooldown_seconds"),
            "mind.inbound_media_reaction_cooldown_seconds",
            600,
        ),
        proactive_followup=_bool(
            raw.get("proactive_followup", False), "mind.proactive_followup"
        ),
        followup_after_hours=_positive_int(
            raw.get("followup_after_hours"), "mind.followup_after_hours", 24
        ),
        followup_daily_cap=_positive_int(
            raw.get("followup_daily_cap"), "mind.followup_daily_cap", 3
        ),
    )


def _build_social(raw: dict[str, Any]) -> SocialConfig:
    """解析可选的 ``social:`` 段；缺段 = 全默认（戳一戳关闭），老配置不受影响。"""
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 的 social 必须是映射")
    return SocialConfig(
        poke_enabled=_bool(raw.get("poke_enabled", False), "social.poke_enabled"),
        poke_cooldown_seconds=_positive_int(
            raw.get("poke_cooldown_seconds"), "social.poke_cooldown_seconds", 300
        ),
        poke_daily_cap=_positive_int(
            raw.get("poke_daily_cap"), "social.poke_daily_cap", 20
        ),
        join_welcome_enabled=_bool(
            raw.get("join_welcome_enabled", False), "social.join_welcome_enabled"
        ),
        join_welcome=str(raw.get("join_welcome") or "").strip(),
        join_welcome_daily_cap=_positive_int(
            raw.get("join_welcome_daily_cap"), "social.join_welcome_daily_cap", 10
        ),
    )


def _build_mirror(
    raw_mirror: object,
    raw_gateway_provider: str,
    raw_wechat_enabled: bool,
    wechat: WeChatConfig,
) -> MirrorConfig:
    """解析 `mirror` 段；关闭时不校验 pairs，启用时做启动期硬校验。

    校验必须拿到降级前的 `gateway.provider` 与 `gateway.wechat.enabled`：
    启用镜像却只有 QQ 通道时，晚到的 downgrade 会把 provider 改成 qq，
    这里就无法报出「需要 both」。
    """
    if raw_mirror is None:
        return MirrorConfig()
    if not isinstance(raw_mirror, dict):
        raise ConfigError("config.yaml 的 mirror 必须是映射")

    enabled = _bool(raw_mirror.get("enabled", False), "mirror.enabled")
    if not enabled:
        return MirrorConfig()

    raw_pairs = raw_mirror.get("pairs")
    if not isinstance(raw_pairs, list):
        raise ConfigError("mirror.pairs 必须是列表")
    if len(raw_pairs) != 1:
        raise ConfigError("mirror.pairs 必须恰好包含一对群（qq/wechat）")

    item = raw_pairs[0]
    if not isinstance(item, dict):
        raise ConfigError("mirror.pairs[0] 必须是包含 qq/wechat 的映射")
    unknown = sorted((str(key) for key in item if key not in ("qq", "wechat")))
    if unknown:
        raise ConfigError(f"mirror.pairs[0] 包含未知键：{', '.join(unknown)}")

    raw_qq = item.get("qq")
    if not isinstance(raw_qq, str):
        raise ConfigError("mirror.pairs[0].qq 必须是 qq: 前缀的字符串")
    qq = raw_qq.strip()
    if not qq.casefold().startswith("qq:"):
        raise ConfigError("mirror.pairs[0].qq 必须以 qq: 开头")
    qq_id = qq[3:].strip()
    if not qq_id:
        raise ConfigError("mirror.pairs[0].qq 缺少冒号后的群号")

    raw_wechat = item.get("wechat")
    if not isinstance(raw_wechat, str):
        raise ConfigError("mirror.pairs[0].wechat 必须是 wxg: 前缀的字符串")
    wechat_id = raw_wechat.strip()
    if not wechat_id.casefold().startswith("wxg:"):
        raise ConfigError("mirror.pairs[0].wechat 必须以 wxg: 开头")
    alias = wechat_id[4:].strip()
    if not alias:
        raise ConfigError("mirror.pairs[0].wechat 缺少冒号后的 alias")

    # 先报微信通道本身缺失，再报 provider：两者的修复动作不同，先报更直接的那个。
    if not raw_wechat_enabled:
        raise ConfigError("mirror.enabled=true 要求 gateway.wechat.enabled=true")
    if raw_gateway_provider != "both":
        raise ConfigError("mirror.enabled=true 要求 gateway.provider=both")

    detail = (
        f"send_min_interval_seconds={wechat.send_min_interval_seconds}（目标 0）、"
        f"send_max_per_hour={wechat.send_max_per_hour}（目标 0）、"
        f"send_max_per_day={wechat.send_max_per_day}（目标 0）、"
        f"quiet_hours={wechat.quiet_hours}（目标起止相同）"
    )
    if (
        wechat.send_min_interval_seconds != 0
        or wechat.send_max_per_hour != 0
        or wechat.send_max_per_day != 0
        or wechat.quiet_hours[0] != wechat.quiet_hours[1]
    ):
        raise ConfigError(
            "mirror.enabled=true 要求微信通道已去节流（gateway.wechat）：" + detail
        )

    pair = MirrorPairConfig(
        qq=f"qq:{qq_id.casefold()}",
        wechat=f"wxg:{alias.casefold()}",
    )
    return MirrorConfig(enabled=True, pairs=(pair,))


def _mirror_capability(on: bool, reasons: list[str]) -> str:
    return "on" if on else f"off({','.join(reasons)})"


def _mirror_file_capability(on: bool, reasons: list[str]) -> str:
    note = "qq-human-inbound-file-no-content"
    if on:
        return f"on({note})"
    return f"off({','.join([*reasons, note])})"


def _log_mirror_capabilities(
    mirror: MirrorConfig,
    wechat: WeChatConfig,
    images: ImagesConfig,
    workspace: WorkspaceConfig,
    speech: SpeechConfig,
) -> None:
    """打印每个方向的能力矩阵；只由配置推导，不做任何网络/桥探测。"""
    if not mirror.enabled:
        return

    outbound_reason = ""
    if wechat.outbound == "uia":
        outbound_reason = "outbound=uia"
    elif (
        wechat.outbound == "auto"
        and not wechat.hook_url
        and not wechat.hook_media_url
    ):
        outbound_reason = "outbound=auto"

    inbound_reason = "" if images.enabled and workspace.enabled else "images/workspace"
    qq_media_reasons = [reason for reason in (outbound_reason, inbound_reason) if reason]

    image_reasons = list(qq_media_reasons)
    voice_reasons = list(qq_media_reasons)
    if not speech.enabled:
        voice_reasons.append("speech")

    sticker_reasons = ["no-content"]
    if outbound_reason:
        sticker_reasons.insert(0, outbound_reason)

    qq_to_wechat = [
        ("text", "on"),
        ("image", _mirror_capability(not image_reasons, image_reasons)),
        ("voice", _mirror_capability(not voice_reasons, voice_reasons)),
        ("video", "off(wechat-hook-no-video)"),
        (
            "file",
            _mirror_file_capability(not outbound_reason, qq_media_reasons),
        ),
        ("sticker", _mirror_capability(False, sticker_reasons)),
        ("forward", "on"),
    ]

    wechat_media_reason = (
        [] if wechat.local_db_enabled and wechat.media.enabled else ["localdb/media"]
    )
    wechat_to_qq = [
        ("text", "on"),
        ("image", _mirror_capability(not wechat_media_reason, wechat_media_reason)),
        ("voice", _mirror_capability(not wechat_media_reason, wechat_media_reason)),
        ("video", _mirror_capability(not wechat_media_reason, wechat_media_reason)),
        ("file", _mirror_capability(not wechat_media_reason, wechat_media_reason)),
        ("forward", "on"),
    ]

    for direction, capabilities in (
        ("qq->wechat", qq_to_wechat),
        ("wechat->qq", wechat_to_qq),
    ):
        for name, state in capabilities:
            if state.startswith("off("):
                logger.warning("mirror capability %s %s=%s", direction, name, state)
        logger.info(
            "mirror capability %s %s",
            direction,
            " ".join(f"{name}={state}" for name, state in capabilities),
        )


def _build_injection(
    workspace_raw: dict[str, Any],
    memory_raw: dict[str, Any],
    mind: MindConfig | None = None,
) -> InjectionConfig:
    """字符预算在 workspace 段（spec §12），开关在 memory 段。"""
    mind = mind or MindConfig()
    return InjectionConfig(
        memory_chars=_positive_int(
            workspace_raw.get("memory_chars"), "workspace.memory_chars", 500
        ),
        summary_chars=_positive_int(
            workspace_raw.get("summary_chars"), "workspace.summary_chars", 375
        ),
        impression_chars=_positive_int(
            workspace_raw.get("impression_chars"), "workspace.impression_chars", 150
        ),
        inject_summaries=_bool(memory_raw.get("inject_summaries", True), "memory.inject_summaries"),
        inject_impressions=_bool(
            memory_raw.get("inject_impressions", True), "memory.inject_impressions"
        ),
        scene_enabled=_bool(
            memory_raw.get("scene_injection", True), "memory.scene_injection"
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
        inject_open_loops=_bool(
            memory_raw.get("inject_open_loops", False), "memory.inject_open_loops"
        ),
        inject_episodes=_bool(
            memory_raw.get("inject_episodes", False), "memory.inject_episodes"
        ),
        open_loop_topk=_positive_int(
            memory_raw.get("open_loop_topk"), "memory.open_loop_topk", 3
        ),
        open_loop_chars=_positive_int(
            memory_raw.get("open_loop_chars"), "memory.open_loop_chars", 200
        ),
        episode_chars=_positive_int(
            memory_raw.get("episode_chars"), "memory.episode_chars", 220
        ),
        mind_chars=_positive_int(
            memory_raw.get("mind_chars", 1000), "memory.mind_chars", 1000
        ),
        # 心智段只在总闸打开时生效：老配置/直接构造 InjectionConfig 都不改变行为。
        inject_state=mind.enabled and mind.state_enabled,
        state_chars=_positive_int(
            memory_raw.get("state_chars"), "memory.state_chars", 100
        ),
        inject_relations=mind.enabled,
        relation_chars=_positive_int(
            memory_raw.get("relation_chars"), "memory.relation_chars", 160
        ),
        inject_stances=mind.enabled and mind.stances_enabled,
        stances_topk=_positive_int(
            memory_raw.get("stance_topk"), "memory.stance_topk", 3
        ),
        stances_chars=_positive_int(
            memory_raw.get("stance_chars"), "memory.stance_chars", 160
        ),
        atmosphere_enabled=_bool(
            memory_raw.get("atmosphere_enabled", False), "memory.atmosphere_enabled"
        ),
    )


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field} 必须是布尔值")
    return value


_NUMERIC_ID = re.compile(r"^[0-9]+$")
_ID_PREFIXES = ("qq:", "wxg:")
# 微信个人账号 id：自动生成的是 wxid_xxx，自定义微信号是字母开头的 6-20 位。
# 这里只做「像不像一个账号」的粗校验，真正的账号归属由 helper 核对。
_WECHAT_ACCOUNT = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,63}$")
_QUIET_HOUR = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_HOOK_URL = re.compile(r"^https?://", re.IGNORECASE)


def parse_group_ids(value: Any, field: str) -> frozenset[str]:
    """跨通道 id 集合：纯数字补 `qq:` 前缀，带前缀的原样保留，统一 casefold。

    面板保存群白名单时也走这里：同一套校验，避免 API 放进了启动时会被
    `load_settings` 拒绝的值。
    """
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ConfigError(f"{field} 必须是列表")
    ids: set[str] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ConfigError(f"{field} 的元素必须是数字或字符串 id")
        text = str(item).strip()
        if not text:
            raise ConfigError(f"{field} 的元素不能为空")
        if _NUMERIC_ID.match(text):
            text = f"qq:{text}"
        lowered = text.casefold()
        if not lowered.startswith(_ID_PREFIXES):
            raise ConfigError(
                f"{field} 的元素必须是纯数字或带 qq:/wxg: 前缀：{str(item)!r}"
            )
        prefix, _, rest = lowered.partition(":")
        if not rest:
            raise ConfigError(f"{field} 的元素缺少冒号后的 id：{str(item)!r}")
        ids.add(f"{prefix}:{rest}")
    return frozenset(ids)


def parse_wechat_peer_ids(value: Any, field: str) -> frozenset[str]:
    """微信私聊 allow/deny 名单：只接受 `wxg:<alias>` 字符串，统一 casefold。

    校验强度与 `parse_group_ids` 一致，但**只**认 `wxg:` 前缀：纯数字（多为
    QQ 号/群号）与 `qq:` 前缀一律拒绝，避免把 QQ 名单写进微信私聊开关后
    在运行期静默失效。
    """
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ConfigError(f"{field} 必须是列表")
    ids: set[str] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, str):
            raise ConfigError(f"{field} 的元素必须是带 wxg: 前缀的字符串")
        text = item.strip()
        lowered = text.casefold()
        if not lowered.startswith("wxg:"):
            raise ConfigError(
                f"{field} 的元素必须带 wxg: 前缀"
                f"（不接受纯数字或 qq: 前缀）：{item!r}"
            )
        _, _, rest = lowered.partition(":")
        if not rest:
            raise ConfigError(f"{field} 的元素缺少冒号后的 alias：{item!r}")
        ids.add(f"wxg:{rest}")
    return frozenset(ids)


def normalize_wechat_account(value: Any, field: str = "gateway.wechat.account") -> str:
    """校验并归一机器人微信账号（wxid）；空字符串表示「不绑定」。

    只拒绝明显不是个人账号的目标（群、公众号、文件传输助手）以及带空白的
    值；账号是否真的存在、是否就是机器人自己，由 helper 启动时核对。
    """
    if not isinstance(value, str):
        raise ConfigError(f"{field} 必须是字符串")
    account = value.strip()
    if not account:
        return ""
    lowered = account.casefold()
    if (
        lowered == "filehelper"
        or lowered.startswith("gh_")
        or lowered.endswith("@chatroom")
    ):
        raise ConfigError(f"{field} 必须是个人微信账号 wxid，不能是群/公众号/文件传输助手")
    if not _WECHAT_ACCOUNT.match(account):
        raise ConfigError(
            f"{field} 格式非法：只能是字母开头的 2-64 位字母、数字、下划线、点或短横线"
        )
    return account


def _build_wechat(
    raw: Any,
    env: dict[str, str],
    *,
    default_quiet_hours: tuple[str, str] = ("00:00", "00:00"),
) -> WeChatConfig:
    """解析 `gateway.wechat`；就绪度缺失只告警，不在加载阶段拒绝。

    `quiet_hours` 未显式配置时用 `default_quiet_hours`（默认起止相同 =
    不静默）。发送端的静默时段与 `triggers.quiet_hours` 解耦：后者只控制
    「要不要回复」，不再影响这条通道能不能发。
    """
    section: dict[str, Any] = {}
    if raw is not None:
        if not isinstance(raw, dict):
            raise ConfigError("gateway.wechat 必须是映射")
        section = raw

    token = str(env.get("DAFEIYU_WECHAT_BRIDGE_TOKEN", "") or section.get("bridge_token") or "")
    account = normalize_wechat_account(section.get("account", ""))
    host = str(section.get("bridge_host") or "127.0.0.1").strip()
    port = _int(section.get("bridge_port", 8790), "gateway.wechat.bridge_port")
    if not 0 < port < 65536:
        raise ConfigError("gateway.wechat.bridge_port 必须在 1-65535 之间")
    poll_seconds = _float(section.get("poll_seconds", 8.0), "gateway.wechat.poll_seconds")
    if poll_seconds <= 0:
        raise ConfigError("gateway.wechat.poll_seconds 必须大于 0")
    full_sweep = _float(
        section.get("full_sweep_seconds", 120.0),
        "gateway.wechat.full_sweep_seconds",
    )
    if full_sweep <= 0:
        raise ConfigError("gateway.wechat.full_sweep_seconds 必须大于 0")
    send_interval = _float(
        section.get("send_min_interval_seconds", 0.0),
        "gateway.wechat.send_min_interval_seconds",
    )
    if send_interval < 0:
        raise ConfigError("gateway.wechat.send_min_interval_seconds 不能为负")
    echo_window = _float(
        section.get("echo_window_seconds", 120.0), "gateway.wechat.echo_window_seconds"
    )
    if echo_window < 0:
        raise ConfigError("gateway.wechat.echo_window_seconds 不能为负")
    local_db_refresh = _float(
        section.get("local_db_refresh_seconds", 1.0),
        "gateway.wechat.local_db_refresh_seconds",
    )
    if local_db_refresh < 0:
        raise ConfigError("gateway.wechat.local_db_refresh_seconds 不能为负")
    outbound = str(section.get("outbound") or "uia").strip().lower()
    if outbound not in ("uia", "hook", "auto"):
        raise ConfigError("gateway.wechat.outbound 只能是 uia / hook / auto")
    hook_url = str(section.get("hook_url") or "").strip()
    if hook_url and not _HOOK_URL.match(hook_url):
        raise ConfigError("gateway.wechat.hook_url 必须是 http:// 或 https:// 地址")
    hook_at_url = str(section.get("hook_at_url") or "").strip()
    if hook_at_url and not _HOOK_URL.match(hook_at_url):
        raise ConfigError("gateway.wechat.hook_at_url 必须是 http:// 或 https:// 地址")
    hook_media_url = str(section.get("hook_media_url") or "").strip()
    if hook_media_url and not _HOOK_URL.match(hook_media_url):
        raise ConfigError(
            "gateway.wechat.hook_media_url 必须是 http:// 或 https:// 地址"
        )
    hook_timeout = _float(
        section.get("hook_timeout_seconds", 5.0),
        "gateway.wechat.hook_timeout_seconds",
    )
    if hook_timeout <= 0:
        raise ConfigError("gateway.wechat.hook_timeout_seconds 必须大于 0")
    verify_timeout = _float(
        section.get("send_verify_timeout_seconds", 8.0),
        "gateway.wechat.send_verify_timeout_seconds",
    )
    if verify_timeout < 0:
        raise ConfigError("gateway.wechat.send_verify_timeout_seconds 不能为负")
    verify_interval = _float(
        section.get("send_verify_interval_seconds", 0.5),
        "gateway.wechat.send_verify_interval_seconds",
    )
    if verify_interval <= 0:
        raise ConfigError("gateway.wechat.send_verify_interval_seconds 必须大于 0")

    quiet_hours = section.get("quiet_hours", list(default_quiet_hours))
    if not isinstance(quiet_hours, (list, tuple)) or len(quiet_hours) != 2:
        raise ConfigError("gateway.wechat.quiet_hours 必须是两个 HH:MM 字符串")
    quiet: list[str] = []
    for item in quiet_hours:
        text = str(item).strip()
        if not _QUIET_HOUR.match(text):
            raise ConfigError("gateway.wechat.quiet_hours 必须是 HH:MM（00:00-23:59）")
        quiet.append(text)

    delays = section.get("typing_delay_seconds", [0.6, 1.2])
    if not isinstance(delays, (list, tuple)) or len(delays) != 2:
        raise ConfigError("gateway.wechat.typing_delay_seconds 必须是两个数字")
    low = _float(delays[0], "gateway.wechat.typing_delay_seconds")
    high = _float(delays[1], "gateway.wechat.typing_delay_seconds")
    if low < 0 or high < low:
        raise ConfigError("gateway.wechat.typing_delay_seconds 必须满足 0 <= 下限 <= 上限")

    kill_switch = str(section.get("kill_switch_file") or "data/wechat.stop").strip()
    if not kill_switch:
        raise ConfigError("gateway.wechat.kill_switch_file 不能为空")

    media_raw = section.get("media")
    if media_raw is None:
        media = WeChatMediaConfig()
    elif not isinstance(media_raw, dict):
        raise ConfigError("gateway.wechat.media 必须是映射")
    else:
        media_max = _int(
            media_raw.get("max_bytes", 10 * 1024 * 1024),
            "gateway.wechat.media.max_bytes",
        )
        if media_max <= 0:
            raise ConfigError("gateway.wechat.media.max_bytes 必须大于 0")
        media = WeChatMediaConfig(
            enabled=_bool(
                media_raw.get("enabled", False), "gateway.wechat.media.enabled"
            ),
            max_bytes=media_max,
            storage_root=str(media_raw.get("storage_root") or "").strip(),
        )

    raw_self = section.get("self_nicknames")
    if raw_self is None:
        raw_self = []
    if not isinstance(raw_self, (list, tuple)):
        raise ConfigError("gateway.wechat.self_nicknames 必须是字符串列表")
    self_nicknames = tuple(
        dict.fromkeys(
            str(item).strip() for item in raw_self if str(item).strip()
        )
    )

    return WeChatConfig(
        enabled=_bool(section.get("enabled", False), "gateway.wechat.enabled"),
        bridge_host=host,
        bridge_port=port,
        bridge_token=token,
        account_nickname=str(section.get("account_nickname") or "").strip(),
        account=account,
        poll_seconds=poll_seconds,
        unread_gate=_bool(
            section.get("unread_gate", True), "gateway.wechat.unread_gate"
        ),
        full_sweep_seconds=full_sweep,
        send_min_interval_seconds=send_interval,
        # 0 = 不限制（发送端 _rate_available 已按 >0 判断）；默认不限制。
        send_max_per_hour=_non_negative_int(
            section.get("send_max_per_hour"), "gateway.wechat.send_max_per_hour", 0
        ),
        send_max_per_day=_non_negative_int(
            section.get("send_max_per_day"), "gateway.wechat.send_max_per_day", 0
        ),
        quiet_hours=(quiet[0], quiet[1]),
        typing_delay_seconds=(low, high),
        ack_requires_observed=_bool(
            section.get("ack_requires_observed", True),
            "gateway.wechat.ack_requires_observed",
        ),
        echo_window_seconds=echo_window,
        keepalive=_bool(section.get("keepalive", False), "gateway.wechat.keepalive"),
        require_foreground_idle=_bool(
            section.get("require_foreground_idle", True),
            "gateway.wechat.require_foreground_idle",
        ),
        local_db_enabled=_bool(
            section.get("local_db_enabled", False), "gateway.wechat.local_db_enabled"
        ),
        local_db_root=str(section.get("local_db_root") or "").strip(),
        local_db_refresh_seconds=local_db_refresh,
        private_enabled=_bool(
            section.get("private_enabled", False), "gateway.wechat.private_enabled"
        ),
        private_reply=_bool(
            section.get("private_reply", False), "gateway.wechat.private_reply"
        ),
        private_allow=parse_wechat_peer_ids(
            section.get("private_allow"), "gateway.wechat.private_allow"
        ),
        private_deny=parse_wechat_peer_ids(
            section.get("private_deny"), "gateway.wechat.private_deny"
        ),
        outbound=outbound,
        hook_url=hook_url,
        hook_at_url=hook_at_url,
        hook_media_url=hook_media_url,
        hook_timeout_seconds=hook_timeout,
        send_verify=_bool(
            section.get("send_verify", True), "gateway.wechat.send_verify"
        ),
        send_verify_timeout_seconds=verify_timeout,
        send_verify_interval_seconds=verify_interval,
        kill_switch_file=kill_switch,
        version_min=str(section.get("version_min") or "4.1.6").strip(),
        language=str(section.get("language") or "zh-CN").strip(),
        allow_test_inject=_bool(
            section.get("allow_test_inject", False), "gateway.wechat.allow_test_inject"
        ),
        media=media,
        self_nicknames=self_nicknames,
    )


def _build_persona(raw: dict[str, Any]) -> PersonaConfig:
    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list) or not aliases:
        raise ConfigError("persona.aliases 必须是非空列表")
    max_chars = _int(raw.get("max_reply_chars", 120), "persona.max_reply_chars")
    max_tech = _int(raw.get("max_reply_chars_technical", 300), "persona.max_reply_chars_technical")
    if max_chars < 1 or max_tech < 1:
        raise ConfigError("persona 回复长度目标必须大于 0")
    hard_total = _int(
        raw.get("reply_hard_total", 2000), "persona.reply_hard_total"
    )
    hard_part = _int(
        raw.get("reply_hard_part", 500), "persona.reply_hard_part"
    )
    if hard_part < 1 or hard_total < hard_part:
        raise ConfigError("persona.reply_hard_total 必须 >= reply_hard_part 且都大于 0")
    segment_delay = _float(
        raw.get("segment_delay_seconds", 0.8), "persona.segment_delay_seconds"
    )
    if not 0 <= segment_delay <= 10:
        raise ConfigError("persona.segment_delay_seconds 必须在 0 到 10 秒之间")
    return PersonaConfig(
        name=str(raw.get("name", "溟月")),
        aliases=tuple(str(a) for a in aliases),
        max_reply_chars=max_chars,
        max_reply_chars_technical=max_tech,
        os_probability=_float(raw.get("os_probability", 0.05), "persona.os_probability"),
        prompt_file=str(raw.get("prompt_file") or "").strip(),
        segment_delay_seconds=segment_delay,
        repeat_guard=_bool(raw.get("repeat_guard", True), "persona.repeat_guard"),
        opportunity_guard=_bool(
            raw.get("opportunity_guard", True), "persona.opportunity_guard"
        ),
        quote_reply=_bool(raw.get("quote_reply", True), "persona.quote_reply"),
        reply_hard_total=hard_total,
        reply_hard_part=hard_part,
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
    batch_seconds = _float(raw.get("batch_seconds", 12), "triggers.batch_seconds")
    addressed_debounce = _float(
        raw.get("addressed_debounce_seconds", 8),
        "triggers.addressed_debounce_seconds",
    )
    addressed_max_wait = _float(
        raw.get("addressed_max_wait_seconds", 30),
        "triggers.addressed_max_wait_seconds",
    )
    ambient_min_interval = _float(
        raw.get("ambient_min_interval_seconds", 15),
        "triggers.ambient_min_interval_seconds",
    )
    ambient_max_turns = _int(
        raw.get("ambient_max_turns", 2), "triggers.ambient_max_turns"
    )
    thread_idle = _int(
        raw.get("thread_idle_seconds", 300), "triggers.thread_idle_seconds"
    )
    thread_closed = _int(
        raw.get("thread_closed_seconds", 600), "triggers.thread_closed_seconds"
    )
    # 已废弃：收尾不再抽样放行（全部交给模型判断）；仅保留老配置兼容解析。
    closed_sample_rate = _float(
        raw.get("closed_sample_rate", 0.05), "triggers.closed_sample_rate"
    )
    if not 0 <= closed_sample_rate <= 1:
        raise ConfigError("triggers.closed_sample_rate 必须在 [0, 1] 之间")
    for name, value in (
        ("triggers.batch_seconds", batch_seconds),
        ("triggers.addressed_debounce_seconds", addressed_debounce),
        ("triggers.addressed_max_wait_seconds", addressed_max_wait),
        ("triggers.ambient_min_interval_seconds", ambient_min_interval),
        ("triggers.ambient_max_turns", ambient_max_turns),
        ("triggers.thread_idle_seconds", thread_idle),
        ("triggers.thread_closed_seconds", thread_closed),
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
        ambient_max_turns=ambient_max_turns,
        thread_idle_seconds=thread_idle,
        thread_closed_seconds=thread_closed,
        closed_sample_rate=closed_sample_rate,
    )


def _validated_mode(value: Any) -> str:
    """把 brain.mode 归一成三档；覆盖层的 OverrideError 统一包成 ConfigError。"""
    try:
        return normalize_mode(value)
    except OverrideError as exc:
        raise ConfigError(
            f"brain.mode 只支持：{' / '.join(VALID_MODES)}"
            "（local 只是 provider 的别名，不能当 mode 用）"
        ) from exc


def _overlay_target(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """取得（必要时创建）raw 里可被覆盖层写入的配置段。"""
    value = raw.get(name)
    if value is None:
        value = {}
        raw[name] = value
    if not isinstance(value, dict):
        raise ConfigError(f"config.yaml 的 {name} 必须是映射")
    return value


def _overlay_subtarget(
    raw: dict[str, Any], section: str, name: str
) -> dict[str, Any]:
    parent = _overlay_target(raw, section)
    value = parent.get(name)
    if value is None:
        value = {}
        parent[name] = value
    if not isinstance(value, dict):
        raise ConfigError(f"config.yaml 的 {section}.{name} 必须是映射")
    return value


def _overlay_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"覆盖层 {field} 必须是字符串")
    return value.strip()


def _overlay_non_negative_int(value: Any, field: str) -> int:
    # bool 是 int 的子类，true/false 混进数值字段要单独拒绝。
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"覆盖层 {field} 必须是整数")
    if value < 0:
        raise ConfigError(f"覆盖层 {field} 必须大于等于 0")
    return value


def _overlay_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"覆盖层 {field} 必须是布尔值")
    return value


def _apply_behavior_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> None:
    """面板改的聊天强度/言论限制单独一段；没有 brain 覆盖时也要生效。"""
    section = overrides.get("behavior")
    if section is None:
        return
    if not isinstance(section, dict):
        raise ConfigError("覆盖层的 behavior 必须是映射")
    target = _overlay_target(raw, "behavior")
    for name in (
        "chat_intensity",
        "restriction_mode",
        "banned_phrases",
        "banned_topics",
    ):
        if name in section:
            target[name] = section[name]


def _apply_groups_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> None:
    """面板保存的群白名单：只认 whitelist，空列表 = 清空（不限制回复对象）。

    其它键（blacklist / admins）仍由 config.yaml 管，避免面板误写扩大权限。
    """
    section = overrides.get("groups")
    if section is None:
        return
    if not isinstance(section, dict):
        raise ConfigError("覆盖层的 groups 必须是映射")
    if "whitelist" not in section:
        return
    _overlay_target(raw, "groups")["whitelist"] = section["whitelist"]


def _apply_mind_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> None:
    """面板保存的观察者开关：只认两个白名单键，其余静默忽略。

    Observer 的其它参数（provider / 间隔 / 最少消息数）仍由 config.yaml 管，
    避免旧版本面板写进来的未知键改变启动行为。
    """
    section = overrides.get("mind")
    if section is None:
        return
    if not isinstance(section, dict):
        raise ConfigError("覆盖层的 mind 必须是映射")
    target = _overlay_target(raw, "mind")
    for name in ("observer_enabled", "observer_daily_calls"):
        if name in section:
            target[name] = section[name]


def _apply_wechat_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> None:
    """面板保存的微信开关与账号：只认五个键，其余静默忽略（与 mind 同风格）。

    `private_enabled` / `private_reply` 必须是布尔值；`private_allow` /
    `private_deny` 用 `parse_wechat_peer_ids` 校验；`account` 用
    `normalize_wechat_account` 校验。桥与 bot 每个轮询周期经 `hotconfig`
    热读四个私聊键；`account` 只在下次启动 hook 时生效，其它
    `gateway.wechat.*` 参数不允许在运行期被面板改。
    """
    section = overrides.get("wechat")
    if section is None:
        return
    if not isinstance(section, dict):
        raise ConfigError("覆盖层的 wechat 必须是映射")
    target = _overlay_subtarget(raw, "gateway", "wechat")
    if "account" in section:
        target["account"] = normalize_wechat_account(
            section["account"], "gateway.wechat.account"
        )
    for name in ("private_enabled", "private_reply"):
        if name in section:
            target[name] = _overlay_bool(section[name], f"gateway.wechat.{name}")
    for name in ("private_allow", "private_deny"):
        if name in section:
            parse_wechat_peer_ids(section[name], f"gateway.wechat.{name}")
            target[name] = section[name]


def _apply_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> bool:
    """把覆盖层白名单键盖到 raw 上，返回覆盖层是否显式给了 brain.mode。

    白名单之外的键（包括 brain.provider、brain.local_model.api_key）一律忽略：
    覆盖层由面板生成，出现别的键说明版本不一致，静默跳过比报错更稳。
    """
    _apply_behavior_overrides(raw, overrides)
    _apply_groups_overrides(raw, overrides)
    _apply_mind_overrides(raw, overrides)
    _apply_wechat_overrides(raw, overrides)
    brain_override = overrides.get("brain")
    if brain_override is None:
        return False
    if not isinstance(brain_override, dict):
        raise ConfigError("覆盖层的 brain 必须是映射")

    has_mode = "mode" in brain_override
    if has_mode:
        _overlay_target(raw, "brain")["mode"] = _validated_mode(
            brain_override["mode"]
        )

    key_override = brain_override.get("key")
    if key_override is not None:
        if not isinstance(key_override, dict):
            raise ConfigError("覆盖层的 brain.key 必须是映射")
        key_target = _overlay_subtarget(raw, "brain", "key")
        for name in ("base_url", "model"):
            if name in key_override:
                key_target[name] = _overlay_text(
                    key_override[name], f"brain.key.{name}"
                )

    local_override = brain_override.get("local_model")
    if local_override is not None:
        if not isinstance(local_override, dict):
            raise ConfigError("覆盖层的 brain.local_model 必须是映射")
        # 覆盖层把本地模型放在 brain 下，config.yaml 里是顶层 local_model。
        local_target = _overlay_target(raw, "local_model")
        for name in ("base_url", "model"):
            if name in local_override:
                local_target[name] = _overlay_text(
                    local_override[name], f"brain.local_model.{name}"
                )
        if "context" in local_override:
            local_target["context"] = _overlay_non_negative_int(
                local_override["context"], "brain.local_model.context"
            )
        if "output" in local_override:
            # output 目前不是 LocalModelConfig 的字段（Task 11 有自己的默认值），
            # 但仍要校验类型，免得面板写坏值后被静默吞掉。
            _overlay_non_negative_int(
                local_override["output"], "brain.local_model.output"
            )
        if "lean" in local_override:
            local_target["lean"] = _overlay_bool(
                local_override["lean"], "brain.local_model.lean"
            )

    return has_mode


def load_settings(
    env_file: Path = Path(".env"),
    config_file: Path = Path("config.yaml"),
) -> Settings:
    if not config_file.exists():
        raise ConfigError(
            f"找不到配置文件：{config_file}。运行 dafeiyu init 生成（模板见 config.example.yaml）"
        )

    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml 格式错误：{exc.__class__.__name__}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 顶层必须是映射")

    try:
        overrides = read_overrides(OVERRIDES_PATH)
    except OverrideError as exc:
        raise ConfigError(f"覆盖层读取失败：{exc}") from exc
    mode_from_overrides = _apply_overrides(raw, overrides)

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
    mode = _validated_mode(brain_raw.get("mode", "free"))
    key_raw = brain_raw.get("key") or {}
    if not isinstance(key_raw, dict):
        raise ConfigError("config.yaml 的 brain.key 必须是映射")
    key_base_url = str(key_raw.get("base_url") or "").strip()
    key_model = str(key_raw.get("model") or "").strip()

    gateway_raw = raw.get("gateway") or {}
    if not isinstance(gateway_raw, dict):
        raise ConfigError("config.yaml 的 gateway 必须是映射")
    gateway_provider = str(gateway_raw.get("provider") or "qq").strip().casefold()
    if gateway_provider not in ("qq", "wechat", "both"):
        raise ConfigError("gateway.provider 只能是 qq、wechat 或 both")
    triggers = _build_triggers(_section(raw, "triggers"))
    # 发送端静默与 triggers.quiet_hours 解耦：后者只管「要不要回复」，
    # 不再作为 gateway.wechat.quiet_hours 的默认值。
    wechat = _build_wechat(gateway_raw.get("wechat"), env)
    mirror = _build_mirror(
        raw.get("mirror"),
        gateway_provider,
        wechat.enabled,
        wechat,
    )
    if gateway_provider in ("wechat", "both") and not wechat.enabled:
        logger.warning(
            "gateway.provider=%s 但 gateway.wechat.enabled=false，降级为 qq",
            gateway_provider,
        )
        gateway_provider = "qq"
    if wechat.enabled and not wechat.account_nickname:
        logger.warning(
            "gateway.wechat.enabled=true 但 account_nickname 为空：无法判定是否被 @"
            "（WeChatAdapter.start() 会抛 ConfigError，dafeiyu doctor 第 ⑧ 项会 FAIL）"
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
        context=_non_negative_int(
            local_model_raw.get("context"), "local_model.context", 0
        ),
        lean=_bool(local_model_raw.get("lean", False), "local_model.lean"),
        timeout_seconds=_float(
            local_model_raw.get("timeout_seconds", 60.0), "local_model.timeout_seconds"
        ),
        reasoning_effort=str(local_model_raw.get("reasoning_effort") or "").strip(),
        autostart=bool(local_model_raw.get("autostart", True)),
        stop_when_unused=_bool(
            local_model_raw.get("stop_when_unused", True),
            "local_model.stop_when_unused",
        ),
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
    keepalive_seconds = _float(
        napcat_raw.get("keepalive_seconds", 300), "napcat.keepalive_seconds"
    )
    if keepalive_seconds < 0:
        raise ConfigError("napcat.keepalive_seconds 不能小于 0")

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
    dash_host = str(dash_raw.get("host") or "0.0.0.0").strip()
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
    # 留空表示「交给 supervisor 首次启动生成并落盘」；写了就必须够长。
    if dash_auth_required and dash_password and len(dash_password) < 12:
        raise ConfigError(
            "DAFEIYU_DASHBOARD_PASSWORD 至少 12 位；留空则由 supervisor "
            "自动生成并写入 data/dashboard.password"
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

    raw_bot_qq = str(env.get("BOT_QQ", "")).strip()
    if not raw_bot_qq:
        raise ConfigError(
            "缺少 BOT_QQ：请在 .env 里填写机器人 QQ 号（BOT_QQ=123456789）"
        )

    workspace = _build_workspace(workspace_raw)
    images = _build_images(raw.get("images") or {})
    speech = _build_speech(raw.get("speech") or {})
    _log_mirror_capabilities(mirror, wechat, images, workspace, speech)

    try:
        behavior = parse_behavior(raw.get("behavior") or {})
    except BehaviorError as exc:
        raise ConfigError(str(exc)) from exc

    mind = _build_mind(raw.get("mind") or {})

    return Settings(
        api_key=api_key,
        base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        model=env.get("DEEPSEEK_MODEL", "deepseek-chat").strip(),
        reasoning_effort=env.get("DEEPSEEK_REASONING_EFFORT", "").strip(),
        bot_qq=_int(raw_bot_qq, "BOT_QQ"),
        ws_host=env.get("NAPCAT_WS_HOST", "127.0.0.1"),
        ws_port=ws_port,
        ws_token=env.get("NAPCAT_WS_TOKEN", ""),
        db_path=env.get("DAFEIYU_DB_PATH", "data/dafeiyu.db"),
        persona=_build_persona(_section(raw, "persona")),
        triggers=triggers,
        groups=GroupsConfig(
            whitelist=parse_group_ids(
                _section(raw, "groups").get("whitelist"), "groups.whitelist"
            ),
            blacklist=parse_group_ids(
                _section(raw, "groups").get("blacklist"), "groups.blacklist"
            ),
            admins=parse_group_ids(
                _section(raw, "groups").get("admins"), "groups.admins"
            ),
        ),
        budget=_build_budget(budget_raw),
        memory=MemoryConfig(
            window_size=window_size,
            impression_topk=_int(memory_raw.get("impression_topk", 5), "memory.impression_topk"),
            extract_every=extract_every,
            retention_days=_non_negative_int(
                memory_raw.get("retention_days"), "memory.retention_days", 180
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
        injection=_build_injection(workspace_raw, memory_raw, mind),
        workspace=workspace,
        images=images,
        speech=speech,
        logging=LoggingConfig(
            level=str(logging_raw.get("level", "INFO")),
            dir=str(logging_raw.get("dir", "logs")),
        ),
        brain=BrainConfig(
            provider=provider,
            mode=mode,
            key_base_url=key_base_url,
            key_model=key_model,
            mode_from_overrides=mode_from_overrides,
        ),
        gateway=GatewayConfig(provider=gateway_provider),
        wechat=wechat,
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
            keepalive_seconds=keepalive_seconds,
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
        behavior=behavior,
        mind=mind,
        social=_build_social(raw.get("social") or {}),
        mirror=mirror,
    )
