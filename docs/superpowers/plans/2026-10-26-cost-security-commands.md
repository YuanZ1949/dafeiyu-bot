# 分册 B：成本、安全与命令 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为已有 bot 补齐全局/月度/费用预算、日志与审计脱敏、control token 加固与轮换、命令注册表、按用户删除消息五项能力。

**Architecture:** 预算判定下沉到无状态 `BudgetGuard`，由 Router、Service、BrainControl 共用；usage 聚合走 `Store.usage_between`。脱敏在 `LogHub.write` 与 `AuditLog.write` 两个入口一次完成。token 生命周期收进 `ControlToken`，dashboard 每请求读取当前值。命令统一由 `CommandRegistry` 匹配与限流。按用户删除只删原始消息与其投递记录，不动摘要/印象。

**Tech Stack:** Python 3.11+、aiohttp、SQLite（stdlib sqlite3）、pytest + pytest-asyncio（`asyncio_mode=auto`），无新增运行时依赖。

**Spec:** `docs/superpowers/specs/2026-10-26-cost-security-commands-design.md`

## Global Constraints

- 只做分册 B；不改分册 A 的备份/恢复/健康语义，不改消息处理、触发规则、记忆抽取与调度语义。
- 不新增运行时依赖；不引入 TLS、多用户、OIDC；不做分册 C。
- `budget.daily_calls` / `daily_tokens` 仍是每群每日硬上限，必须 > 0。
- 预算里所有 `0` 表示“未启用”，跳过对应检查；`on_exceed` 只影响全局日与月度 calls/tokens，月度费用超限无论如何 DENY。
- 预算只统计 `purpose IN ('reply','ambient')`；其他 purpose 直接 ALLOW。
- 日期一律上海时区 `YYYY-MM-DD`；月份 `YYYY-MM`；使用 `dafeiyu.timeutil.now_shanghai/SHANGHAI`，不使用 `ZoneInfo`。
- 所有新增用户可见文案用中文；日志/审计不得出现消息正文、token、密码、API Key。
- 破坏性操作 confirm 字符串固定：`DELETE`、`RESTORE`、`ROTATE`、`DELETE_USER`。
- 每个任务结束必须 `python -m pytest <本任务测试> -q` 全绿后再提交。
- 开工前把分册 B 验收项写进 `.tamako/contract.json`（本地、git 忽略），收尾时逐条闭环。
- 前端只加运维卡片/控件，不改现有视觉主题，不重排既有卡片。

## Review Focus

- **预算零值**：全局/月度/费用限制为 0 时不得误拦；费用限额 >0 但单价全 0 必须在加载配置时报错。
- **边界相等**：usage 恰好等于限额、ambient 恰好等于 `daily * ambient_ratio` 时的动作必须与 spec 一致（达到即不可再放行）。
- **脱敏误伤**：中文“token 用量”不得被替换；`Authorization: Bearer xxx`、`DEEPSEEK_API_KEY=xxx`、`Set-Cookie:` 必须整段脱敏。
- **轮换失败**：写盘或 bot 重启失败时旧 token 必须继续可用，且 bot 只重启一次。
- **命令权限与冷却**：群内非管理员执行管理员命令必须静默；冷却按 `(command, chat_type, chat_id)` 隔离，私聊按 `(command, private, user_id)`。
- **按用户删除边界**：只删该 chat + user 的 `messages` 与对应 `delivered_messages`，其他用户、摘要、印象、session_summaries 必须保留。

---

### Task 1: 预算配置与 usage 聚合

**Files:**
- Modify: `src/dafeiyu/config.py:52-57`（`BudgetConfig`）、`src/dafeiyu/config.py:806-808`（budget 构建）
- Modify: `src/dafeiyu/memory/store.py:71-82`（usage schema 索引）、`src/dafeiyu/memory/store.py:1503`（`usage_totals` 之后）
- Create: `tests/test_budget_config.py`
- Create: `tests/test_usage_queries.py`

**Interfaces:**
- Consumes: `Settings.budget`、`Store._conn`、`ChatType`（`dafeiyu.models`）。
- Produces:
  - `BudgetConfig(daily_calls:int, daily_tokens:int, ambient_ratio:float=0.8, global_daily_calls:int=0, global_daily_tokens:int=0, monthly_calls:int=0, monthly_tokens:int=0, monthly_cost_limit:float=0.0, currency:str="CNY", price_input_per_million:float=0.0, price_output_per_million:float=0.0, warning_ratios:tuple[float,...]=(0.5,0.8,1.0), on_exceed:str="degrade")`
  - `@dataclass(frozen=True) class UsageTotals: calls:int; prompt_tokens:int; completion_tokens:int; estimated:bool=False`，属性 `tokens -> int` 返回两者之和；`estimated` 表示区间内是否含估算用量（`MAX(estimated)=1`）。
  - `Store.usage_between(start_date:str, end_date:str, *, purposes:tuple[str,...]|None=None, chat_type:ChatType|None=None, chat_id:int|None=None) -> UsageTotals`
  - `Store.usage_global_today(date:str) -> UsageTotals`
  - `Store.usage_month(month:str) -> UsageTotals`（`month` 形如 `2026-10`）

- [ ] **Step 1: 写失败测试**

`tests/test_budget_config.py`：

```python
import yaml
from dafeiyu.config import ConfigError, load_settings
from conftest import TEST_CONFIG_YAML
import pytest

def write(tmp_path, budget: dict):
    cfg = yaml.safe_load(TEST_CONFIG_YAML)
    cfg["budget"] = budget
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return load_settings(path)

def test_budget_defaults(tmp_path):
    s = write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000})
    b = s.budget
    assert (b.global_daily_calls, b.monthly_calls, b.monthly_cost_limit) == (0, 0, 0.0)
    assert (b.price_input_per_million, b.price_output_per_million) == (0.0, 0.0)
    assert b.warning_ratios == (0.5, 0.8, 1.0)
    assert b.on_exceed == "degrade"
    assert b.currency == "CNY"

def test_warning_ratios_sorted_unique(tmp_path):
    s = write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "warning_ratios": [1.0, 0.5, 0.8, 0.5]})
    assert s.budget.warning_ratios == (0.5, 0.8, 1.0)

@pytest.mark.parametrize("ratio", [0, 1.1, -0.1])
def test_warning_ratio_out_of_range(tmp_path, ratio):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "warning_ratios": [ratio]})

def test_on_exceed_enum(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "on_exceed": "block"})

def test_cost_limit_requires_price(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "monthly_cost_limit": 10})

def test_negative_budget_values_rejected(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "monthly_calls": -1})

def test_daily_limits_must_be_positive(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 0, "daily_tokens": 1000})

def test_warning_ratios_cannot_be_empty(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "warning_ratios": []})
```

`tests/test_usage_queries.py`：用 `Store(tmp_path/"t.db")`，辅助函数直接 `store._conn.execute("INSERT INTO usage (...) VALUES (?,?,?,?,?,?,?,?)", ...)` 写指定日期；断言：

```python
def test_usage_between_is_closed_interval_and_filters(store):
    totals = store.usage_between("2026-10-01", "2026-10-31", purposes=("reply","ambient"), chat_type=ChatType.GROUP, chat_id=1)
    assert totals.calls == 2
    assert totals.prompt_tokens == 30
    assert totals.completion_tokens == 12
    assert totals.tokens == 42

def test_usage_month_and_global_today(store):
    assert store.usage_global_today("2026-10-26").calls == 1
    assert store.usage_month("2026-10").calls == 2

def test_usage_date_index_exists(store):
    row = store._conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_usage_date'").fetchone()
    assert row is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_budget_config.py tests/test_usage_queries.py -q`
Expected: FAIL，`TypeError`/`AttributeError`（缺字段、缺 `usage_between`）。

- [ ] **Step 3: 实现**

`config.py`：扩 `BudgetConfig` 字段；把 `_build_budget(raw: dict[str, Any]) -> BudgetConfig` 抽出来并在原 806 行处调用。校验：
- `daily_calls`/`daily_tokens` 必须 >0；global/monthly counts 必须 >=0。
- `on_exceed in {"degrade","stop","notify"}`。
- 三个价格与 cost_limit >=0；`monthly_cost_limit > 0` 时至少一个价格 >0。
- `warning_ratios` 每项 `0 < r <= 1`，排序去重后非空。
- `currency` 去空白后非空。

`store.py`：在 usage 建表语句后加 `CREATE INDEX IF NOT EXISTS idx_usage_date ON usage (date);`；在 `usage_totals` 后加 `UsageTotals` 与查询，动态拼 WHERE（参数化，不用字符串插值值）；`usage_month` 用 `calendar.monthrange` 算月末。

`config.yaml`：在 `budget` 段补齐新键并写中文注释（0=未启用、费用按百万 token 估算、`on_exceed` 三态、`warning_ratios` 取值 0–1）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_budget_config.py tests/test_usage_queries.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/config.py src/dafeiyu/memory/store.py tests/test_budget_config.py tests/test_usage_queries.py config.yaml
git commit -m "feat: extend budget config and usage queries"
```

---

### Task 2: BudgetGuard、费用与预警阈值

**Files:**
- Create: `src/dafeiyu/budget.py`
- Create: `tests/test_budget.py`

**Interfaces:**
- Consumes: Task 1 的 `BudgetConfig`、`UsageTotals`、`Store.usage_between/usage_global_today/usage_month`。
- Produces:
  - `class BudgetAction(str, Enum): ALLOW="allow"; DEGRADE="degrade"; DENY="deny"`
  - `@dataclass(frozen=True) class BudgetDecision: action:BudgetAction; reason:str; scope:str; warnings:tuple[str,...]=(); remaining_calls:int|None=None; remaining_tokens:int|None=None; remaining_cost:float|None=None`
  - `@dataclass(frozen=True) class BudgetReport: per_chat:dict[str,Any]; global_daily:dict[str,Any]; monthly:dict[str,Any]; cost:dict[str,Any]; warnings:tuple[dict[str,Any],...]`
  - `class BudgetGuard: __init__(self, store, config:BudgetConfig); evaluate(self, chat_type:ChatType, chat_id:int, *, addressed:bool, purpose:str, now:datetime|None=None)->BudgetDecision; report(self, chat_type:ChatType, chat_id:int, *, now:datetime|None=None)->BudgetReport`
  - 模块函数 `estimate_cost(prompt_tokens:int, completion_tokens:int, config:BudgetConfig) -> float`

- [ ] **Step 1: 写失败测试**

`tests/test_budget.py` 用 `Store(":memory:")` + helper `usage(store, *, date, chat_type, chat_id, purpose="reply", prompt=0, completion=0)` 直插 usage。至少覆盖：

```python
def test_per_chat_limit_denies(guard, store):
    usage(store, date=TODAY, chat_id=1, prompt=10, completion=10)
    d = guard.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.DENY and d.scope == "per_chat"

def test_ambient_ratio_boundary(store):
    g = guard(store, daily_calls=10, daily_tokens=1000, ambient_ratio=0.8)
    for _ in range(8):
        usage(store, date=TODAY, chat_id=1, purpose="ambient", prompt=1)
    assert g.evaluate(ChatType.GROUP, 1, addressed=False, purpose="ambient", now=NOW).action is BudgetAction.DEGRADE
    assert g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW).action is BudgetAction.ALLOW

@pytest.mark.parametrize("on_exceed,action", [("degrade", BudgetAction.DEGRADE), ("stop", BudgetAction.DENY), ("notify", BudgetAction.ALLOW)])
def test_global_daily_on_exceed(...): ...

def test_monthly_cost_is_hard_stop_even_notify(store):
    g = guard(store, on_exceed="notify", monthly_cost_limit=1.0, price_input_per_million=1000.0)
    usage(store, date=TODAY, prompt=2000)
    d = g.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.DENY and d.scope == "cost"

def test_warnings_do_not_change_action(...):
    # calls=5/10, ratios (0.5, 0.8) -> ("per_chat:0.5",) 且 action ALLOW

def test_storage_error_degrades(monkeypatch, guard):
    monkeypatch.setattr(guard.store, "usage_between", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    d = guard.evaluate(ChatType.GROUP, 1, addressed=True, purpose="reply", now=NOW)
    assert d.action is BudgetAction.DEGRADE
    assert d.scope == "storage_error" and d.warnings == ("budget_storage_error",)

def test_other_purpose_allows(...):  # purpose="summary" -> ALLOW
def test_report_cost(...):  # 1M input + 0.5M output 按单价算，estimated True
def test_zero_limits_skipped(...):  # global/monthly 全 0，usage 超大仍 ALLOW
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_budget.py -q`
Expected: FAIL，`ModuleNotFoundError: dafeiyu.budget`。

- [ ] **Step 3: 实现 `budget.py`**

顺序固定：per-chat 日 → global 日 → 月度 → 费用 → warnings。每步先算 `UsageTotals`，限额为 0 跳过。`on_exceed` 映射只用于 global/monthly：`degrade→DEGRADE`、`stop→DENY`、`notify→ALLOW` 并追加 `f"{scope}:1.0"` 警告。费用计算用 `prompt/1e6*price_input + completion/1e6*price_output`，`report` 的 `estimated` 取月度 `UsageTotals.estimated`。`report` 用同一次查询填充 `per_chat/global_daily/monthly/cost/warnings`。

`evaluate` 捕获 `Exception`（非 `CancelledError`）→ `BudgetDecision(DEGRADE,"budget_storage_error","storage_error",("budget_storage_error",))`，并 `log.warning`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_budget.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/budget.py tests/test_budget.py
git commit -m "feat: add budget guard with cost and warnings"
```

---

### Task 3: 接入 Router / Service / 健康快照 / 告警

**Files:**
- Modify: `src/dafeiyu/router.py:50-63`（`_CHECK_LABELS`）、`router.py:113-120`（`Router.__post_init__`）、`router.py:259-288`（`should_handle`）、`router.py:290-370`（`explain`）
- Modify: `src/dafeiyu/service.py:90-113`（`__init__`）、`service.py:748-765`（`_budget_for`）、`service.py:941-947`（`_private_budget_ok`）、`service.py:280-302`（explain command 分支）
- Modify: `src/dafeiyu/control.py:277-292`（guard 字段）、`control.py:392-415`（budget 快照）
- Modify: `src/dafeiyu/supervisor/alerts.py:200-308`（conditions）、`alerts.py:310-321`（repeat）、`alerts.py:324-430`（Incident data / audit）
- Modify: `src/dafeiyu/config.py:212-220`（`AlertRulesConfig.budget_warning=True`）
- Test: `tests/test_router_budget.py`（新建）、`tests/test_service_budget.py`（新建）、`tests/test_health_snapshot.py`、`tests/test_alerts.py`

**Interfaces:**
- Consumes: `BudgetGuard.evaluate/report`、`BudgetAction`。
- Produces:
  - `Router.budget_guard: BudgetGuard`（`field(init=False)`，`__post_init__` 构造）。
  - `BrainControl.budget_guard: BudgetGuard`（`__init__` 构造）。
  - `health_snapshot()["budget"]` 形状：
    ```python
    {
      "per_chat": {"calls": int, "tokens": int, "daily_calls": int, "daily_tokens": int},
      "global_daily": {"calls": int, "tokens": int, "limit_calls": int, "limit_tokens": int},
      "monthly": {"calls": int, "tokens": int, "limit_calls": int, "limit_tokens": int},
      "cost": {"cost": float, "cost_limit": float, "currency": str, "estimated": bool},
      "warnings": [{"scope": str, "ratio": float, "calls": int, "limit": int}],
    }
    ```
  - `AlertManager` 支持 `budget_warning:<scope>:<ratio>` 指纹；`Condition`/`Incident` 新增 `data: dict[str, Any]`。

- [ ] **Step 1: 写失败测试**

`tests/test_router_budget.py`（fixture 用 `load_settings` 加载 `TEST_CONFIG_YAML`，再覆盖 `budget.global_daily_calls` 等字段）：

```python
def test_global_budget_blocks_non_hard(router, store, msg):
    usage(store, date=TODAY, chat_type=ChatType.GROUP, chat_id=msg.chat_id, calls=10)
    d = router.should_handle(msg)  # 非 @，且 global_daily_calls=10
    assert d.handle is False and d.reason == "budget"

def test_hard_trigger_survives_degrade(router, msg_at_bot):
    # ambient 用满 ratio，@ 消息仍 hard
    d = router.should_handle(msg_at_bot)
    assert d.handle is True and d.reason == "hard"

def test_explain_lists_three_new_checks(router, msg):
    data = router.explain(msg)
    labels = [c["label"] for c in data["checks"]]
    for label in ("每日预算", "全局日预算", "月度预算", "月度费用"):
        assert label in labels
    assert labels.index("全局日预算") == labels.index("每日预算") + 1
    assert labels.index("月度预算") == labels.index("全局日预算") + 1
    assert labels.index("月度费用") == labels.index("月度预算") + 1
    assert data["metrics"]["budget_action"] in {"allow", "degrade", "deny"}
```

`tests/test_service_budget.py`：monkeypatch `service.router.budget_guard.evaluate` 返回 `DENY`，调用 `_budget_for(...)` 断言 False；返回 `DEGRADE` 时 addressed True/False 分别 True/False；`_private_budget_ok` 在 DENY 时 False。

`tests/test_health_snapshot.py` 增加：写入 usage 后 `snapshot["budget"]["global_daily"]["calls"]` 与 `monthly`、`cost` 数值正确，`warnings` 为 list[dict]。

`tests/test_alerts.py` 增加：snapshot 带 `{"budget":{"warnings":[{"scope":"global_daily","ratio":0.8,"calls":8,"limit":10}]}}` 时产生 `budget_warning:global_daily:0.8`；推进 24h 内不重复、超过 24h 再告警；audit 有 `budget.warning` 且 detail 只含 `calls/limit`。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_router_budget.py tests/test_service_budget.py tests/test_health_snapshot.py tests/test_alerts.py -q`
Expected: FAIL，缺 `budget_guard`、新检查项、`data` 字段。

- [ ] **Step 3: 实现**

- `Router.__post_init__`：`self.budget_guard = BudgetGuard(self.store, self.settings.budget)`；`clone` 保持新建只读 guard（不改共享状态）。
- `should_handle`：保留 self/muted/blacklist；删掉旧的两行 per-chat 硬编码，改为 `hard = self.hard_trigger(msg)` 后调用 `decision = self.budget_guard.evaluate(msg.chat_type, msg.chat_id, addressed=hard, purpose="reply" if hard else "ambient", now=now)`；`DENY` 或 `(DEGRADE and not hard)` → `TriggerDecision(False, False, "budget")`；`hard` → `(True, True, "hard")`；之后 whitelist/rules 不变。
- `_CHECK_LABELS` 在 `budget` 后插入 `budget_global/budget_monthly/budget_cost` 三项中文标签；`explain` 在 per-chat 后依次展示三项，metrics 增加 `global_calls/global_tokens/monthly_calls/monthly_tokens/monthly_cost/budget_action/budget_scope/warnings`。`_check_detail` 为三项各返回“已用/上限”。
- `Service.__init__`：`self.budget_guard = router.budget_guard`，`self._started_at = time.monotonic()`（供 `/status`）。
- `_budget_for`：调 guard，`purpose="reply" if addressed else "ambient"`；`DENY→False`；`DEGRADE and not addressed→False`；其余 True。
- `_private_budget_ok`：`evaluate(..., addressed=True, purpose="reply")`，非 DENY 即 True。
- `BrainControl.__init__` 构造 guard；`health_snapshot` 用 `report` 填 global/monthly/cost/warnings，`per_chat` 保留原 today 值。
- `alerts.py`：`Condition` 与 `Incident` 加 `data`；conditions 遍历 `budget.warnings` 生成指纹与 `data={"calls":..,"limit":..}`；`_repeat_seconds` 对 `rule.startswith("budget_warning:")` 返回 86400；`_dispatch` 在 firing 时写 `budget.warning` 审计，target 为去掉前缀的 `scope:ratio`，detail 仅 `calls/limit`。
- `AlertRulesConfig` 加 `budget_warning: bool = True`，conditions 仅在开启时生成；`config.yaml` 的 `alerts.rules` 同步加 `budget_warning: true`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_router_budget.py tests/test_service_budget.py tests/test_health_snapshot.py tests/test_alerts.py tests/test_router.py tests/test_service.py -q`
Expected: PASS（既有 router/service 用例不回归）。

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/router.py src/dafeiyu/service.py src/dafeiyu/control.py src/dafeiyu/supervisor/alerts.py src/dafeiyu/config.py tests/test_router_budget.py tests/test_service_budget.py tests/test_health_snapshot.py tests/test_alerts.py config.yaml
git commit -m "feat: wire budget guard into router service and alerts"
```

---

### Task 4: Dashboard 预算卡与 `/api/budget`

**Files:**
- Modify: `src/dafeiyu/control.py`（`budget_snapshot` + 路由）
- Modify: `src/dafeiyu/supervisor/dashboard.py`（`/api/budget` 代理）
- Modify: `src/dafeiyu/supervisor/dashboard/index.html:356-374`（usage 卡内预算区块）
- Test: `tests/test_supervisor_ops_api.py`、`tests/test_supervisor_dashboard.py`

**Interfaces:**
- Consumes: Task 3 的 `health_snapshot()["budget"]`、`BrainControl.budget_guard.report`。
- Produces:
  - Bot `GET /api/budget?chat_type=group|private&chat_id=<int>` → `BudgetReport` 的 JSON（字段同 Task 3 budget 快照，另加 `chat_type/chat_id`）。
  - Dashboard `GET /api/budget?chat_type=&chat_id=` → 透传 bot 状态码与 JSON。
  - HTML 锚点：`id="budget-panel"`、`id="budget-global"`、`id="budget-monthly"`、`id="budget-cost"`、`id="budget-warnings"`、`id="budget-chat-type"`、`id="budget-chat-id"`、`id="btn-budget-refresh"`。

- [ ] **Step 1: 写失败测试**

`tests/test_supervisor_ops_api.py` 增加：登录后 GET `/api/budget?chat_type=group&chat_id=1` → 200 且含 `per_chat/global_daily/monthly/cost/warnings`；非法 `chat_type` → 400。
`tests/test_supervisor_dashboard.py` 增加：`index.html` 含上述锚点；JS 引用 `/api/budget`、`budget-global`、`budget-warnings`；`btn-budget-refresh` 绑定点击。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_supervisor_ops_api.py tests/test_supervisor_dashboard.py -q`
Expected: FAIL，404 / 缺锚点。

- [ ] **Step 3: 实现**

- `BrainControl.budget_snapshot(chat_type, chat_id)`：`report = self.budget_guard.report(...)`，转 dict，附 `chat_type.value/chat_id`。
- 路由 `GET /api/budget`：解析 query，非法类型 400，其余 200。
- Dashboard `budget` handler：`await bot_call("GET", f"/api/budget?chat_type={...}&chat_id={...}")`，返回状态与 JSON；加入受保护路由表。
- HTML/JS：在 usage 卡第一列 totals 后插入预算面板；渲染 per-chat calls/tokens 文本与 3 组进度条（global 日 / monthly / cost），宽度 `min(100, used/limit*100)`，颜色阈值 `<50` 中性、`50–79` 黄、`80–99` 橙、`>=100` 红；warning 以 tag 显示 `scope ratio`；chat 选择变化时重拉 `/api/budget`。0 限额显示“未启用”，不画条。价格标注“估算”。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_supervisor_ops_api.py tests/test_supervisor_dashboard.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/control.py src/dafeiyu/supervisor/dashboard.py src/dafeiyu/supervisor/dashboard/index.html tests/test_supervisor_ops_api.py tests/test_supervisor_dashboard.py
git commit -m "feat: add dashboard budget card"
```

---

### Task 5: 日志与审计脱敏

**Files:**
- Create: `src/dafeiyu/supervisor/redact.py`
- Modify: `src/dafeiyu/supervisor/logs.py:245-270`（`LogHub.write` 入口）
- Modify: `src/dafeiyu/supervisor/audit.py:70-110`（`write` detail）
- Create: `tests/test_redact.py`
- Create: `tests/test_logs_redact.py`（LogHub 入口与 AuditLog detail 集成）

**Interfaces:**
- Produces: `redact(text: str) -> str`。
- `LogHub.write(source, text)` 先 `text = redact(str(text))`，保证 ring/file/SSE 全部使用脱敏文本。
- `AuditLog.write` 对 `detail` 递归脱敏字符串值；`sanitize` 仍先执行。

- [ ] **Step 1: 写失败测试**

`tests/test_redact.py`：

```python
@pytest.mark.parametrize("raw,expected", [
    ("Authorization: Bearer abc.def", "Authorization: ***"),
    ("authorization: Basic dXNlcjpwYXNz", "Authorization: ***"),
    ("X-Control-Token: s3cret", "X-Control-Token: ***"),
    ("DEEPSEEK_API_KEY=sk-live-123", "DEEPSEEK_API_KEY=***"),
    ("NAPCAT_QUICK_PASSWORD=abc123", "NAPCAT_QUICK_PASSWORD=***"),
    ("api_key: sk-1", "api_key: ***"),
    ("password = hunter2", "password = ***"),
    ("Set-Cookie: sid=abc; HttpOnly", "Set-Cookie: ***"),
])
def test_redact_secrets(raw, expected):
    assert redact(raw) == expected

def test_redact_keeps_chinese_token_usage():
    assert redact("今日 token 用量 1234") == "今日 token 用量 1234"
```

`tests/test_logs_redact.py`：`LogHub.write("bot", "Authorization: Bearer abc")` 后 ring 行与磁盘文件都不含 `abc`；`AuditLog.write(..., detail={"token":"abc","note":"token 用量"})` 后 detail 中 token 为 `***`、note 不变。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_redact.py -q`
Expected: FAIL，`ModuleNotFoundError`。

- [ ] **Step 3: 实现**

`redact.py` 按顺序替换（正则统一 `re.MULTILINE`，整行型规则吃掉行尾）：
1. `(?im)^(\s*Set-Cookie\s*:\s*).*$` → `\1***`
2. `(?im)^(\s*Authorization\s*:\s*).*$` → `\1***`
3. `(?im)^(\s*X-Control-Token\s*:\s*).*$` → `\1***`
4. `(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+` → `\1***`
5. `(?i)\b(api[_-]?key|apikey|password|passwd|secret|token)\b(\s*[:=]\s*)([^\s,;]+)` → `\1\2***`
6. `(?i)\b[A-Z0-9_]*(?:API_KEY|PASSWORD|SECRET|TOKEN)\s*=\s*[^\s,;]+` → 保留键名 + `=***`
只在字符串层面替换，不做大小写归一化输出。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_redact.py tests/test_logs_redact.py tests/test_supervisor_ops_api.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/supervisor/redact.py src/dafeiyu/supervisor/logs.py src/dafeiyu/supervisor/audit.py tests/test_redact.py tests/test_logs_redact.py
git commit -m "feat: redact secrets in logs and audit detail"
```

---

### Task 6: Control token ACL 与轮换

**Files:**
- Create: `src/dafeiyu/supervisor/control_token.py`
- Modify: `src/dafeiyu/supervisor/main.py:74-85`（`load_or_create_token`）、`main.py:406-430`（holder / specs / serve）
- Modify: `src/dafeiyu/supervisor/dashboard.py:94-210`（token provider）、`dashboard.py:806-851`（rotate 路由）、`dashboard/index.html`（安全区轮换控件）
- Modify: `src/dafeiyu/supervisor/health.py:28-45`（允许更新 `.token`）
- Create: `tests/test_control_token.py`
- Test: `tests/test_supervisor_dashboard.py`

**Interfaces:**
- Produces:
  - `harden_token_file(path: Path) -> None`（失败只 warning）。
  - `class ControlToken: __init__(self, path: Path); load_or_create(self) -> str; @property value -> str; rotate(self) -> str; set(self, value: str) -> None`
  - `load_or_create_token(path: Path = TOKEN_PATH) -> str` 保留为兼容包装，内部 `ControlToken(path).load_or_create()`；已有 token 文件也要执行 ACL 加固。
  - `create_app(..., token: str | Callable[[], str] = "", ..., control_token: ControlToken | None = None)`
  - `POST /api/control-token/rotate`，body `{"confirm":"ROTATE"}` → `{"ok": true, "restarted": true}`。
  - 审计：`token.rotate` target `control` detail `{"restarted": bool}`。
  - HTML 锚点：`id="btn-rotate-token"`、`id="rotate-token-confirm"`、`id="rotate-token-status"`。

- [ ] **Step 1: 写失败测试**

`tests/test_control_token.py`：用 `tmp_path`；monkeypatch `subprocess.run` 记录 Windows icacls 参数，断言 `/inheritance:r` 与 `:(R,W)`；POSIX 分支 monkeypatch `os.chmod` 断言 0o600；写盘失败（monkeypatch `Path.write_text` 抛 OSError）时 `rotate()` 抛错且 `value` 仍为旧值；`set(old)` 可恢复。

`tests/test_supervisor_dashboard.py` 增加：未登录 POST rotate → 401；登录但 confirm 错 → 400 且 token 不变；成功时 `supervisor.specs["bot"].env["DAFEIYU_CONTROL_TOKEN"] == new`、`ops.health.token == new`、`supervisor.restarted == ["bot"]`、audit 有 `token.rotate`；用本地 aiohttp 假 bot 校验请求头：轮换后新 token 返回 200、旧 token 返回 401；restart 抛错 → 500、token 恢复旧值、bot 只调用一次 restart、旧 token 仍可用；HTML 含三个轮换锚点且按钮绑定 POST。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_control_token.py tests/test_supervisor_dashboard.py -q`
Expected: FAIL，模块不存在 / 路由 404。

- [ ] **Step 3: 实现**

`control_token.py`：原子写（temp + `os.replace`），写后 `harden_token_file`；`rotate` 生成 `secrets.token_urlsafe(24)`，任一步失败回滚旧值；`set` 用于 dashboard 失败回滚。

`main.py`：`holder = ControlToken(TOKEN_PATH); holder.load_or_create()`；`build_specs(settings, holder.value)`；`serve(..., token=lambda: holder.value, control_token=holder)`；`build_ops` 仍传初始 token 给 HealthMonitor。

`dashboard.py`：`token_provider = token if callable(token) else (lambda: token)`；`bot_session` 不再设默认 header，`bot_call/bot_raw` 每次请求加 `headers={"X-Control-Token": token_provider()}`；`serve` 透传 `control_token`。rotate handler 校验 confirm → `old=holder.value; new=holder.rotate()` → 更新 `supervisor.specs["bot"].env` 与 `ops.health.token` → `await supervisor.restart("bot")`；异常时 `holder.set(old)`、恢复 env/health、写失败审计、500。成功写 `token.rotate`。HTML 在 backups 卡后追加“安全”小卡：说明轮换会重启 bot、输入 `ROTATE` 的确认框与按钮，成功后显示 `已轮换，bot 已重启`；失败显示返回错误。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_control_token.py tests/test_supervisor_dashboard.py tests/test_supervisor_ops_api.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/supervisor/control_token.py src/dafeiyu/supervisor/main.py src/dafeiyu/supervisor/dashboard.py src/dafeiyu/supervisor/health.py tests/test_control_token.py tests/test_supervisor_dashboard.py
git commit -m "feat: add control token acl and rotation"
```

---

### Task 7: 命令注册表与 `/help /stats /status /budget`

**Files:**
- Create: `src/dafeiyu/commands.py`
- Modify: `src/dafeiyu/service.py:43-44`（删旧集合）、`service.py:192-194`（命令分支）、`service.py:280-302`（explain）、`service.py:965-998`（`_run_command`）
- Create: `tests/test_commands.py`
- Test: `tests/test_service.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class CommandSpec: name:str; aliases:frozenset[str]; scopes:frozenset[ChatType]; admin_only:bool; cooldown_seconds:int; help:str`
  - `class CommandRegistry: match(self, text:str) -> tuple[CommandSpec,str]|None; is_known(self, name:str, chat_type:ChatType) -> bool; help_text(self, chat_type:ChatType, *, is_admin:bool) -> str`
  - `Service.commands: CommandRegistry`；`Service._command_clock: Callable[[], float]`（默认 `time.monotonic`）。
- 命令表：`/help`（帮助，双端，5s）、`/stats`（统计，群 60s / 私 5s）、`/status`（状态，admin，30s）、`/budget`（预算，admin，30s）、`/闭嘴|shutup`、`/醒来|wake`、`/forget`。

- [ ] **Step 1: 写失败测试**

`tests/test_commands.py`：

```python
def test_match_aliases_and_scopes(registry):
    spec, args = registry.match("/帮助 额外")
    assert spec.name == "help" and args == "额外"
    assert registry.match("/stats").spec.name == "stats"

def test_unknown_and_wrong_scope(registry):
    assert registry.match("/nope") is None
    assert registry.match("/shutup", ChatType.PRIVATE) is None  # 若 shutup 仅群

def test_help_text_hides_admin_commands_for_non_admin(registry):
    text = registry.help_text(ChatType.GROUP, is_admin=False)
    assert "/stats" in text and "/budget" not in text and "/status" not in text
    assert "/budget" in registry.help_text(ChatType.PRIVATE, is_admin=True)
```

`tests/test_service.py` 增加：群内非管理员 `/budget` 不回复、返回 True；管理员 `/budget` 回复含“全局”与“月度”；`/stats` 群内回复不含“元/¥”；同一命令 1 秒内第二次被冷却拦截；私聊 `/status` 非管理员回复“这个命令只有管理员能用。”；`/help` 在群/私聊均回复。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_commands.py tests/test_service.py -q`
Expected: FAIL，模块不存在 / 命令未识别。

- [ ] **Step 3: 实现**

`commands.py` 用 `router.parse_command` 解析；别名统一 casefold；`match` 返回 `(spec, " ".join(args))`；`help_text` 按 scope + admin 过滤。

`Service.__init__`：构造 `self.commands`、`self._command_clock`、`self._command_last: dict[tuple[str,str,int], float]`。`handle` 改用 `matched = self.commands.match(msg.content)`，命中才调 `_run_command(msg, spec, args)`。`_run_command`：
- 冷却键：群 `(spec.name, "group", chat_id)`，私聊 `(spec.name, "private", user_id)`；冷却秒数取 `spec.cooldown_seconds`，唯一例外是 `/stats` 私聊固定 5 秒（群 60 秒）。未到点返回 True 不回复。
- `admin_only and user not in settings.groups.admins`：群内 `log.info` 后 return True（静默）；私聊回复“这个命令只有管理员能用。”。
- 分发到 `_cmd_help/_cmd_stats/_cmd_status/_cmd_budget/_forget_chat/quiet/wake`。
- `/stats`：`usage_global_today` 与当前 chat 的 `usage_between`，剩余量 = limit - used（0 显示“未启用”）；群内不显示费用。
- `/status`：gateway connected、scheduler pending、quota、`budget_guard.evaluate(...).action`、uptime 秒；不输出模型名。
- `/budget`：`budget_guard.report(msg.chat_type, msg.chat_id)`，输出 per-chat/global/monthly/cost 与 warnings。
- 命令执行成功时写审计 `command.admin`（仅 admin_only 命令），target `/<name>`，detail `{chat_type,chat_id,user_id}`。

`explain` 的 command 分支改用 `self.commands.is_known(command.name, msg.chat_type)`，并保留“/help 只识别不执行”。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_commands.py tests/test_service.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/commands.py src/dafeiyu/service.py tests/test_commands.py tests/test_service.py
git commit -m "feat: add command registry and admin commands"
```

---

### Task 8: 按用户删除消息（API + UI + 审计）

**Files:**
- Modify: `src/dafeiyu/memory/store.py`（`delete_user_messages`）
- Modify: `src/dafeiyu/control.py`（DELETE 路由）
- Modify: `src/dafeiyu/supervisor/dashboard.py`（代理 + 审计）
- Modify: `src/dafeiyu/supervisor/dashboard/index.html:320-350`（memory 卡）、`index.html:967-984`（审计卡详情）
- Create: `tests/test_store_user_delete.py`
- Test: `tests/test_supervisor_ops_api.py`、`tests/test_supervisor_dashboard.py`

**Interfaces:**
- Produces:
  - `Store.delete_user_messages(chat_type:ChatType, chat_id:int, user_id:int) -> int`
  - Bot `DELETE /api/memory/{chat_type}/{chat_id}/user/{user_id}`，body `{"confirm":"DELETE_USER"}` → `{"ok": True, "deleted_messages": int}`
  - Dashboard `DELETE /api/memory/{chat_type}/{chat_id}/user/{user_id}` → 透传；成功后审计 `memory.user_delete` target `group/{chat_id}/user/{user_id}` detail `{deleted_messages,chat_type,chat_id,user_id}`。
  - HTML 锚点：`id="delete-user-form"`、`id="delete-user-id"`、`id="delete-user-confirm"`、`id="btn-delete-user"`。

- [ ] **Step 1: 写失败测试**

`tests/test_store_user_delete.py`：插 3 条用户 7 的消息、1 条用户 8、1 条 bot；插对应 delivered 行；断言删除用户 7 返回 3、用户 8 与 bot 仍在、delivered 只剩用户 8 的 message_id、`count_messages` 同步下降、summaries/impressions 行数不变。

`tests/test_supervisor_ops_api.py`：confirm 缺失/错误 → 400；成功 → 200 且 `deleted_messages` 正确、audit 有 `memory.user_delete`、detail 不含消息正文；bot 返回 4xx 时 dashboard 透传且不写成功审计。
`tests/test_supervisor_dashboard.py`：审计卡每行渲染 `<details>` 与 `row.detail` 的 JSON，空列表显示“暂无审计记录”。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_store_user_delete.py tests/test_supervisor_ops_api.py -q`
Expected: FAIL，缺方法/路由。

- [ ] **Step 3: 实现**

`Store.delete_user_messages` 单事务：先 `DELETE FROM delivered_messages WHERE chat_type=? AND chat_id=? AND message_id IN (SELECT message_id FROM messages WHERE chat_type=? AND chat_id=? AND user_id=?)`，再删 messages，返回 `rowcount`。不触碰 `summaries/impressions/session_summaries/delivered` 之外的聚合表。

`control.py`：路由解析并校验 `ChatType`、正整数 id；非 `DELETE_USER` → 400；调 store，返回计数。

`dashboard.py`：代理 DELETE 带 confirm；成功写审计（detail 仅计数与标识）。UI 在 memory 卡底部加输入（群号/用户 QQ/确认词）与按钮，提示“只删除该用户原始消息，摘要与印象可能仍保留痕迹”。审计卡每行改为 `<details>`，summary 保留时间/动作/actor，展开后 `<pre>` 显示 `JSON.stringify(row.detail, null, 2)`；无记录时保留“暂无审计记录”。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_store_user_delete.py tests/test_supervisor_ops_api.py tests/test_control_api.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/memory/store.py src/dafeiyu/control.py src/dafeiyu/supervisor/dashboard.py src/dafeiyu/supervisor/dashboard/index.html tests/test_store_user_delete.py tests/test_supervisor_ops_api.py
git commit -m "feat: add per-user memory deletion"
```

---

### Task 9: 配置、文档与全量验证

**Files:**
- Modify: `README.md`（预算、脱敏、token 轮换、命令、隐私删除）
- Modify: `.tamako/contract.json`（分册 B 契约 verdict 闭环；本地文件，git 忽略）
- Test: 全量

**Interfaces:**
- Consumes: Task 1-8 全部能力。
- Produces: 可运行的默认配置与运维文档。

- [ ] **Step 1: 核对 `config.yaml`**

`budget` 新键与 `alerts.rules.budget_warning` 已由 Task 1 / Task 3 写入。这里只核对键齐全、注释说明 0=关闭与费用为估算；缺什么补什么，不重复改。

- [ ] **Step 2: 更新 README**

新增/扩展：
- 预算：四层限额、`on_exceed` 三态、费用单价与“估算”说明、0 值语义。
- 安全：日志/审计脱敏范围；token 文件 ACL；dashboard 轮换步骤与失败回滚；confirm 字符串。
- 命令：`/help /stats /status /budget` 权限、冷却、群/私聊差异。
- 隐私：按用户删除入口、只删原始消息、聚合可能保留痕迹。

- [ ] **Step 3: 闭环契约**

`.tamako/contract.json` 的验收项应在开工前写好。这里逐条核对证据并改为 `pass`，全部通过后 `state` 改 `closed`；该目录被 git 忽略，契约只留在本地，不进入提交。

- [ ] **Step 4: 全量验证**

Run: `python -m pytest -q`
Expected: 全部 PASS（基线 691 + 新增）。

- [ ] **Step 5: 提交**

```bash
git add README.md
git commit -m "docs: document cost security and commands"
```

---

## 执行顺序

1. Task 1 → 2 → 3 → 4（预算批次）
2. Task 5 → 6（安全批次）
3. Task 7 → 8 → 9（命令、隐私、文档）

每个批次结束跑一次相关测试；Task 9 跑全量。批次之间可以独立评审，但接口按上表固定，不得改名。
