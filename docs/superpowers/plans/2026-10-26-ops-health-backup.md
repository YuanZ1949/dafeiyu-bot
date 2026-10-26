# 分册 A：可观测与可恢复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 supervisor 知道哪里不对、主动告警、数据坏了能安全换回来，并把操作留痕。

**Architecture:** bot 侧新增结构化健康快照与 `/api/health/detail`、`/api/notify/admin`；supervisor 新增 HealthMonitor（聚合进程/bot/ NapCat/磁盘/备份）、AlertManager（规则状态机 + Webhook/QQ 通道）、BackupManager（SQLite backup API + manifest + 保留）、RestoreService（校验/预备份/停-换-起/回滚）、AuditLog（JSONL + 轮转）；dashboard 暴露运维 API 并新增三张只读卡。所有新配置默认保持旧行为。

**Tech Stack:** Python 3.11、aiohttp、sqlite3、PyYAML、标准库（无新增运行时依赖）。

**Spec:** `docs/superpowers/specs/2026-10-26-ops-health-backup-design.md`（总设计：`2026-10-26-ops-quality-suite-design.md`）

## Global Constraints

- 不新增运行时依赖；只用标准库与现有 aiohttp/PyYAML/sqlite3。
- bot SQLite 只由 bot 写；supervisor 只读备份，恢复时先停 bot。
- 新增配置默认不改变旧行为：`alerts.enabled=false`、`backup.enabled=true`（只写本地）、预算字段本轮不做。
- 告警、审计、健康快照不得包含消息正文、昵称、token、API Key、完整 prompt。
- 所有状态变更接口：登录（dashboard）或 control token + 同源检查（沿用 `_same_origin`）+ 审计。
- 恢复必须二次确认 `confirm=RESTORE`；删除备份可带 `confirm=DELETE`。
- 每个分册独立提交；提交前 `python -m pytest -q` 全绿。

## Review Focus

- **WAL 未合并**：bot 正在写 WAL 时备份必须包含已提交数据，不能用文件复制。
- **Windows 文件占用**：bot 未退出、`-wal/-shm` 未清理时 `os.replace` 会失败；必须停稳并清理 sidecar。
- **坏上传**：截断/非 SQLite/缺表文件必须 400，当前 DB 的 sha256 不变，临时文件删除。
- **告警风暴/通道失败**：同 fingerprint 去重 + repeat + 单轮上限；通道异常不得中断健康检查。
- **审计轮转**：达到 max_bytes 后轮转且保留指定份数；坏行跳过而不是整表报错。

---

### Task 1: 配置段与 Store 版本/统计

**Files:**
- Modify: `src/dafeiyu/config.py`（dataclass + `load_settings`）
- Modify: `src/dafeiyu/memory/store.py`（`SCHEMA_VERSION`、`user_version`、`db_stats`、`usage_totals`、`error_stats`）
- Test: `tests/test_config_ops.py`、`tests/test_store_schema_version.py`

**Interfaces:**
- Produces:
  - `AlertsConfig(enabled, check_seconds, repeat_seconds, webhook: WebhookConfig, qq_admin, rules: AlertRulesConfig)`
  - `BackupConfig(enabled, dir, daily_keep, weekly_keep, schedule_hour, max_total_gb, max_upload_mb)`
  - `AuditConfig(path, max_bytes, backups)`
  - `Settings.alerts/backup/audit`
  - `Store.SCHEMA_VERSION = 1`
  - `Store.db_stats() -> {"db_path","db_bytes","wal_bytes","message_count","schema_version"}`
  - `Store.usage_totals(start_date: str, end_date: str) -> tuple[int, int]`
  - `Store.error_stats(window_seconds: int) -> {"count": int, "last_at": str | None}`

- [ ] **Step 1: Write failing config tests**

```python
def test_ops_config_defaults(tmp_path):
    s = load(tmp_path)
    assert s.alerts.enabled is False
    assert s.alerts.check_seconds == 30
    assert s.alerts.webhook.format == "generic"
    assert s.backup.enabled is True
    assert s.backup.dir == "data/backups"
    assert s.audit.path == "data/audit.jsonl"

@pytest.mark.parametrize("extra, message", [
    ("alerts:\n  check_seconds: 2\n", "alerts.check_seconds"),
    ("alerts:\n  repeat_seconds: 60\n", "alerts.repeat_seconds"),
    ("alerts:\n  webhook:\n    format: slack\n", "alerts.webhook.format"),
    ("backup:\n  schedule_hour: 24\n", "backup.schedule_hour"),
    ("backup:\n  max_upload_mb: 0\n", "backup.max_upload_mb"),
    ("audit:\n  max_bytes: 1024\n", "audit.max_bytes"),
])
def test_ops_config_rejects_bad_values(tmp_path, extra, message):
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, extra_yaml=extra)
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config_ops.py -q`
Expected: FAIL（`AlertsConfig` 不存在 / 无 `alerts` 属性）

- [ ] **Step 3: Implement config**

在 `config.py` 增加三个 dataclass 与 `_build_alerts/_build_backup/_build_audit`，并在 `load_settings` 中调用。校验规则严格照 spec §10：

- `check_seconds` 5..3600；`repeat_seconds` ≥300；`webhook.format ∈ {generic,ntfy,wecom,dingtalk}`；`qq_admin ≥0`。
- `rules.disk_free_percent` 1..50；`reply_error_window_seconds ≥60`；`reply_error_count ≥1`。
- `backup.dir` 非空；`daily_keep/weekly_keep ≥0`；`schedule_hour 0..23`；`max_total_gb 1..1024`；`max_upload_mb 1..4096`。
- `audit.path` 非空；`max_bytes ≥1048576`；`backups ≥1`。
- `alerts/backup/audit` 段缺省时使用默认值，不报错（旧配置兼容）。

- [ ] **Step 4: Write failing store tests**

```python
def test_new_db_sets_schema_version(tmp_path):
    s = Store(tmp_path / "t.db"); s.init_schema()
    assert s.db_stats()["schema_version"] == 1

def test_old_db_upgrades_schema_version(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path); conn.execute("CREATE TABLE messages (id INTEGER)")
    conn.commit(); conn.close()
    s = Store(path); s.init_schema()
    assert s.db_stats()["schema_version"] == 1

def test_db_stats_counts_messages(tmp_path):
    s = Store(tmp_path / "t.db"); s.init_schema()
    assert s.db_stats()["message_count"] == 0
    assert s.db_stats()["db_bytes"] > 0

def test_error_stats_window(tmp_path):
    s = Store(tmp_path / "t.db"); s.init_schema()
    s.record_usage(..., purpose="error")  # 用现有写入方法
    assert s.error_stats(900)["count"] == 1
```

- [ ] **Step 5: Implement store changes**

`init_schema()` 末尾执行 `PRAGMA user_version`：读当前值，`0` 时写 1；`>SCHEMA_VERSION` 时抛 `RuntimeError("数据库版本 X 高于程序支持版本 1")`。`db_stats()` 用 `Path.stat()` 计算 db/wal 字节，`SELECT COUNT(*) FROM messages`。`usage_totals` 用 `SUM(prompt_tokens+completion_tokens)`，日期闭区间。`error_stats` 用 `usage WHERE purpose='error' AND created_at >= datetime('now', ?)`。

- [ ] **Step 6: Run both test files, then commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config_ops.py tests/test_store_schema_version.py -q`
Expected: PASS
Commit: `feat: add ops config and store schema version`

---

### Task 2: AuditLog

**Files:**
- Create: `src/dafeiyu/supervisor/audit.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Produces:
  - `class AuditLog`
  - `AuditLog(path: Path, max_bytes: int = 10*1024*1024, backups: int = 3, sanitize: Callable[[dict], dict] | None = None, clock: Callable[[], datetime] | None = None)`
  - `write(action, *, actor="system", target="", result="ok", detail=None, request_id="", ip="") -> dict`
  - `query(limit=50, before=None) -> list[dict]`
- 事件字段严格为 spec §8.3 的 9 个键。

- [ ] **Step 1: Write failing tests**

```python
def test_append_and_query_newest_first(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", clock=fake_clock)
    log.write("backup.create", actor="dashboard:admin@127.0.0.1", target="a.db")
    log.write("backup.delete", target="b.db")
    rows = log.query(limit=10)
    assert [r["action"] for r in rows] == ["backup.delete", "backup.create"]
    assert set(rows[0]) == {"id","ts","actor","action","target","result","detail","request_id","ip"}

def test_bad_lines_are_skipped(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"action":"ok"}\nnot-json\n', encoding="utf-8")
    assert [r["action"] for r in AuditLog(path).query()] == ["ok"]

def test_rotation_keeps_backups(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=300, backups=2)
    for i in range(20):
        log.write("x.y", detail={"i": i})
    assert (tmp_path / "audit.jsonl").exists()
    assert len(list(tmp_path.glob("audit.jsonl.*"))) <= 2

def test_sanitize_hook_is_applied(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", sanitize=lambda d: {"ok": True})
    log.write("x", detail={"token": "secret"})
    assert log.query()[0]["detail"] == {"ok": True}
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_audit.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: Implement**

写入：`sanitize` → `json.dumps(ensure_ascii=False)` → append + newline；`OSError` 只记 `logging.warning`，不抛。写后检查大小，超过 `max_bytes` 就 `Path.replace` 成 `audit.jsonl.1`，旧 `.N` 顺延，删除超出 `backups` 的。`query` 从当前文件 + 轮转文件倒序读取，跳过坏行，按 `ts` 倒序，`before` 为严格小于。

- [ ] **Step 4: Run tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_audit.py -q`
Expected: PASS
Commit: `feat: add supervisor audit log`

---

### Task 3: BackupManager

**Files:**
- Create: `src/dafeiyu/supervisor/backup.py`
- Test: `tests/test_backup_restore.py`（本轮只加备份/校验/保留用例）

**Interfaces:**
- Produces:
  - `REQUIRED_TABLES: tuple[str, ...]`、`REQUIRED_COLUMNS: dict[str, tuple[str, ...]]`
  - `validate_database(path: Path, max_schema_version: int = 1) -> tuple[bool, str]`
  - `@dataclass(frozen=True) BackupManifest(id, filename, created_at, reason, size_bytes, sha256, schema_version, integrity_ok, app_version)`
  - `class BackupManager(db_path, backup_dir, *, daily_keep=7, weekly_keep=4, max_total_gb=20, audit=None, clock=None)`
  - `async create(reason="manual") -> BackupManifest`
  - `list() -> list[BackupManifest]`
  - `delete(backup_id: str) -> None`
  - `prune() -> list[str]`
  - `status() -> {"last_success_at","last_error_at","count","running"}`
  - `last_scheduled_date() -> str | None`

- [ ] **Step 1: Write failing tests**

```python
async def test_create_writes_manifest_and_hash(tmp_path):
    db = make_db(tmp_path / "dafeiyu.db")
    m = BackupManager(db, tmp_path / "backups")
    manifest = await m.create("manual")
    path = tmp_path / "backups" / manifest.filename
    assert path.exists()
    assert manifest.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest.integrity_ok is True
    assert json.loads((path.with_suffix(".json")).read_text())["id"] == manifest.id

async def test_backup_includes_committed_wal_data(tmp_path):
    db = make_db(tmp_path / "dafeiyu.db")
    s = Store(db); s.init_schema(); s.add_message(msg)  # 不 close，保持 WAL
    manifest = await BackupManager(db, tmp_path / "b").create()
    with sqlite3.connect(tmp_path / "b" / manifest.filename) as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1

def test_validate_rejects_truncated_file(tmp_path):
    bad = tmp_path / "bad.db"; bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 10)
    ok, reason = validate_database(bad)
    assert ok is False and reason

async def test_retention_keeps_recent_scheduled(tmp_path):
    # 造 10 个 scheduled manifest（不同日期），daily_keep=3
    removed = await make_manager(daily_keep=3).prune()
    assert len(removed) == 7

def test_delete_rejects_path_traversal(tmp_path):
    with pytest.raises(BackupError):
        BackupManager(tmp_path/"d.db", tmp_path/"b").delete("../../x")
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backup_restore.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: Implement**

`create` 用 `asyncio.to_thread` 跑同步流程：只读 URI 连接源库（不存在抛 `BackupError`）→ `src.backup(dest)` 到临时文件 → `fsync` → sha256 → `os.replace` 到 `dafeiyu-YYYYmmdd-HHMMSS-<reason>.db` → `validate_database` → 写同名 manifest（临时文件 + replace）→ `prune()` → 审计 `backup.create`。文件名用本地时间（`ZoneInfo("Asia/Shanghai")`，项目已有 `timeutil`）。

`validate_database`：header `SQLite format 3\0`；`PRAGMA integrity_check == "ok"`；`PRAGMA user_version` 在 `0..max_schema_version`；必需表存在；必需列抽查。返回 `(False, "原因")` 而不是抛。

`prune`：`scheduled` 按日期倒序保留最近 `daily_keep`；周日备份额外保留 `weekly_keep`；`manual/pre-restore` 不自动删；总量超过 `max_total_gb` 时从最旧 scheduled 删，至少留 3 个。文件缺失时清 manifest 并记日志。

- [ ] **Step 4: Run tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backup_restore.py -q`
Expected: PASS
Commit: `feat: add backup manager with manifest and retention`

---

### Task 4: RestoreService

**Files:**
- Create: `src/dafeiyu/supervisor/restore.py`
- Test: `tests/test_backup_restore.py`（恢复用例追加）

**Interfaces:**
- Produces:
  - `class RestoreError(Exception)`，属性 `stage: str`、`status: int`
  - `class RestoreService(*, db_path, backups, supervisor, audit=None, bot_name="bot", ready_check: Callable[[], Awaitable[bool]] | None = None, ready_timeout=30.0)`
  - `busy: bool`
  - `async restore(upload_path: Path, *, actor: str, ip: str = "", request_id: str = "") -> dict`
  - 返回 spec §7.3 结构；失败抛 `RestoreError`，不返回堆栈。

- [ ] **Step 1: Write failing tests**

```python
async def test_restore_replaces_db_and_returns_summary(tmp_path):
    # 备份 v1 -> 写入 v2 -> restore
    result = await service.restore(upload)
    assert result["ok"] is True
    assert result["bot_started"] is True
    assert supervisor.calls == [("stop","bot"), ("start","bot")]
    assert read_rows(db) == ["v1"]
    assert result["pre_restore_backup"].endswith("-pre-restore.db")

async def test_corrupt_upload_leaves_db_unchanged(tmp_path):
    before = sha256(db)
    with pytest.raises(RestoreError) as exc:
        await service.restore(bad)
    assert exc.value.stage == "validate" and exc.value.status == 400
    assert sha256(db) == before
    assert supervisor.calls == []

async def test_start_failure_rolls_back(tmp_path):
    supervisor.fail_start = True
    with pytest.raises(RestoreError) as exc:
        await service.restore(upload)
    assert exc.value.stage == "health"
    assert read_rows(db) == ["v2"]  # 回滚到 pre-restore
    assert ("start","bot") in supervisor.calls

async def test_concurrent_restore_is_rejected(tmp_path):
    service._lock ...  # 先占用
    with pytest.raises(RestoreError) as exc:
        await service.restore(upload)
    assert exc.value.status == 409
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backup_restore.py -q`
Expected: FAIL（`RestoreService` 不存在）

- [ ] **Step 3: Implement**

流程严格按 spec §7.1。要点：
- `asyncio.Lock()`；拿不到立即 409（`locked()` 判断）。
- validate 失败 → 400，不碰 DB、不建预备份。
- `await backups.create("pre-restore")` 失败 → 400（stage=backup）。
- `await supervisor.stop(bot_name)` 失败 → 500（stage=stop），不替换。
- 删 `db-wal`、`db-shm`；`os.replace(upload, db)`（同盘失败时 `shutil.move` + 明确错误）。
- `await supervisor.start(bot_name)`；再 `ready_check()`（默认检查 `/api/health/detail` 200）；失败 → 停 bot、把 pre-restore 文件换回、重启、抛 `stage=health`。
- 每阶段写审计 `backup.restore`（result ok/fail，detail 带 stage/duration）；不记录文件内容。

- [ ] **Step 4: Run tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backup_restore.py -q`
Expected: PASS
Commit: `feat: add guarded database restore with rollback`

---

### Task 5: bot 健康快照与运维接口

**Files:**
- Modify: `src/dafeiyu/control.py`
- Modify: `src/dafeiyu/gateway/qq.py`（时间戳）
- Modify: `src/dafeiyu/delivery/scheduler.py`（计数）
- Test: `tests/test_health_snapshot.py`、`tests/test_control_api.py`（追加）

**Interfaces:**
- Produces:
  - `BrainControl.health_snapshot() -> dict`（spec §4.1 结构）
  - `GET /api/health` → `{"ok": True}`（不变）
  - `GET /api/health/detail` → 快照（需 token）
  - `POST /api/notify/admin` body `{"text","dedupe_key"}` → 200/400/429/503
  - `QQAdapter.last_event_at: float | None`、`last_disconnect_at: float | None`、`last_send_at: float | None`
  - `BatchScheduler.pending_count() -> int`、`inflight_count() -> int`

- [ ] **Step 1: Write failing tests**

```python
def test_health_snapshot_has_no_message_content(control):
    snap = control.health_snapshot()
    assert snap["ok"] is True
    assert set(snap) >= {"generated_at","uptime_seconds","version","gateway","traffic",
                         "scheduler","budget","quota","brain","memory","errors"}
    text = json.dumps(snap, ensure_ascii=False)
    assert "hello" not in text and "sk-" not in text

def test_health_detail_requires_token(control):
    # app = create_app(control, token="t")
    client.get("/api/health") -> 200 {"ok": True}
    client.get("/api/health/detail") -> 401
    client.get("/api/health/detail", headers={"X-Control-Token":"t"}) -> 200

async def test_notify_admin_validation(control):
    # 缺 text -> 400；>1000 字 -> 400；10 秒内第二条 -> 429；adapter 未连接 -> 503
    assert sent == ["【大肥鱼告警】..."]
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_health_snapshot.py tests/test_control_api.py -q`
Expected: FAIL（`health_snapshot`/路由不存在）

- [ ] **Step 3: Implement**

- `control.py` 顶部 `PROCESS_STARTED_AT = time.monotonic()`；`health_snapshot()` 全部字段用 `getattr(..., None)` 容错，读库失败设 `ok=False` 并填 `errors.last_error_at`，不抛。
- gateway/scheduler 只加时间戳与计数属性，不改消息语义。
- `/api/notify/admin`：token 中间件已保护；`text` 去空白、1..1000 字；`dedupe_key` 可选 ≤64 字；进程内 `last_notify_at` 限流 10s；目标 `settings.groups.admins` 第一个，空则 503；`adapter.send(PRIVATE, target, text)`；发送异常 503。
- 注册路由。

- [ ] **Step 4: Run tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_health_snapshot.py tests/test_control_api.py -q`
Expected: PASS
Commit: `feat: expose bot health snapshot and admin notify`

---

### Task 6: HealthMonitor 与 AlertManager

**Files:**
- Create: `src/dafeiyu/supervisor/health.py`
- Create: `src/dafeiyu/supervisor/alerts.py`
- Create: `src/dafeiyu/supervisor/ops.py`（`OpsServices` dataclass）
- Test: `tests/test_health_monitor.py`、`tests/test_alerts.py`

**Interfaces:**
- Produces:
  - `HealthMonitor(*, supervisor, settings, bot_url, token, backups, napcat_login=None, session=None, clock=None)`；`async snapshot() -> dict`；`async close()`
  - `@dataclass Incident(id, rule, severity, status, fingerprint, title, detail, first_seen_at, last_seen_at, resolved_at, notify_count)`
  - `class AlertManager(cfg, state_path, channels, *, audit=None, clock=None)`
  - `async evaluate(snapshot: dict) -> list[Incident]`
  - `events(include_resolved_hours=24) -> list[dict]`
  - `summary() -> {"firing","resolved_24h"}`
  - `async send_test() -> dict`
  - `class WebhookChannel(url, format="generic")`、`class QQAdminChannel(bot_url, token)`，均实现 `async send(incident, event)`.

- [ ] **Step 1: Write failing alert tests**

```python
async def test_pending_then_firing(tmp_path):
    mgr = AlertManager(cfg.pending, tmp_path/"s.json", [fake_channel], clock=clock)
    snap = down_snapshot()
    await mgr.evaluate(snap); assert fake_channel.sent == []      # pending
    clock.advance(121); await mgr.evaluate(snap)
    assert fake_channel.sent[0][1] == "firing"

async def test_recovery_needs_two_checks(tmp_path):
    ... firing ...
    await mgr.evaluate(up_snapshot()); assert status == "firing"
    await mgr.evaluate(up_snapshot()); assert status == "resolved"
    assert fake_channel.sent[-1][1] == "resolved"

async def test_repeat_is_deduped(tmp_path):
    # firing 后推进 6h，只再通知一次
    ...

def test_channel_failure_does_not_raise(tmp_path):
    channel = ExplodingChannel()
    await mgr.evaluate(down_snapshot())  # 不抛
    assert mgr.summary()["firing"] == 1
```

- [ ] **Step 2: Write failing health tests**

```python
async def test_snapshot_aggregates_and_marks_bot_timeout(tmp_path):
    mon = HealthMonitor(supervisor=fake_sup, ..., bot_url="http://127.0.0.1:1")
    snap = await mon.snapshot()
    assert snap["processes"]["bot"]["status"] == "stopped"
    assert snap["bot"]["ok"] is False
    assert "disk" in snap and "backups" in snap
```

- [ ] **Step 3: Run and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_health_monitor.py tests/test_alerts.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 4: Implement HealthMonitor**

- 并行：`supervisor.status()`（同步）、bot `GET /api/health/detail`（2s 超时，用注入/自建 aiohttp session）、napcat login state、`shutil.disk_usage(REPO_ROOT)`、`backups.status()`。
- bot 失败/超时：`bot={"ok": False, "error": "..."}`，进程状态照常返回；`ok` 总体 = 进程无 failed 且 bot.ok。
- 磁盘异常 → `{"path":..., "free_bytes": null, ...}`，不抛。
- `close()` 关闭自建 session。

- [ ] **Step 5: Implement AlertManager**

- 每次 `evaluate`：先把 snapshot 归一成 `conditions: dict[fingerprint, (active, severity, title, detail)]`，再按 8 条规则计算。
- 状态持久化：`{"incidents": {...}, "since": {...}, "recover": {...}}`，临时文件 + `os.replace`。
- pending 秒数来自 `AlertsConfig.rules`；`process_failed` pending 0；`bot_down/opencode_down/napcat_down` 用配置秒数；其余 0。
- firing 去重：同 fingerprint 只更新 `last_seen_at`；repeat 到点才再发。
- 恢复连续 2 次；resolved 发一次，保留 24h。
- 单轮最多 5 条，超出记日志并合并一条 `alert_flood`（不进持久状态）。
- 通道失败 `try/except` + `log.warning`，不触发新告警。
- Webhook payload 照 spec §5.3；`ntfy/wecom/dingtalk` 在通用 JSON 基础上加各自 `text/markdown` 字段。
- `QQAdminChannel` 跳过 `bot_down`（spec §5.3 最后一行）。

- [ ] **Step 6: Run tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_health_monitor.py tests/test_alerts.py -q`
Expected: PASS
Commit: `feat: add supervisor health monitor and alert state machine`

---

### Task 7: dashboard 运维 API 与三张卡

**Files:**
- Modify: `src/dafeiyu/supervisor/dashboard.py`
- Modify: `src/dafeiyu/supervisor/dashboard/index.html`
- Test: `tests/test_supervisor_ops_api.py`、`tests/test_dashboard_auth_app.py`（追加）

**Interfaces:**
- `create_app(..., ops: OpsServices | None = None)`；`serve(..., ops=None)`。
- 新增路由（全部受现有登录中间件）：
  - `GET /api/health/detail` → `ops.health.snapshot()` + `alerts` 汇总
  - `GET /api/alerts`、`POST /api/alerts/test`
  - `GET/POST /api/backups`、`DELETE /api/backups/{id}`、`POST /api/backups/restore`（multipart）
  - `GET /api/audit?limit&before`
- 登录成功/失败、登出写 `auth.login/auth.logout` 审计；`/api/control/*` 写 `process.start|stop|restart`；`/api/shutdown` 写 `shutdown`。

- [ ] **Step 1: Write failing API tests**

```python
async def test_ops_routes_require_auth(tmp_path):
    app, _ = make_ops_app(tmp_path, auth=make_auth())
    for path in ("/api/health/detail", "/api/backups", "/api/alerts", "/api/audit"):
        resp = await client.get(path)
        assert resp.status == 302 if ... else 401   # 按现有中间件行为断言

async def test_backup_create_list_delete(tmp_path):
    resp = await client.post("/api/backups", json={"reason":"manual"})
    assert resp.status == 200 and data["backup"]["reason"] == "manual"
    listed = await client.get("/api/backups")
    assert listed.status == 200 and len(listed["backups"]) == 1
    deleted = await client.delete(f"/api/backups/{id}", json={"confirm":"DELETE"})
    assert deleted.status == 200

async def test_restore_requires_confirm_and_file(tmp_path):
    resp = await client.post("/api/backups/restore", data={})
    assert resp.status == 400
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_supervisor_ops_api.py -q`
Expected: FAIL（路由 404）

- [ ] **Step 3: Implement API**

- `ops is None` 时新路由统一 503 `{"error":"运维服务未启用"}`，保证旧测试不炸。
- 上传：`request.multipart()` 读 `file`，边读边计数，超过 `max_upload_mb*1024*1024` → 413 并删临时文件；字段名 `confirm` 必须为 `RESTORE`。
- 读取请求体时复用 `_json_body`；DELETE 的 confirm 可选（缺省也允许，spec §6.6 写“可带”）。
- 审计 actor 取 `dashboard:{username}@{ip}`，从 auth session 取用户名；无登录时 `dashboard:local@ip`。
- 登录处理器在成功/失败后 `audit.write`；登出同样。

- [ ] **Step 4: Implement UI**

在 `index.html` 的 `<main>` 顶部（`#processes` 之后）插入三张卡，沿用现有 `.card/.grid-3/.kv/.pill/.btn` 类：

- `#health-card`：顶部状态、进程三列、bot 指标、资源三列。
- `#alerts-card`：`#alerts-list`、`#btn-test-alert`、`#alerts-hint`。
- `#backups-card`：`#backup-list`、`#btn-backup-now`、`#backup-restore-file`、`#backup-restore-confirm`、`#btn-backup-restore`、`#backups-hint`；`#audit-list` 放在备份卡下方或事件卡内。
- JS 追加：`refreshHealth()` 10s 定时、页面隐藏暂停；`refreshAlerts()`；`refreshBackups()`；按钮事件；401 交给现有全局处理。不要改既有卡片顺序与主题变量。

- [ ] **Step 5: Run tests + static anchor check and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_supervisor_ops_api.py tests/test_dashboard_auth_app.py tests/test_supervisor_dashboard.py -q`
Plus: `Select-String -Path ...\index.html -Pattern 'id="(health-card|alerts-card|backups-card|btn-test-alert|btn-backup-now|btn-backup-restore)"'` 每条至少 1 次。
Commit: `feat: add dashboard ops cards and APIs`

---

### Task 8: 装配、定时循环、文档与全量验证

**Files:**
- Modify: `src/dafeiyu/supervisor/main.py`
- Modify: `config.yaml`、`.env.example`、`README.md`
- Test: `tests/test_supervisor_ops_wiring.py`（可只测纯函数 `build_ops`）

**Interfaces:**
- Produces: `build_ops(settings, supervisor, logs, bot_url, token, napcat_login=None) -> OpsServices`
- `alert_loop(ops, settings, stop_event)`、`backup_loop(ops, settings, stop_event)`（30s/60s 轮询，异常只记日志）

- [ ] **Step 1: Write failing wiring test**

```python
def test_build_ops_uses_configured_paths(tmp_path, settings):
    ops = build_ops(settings, fake_supervisor, logs, "http://127.0.0.1:1", "t")
    assert ops.backups.backup_dir == REPO_ROOT / settings.backup.dir
    assert ops.audit.path == REPO_ROOT / settings.audit.path
```

- [ ] **Step 2: Implement wiring**

- `run()`：构造 `audit → backups → restore → health → alerts → ops`，`serve(..., ops=ops)`。
- 启动 `alert_loop`（仅 `settings.alerts.enabled`）与 `backup_loop`（仅 `settings.backup.enabled`）；`finally` 取消并 `health.close()`。
- 备份循环：每 60s，`now.hour == schedule_hour` 且今天无 `scheduled` 成功备份 → `create("scheduled")`；失败记 `backup_failed`。
- `ready_check`：`GET {bot_url}/api/health/detail` + token，状态 200 且 `ok=True`。

- [ ] **Step 3: Docs**

- `config.yaml` 追加 `alerts/backup/audit` 段（照 spec §10，alerts 保持 false）。
- `.env.example`：说明 `DAFEIYU_DASHBOARD_*` 已存在即可，不新增 secret。
- `README.md`：运维 runbook（健康/告警/备份/恢复/审计 + 失败回滚 + 不恢复 OpenCode DB）。

- [ ] **Step 4: Full verification**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: 全绿（≥597 + 新增）
Run: `git status --short`
Expected: 只有本轮应提交的源码/测试/文档。

- [ ] **Step 5: Commit**

`feat: wire supervisor ops services`

---

## Self-Review (plan)

- Spec §4–§11 均有对应任务；§12 测试分布到各任务；§13 验收由契约覆盖。
- 分册 B/C 明确排除，未偷跑。
- 类型命名前后一致：`OpsServices`、`BackupManifest`、`RestoreError.stage`、`AlertManager.summary`。
- Review Focus 的五项均有测试：WAL（Task 3）、Windows 停替（Task 4）、坏上传（Task 4）、告警风暴（Task 6）、审计轮转（Task 2）。
