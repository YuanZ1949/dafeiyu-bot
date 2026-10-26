# 分册 A：可观测与可恢复 — 健康、告警、备份恢复

- 日期：2026-10-26
- 状态：待实现
- 基线：`eb40a49`（597 passed）
- 总设计：`2026-10-26-ops-quality-suite-design.md`

## 0. 一句话

让 supervisor 不再只是「进程挂了帮你重启」，而是「知道哪里不对、主动告诉你、数据坏了能安全换回来」。

## 1. 目标与非目标

### 1.1 目标

1. bot 暴露结构化健康快照：网关、最后消息/回复、待投批次、配额、预算、DB、错误。
2. supervisor 聚合进程状态、bot 健康、NapCat 登录、磁盘、备份状态。
3. 关键故障通过 Webhook 主动告警；QQ 管理员作为辅助通道。
4. 备份可创建、列出、下载、删除、定时执行、按保留策略清理。
5. 恢复可上传、校验、预恢复备份、停 bot、原子替换、启动、失败回滚。
6. 审计底座：备份/恢复/进程控制/关闭等状态变更写 JSONL。
7. dashboard 增加健康卡、事件列表、备份卡；不改变现有视觉主题。

### 1.2 非目标

- 不做全局/月度预算（分册 B）。
- 不做命令注册表（分册 B）。
- 不做调试器批次预览（分册 C）。
- 不做人设评测、自启动、CI（分册 C）。
- 不恢复 OpenCode session DB。
- 不做告警升级策略、值班表、短信/电话。
- 不做多实例告警聚合。

## 2. 现状与问题

| 事实 | 证据 | 影响 |
| --- | --- | --- |
| Supervisor 自动重启失败进程 | `supervisor/manager.py:180-215` | 重启只发生在本地，用户不在电脑前就不知道 |
| bot `/api/health` 恒返回 `{"ok": true}` | `control.py:704-705` | 无法区分「进程活着但网关断了」和「完全正常」 |
| dashboard health 只是简单转发 | `supervisor/dashboard.py` health handler | 没有聚合指标 |
| 备份只有下载 | `control.py:1084-1106` | 恢复靠手工替换 DB，容易停错服务/漏 WAL |
| README 要求停服务手工替换 | `README.md:272-274` | 没有校验、预备份、回滚 |
| 日志有轮转但无脱敏 | `supervisor/logs.py` | token/密码可能进日志 |
| 状态变更没有独立审计 | `supervisor/dashboard.py` control/shutdown | 出事无法追溯谁在什么时候恢复/删除 |

## 3. 架构

```text
bot
  ├─ HealthSnapshot.build()
  ├─ GET /api/health           # 极简，公开
  └─ GET /api/health/detail   # token

supervisor
  ├─ HealthMonitor
  │    ├─ supervisor.status()
  │    ├─ bot health detail (2s timeout)
  │    ├─ napcat login state
  │    ├─ disk usage
  │    └─ backup state
  ├─ AlertManager
  │    ├─ rule evaluation
  │    ├─ state machine / dedupe / recovery
  │    ├─ WebhookChannel
  │    └─ QQAdminChannel -> POST bot /api/notify/admin
  ├─ BackupManager
  │    ├─ SQLite backup API
  │    ├─ manifest + sha256
  │    ├─ retention
  │    └─ scheduler loop
  ├─ RestoreService
  │    ├─ validate upload
  │    ├─ pre-restore backup
  │    ├─ stop bot -> replace -> start bot
  │    └─ rollback on failure
  └─ AuditLog (JSONL)
```

### 3.1 新增文件

| 文件 | 职责 |
| --- | --- |
| `src/dafeiyu/supervisor/health.py` | `HealthMonitor`、聚合快照、磁盘/DB 指标 |
| `src/dafeiyu/supervisor/alerts.py` | `Incident`、规则、状态机、Webhook/QQ 通道 |
| `src/dafeiyu/supervisor/backup.py` | `BackupManager`、manifest、校验、保留 |
| `src/dafeiyu/supervisor/restore.py` | `RestoreService`、锁、回滚 |
| `src/dafeiyu/supervisor/audit.py` | `AuditLog`、轮转、查询 |
| `src/dafeiyu/supervisor/dashboard/ops.js` | 健康/告警/备份前端逻辑（也可并入现有脚本） |

### 3.2 修改文件

| 文件 | 改动 |
| --- | --- |
| `src/dafeiyu/control.py` | `health_snapshot()`、`/api/health/detail`、`/api/notify/admin` |
| `src/dafeiyu/memory/store.py` | `SCHEMA_VERSION`、`PRAGMA user_version`、`db_stats()` |
| `src/dafeiyu/supervisor/dashboard.py` | 新路由、聚合、代理、审计 |
| `src/dafeiyu/supervisor/main.py` | 构造 HealthMonitor/AlertManager/BackupManager/RestoreService/AuditLog，启动循环 |
| `src/dafeiyu/config.py` | `AlertsConfig`、`BackupConfig`、`AuditConfig` 加载校验 |
| `config.yaml` / `.env.example` | 新增配置段/变量说明 |
| `README.md` | 运维 runbook、告警、备份/恢复说明 |

## 4. 健康快照

### 4.1 bot 侧

`BrainControl.health_snapshot()` 返回：

```json
{
  "ok": true,
  "generated_at": "2026-10-26T12:00:00+08:00",
  "uptime_seconds": 3600.0,
  "version": "0.1.0",
  "gateway": {
    "connected": true,
    "last_event_at": "2026-10-26T11:59:50+08:00",
    "last_disconnect_at": null
  },
  "traffic": {
    "last_message_at": "2026-10-26T11:59:50+08:00",
    "last_reply_at": "2026-10-26T11:58:10+08:00"
  },
  "scheduler": {"pending_chats": 1, "inflight_chats": 0},
  "budget": {
    "per_chat": {"calls": 10, "tokens": 12000, "daily_calls": 200, "daily_tokens": 300000},
    "global_daily": {"calls": 50, "tokens": 60000, "limit_calls": 0, "limit_tokens": 0},
    "monthly": {"calls": 500, "tokens": 600000, "limit_calls": 0, "limit_tokens": 0, "cost": 0.0, "cost_limit": 0.0},
    "warnings": []
  },
  "quota": {"paused_until": null, "reason": ""},
  "brain": {"provider": "opencode", "model": "deepseek-chat"},
  "memory": {"db_path": "data/dafeiyu.db", "db_bytes": 1048576, "wal_bytes": 0, "message_count": 1234},
  "errors": {"recent_count": 0, "last_error_at": null}
}
```

约束：

- 不含消息正文、昵称、token、API Key、prompt。
- 预算字段在分册 B 扩展；A 先保留占位结构，读现有 per-chat 数据。
- 读库失败时 `ok=false`，`errors.last_error_at` 记录，不抛 500；supervisor 仍能拿到部分指标。

### 4.2 supervisor 侧

`HealthMonitor.snapshot()` 聚合：

```json
{
  "ok": true,
  "generated_at": "...",
  "processes": {"bot": {...}, "opencode": {...}, "napcat": {...}},
  "bot": { "...bot health detail..." },
  "napcat": {"logged_in": true, "qr_age_seconds": null},
  "disk": {"path": "C:\\...", "free_bytes": 123, "total_bytes": 456, "free_percent": 12.3},
  "backups": {"last_success_at": "...", "last_error_at": null, "count": 7},
  "alerts": {"firing": 0, "resolved_24h": 2}
}
```

- bot 探活超时 2s；超时视为 `bot.ok=false`，保留进程状态。
- 磁盘用 `shutil.disk_usage`，路径取仓库根目录。
- 备份状态来自 `BackupManager` 内存状态 + manifest 列表。
- `GET /api/health` 只返回 `{ok, generated_at, processes}`；`GET /api/health/detail` 返回完整对象。
- dashboard 的健康卡只渲染白名单字段，不把原始 API Key/消息内容带进前端。

### 4.3 路由

| 方法 | 路径 | 鉴权 | 返回 |
| --- | --- | --- | --- |
| GET | `/api/health` | 否（dashboard 仍受登录中间件；bot control 公开） | 极简 |
| GET | `/api/health/detail` | dashboard 登录 / bot control token | 完整 |
| GET | `/api/status` | 保持现状 | supervisor 进程状态 |

## 5. 告警

### 5.1 规则

| rule | 条件 | 级别 | pending | repeat | 恢复 |
| --- | --- | --- | --- | --- | --- |
| `process_failed` | 任一进程 `failed`，或 10 分钟内 restart_count 增加 ≥3 | critical | 0s | 6h | 进程 running 连续 2 次检查 |
| `bot_down` | bot 进程非 running，或 bot `/api/health` 连续 120s 失败 | critical | 120s | 6h | 连续 2 次健康 |
| `opencode_down` | opencode 进程非 running，或 `/api/info` 连续 120s 失败 | critical | 120s | 6h | 连续 2 次健康 |
| `napcat_down` | napcat 进程非 running，或网关断开连续 180s | critical | 180s | 6h | 连续 2 次连接 |
| `quota_pause` | `quota.paused_until` 在未来 | warning | 0s | 6h | 暂停结束 |
| `reply_errors` | 15 分钟内 error 用量 ≥3 | warning | 0s | 6h | 窗口内无新 error |
| `disk_low` | 磁盘可用 < 配置百分比 | warning | 0s | 12h | 回到阈值以上 |
| `backup_failed` | 最近一次备份失败 | warning | 0s | 6h | 下一次备份成功 |

`alerts.rules` 可覆盖阈值；未配置的规则使用默认值。

### 5.2 状态机

```text
ok --(条件成立超过 pending)--> firing --(连续 2 次恢复)--> resolved
 ^                                  |
 |                                  +--(repeat_seconds 到)--> 再通知
 +--(条件消失但未确认)--------------+
```

- `fingerprint` = `rule` 或 `rule:scope`（如 `budget_warning:0.8`）。
- `pending` 内条件消失则回到 ok，不发通知。
- `firing` 期间同一 fingerprint 不重复创建，只更新 `last_seen_at`。
- `resolved` 发一次恢复通知；保留 24h 在 dashboard 历史里。
- 状态写 `data/alert-state.json`：先写临时文件，`os.replace` 原子替换。

### 5.3 通道

```python
class AlertChannel(Protocol):
    async def send(self, incident: Incident, event: str) -> None: ...
```

**WebhookChannel**

- `POST alerts.webhook.url`，JSON：
  ```json
  {
    "version": 1,
    "event": "firing",
    "service": "dafeiyu",
    "incident": {
      "rule": "bot_down",
      "severity": "critical",
      "title": "bot 不在线",
      "detail": "连续 120 秒健康检查失败",
      "first_seen_at": "...",
      "last_seen_at": "..."
    }
  }
  ```
- 超时 5s，失败重试 2 次（1s/3s 退避），仍失败写日志。
- `format`：`generic` 默认；`ntfy` 用 `text` 字段；`wecom`/`dingtalk` 用各自 `text`/`markdown` 结构。实现时只做通用 JSON + 两个适配器，避免过度设计。
- 不发送消息正文、token、路径以外的敏感信息。

**QQAdminChannel**

- `POST {bot_url}/api/notify/admin`，body：
  ```json
  {"text": "【大肥鱼告警】bot 不在线\n连续 120 秒健康检查失败", "dedupe_key": "bot_down"}
  ```
- bot 端校验：control token、text ≤ 1000 字、限流 1 条/10s；通过 `adapter.send` 发给 `groups.admins` 第一个或 `alerts.qq_admin`。
- bot 不可用/未连接时返回 503；supervisor 记录失败，不重试到死。
- 该通道只用于 bot 活着时的辅助通知；`bot_down` 规则默认只走 Webhook。

### 5.4 去重与风暴保护

- 同一 fingerprint 在 `repeat_seconds` 内不重复通知。
- 一次检查最多发 5 条告警；超出写日志并合并为一条 `alert_flood`。
- 告警通道失败不触发新告警，避免递归。
- 告警文本限长 1000 字；detail 超长截断。

### 5.5 dashboard

- 健康卡：进程徽标、bot 网关、最后消息/回复、待投批次、配额、磁盘、备份状态。
- 事件卡：firing 优先，显示 rule/severity/时间/详情；resolved 折叠。
- “测试告警”按钮：`POST /api/alerts/test`，发一条 `test` 事件，不写 firing 状态。
- 空态：“暂无活动事件”。

## 6. 备份

### 6.1 数据模型

`BackupManifest`：

```text
id, filename, created_at, reason, size_bytes, sha256,
schema_version, integrity_ok, app_version
```

`reason`：`scheduled`、`manual`、`pre-restore`。

### 6.2 创建

1. 取 `settings.db_path`，解析为绝对路径；不存在则报错。
2. 用 `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` 打开只读连接。
3. `dest = sqlite3.connect(tmp)`；`src.backup(dest)`；关闭。
4. `fsync` 临时文件；计算 sha256；`os.replace` 到最终文件。
5. 校验：header `SQLite format 3\0`、`PRAGMA integrity_check`、必需表/列。
6. 写 manifest JSON（临时文件 + replace）。
7. 清理保留策略。
8. 写审计 `backup.create`。

文件名：`dafeiyu-YYYYmmdd-HHMMSS-<reason>.db`，manifest 同名 `.json`。

### 6.3 校验

必需表：`messages`、`impressions`、`summaries`、`usage`、`opencode_sessions`、`brain_sessions`、`session_summaries`、`delivered_cursors`、`delivered_messages`、`memory_backoff`、`memory_rate_limit`。

必需列抽查：

- `messages`: `chat_type, chat_id, message_id, user_id, content, timestamp`
- `usage`: `date, purpose, prompt_tokens, completion_tokens`
- `brain_sessions`: `provider, chat_type, chat_id, session_id`

`PRAGMA user_version`：

- A 阶段给 `Store` 增加 `SCHEMA_VERSION = 1`；初始化时 `PRAGMA user_version = 1`。
- 旧库打开时 user_version 为 0，自动升到 1。
- 备份校验接受 0 或 1；大于当前版本拒绝（防止新库回灌旧程序）。

### 6.4 保留

- 每天保留最近 `daily_keep` 个 `scheduled` 备份。
- 每周保留最近 `weekly_keep` 个（周日 04:00 的备份标记 weekly）。
- `manual` 和 `pre-restore` 不自动清理，除非用户手动删除；超过 30 天在 UI 标记“旧备份”。
- 总大小超过 `backup.max_total_gb`（默认 20GB）时，从最旧的 `scheduled` 开始删，保留至少 3 个。
- 删除前校验 manifest；文件丢失时清理 manifest 并记日志。

### 6.5 定时

- supervisor 启动后注册 `backup_loop`：每 60s 检查一次，到了 `schedule_hour` 且今天没成功备份就执行。
- 备份失败写 `backup_failed` 告警，下一小时重试一次，之后等次日。
- 备份期间不阻塞 dashboard；状态显示“备份中”。

### 6.6 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/backups` | 列表 + 状态 |
| POST | `/api/backups` | 立即备份，body `{reason:"manual"}` |
| DELETE | `/api/backups/{id}` | 删除，body 可带 `confirm:"DELETE"` |
| POST | `/api/backups/restore` | multipart 上传恢复，必须 `confirm=RESTORE` |

## 7. 恢复

### 7.1 流程

```text
upload -> size/type check -> temp file
  -> validate (header/integrity/tables/version)
  -> acquire restore lock
  -> create pre-restore backup
  -> stop bot (timeout 5s)
  -> remove stale -wal/-shm
  -> os.replace(temp, db_path)
  -> start bot
  -> wait ready (30s) + health detail
  -> success: audit, return
  -> failure: stop bot, restore pre-restore, start bot, audit, return 500
```

### 7.2 约束

- 上传上限 `backup.max_upload_mb`，默认 512MB；超过返回 413。
- 只接受 `.db` 文件或 `application/octet-stream`；文件名只用于显示，不参与路径。
- 恢复期间 dashboard 显示“恢复中”，禁止再次提交（后端锁 + 前端禁用）。
- 恢复前必须创建 `pre-restore` 备份；如果 pre-restore 备份失败，直接拒绝恢复。
- 替换前删除 `db_path-wal`、`db_path-shm`；替换后不要保留旧文件。
- 恢复后健康检查失败：回滚 pre-restore，重新启动；如果回滚也失败，写 critical 告警，dashboard 显示错误。
- 不恢复 OpenCode DB；UI 明确写“只恢复 bot 记忆库，不影响 OpenCode session”。
- 恢复操作必须带 `confirm=RESTORE`，否则 400。
- 进程控制沿用现有 dashboard 按钮，但“停止全部/重启全部”需二次确认 `STOP`/`RESTART`；单进程启动不需要确认。所有进程控制写审计。

### 7.3 恢复结果

```json
{
  "ok": true,
  "restored_from": "dafeiyu-20261026-040000-scheduled.db",
  "pre_restore_backup": "dafeiyu-20261026-120000-pre-restore.db",
  "bot_started": true,
  "duration_seconds": 12.3
}
```

失败时 `ok=false`，带 `stage`（validate/backup/stop/replace/start/health/rollback）和 `error`；不返回堆栈。

## 8. 审计底座

### 8.1 事件

A 阶段记录：

- `backup.create`、`backup.delete`、`backup.restore`
- `process.start`、`process.stop`、`process.restart`
- `shutdown`
- `auth.login`（成功/失败）、`auth.logout`

B 阶段扩展：`token.rotate`、`memory.user_delete`、`budget.change` 等。

### 8.2 存储

- `data/audit.jsonl`，一行一个 JSON。
- 10MB 轮转，保留 3 个历史。
- 写入前调用 `redact`（B 阶段实现；A 先预留 `sanitize` 钩子，默认不改变内容）。
- `GET /api/audit?limit=50&before=<ts>` 返回倒序事件；limit 1..200。

### 8.3 字段

```json
{
  "id": "uuid",
  "ts": "2026-10-26T12:00:00+08:00",
  "actor": "dashboard:admin@127.0.0.1",
  "action": "backup.restore",
  "target": "dafeiyu-20261026-040000-scheduled.db",
  "result": "ok",
  "detail": {"stage": "done", "duration_seconds": 12.3},
  "request_id": "req-...",
  "ip": "127.0.0.1"
}
```

## 9. dashboard

### 9.1 健康卡

- 顶部状态：`一切正常` / `有 N 个告警` / `数据不可用`。
- 三列：进程（bot/opencode/napcat）、bot（网关/最后消息/最后回复/待投）、资源（DB/磁盘/备份）。
- 每 10s 刷新；页面隐藏时暂停；失败显示上次成功时间。

### 9.2 事件卡

- 活动事件列表；severity 用现有红/黄/绿变量。
- “测试告警”按钮，禁用状态防重复点击。
- 历史事件可折叠；不自动滚动。

### 9.3 备份卡

- 表格：时间、原因、大小、校验、操作（下载/恢复/删除）。
- “立即备份”按钮；备份中显示 spinner。
- 恢复：文件选择 + 输入 `RESTORE` + 警告文案；提交后显示阶段进度（validate/stop/replace/start/health）。
- 删除：确认 `DELETE`。
- 空态：`还没有备份。建议先创建一次。`

### 9.4 锚点

新增 `id=health-card`、`id=alerts-card`、`id=backups-card`、`id=btn-test-alert`、`id=btn-backup-now`、`id=backup-restore-file`、`id=backup-restore-confirm`、`id=btn-backup-restore`、`id=audit-list`，供 Playwright 断言。

## 10. 配置

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

audit:
  path: data/audit.jsonl
  max_bytes: 10485760
  backups: 3
```

校验：

- `check_seconds` 5..3600。
- `repeat_seconds` ≥ 300。
- `webhook.format` 必须是 `generic|ntfy|wecom|dingtalk`。
- `qq_admin` 0 或正整数。
- `disk_free_percent` 1..50。
- `backup.dir` 非空；`daily_keep`/`weekly_keep` ≥0；`schedule_hour` 0..23；`max_total_gb` 1..1024；`max_upload_mb` 1..4096。
- `audit.max_bytes` ≥ 1MB；`backups` ≥1。

## 11. 失败处理

| 场景 | 行为 |
| --- | --- |
| bot health 超时 | 进程状态保留；`bot.ok=false`；告警规则继续评估 |
| 备份源库不存在 | 400/500；不创建空 manifest |
| 备份时 DB 被锁 | SQLite backup API + busy_timeout 5s；失败重试一次 |
| 上传文件过大 | 413；临时文件删除 |
| 校验失败 | 400；当前 DB 不动；临时文件删除 |
| 恢复时 bot 停不下来 | 5s 超时后 `taskkill /T /F`（复用 supervisor 现有逻辑）；失败则不替换 |
| 恢复后启动超时 | 回滚；`bot_started=false` |
| 回滚失败 | critical 告警；保留 pre-restore 文件；UI 显示手工恢复路径 |
| 告警 webhook 失败 | 日志 + 下一轮重试；不阻塞健康检查 |
| 审计写失败 | 记录 supervisor 日志；不阻塞主操作 |

## 12. 测试

### 12.1 单元

- `test_health_snapshot.py`：字段、读库失败、不含敏感字段。
- `test_alerts.py`：pending/firing/resolved、去重、repeat、恢复确认、通道失败、风暴保护。
- `test_backup_restore.py`：manifest、校验、保留、坏文件、恢复成功、恢复回滚。
- `test_audit.py`：append、轮转、查询、坏行跳过。
- `test_config_ops.py`：新增配置校验。

### 12.2 集成

- 临时 SQLite + fake bot + aiohttp test client。
- 备份→改数据→恢复→断言数据回退。
- 坏文件恢复→400→当前 DB hash 不变。
- 恢复后启动失败→回滚→旧数据仍在。
- dashboard 登录后 health/alerts/backups/audit 200；未登录 401。

### 12.3 E2E

- Playwright：登录 → 健康卡可见 → 测试告警 → 立即备份 → 备份列表出现新行 → 恢复确认按钮禁用/启用逻辑。
- 不在 E2E 里真实替换生产 DB；恢复流程用临时环境或集成测试覆盖。

## 13. 验收

1. 杀掉 bot，`alerts.enabled=true` 且配置 webhook 时，2 分钟内收到 `bot_down`；恢复后收到 resolved。
2. 备份列表能创建、下载、删除；manifest 的 sha256 与文件一致。
3. 上传损坏文件返回 400，当前 DB 的 sha256 不变。
4. 备份→修改数据→恢复→数据回到备份时点；bot 自动启动。
5. 恢复后启动失败时自动回滚，旧 DB 可用。
6. dashboard 未登录访问 health/detail、backups、audit 均 401。
7. 全量 `pytest -q` 通过；新增测试覆盖上述路径。

## 14. 实施步骤

1. `Store.SCHEMA_VERSION` + `PRAGMA user_version` + `db_stats()`；测试旧库升级。
2. `AuditLog` + 配置 + 单元测试。
3. `BackupManager` + manifest + 校验 + 保留 + 单元测试。
4. `RestoreService` + 锁 + 回滚 + 集成测试。
5. bot `health_snapshot()` + `/api/health/detail` + `/api/notify/admin`。
6. `HealthMonitor` + `AlertManager` + 通道 + 规则测试。
7. dashboard 路由/UI + Playwright 锚点。
8. `config.yaml`/README/`.env.example` 文档。
9. 全量测试 + 提交。

## 15. 风险

- **SQLite 备份与 WAL**：必须用 backup API，不能直接复制 `.db`；测试覆盖 WAL 中已提交数据。
- **Windows 文件替换**：bot 未完全退出时 `os.replace` 会失败；必须等待进程退出并清理 `-wal/-shm`。
- **告警误报**：NapCat/网关抖动可能触发；用 pending + 连续恢复确认降低噪音。
- **磁盘指标路径**：`shutil.disk_usage` 在 Windows 网络盘可能慢；超时/异常时返回 unknown，不阻塞健康。
- **备份体积**：大库备份可能超过 30s；dashboard 请求超时需单独设置，UI 显示进行中。
- **审计文件增长**：轮转 + 保留；不把审计放进备份，避免恢复时覆盖审计历史。
