# 大肥鱼运维与质量套件 — 总体设计

- 日期：2026-10-26
- 状态：总体拆法与关键决策已确认；待分册实现
- 基线：`eb40a49`（工作区干净，全量 `pytest -q` 597 passed）
- 关联分册：
  - `2026-10-26-ops-health-backup-design.md`（A：可观测与可恢复）
  - `2026-10-26-cost-security-commands-design.md`（B：成本与安全治理）
  - `2026-10-26-debugger-eval-deploy-design.md`（C：质量与交付）

## 0. 一句话

把「能跑」升级成「能长期无人值守地跑」：出问题有人知道，数据坏了能恢复，成本有上限，操作有审计，群里有自助入口，改人设有回归，换机器能自启动。

## 1. 背景

大肥鱼已经具备完整的 QQ 群聊链路、触发规则、批次投递、长期记忆、OpenCode 专用实例、dashboard 登录和基础会话管理。当前的主要缺口不是「能不能聊天」，而是「出了事能不能发现、能不能恢复、会不会悄悄烧钱」。

已确认的现状：

| 能力 | 现状 | 缺口 |
| --- | --- | --- |
| 进程守护 | Supervisor 有失败重启和退避 | 只在面板/日志可见，没有主动告警 |
| 健康检查 | bot `GET /api/health` 只返回 `{"ok": true}` | 没有网关、最后消息、待投批次、配额、DB、磁盘等结构化指标 |
| 备份 | 只能下载整库快照 | 没有上传恢复、完整性校验、恢复前备份、回滚、定时和保留 |
| 预算 | 每群每日 calls/tokens + ambient_ratio | 没有全局日/月度上限、费用估算、阈值预警 |
| 安全 | dashboard 登录、同源检查、SameSite=Strict | token 明文落盘、日志未脱敏、状态变更无独立审计 |
| 群命令 | 群管理员 `/闭嘴` `/醒来` `/forget`，私聊 `/stats` `/forget` | 缺 `/help`、群内 `/stats`、管理员 `/status` `/budget` |
| 调试 | 单条消息的只读触发推演 | 看不到批次组成、最终 prompt、token 估算 |
| 人设 | persona.md + few-shot | 没有 golden cases、离线回放和 baseline diff |
| 部署 | PowerShell 手工启动，依赖桌面会话 | 无自启动、无 doctor、无 CI |

## 2. 目标与非目标

### 2.1 目标

1. **可观测**：dashboard 一屏能看出 supervisor、NapCat、OpenCode、bot、网关、预算、DB、磁盘的状态。
2. **可告警**：关键故障主动通知，不依赖用户盯面板；bot 挂了也能告警。
3. **可恢复**：备份可创建、可校验、可上传恢复；恢复失败自动回滚。
4. **可控制成本**：per-chat、全局日、月度、费用四层预算；阈值预警；可降级或停止。
5. **可审计**：重启、停止、恢复、删除、轮换 token 等操作留痕。
6. **可自助**：群内 `/help`、`/stats`；管理员 `/status`、`/budget`。
7. **可预演**：调试器能看到批次组成、prompt、token/费用估算，且不产生副作用。
8. **可回归**：人设 golden cases 离线跑分，prompt 改动有 diff。
9. **可交付**：Windows 自启动、`doctor` 体检、CI 跑全量测试。

### 2.2 非目标

- 不做微信实际接入、多模态、embedding、多租户、多机器人实例。
- 不做 dashboard 内完整配置编辑。
- 不做内置 TLS、多用户/角色、OIDC、面板改密码。
- 不为告警、备份、评测引入新的运行时依赖。
- 不让调试器真实调用模型；只做只读预演。
- 不恢复 OpenCode session DB；只恢复 bot 业务库。
- 不把审计、告警状态写进 bot 业务库。
- 不改用户全局 OpenCode 配置。

## 3. 设计原则

1. **控制面与数据面分离**：Supervisor 负责进程、告警、备份、审计；bot 负责消息、路由、记忆、预算、命令。
2. **单一写者**：bot SQLite 只由 bot 写；Supervisor 只读备份和健康检查；dashboard 不直接写库。
3. **只读预演**：调试器、评测、健康快照都不改变真实状态。
4. **失败可见**：告警通道失败要进日志；恢复失败要回滚并告警；预算读库失败要降级并告警。
5. **破坏性操作可逆**：恢复前必须先备份；删除/恢复/轮换必须二次确认并审计。
6. **向后兼容**：新增配置默认关闭或保持旧行为；旧库可打开；旧测试不因默认值改变而失败。
7. **不新增运行时依赖**：优先标准库、现有 aiohttp/httpx/sqlite/PyYAML。

## 4. 总体架构

```text
QQ / NapCat
    │ OneBot v11
    ▼
┌──────────────────────────────────────────────┐
│ bot 数据面                                    │
│  ingress → router → scheduler → service      │
│  memory(SQLite) / budget guard / commands     │
│  GET /api/health/detail  GET /api/budget      │
│  POST /api/notify/admin                       │
└───────────────┬──────────────────────────────┘
                │ token
┌───────────────▼──────────────────────────────┐
│ supervisor 控制面                             │
│  process manager                              │
│  health aggregator → alert manager            │
│  backup manager / restore service             │
│  audit log                                    │
│  dashboard (登录后)                            │
└───────┬───────────────────────┬──────────────┘
        │                       │
        ▼                       ▼
   Webhook（主通道）        QQ 管理员（辅通道）
   不依赖 bot              经 bot control，bot 活着才可用
```

关键点：

- **告警不能只依赖 bot**。bot 挂了，QQ 管理员通道也挂了；所以 Webhook 是主通道。
- **恢复必须经过 supervisor**。只有 supervisor 能停 bot、替换 DB、再起 bot。
- **dashboard 只做编排和展示**。它不打开 bot SQLite 写入，不绕过 bot control 改业务数据。

## 5. 共享模型

这些类型在总设计里先定，分册实现时不要各造一套。

### 5.1 HealthSnapshot

bot 侧快照，只含运行指标，不含消息正文、token、密码。

```text
ok, generated_at, uptime_seconds, version
gateway: connected, last_event_at, last_disconnect_at
traffic: last_message_at, last_reply_at
scheduler: pending_chats, inflight_chats
budget: per_chat, global_daily, monthly, warnings
quota: paused_until, reason
brain: provider, model
memory: db_path, db_bytes, wal_bytes, message_count
errors: recent_count, last_error_at
```

### 5.2 Incident

```text
id, rule, severity, status(firing|resolved), fingerprint,
title, detail, first_seen_at, last_seen_at, resolved_at, notify_count
```

`fingerprint` 用于去重，例如 `bot_down`、`napcat_down`、`budget_warning:0.8`。

### 5.3 AuditEvent

```text
id, ts, actor, action, target, result, detail, request_id, ip
```

`actor` 形如 `dashboard:admin@127.0.0.1` 或 `system`。`detail` 写之前统一脱敏。

### 5.4 BudgetDecision

```text
action: allow | degrade | deny
reason: per_chat | global_daily | monthly | cost | ""
scope: 具体命中的层级
warnings: ["global_daily:0.8", ...]
remaining: calls / tokens / cost
```

`degrade` 的语义固定为：**环境发言拒绝，点名发言允许**；`deny` 才是全部拒绝。

### 5.5 Command

```text
name, aliases, scopes, admin_only, cooldown_seconds, help
```

命令注册表是唯一名单；Router、Service、调试器都从这里取，不再各自维护集合。

## 6. 边界与不变量

1. bot SQLite 只由 bot 写；Supervisor 只读备份/健康；恢复时先停 bot。
2. 所有状态变更接口必须：登录（dashboard）或 token（bot control）+ 同源检查 + 二次确认 + 审计。
3. 详细健康、预算、审计、备份列表都必须鉴权；bot 的 `/api/health` 只保留极简探活。
4. 告警内容不得包含消息正文、API Key、token、密码、完整 prompt。
5. 调试器和评测不得写 store、不得改 scheduler、不得发消息、不得消耗预算。
6. 备份校验失败不得替换当前 DB；恢复失败必须回滚到 pre-restore 备份。
7. 预算判定失败时：环境发言拒绝，点名发言允许，并发出 warning；不能因为读库失败让 bot 完全沉默。
8. 新增配置默认不改变旧行为；`0` 表示不启用上限，除非分册另有说明。

## 7. 分册拆分

| 分册 | 主题 | 主要内容 | 依赖 |
| --- | --- | --- | --- |
| A | 可观测与可恢复 | 健康快照、告警、备份/恢复、审计底座 | 无 |
| B | 成本与安全治理 | 全局/月度/费用预算、日志脱敏、token ACL/轮换、命令注册表、隐私删除 | A 的审计与健康接口 |
| C | 质量与交付 | 调试器批次预览、人设评测、自启动、doctor、CI | B 的预算判定；其余独立 |

实施顺序：A → B → C。A 先落地是因为 B 的预算预警要挂在告警上，C 的批次预览要显示 B 的预算判定。C 的人设评测和部署可以并行，但仍在 C 分册内交付。

## 8. 跨分册接口

### 8.1 bot control

| 方法 | 路径 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/health` | 无 | 极简探活：`{ok, uptime_seconds, version}` |
| GET | `/api/health/detail` | control token | 结构化健康快照 |
| GET | `/api/budget` | control token | per-chat / global / monthly / cost |
| POST | `/api/notify/admin` | control token | 告警通道让 bot 给管理员发 QQ |
| DELETE | `/api/memory/{chat_type}/{chat_id}/user/{user_id}` | control token | 管理员按用户删除原始消息 |

### 8.2 dashboard

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health/detail` | 聚合健康 |
| GET | `/api/alerts` | 当前/历史事件 |
| POST | `/api/alerts/test` | 发送测试告警 |
| GET | `/api/backups` | 备份列表 |
| POST | `/api/backups` | 立即备份 |
| POST | `/api/backups/restore` | 上传恢复 |
| DELETE | `/api/backups/{id}` | 删除备份 |
| GET | `/api/audit` | 审计查询 |
| POST | `/api/control-token/rotate` | 轮换 control token |
| POST | `/api/debug/batch` | 批次预览 |

所有 dashboard 接口都走现有登录中间件；POST/PUT/PATCH/DELETE 继续走 `_same_origin`。

### 8.3 数据文件

| 路径 | 内容 | 写入方 |
| --- | --- | --- |
| `data/backups/*.db` | bot 整库快照 | supervisor |
| `data/backups/*.json` | 备份 manifest | supervisor |
| `data/audit.jsonl` | 审计事件 | supervisor |
| `data/alert-state.json` | 告警状态（原子写） | supervisor |
| `data/dafeiyu.db` | bot 业务库 | bot |

## 9. 配置总览

新增配置段，默认值保持旧行为：

```yaml
alerts:
  enabled: false
  check_seconds: 30
  repeat_seconds: 21600
  webhook:
    url: ""
    format: generic
  qq_admin: 0
  rules:
    bot_down_seconds: 120
    napcat_down_seconds: 180
    opencode_down_seconds: 120
    quota_pause: true
    disk_free_percent: 10
    reply_error_window_seconds: 900
    reply_error_count: 3

backup:
  enabled: true
  dir: data/backups
  daily_keep: 7
  weekly_keep: 4
  schedule_hour: 4
  max_total_gb: 20
  max_upload_mb: 512

budget:
  global_daily_calls: 0
  global_daily_tokens: 0
  monthly_calls: 0
  monthly_tokens: 0
  monthly_cost_limit: 0
  currency: CNY
  price_input_per_million: 0
  price_output_per_million: 0
  warning_ratios: [0.5, 0.8, 1.0]
  on_exceed: degrade

audit:
  path: data/audit.jsonl
  max_bytes: 10485760
  backups: 3
```

说明：`alerts.enabled` 默认 `false`，避免升级后突然发通知；`backup.enabled` 默认 `true` 但只做本地快照，不发外部请求；预算的 `0` 表示不启用该上限，保持旧行为。`config.yaml` 示例给出建议值，但不在加载时强制打开。

## 10. 安全与失败模式

| 风险 | 处理 |
| --- | --- |
| bot 挂了，QQ 告警发不出去 | Webhook 主通道；QQ 仅辅通道 |
| Webhook 失败 | 记录日志，下一轮重试；不阻塞健康检查 |
| 备份文件损坏 | header + `PRAGMA integrity_check` + 必需表/列校验，失败不替换 |
| 恢复后 bot 起不来 | 自动回滚 pre-restore 备份并重新启动 |
| 恢复时并发操作 | 全局 restore lock；同一时间只允许一个恢复 |
| 预算读库失败 | 环境拒绝、点名允许、发 warning；不静默 |
| 日志泄露 token | `LogHub.write` 前统一脱敏；审计 detail 同样脱敏 |
| token 文件被其他用户读 | 创建后收紧 ACL（Windows）或 chmod 600（POSIX） |
| 告警风暴 | fingerprint 去重 + repeat_seconds + 恢复确认 |
| 调试器泄露 prompt | 仅登录管理员可见；限制长度；不写日志正文 |
| 评测烧钱 | 默认 fake/离线；真实模型评测手动执行，不进 CI |

CSRF：本轮保留同源检查 + SameSite=Strict，不新增 CSRF token。破坏性操作加二次确认字符串和审计。若未来出现跨站嵌入或非浏览器客户端，再评估。

## 11. 测试策略

### 11.1 单元

- 健康快照字段、超时、降级
- 告警状态机、去重、恢复、通道失败
- 备份 manifest、校验、保留策略
- 恢复成功/失败/回滚
- 预算四层顺序、degrade/stop、成本计算、warning 只发一次
- 脱敏正则、token ACL（mock）
- 命令权限矩阵、冷却
- 调试器预览无副作用
- 评测 checks/report/baseline

### 11.2 集成

- aiohttp test client + 临时 SQLite
- fake supervisor / fake bot / fake adapter
- dashboard 登录后访问 health/alerts/backups/audit
- 恢复：临时 DB → 备份 → 改数据 → 恢复 → 断言数据回退
- 恢复失败：坏文件 → 400，当前 DB hash 不变

### 11.3 E2E

- Playwright：登录 → 健康卡 → 告警测试 → 备份列表 → 恢复确认（临时环境）
- 调试器批次预览：登录 → 选会话 → 预览 → 无 pageerror
- 不连接真实 QQ/OpenCode/NapCat

### 11.4 CI

- Ubuntu + Windows，Python 3.11/3.12
- `pip install -e .[dev]`
- `pytest -q`
- 不跑真实模型、不发网络请求

## 12. 实施顺序与退出条件

| 阶段 | 退出条件 |
| --- | --- |
| A | 健康/告警/备份/恢复/审计全部通过；坏文件不伤库；恢复失败可回滚 |
| B | 预算四层判定和告警通过；日志无明文 token；命令权限矩阵通过；隐私删除有审计 |
| C | 批次预览与真实批次一致且无副作用；30 条 golden cases 出报告；自启动/doctor/CI 可用 |

每个分册独立提交、独立跑全量测试；不把三个分册塞进一个 PR。

## 13. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 备份期间 bot 正在写 WAL | 使用 SQLite backup API，不直接复制文件；恢复前停 bot |
| 恢复后 OpenCode session 与记忆不一致 | 明确只恢复 bot DB；文档写清 session 不在恢复范围 |
| 告警通道不可用 | dashboard 始终显示事件；Webhook 失败重试并记录 |
| 预算默认值改变旧行为 | 默认 0/关闭；只在 config.yaml 示例中给建议值 |
| 命令注册表重构影响旧命令 | 先包一层兼容层，测试覆盖旧别名和沉默行为 |
| 调试器暴露 prompt | 仅登录管理员；限制长度；不写日志正文 |
| Windows 计划任务依赖登录 | 文档说明；需要开机无人值守时再评估 WinSW 服务 |
| CI 平台不确定 | workflow 独立；不在 CI 中依赖 GitHub 专有服务 |

## 14. 关联文档

- `2026-10-24-im-bot-design.md`：v1 总设计；其中「不做 Web 管理后台」已被后续实现推翻，分册 C 会补一条变更说明，不改历史正文。
- `2026-10-24-opencode-supervisor-dashboard-design.md`：supervisor/dashboard 基线。
- `2026-10-24-bot-memory-workspace-web-design.md`：记忆、批次、工作区基线。
- `README.md`：实现后补运维 runbook。
