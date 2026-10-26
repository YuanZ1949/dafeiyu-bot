# 分册 B：成本与安全治理 — 预算、审计、命令

- 日期：2026-10-26
- 状态：待实现
- 基线：`eb40a49`（597 passed）
- 总设计：`2026-10-26-ops-quality-suite-design.md`
- 前置：分册 A（审计、健康接口）

## 0. 一句话

给大肥鱼装上「钱袋子、门锁和自助服务台」：钱花到哪、什么时候该停、谁动了系统、群里能查什么，都有明确答案。

## 1. 目标与非目标

### 1.1 目标

1. 预算从「每群每日」扩展为 per-chat、全局日、月度、费用四层。
2. 支持 `degrade`（只停环境发言）、`stop`（全停）、`notify`（只告警）三种超限策略。
3. 50%/80%/100% 阈值预警，接到分册 A 的告警通道。
4. 日志统一脱敏；token 文件收紧 ACL；支持轮换 control token。
5. 破坏性操作全部写审计；dashboard 可查。
6. 命令注册表统一命令名单；新增 `/help`、群内 `/stats`、管理员 `/status`、`/budget`。
7. 管理员可按用户删除原始消息（隐私清理），有审计、有确认。

### 1.2 非目标

- 不做账单对接、发票、按模型分别计价（只做统一单价，可配）。
- 不做多用户/角色；管理员仍是 `groups.admins`。
- 不做面板内改密码、改配置。
- 不做分布式限流、跨实例预算共享。
- 不做 CSRF token（保留同源 + SameSite=Strict）。
- 不做群内用户自助删除他人消息；只做管理员按用户删除。

## 2. 现状

| 事实 | 证据 |
| --- | --- |
| 每群每日预算 | `router.py:272-278`、`service.py:748-764` |
| 私聊只查 per-chat | `service.py:941-947` |
| `usage` 有 prompt/completion tokens | `memory/store.py:71-82` |
| 只有 `idx_usage_chat_date` | `memory/store.py:82` |
| 日志原样写入 | `supervisor/logs.py:57-88` |
| control token 明文写文件 | `supervisor/main.py:63-74` |
| 群命令名单重复 | `service.py:43-44, 280-296` |

## 3. 预算

### 3.1 配置

```yaml
budget:
  daily_calls: 200
  daily_tokens: 300000
  ambient_ratio: 0.8
  global_daily_calls: 0
  global_daily_tokens: 0
  monthly_calls: 0
  monthly_tokens: 0
  monthly_cost_limit: 0
  currency: CNY
  price_input_per_million: 0
  price_output_per_million: 0
  warning_ratios: [0.5, 0.8, 1.0]
  on_exceed: degrade       # degrade | stop | notify
```

规则：

- `0` 表示不启用该上限；`daily_calls`/`daily_tokens` 保持必填，旧行为不变。
- `on_exceed` 只作用于全局日/月度 calls/tokens；`monthly_cost_limit` 是硬停止，不受 `on_exceed` 影响。
- `warning_ratios` 必须在 (0, 1]，升序；重复值去掉。
- `price_*` 为 0 时费用不计算，`monthly_cost_limit` 必须为 0。
- `monthly_cost_limit > 0` 时至少一个价格必须 > 0。
- `currency` 仅用于展示，不做汇率换算。

### 3.2 数据查询

新增 `UsageTotals`：

```python
@dataclass(frozen=True)
class UsageTotals:
    calls: int
    prompt_tokens: int
    completion_tokens: int

    @property
    def tokens(self) -> int: ...
```

`Store` 新增：

```python
def usage_between(
    self, start_date: str, end_date: str,
    purposes: Sequence[str] | None = None,
    chat_type: ChatType | None = None,
    chat_id: int | None = None,
) -> UsageTotals
```

- `purposes=None` 表示全部用途；预算只统计 `reply`/`ambient`，错误/摘要/印象/调试不计入对话预算。
- 日期闭区间，格式 `YYYY-MM-DD`，与现有 `usage.date` 一致（上海时区）。
- `usage_global_today(date)`、`usage_month(month)` 作为薄封装，便于测试和 debugger。
- 新增 `CREATE INDEX IF NOT EXISTS idx_usage_date ON usage(date)`；旧库启动时自动建。

### 3.3 BudgetGuard

新增 `src/dafeiyu/budget.py`：

```python
class BudgetAction(str, Enum):
    ALLOW = "allow"
    DEGRADE = "degrade"
    DENY = "deny"

@dataclass(frozen=True)
class BudgetDecision:
    action: BudgetAction
    reason: str
    scope: str
    warnings: tuple[str, ...] = ()
    remaining_calls: int | None = None
    remaining_tokens: int | None = None
    remaining_cost: float | None = None

@dataclass(frozen=True)
class BudgetReport:
    per_chat: dict
    global_daily: dict
    monthly: dict
    cost: dict
    warnings: tuple[str, ...]

class BudgetGuard:
    def __init__(self, store: Store, config: BudgetConfig): ...
    def evaluate(self, chat_type, chat_id, *, addressed: bool, purpose: str, now: datetime) -> BudgetDecision: ...
    def report(self, chat_type, chat_id, *, now: datetime) -> BudgetReport: ...
```

判定顺序固定：

1. **per-chat daily**：`calls >= daily_calls` 或 `tokens >= daily_tokens` → `DENY`，`scope=per_chat`。
2. **global daily**：`global_daily_calls/tokens` 超限 → 按 `on_exceed`：
   - `degrade` → `DEGRADE`，`scope=global_daily`
   - `stop` → `DENY`
   - `notify` → `ALLOW` + warning
3. **monthly calls/tokens**：同 global 规则，`scope=monthly`。
4. **monthly cost**：`cost > monthly_cost_limit` → `DENY`，`scope=cost`，无论 `on_exceed`。
5. **warnings**：全局日/月度/费用达到 `warning_ratios`，返回 `global_daily:0.8`、`monthly:1.0` 等；不改变 action。

语义：

- `DEGRADE` + `addressed=True` → 实际允许；调用方把 action 当 allow。
- `DEGRADE` + `addressed=False` → 实际拒绝（环境发言停止）。
- `DENY` → 全部拒绝。
- `notify` 不阻止调用，只产生 warning；用于“先观察再收紧”。
- `purpose` 只接受 `reply`/`ambient`；其它用途不进入预算，直接 allow。

失败语义：

- 读库异常：返回 `DEGRADE`，`scope=storage_error`，warning `budget_storage_error`；环境拒绝、点名允许。写 warning 日志。
- 配置为 0 的上限跳过，不产生 warning。

### 3.4 成本

```text
cost = prompt_tokens / 1_000_000 * price_input_per_million
     + completion_tokens / 1_000_000 * price_output_per_million
```

- `report()` 返回 `cost`、`cost_limit`、`currency`、`estimated`（如果 usage 里有 estimated=1）。
- 成本是估算值，dashboard 标注“按当前配置单价估算”。
- 月度成本按自然月（1 日 00:00 起）累计。

### 3.5 接入点

**Router（rules 模式）**

- `Router.__init__` 构造 `BudgetGuard`。
- `should_handle` 顺序调整为：
  1. self
  2. muted
  3. blacklist
  4. per-chat budget（`BudgetGuard.evaluate` 的 per_chat 部分，或保留原查询）
  5. hard trigger 判定
  6. global/monthly/cost（`addressed=hard`）
  7. 若 `DENY` 或（`DEGRADE` 且非 hard）→ `budget`
  8. hard → 硬触发
  9. whitelist/rules
- 现有 12 个检查项扩展为 15 个：`budget` 后插入 `budget_global`、`budget_monthly`、`budget_cost`；标签分别为“全局日预算”“月度预算”“月度费用”。
- 调试器 metrics 增加 `global_calls/tokens`、`monthly_calls/tokens`、`monthly_cost`、`budget_action`、`budget_scope`、`warnings`。
- 真实 `Router` 状态和 RNG 仍不被触碰；`clone` 带上 guard 的只读引用。

**Service（model 模式）**

- `Service._budget_for` 改为 `BudgetGuard.evaluate(..., addressed=addressed, purpose=...)`；返回 `decision.action != DENY and (action != DEGRADE or addressed)`。
- `_private_budget_ok` 改为 `addressed=True` 的 guard 判定；私聊视为点名。
- `_run_batch_locked` 被预算拒绝时保持现有 `mark_skipped(..., SKIP_BUDGET)`；日志带 scope/reason。
- `health_snapshot()` 的 `budget` 字段使用 `BudgetGuard.report`；supervisor 告警读取 warnings。

### 3.6 预警

- `BudgetGuard.report()` 返回 warnings；bot `health_snapshot()` 带出。
- supervisor `AlertManager` 新增规则 `budget_warning`：
  - fingerprint `budget_warning:<scope>:<ratio>`
  - severity warning
  - repeat 6h
  - detail 例如“全局日调用达到 80%（800/1000）”
- 不在 bot 里直接发 QQ；统一走 supervisor 告警通道。
- `budget_warning` 的 repeat 为 24 小时：同一阈值每天最多提醒一次；supervisor 负责去重。若比例回落后再次上升，同一自然日不重复提醒。
- 告警 detail 只带 calls/tokens/cost 和上限，不带消息正文。

### 3.7 dashboard

- 用量卡新增：
  - 今日全局 calls/tokens 进度条
  - 本月 calls/tokens/费用进度条
  - 当前 per-chat 用量（选中会话时）
  - warnings 标签
  - 单价和“估算”说明
- 进度条颜色：<50% 中性，50–79% 黄，80–99% 橙，≥100% 红。
- 不提供“一键改预算”；预算仍走 config.yaml。

## 4. 安全

### 4.1 日志脱敏

新增 `src/dafeiyu/supervisor/redact.py`：

```python
def redact(text: str) -> str: ...
```

规则（大小写不敏感）：

| 模式 | 替换 |
| --- | --- |
| `Authorization: <value>` | `Authorization: ***` |
| `X-Control-Token: <value>` | `X-Control-Token: ***` |
| `Bearer <token>` | `Bearer ***` |
| `api_key` / `api-key` / `apikey` / `password` / `secret` / `token` + `:` 或 `=` + 值 | `key: ***` |
| `DEEPSEEK_API_KEY=...`、`NAPCAT_QUICK_PASSWORD...=...` | `KEY=***` |
| `Set-Cookie: ...` | `Set-Cookie: ***` |

实现要点：

- 只在 `LogHub.write()` 入口调用一次；内存环、文件、SSE 都拿到脱敏后的文本。
- `AuditLog` 的 `detail` 写之前调用同一个函数。
- 不脱敏普通中文“token 用量”这类没有 `:`/`=` 的文本。
- 测试覆盖：Bearer、header、env、JSON 字段、多行、无敏感词原文不变。

### 4.2 token 文件 ACL

`load_or_create_token()` 创建/读取后调用 `_harden_token_file(path)`：

- Windows：`icacls <path> /inheritance:r /grant:r <user>:(R,W)`；如果失败，写 warning，不阻止启动。
- POSIX：`os.chmod(path, 0o600)`。
- 测试 mock `subprocess.run`/`os.chmod`，不在测试机真的改 ACL。
- 已有文件也收紧，不只新文件。

### 4.3 token 轮换

新增 `src/dafeiyu/supervisor/control_token.py`：

```python
class ControlToken:
    def __init__(self, path: Path): ...
    @property
    def value(self) -> str: ...
    def rotate(self) -> str: ...   # 写新 token + ACL，返回新值
```

- `main.py` 用 `ControlToken` 替代裸字符串。
- `build_specs` 启动时取 `token.value`；轮换后重启 bot，让 bot 拿到新 token。
- dashboard `bot_call`/`bot_raw` 每次请求从 `token_provider()` 取 header，不把 token 写死在 `ClientSession` 默认 header 里。
- 路由：`POST /api/control-token/rotate`，body `{confirm:"ROTATE"}`；成功后：
  1. 生成新 token + 收紧 ACL
  2. 更新 holder
  3. 重启 bot（只重启 bot，不动 NapCat/OpenCode）
  4. 写审计 `token.rotate`
  5. 返回 `{ok, restarted}`
- 失败：返回 500，保留旧 token 可用；写 critical 日志/告警。
- 不做自动轮换；只手动。

### 4.4 审计

分册 A 已有 `AuditLog`。B 扩展事件：

| action | target | detail |
| --- | --- | --- |
| `token.rotate` | `control` | `{restarted: bool}` |
| `memory.user_delete` | `group/123/user/456` | `{deleted_messages: N, chat_type, chat_id, user_id}` |
| `budget.warning` | `global_daily:0.8` | `{calls, limit}` |
| `command.admin` | `/budget` | `{chat_type, chat_id, user_id}` |

- `GET /api/audit?limit=50&before=<id>`；limit 1..200。
- dashboard 审计卡：时间、actor、action、target、result；点击展开 detail；空态“暂无审计记录”。
- 审计 detail 不含消息正文；用户删除只记数量和目标 ID。

### 4.5 CSRF 决策

- 保留现有 `_same_origin` + `SameSite=Strict`。
- 破坏性操作增加确认字符串：`DELETE`、`RESTORE`、`ROTATE`、`DELETE_USER`。
- 不新增 CSRF token；若未来出现跨站嵌入或非浏览器客户端，再评估。
- 所有状态变更继续走 dashboard 登录；bot control 走 token。

## 5. 命令

### 5.1 CommandRegistry

新增 `src/dafeiyu/commands.py`：

```python
@dataclass(frozen=True)
class CommandSpec:
    name: str
    aliases: tuple[str, ...]
    scopes: frozenset[ChatType]
    admin_only: bool
    cooldown_seconds: float
    help: str

class CommandRegistry:
    def match(self, text: str) -> tuple[CommandSpec, str] | None: ...
    def is_known(self, name: str, chat_type: ChatType) -> bool: ...
    def help_text(self, chat_type: ChatType, *, is_admin: bool) -> str: ...
```

- 复用现有 `router.parse_command()` 的解析结果；registry 只负责名单、权限、冷却、帮助。
- 未知斜杠不拦截，继续走原来的 `sample_skip` 分支。
- `Service._GROUP_COMMANDS`/`_PRIVATE_COMMANDS` 删除；`Service.explain_trigger` 的命令分支改用 registry。
- 冷却键：`(command, chat_type, chat_id)`；私聊按 `(command, private, user_id)`。
- 管理员判定：`msg.user_id in settings.groups.admins`。
- 非管理员在群里执行管理员命令：保持现有行为，静默返回（不回复），只记 info 日志。
- 私聊非管理员执行管理员命令：回复“这个命令只有管理员能用。”，避免用户以为 bot 没反应。

### 5.2 命令表

| 命令 | 别名 | 作用域 | 权限 | 冷却 | 行为 |
| --- | --- | --- | --- | --- | --- |
| `/help` | `帮助` | 群/私聊 | 所有人 | 5s | 列出当前作用域可用命令；管理员多出 `/status` `/budget` |
| `/stats` | `统计` | 群/私聊 | 所有人 | 群 60s / 私聊 5s | 今日 calls/tokens + 剩余额度；群内不显示费用 |
| `/status` | `状态` | 群/私聊 | 管理员 | 30s | 网关、待投批次、配额、预算、uptime |
| `/budget` | `预算` | 群/私聊 | 管理员 | 30s | per-chat + 全局日 + 月度 + 费用 |
| `/闭嘴` | `shutup` | 群 | 管理员 | 0s | 静音 30 分钟（现有） |
| `/醒来` | `wake` | 群 | 管理员 | 0s | 解除静音（现有） |
| `/forget` | 无 | 群/私聊 | 群管理员 / 私聊所有人 | 0s | 清当前会话（现有） |

`/help` 输出示例（群、非管理员）：

```text
可用命令：/help 帮助 · /stats 今日统计
```

管理员多两行：

```text
管理员：/status 运行状态 · /budget 预算
```

`/status` 不调用真实模型；只读 `health_snapshot()` 和 scheduler 计数。

### 5.3 隐私删除

不新增群内自助删除，避免“谁都能删别人消息”的滥用。提供管理员 dashboard 操作：

- bot control：`DELETE /api/memory/{chat_type}/{chat_id}/user/{user_id}`，body `{confirm:"DELETE_USER"}`。
- 语义：
  - 删除 `messages` 中该 `(chat_type, chat_id, user_id)` 的原始消息。
  - 删除对应的 `delivered_messages` 行（按 message_id）。
  - 不删除 `summaries`/`impressions`/`session_summaries`（聚合数据无法可靠归因）。
  - 返回 `{ok, deleted_messages}`；UI 提示“摘要和印象是聚合数据，可能仍包含痕迹；如需彻底重建，请联系管理员执行 /forget”。
- dashboard：记忆区增加“按用户删除”输入 + 确认；调用 bot control；写审计 `memory.user_delete`。
- 测试：删除后消息数下降、其它用户消息保留、delivered 行清理、审计写入。

### 5.4 调试器命令兼容

- `Service.explain_trigger` 的命令分支改为 `registry.match()`；已知命令返回 `verdict=command`，未知斜杠继续走 sample_skip。
- `_GROUP_COMMANDS`/`_PRIVATE_COMMANDS` 删除后，相关测试改为 registry 断言。
- `/help` 新增后，调试器能识别；不会执行命令。

## 6. 测试与验收

### 6.1 单元

- `test_budget_guard.py`：
  - per-chat 超限 → deny
  - global degrade 环境拒绝、点名允许
  - global stop → deny
  - monthly cost 硬停止
  - warning 比例、0 上限跳过、读库失败降级
  - 成本计算（输入/输出价格）
- `test_usage_queries.py`：`usage_between` 日期/用途/聊天过滤、索引存在。
- `test_redact.py`：所有模式、误伤检查。
- `test_control_token.py`：ACL mock、轮换、旧 token 失效。
- `test_commands.py`：权限矩阵、冷却、别名、未知命令、私聊管理员提示。
- `test_store_user_delete.py`：删除范围、审计。

### 6.2 集成

- aiohttp bot control：`/api/budget`、`/api/notify/admin`、`/api/memory/.../user/...`。
- dashboard：usage 卡、审计卡、token 轮换、按用户删除。
- 预算接入 Router/Service 后，现有触发测试保持通过；新增全局预算场景。

### 6.3 验收

1. 配置 global daily 上限为 1，第二次调用被拒；点名/环境行为符合 `degrade`。
2. `on_exceed=stop` 时点名也被拒。
3. 月度费用超限时全部拒绝，即使 `on_exceed=notify`。
4. 50%/80%/100% 每个自然日各只产生一次 `budget_warning`（supervisor 按 24h 窗口去重）。
5. 日志中不再出现明文 token/Authorization/API Key。
6. token 文件 ACL 收紧；轮换后旧 token 401，新 token 可用。
7. `/help`、`/stats`、`/status`、`/budget` 权限和冷却符合表格。
8. 按用户删除只删该用户原始消息，写审计。
9. 全量 `pytest -q` 通过。

## 7. 实施步骤

1. `BudgetConfig` 扩展 + 校验 + 默认值。
2. `Store.usage_between` + `idx_usage_date` + 测试。
3. `BudgetGuard` + 成本 + warnings + 单元测试。
4. Router/Service 接入 + 调试器检查项扩展 + 现有测试修复。
5. `redact.py` + `LogHub` 接入 + 测试。
6. `ControlToken` + ACL + 轮换 + dashboard 路由 + 测试。
7. `CommandRegistry` + 命令迁移 + `/help` `/stats` `/status` `/budget` + 测试。
8. 按用户删除 API/UI + 审计。
9. 文档 + 全量测试 + 提交。

## 8. 风险

- **预算改变触发顺序**：Router 的检查顺序会影响现有测试和真实行为；必须保持 hard trigger 优先，并用回归测试锁定。
- **全局预算查询性能**：`usage` 可能很大；`idx_usage_date` + 只查 `reply/ambient`；必要时加月度缓存（首版不做）。
- **费用单价变化**：历史成本按当前单价重算，不是真实账单；UI 明确“估算”。
- **token 轮换重启 bot**：重启期间消息可能丢失；轮换前提示，失败保留旧 token。
- **脱敏误伤**：只匹配明确的 `key: value`/Bearer，不对自由文本做泛化替换。
- **命令冷却内存态**：重启后冷却清零；可接受，避免再建表。
- **隐私删除不彻底**：摘要/印象是聚合数据；文档和 UI 必须说清楚，避免过度承诺。
