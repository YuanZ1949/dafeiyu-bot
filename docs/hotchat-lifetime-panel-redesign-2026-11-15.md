# 热聊攒着寿命 · 气泡排版 · 面板改版（2026-11-15）

三条诉求对应三组改动，一个特性一个提交。验收分四波：**F1 合规审计 → F2 代码质量 →
F3 真机走查 → F4 需求对照**，四波全过才算完成。

## 1. 热聊攒着要有寿命

**问题**：`hold`（把一批消息攒着不立刻回复）原本没有上限，攒到模型终于开口时，话题
可能早就结束了。

**改动**：

- 新增 `persona.hold_max_seconds`，默认 **300 秒**，`0` = 关闭寿命。
- 过期不是丢弃，而是 `release` + `<checkpoint>_expired` 决策，**照常送模型**，并在
  prompt 里追加一句「这批消息已经攒了 N 分钟」。
- 攒着上限仍是硬编码常量 `PENDING_BATCH_LIMIT = 20`，不变成配置项。
- 两个 hold 点（`opportunity_hold` / `intensity_quiet`）共用同一套寿命判断，
  预演（`preview_batch`）与实际执行同口径。
- `mark_skipped` 全局只有 4 个调用点（预算不足 / 模型失败 / 手动丢弃 / 前置跳过），
  **没有任何一个在过期路径上**。

**为什么不丢弃**：话题是否结束只能由模型判断，按秒数删消息等于替模型做决定，还会让
面板上出现「有积压但发不出去」的错乱。过期段一律交给模型，由它决定接不接。

## 2. 气泡宽度与右对齐

**问题**：原来靠 `max-width` 拍数字，长句被拉伸、短句被撑满，右侧也没有真正贴边。

**改动**（`src/dafeiyu/supervisor/dashboard/index.html`）：

- 宽度：`.timeline-msg { width: fit-content; max-width: min(72ch, 88%) }` —— 宽度由内容
  决定，上限取「72 字符」与「容器 88%」的较小者。
- 对齐：`.timeline-body { justify-items: start }` + `.timeline-msg.bot { justify-self: end }`
  —— 人贴左、bot 贴右，用网格对齐而不是 `margin-left: auto`。
- 手机档删掉 `max-width: 100%` 覆盖（那正是原先右侧错位的来源）。
- 横向溢出真因：`main` / `.pane` 的隐式 `auto` 网格轨道会被 nowrap 的
  `.decision-summary` 撑宽，改成显式 `minmax(0, 1fr)`。

**实测**（桌面 1280×900 / 手机 390×844）：

| 视口 | bot 右缘 / 内容右缘 | 人左缘 / 内容左缘 | 最宽气泡 | 横向溢出 |
|---|---|---|---|---|
| 桌面 | 1245.0 / 1245.0 | 341.0 / 341.0 | 543.4px（72ch） | 无 |
| 手机 | 378.0 / 378.0 | 12.0 / 12.0 | 322.1px（88%） | 无 |

离屏探针：1 字 = 72.0px、10 字 = 168.0px，同一组件同一视口 —— 宽度确实按内容算，
不是固定值。数字由 `scripts/qa_dashboard_shots.py` 产出，`tests/test_dashboard_visual.py`
按 ±1px 容差断言，不靠人眼看图。

## 3. 面板风格与功能性

- **六个分区**：总览 / 记忆 / 会话 / 投递 / 配置 / 日志，导航直达。
- **卡片统一**：内边距 / 圆角 / 间距收敛成同一组 CSS 变量；每张实时数据卡右上角显示
  「更新于 HH:MM:SS」，超过 60 秒没刷新标记为过期。
- **主题**：深色 / 浅色一键切换，选择只写一个键 `dafeiyu.theme`。
- **全局搜索**：一次搜会话 / 消息 / 决策三路并分组，明确标注「只搜最近 200 条」，
  不假装全库。
- **持有卡**：哪些会话在攒、攒了几条、攒了多久，带「放行 / 丢弃」按钮
  （`/api/holds` + `/api/sessions/{type}/{id}/release|discard`）。
- **话题卡**：会话是否在收尾、剩余多少秒（`/api/thread-states`，只读，不写库）。
- **保活卡**：接回 `/api/health/detail` 的 `bot.keepalive`，多显示「发的内容」。
- 首页 HTML 带 `Cache-Control: no-store`，改版后不会看到旧页面。

## 4. 验收结果

| 波次 | 覆盖 | 结果 |
|---|---|---|
| F1 合规审计 | GAP 22/22、IS 14/14、Must-NOT 13/13、证据 23/23 | 通过 |
| F2 代码质量 | 逐文件评审 + LSP/ruff 增量 + 全量回归 | 通过（整改 2 处新代码类型 error） |
| F3 真机走查 | 10 项（8 完整 + 2 降级），4 张 PNG + `metrics.json` + `interactions.json` | 通过 |
| F4 需求对照 | IS-1..IS-14 逐条对照 | 14/14 达标 |

- **静态检查**：新代码行 LSP error = 0（`git blame` 逐行归因，剩余 error 全在基线）；
  ruff 在 product 代码上新增 0 条。
- **回归**：`3886 passed, 30 skipped`（30 条 skip 中 9 条是视觉测试在未导入口令时的
  设计性跳过，导入口令后真实面板 9 passed）。
- **提交**：基线 `41f7e7f` 之后 30 个提交（23 个 todo + F2 整改 2 条 + 收尾 2 条 + 本文档），
  每个改动都做过反向验证（改坏实现 → 断言失败 → 原样还原）。此后的清理提交（测试环境隔离、
  改动文件 lint 清零、备份上传路径类型收窄、类型债清单）不改变本页结论。

## 5. 已知边界

- 面板空闲时 `/api/holds` 为空，「放行 / 丢弃」按钮无法在真实数据上点击；动作链路
  由真实 HTTP 请求 + e2e 覆盖，按钮渲染由单测覆盖。
- 没有会话处于收尾冷却窗口时，「已收尾 · 剩余 N 秒」只在测试里覆盖。
- 单条时间线只有一个通道，QQ / 微信配色差异靠静态断言；两通道同时出现时才有对比断言。
- 视觉回归依赖运行中的面板与本机 Edge / Chrome：没有面板时 `skip` 并写明原因，
  绝不当成通过。

## 6. 相关文件

- 计划与证据：`.omo/plans/`、`.omo/evidence/F1..F4-*.md`（`.omo/` 被 gitignore，
  不入库，仅留本地）。
- 主要改动：`src/dafeiyu/config.py`、`src/dafeiyu/service.py`、`src/dafeiyu/control.py`、
  `src/dafeiyu/memory/store.py`、`src/dafeiyu/supervisor/dashboard.py`、
  `src/dafeiyu/supervisor/dashboard/index.html`、`scripts/qa_dashboard_shots.py`。
