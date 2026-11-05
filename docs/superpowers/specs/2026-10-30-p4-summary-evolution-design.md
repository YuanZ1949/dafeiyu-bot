# P4 摘要演进设计（A2 + B2 + C2）

日期：2026-10-30
状态：已实现（2026-11-05 核对：A2 `_summary_budget`、B2 周汇总、C2 `reply_to_user_id` 均已落地；缺口周不生成周汇总，属已知边界）
范围：日摘要动态字数（A2）、周汇总（B2）、回复目标上下文（C2）

## 1. 背景

当前摘要链路有三个已确认的问题：

1. **忙日被压平**：`MAX_SUMMARY_CHARS = 200` 是全局常量，忙群一天几百条消息和闲群十几条消息拿到同样 200 字，信息密度被拉平。
2. **旧摘要很快消失**：注入只取最近 3 天内 2 条日摘要，3 天前的摘要不再进入 prompt；没有周级汇总，长期记忆断层。
3. **缺回复上下文**：`_summary_line` 只渲染 `[HH:MM] 昵称: 内容`。`Message` 只有 `at_bot` / `reply_to_bot` 两个布尔，摘要模型不知道「这句是在回谁」，指代和对话线索容易丢。

本设计只解决这三项，不扩大到摘要的其他形态。

## 2. 目标与非目标

### 目标

- 日摘要长度按当天消息量分档，忙群有更多字数，闲群不浪费预算。
- 每周生成一条周汇总，注入时提供跨天的长期背景。
- 摘要输入带上回复目标（昵称优先），让模型能还原对话指向。

### 非目标

- 不在 dashboard 展示周汇总（后续单独做）。
- 不改 A1（prompt 分点）、A3（多条主题摘要）、B1（窗口直接扩到 7 天）、B3（注入时压缩旧摘要）。
- 不为历史消息回填 `reply_to_*`（旧消息没有该信息）。
- 不改轮换摘要（`summarize_for_rotation`）的字数与格式，仍为 200 字。

## 3. A2：日摘要动态字数

### 3.1 预算函数

```python
def _summary_budget(message_count: int) -> int:
    if message_count <= 30:
        return 150
    if message_count <= 200:
        return 200
    return 300
```

- 边界：30 → 150，31 → 200，200 → 200，201 → 300。
- 计数用**当天全部消息数**（`len(messages)`），不受 `MAX_SOURCE_LINES` 截断影响。
- 0 条消息的日期本来就不会进入 `pending_summary_slots`，不需要额外分支。

### 3.2 截断与 prompt

- `_clip_summary(text: str, limit: int = MAX_SUMMARY_CHARS) -> str`，原逻辑不变，只把常量换成参数。
- `SUMMARY_SYSTEM` 改成带预算的模板（例如 `SUMMARY_SYSTEM_TEMPLATE.format(limit=budget)`），prompt 里明确「不超过 N 字」，减少先写超再截。
- `summarize_chat` 计算预算后同时传给 prompt 与 `_clip_summary`。
- `summarize_for_rotation` 继续用 `MAX_SUMMARY_CHARS`（200），不调用 `_summary_budget`。

### 3.3 兼容

`MAX_SUMMARY_CHARS` 保留为默认值，`_clip_summary` 的旧调用点不传 `limit` 时行为不变。

## 4. C2：回复目标

### 4.1 模型与落库

`Message` 新增：

```python
reply_to_message_id: str | None = None
reply_to_user_id: int | None = None
```

- `gateway/qq.py` 的 `_message_from_event` 把已有的 `reply_id` / `reply_qq` 填进这两个字段。
- `messages` 表新增两列：`reply_to_message_id TEXT`、`reply_to_user_id INTEGER`，写进 `_MIGRATIONS["messages"]`，老库自动补列。
- `store.add_message` / `_row_to_message` / `control.py` 的调试重建同步这两个字段。

### 4.2 摘要行渲染

`_summary_line` 增加可选后缀：

```
[HH:MM] 昵称: 内容（回复 阿猫）
```

- 昵称解析：生成摘要时收集当天消息的 `reply_to_message_id`，一次批量查询
  `SELECT message_id, nickname FROM messages WHERE chat_type=? AND chat_id=? AND message_id IN (...)`。
- 查到 → 昵称；查不到但 `reply_to_user_id` 存在 → `QQ{uid}`；两者都没有 → 不加后缀。
- 回复 bot 的消息同时带 `reply_to_bot=True`，后缀照常显示（例如「回复 大肥鱼」），不额外加「回复bot」字样。

### 4.3 为什么不在入库时解析昵称

入库是热路径，每条回复消息多一次查询不划算；摘要是一次性批处理，批量查一次即可。历史消息没有这两个字段，摘要自然退化，不需要迁移回填。

## 5. B2：周汇总

### 5.1 数据模型

新表，不复用 `summaries`：

```sql
CREATE TABLE IF NOT EXISTS weekly_summaries (
    chat_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,          -- 周一，YYYY-MM-DD
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_type, chat_id, week_start)
);
```

不复用 `summaries` 的原因：`pending_summary_slots` 用 `summaries.date` 与消息日期做 LEFT JOIN 判断缺口，周日期（如 `2026-10-19` 表示整周）会被误判成某一天的日摘要，导致缺口统计和注入查询都出错。

### 5.2 周的定义与生成时机

- 自然周：周一 00:00:00 至周日 23:59:59（Asia/Shanghai）。
- 下周一 4am 之后生成上一周的周汇总；与日摘要同一轮 `run_pending`，顺序为：先补日摘要，再生成周汇总。
- `week_start` 用上周一的 ISO 日期。
- 回补：最近 2 个完整周。周一失败时，下一次运行仍能补上，不会永久断档。

### 5.3 生成条件

对每个 `(chat_type, chat_id)`：

1. 该周至少有一条日摘要；0 条则跳过，不写空记录。
2. 该周没有已存在的周汇总（`get_weekly_summary` 为空）。
3. 未处于该 `(chat, week_start)` 的退避中。

输入是这 7 天里实际存在的日摘要，按日期正序，带日期前缀。prompt 要求：

- 不超过 300 字；
- 按话题/事件/人物关系归纳，不逐日复述；
- 信息不足时宁可短，不编造。

输出经 `_clip_summary(text, 300)` 后写入。

### 5.4 调度

`SummaryScheduler.run_pending` 末尾追加周汇总阶段（每天 4am 后都尝试，靠回补窗口兜底，不只在周一跑）：

```python
await self._run_weekly(now, budget=MAX_BACKFILL_PER_RUN - created)
```

- `_run_weekly` 计算 `last_monday = now.date() - timedelta(days=now.weekday() + 7)`，再往前取 1 周，共 2 个候选周。
- 每个候选周查 `pending_weekly_slots(week_start)`（有日摘要且无周汇总的会话）。
- 每轮最多生成 `MAX_BACKFILL_PER_RUN` 条，与日摘要共享上限；失败写入同一个 `FailureBackoff`，key 为 `(chat_type, chat_id, week_start)`。
- 全局配额熔断（`backoff.is_paused()`）同样生效。

### 5.5 注入

`context._recent_summary` 改为 `_summary_block`：

1. 取最近 1 条周汇总，渲染为 `周汇总（MM-DD~MM-DD）：内容`，上限 300 字；`week_start` 距今超过 21 天则不注入（`WEEKLY_WINDOW_DAYS`）。
2. 取最近 3 天内最多 2 条日摘要，渲染为 `MM-DD：内容`。
3. 顺序：周汇总在前，日摘要按日期正序在后。
4. 总预算 750 字（`SUMMARY_TOTAL_CHARS` 与 `cfg.summary_chars * 2` 取小；默认 `summary_chars=375`）。750 = 300 字周汇总 + 2×200 字日摘要 + 日期/标题前缀余量。
5. 超预算时：先丢较早的日摘要；**最新一条日摘要永远保留**，仍超则把周汇总截到剩余额度（`room <= 0` 时只留最新日摘要）。
6. 没有周汇总时退化为现有行为，不改变已有输出格式的顺序（日摘要仍按日期正序）。
7. 日摘要回补窗口 `BACKFILL_DAYS` 由 7 天提到 14 天，让周汇总尽量基于完整的一周。

### 5.6 保留

- 周汇总与 `summaries` 一致：**永久保留**，不进入 retention 清理。
- 依据：`prune_messages` 明确「摘要与印象是独立表，保留」；消息本体按 180 天清理，聚合摘要不清理。
- 若未来要清理，另立策略，不在本轮实现。

## 6. 错误处理

| 场景 | 行为 |
| --- | --- |
| 日摘要模型失败/超时 | 维持现状：`on_error` 记退避，不写库，下一轮回补 |
| 周汇总模型失败/超时 | 按 `(chat, week_start)` 退避，不影响同日其他会话的日摘要 |
| 周内 0 条日摘要 | 跳过，不写空行 |
| 周汇总已存在 | 跳过，不重复生成 |
| 回复目标昵称查不到 | 退化为 `QQ{uid}`；没有 `reply_to_user_id` 则不加后缀 |
| 老库缺列 | `_MIGRATIONS` 自动 `ALTER TABLE` 补列，默认 NULL |

## 7. 测试计划

### A2

- `_summary_budget` 四个边界：30/31/200/201。
- `summarize_chat` 在 10 条、100 条、300 条消息下分别按 150/200/300 截断。
- prompt 包含对应预算数字。
- 轮换摘要仍按 200 截断。

### B2

- `week_start` 计算：普通周、跨年周（12 月底/1 月初）。
- 7 条日摘要合并生成 1 条周汇总；缺 2 天时只用 5 条。
- 空周不生成。
- 重复运行不重复写（幂等）。
- 生成失败不写库，且退避生效。
- 注入：周汇总 + 2 条日摘要的顺序与总预算；超预算先丢旧日摘要。

### C2

- gateway 解析 `reply_qq` / `reply_id` 并落到 `Message`。
- `store` 往返保留两个新字段；老库迁移后列存在。
- 摘要行带「（回复 昵称）」；昵称缺失退化为 `QQ{uid}`；无回复段不加后缀。

### 回归

- 全量 `pytest` 通过；`ruff check` 无新增告警。
- `tests/test_dashboard_html.py` 不受影响。

## 8. 风险与取舍

- **周汇总增加调用量**：每会话每周 1 次，量级远小于日摘要；共享 `MAX_BACKFILL_PER_RUN` 与熔断。
- **注入预算竞争**：周汇总占 300 字时，日摘要可用额度下降；通过「先丢旧日摘要」保证最近细节优先。
- **昵称批量查询**：一次 `IN` 查询，参数上限按当天消息数控制；SQLite 变量上限 999，超过则分批。
- **历史数据**：旧消息没有 reply 字段，摘要会缺少回复上下文；不做回填，接受退化。
- **周汇总语义**：只归纳「有日摘要的周」，若日摘要连续缺失，周汇总也会缺失；这是日摘要缺口的连带结果，不在本轮修复。

## 9. 实施顺序

1. C2 模型/落库/摘要行（独立，可先合）。
2. A2 预算函数与截断参数化。
3. B2 表、store 方法、周生成、调度。
4. B2 注入改造与预算取舍。
5. 全量回归、文档与风险登记更新。
