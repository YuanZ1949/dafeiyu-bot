# 端到端验收记录（Task 14）

- 计划：`docs/superpowers/plans/2026-10-24-bot-memory-workspace-web.md`（Task 14）
- 设计：`docs/superpowers/specs/2026-10-24-bot-memory-workspace-web-design.md` §14.3
- 分支：`feat/qq-bot`，起点 HEAD `22689e3`（Task 13.1 完成时）
- 日期：2026-10-25
- 环境：Windows 10 / Python 3.11.7 / opencode 2.0.18

## 结论

| 范围 | 结果 |
| --- | --- |
| 离线（MockTransport，不连 QQ） | 5 条完全通过（1 / 5 / 6 / 7 / 9），2 条部分通过（2 / 3） |
| 真机（真实 opencode server + NapCat + 模型） | 2 条待放行（4 / 8），另 2 条的剩余部分（2 / 3）同批验证 |
| 全量测试 | `390 passed in 95.78s`（基线 386，新增 `tests/test_e2e_batch.py` 4 条） |
| 发现缺陷 | 1 个：`/forget` 不删远端 session（已修，见「偏差与修复」） |

离线可判定的部分全部通过；剩余 4 条必须连真实 server / QQ / 模型才能判定，操作步骤见文末「真机联调清单」。

## 验收明细

### 1. `pytest -q` 全绿，新增用例覆盖 14.1 的 17 项 —— ✅ 通过

操作：

```
.venv\Scripts\python.exe -m pytest -q
```

实际输出：

```
390 passed in 95.78s (0:01:35)
```

覆盖归属（文件级映射，逐条断言见测试源码）：

| §14.1 条目 | 覆盖文件（用例数） |
| --- | --- |
| 1 `complete()` 建 session / 取末条 / DELETE | `test_brain_complete.py`(6) |
| 2 DELETE 404、轮询超时 | `test_brain_complete.py`、`test_opencode_http.py`(15) |
| 3 `_ask()` 多步 / 自己排序 | `test_ask_round.py`(7) |
| 4 `_ask()` 超时、跳过无文本 tool-call | `test_ask_round.py` |
| 5 `on_tool` 回调次数与 60s 去重 | `test_ask_round.py`、`test_run_batch.py`(9) |
| 6 `build_injection` 三段 / 截断 / 私聊省略 | `test_injection.py`(4) |
| 7 工作区读取 / 超长截断 / 尾部优先 | `test_workspace.py`(5) |
| 8 45s 窗口 / 8s 防抖 / 30s 上限 / 90s 间隔 / 串行 | `test_delivery_window.py`(5)、`test_delivery_scheduler.py`(4)、`test_service_batches.py`(6) |
| 9 积压乱序 | `test_store_batch.py`(6)、`test_service_batches.py` |
| 10 预算 80% / 点名尾部 / 耗尽丢弃 | `test_run_batch.py`、`test_service_batches.py` |
| 11 BrainError 后不重投 | `test_run_batch.py`、`test_service_batches.py` |
| 12 轮换（阈值 / 摘要 / 旧 session 保留 / 【接续】） | `test_rotation.py`(6)、`test_e2e_batch.py`(4) |
| 13 记账（多步累加 / calls +1 / 分用途） | `test_review_fixes.py`(16)、`test_run_batch.py`、`test_e2e_batch.py` |
| 14 守卫（synthetic / 窗口外 user / 0 误报） | `test_brain_complete.py`、`test_brain_sessions.py`(7) |
| 15 回滚（rules 路径 / workspace.dir=""） | `test_config_agents.py`(15)、`test_config_phase2.py`(18)、`test_service.py`(18) |
| 16 `/forget`（清库 / DELETE / 保留笔记） | `test_service.py`、`test_brain_complete.py`、`test_e2e_batch.py` |
| 17 跨群越权（无 glob/grep allow、read 限工作区） | `test_config_agents.py` |

### 2. 摘要与印象真跑一次，`summaries` / `impressions` 行数 > 0，dashboard 可见 —— ⚠️ 部分通过

离线已证（落库与展示链路）：

- `test_summary.py`(6)：`summarize` 截断到 200 字并落库、`get_summary` 往返。
- `test_impressions.py`(8)：印象 upsert / 去重 / 排序。
- `test_review_fixes.py`(16)：摘要循环定期执行直到停止、印象 worker 记账、印象按消息数而非回复数计。
- `test_supervisor_dashboard.py`(16)：dashboard 读取侧。

缺口：没有一条用例跑真实模型产出摘要/印象。**待真机**：见清单第 2 步。

### 3. 群里问联网问题：10 秒内先出提示语，最终回复引用页面事实 —— ⚠️ 部分通过

离线已证（机制，非时延）：

- `test_ask_round.py`(7)：tool part 出现 → `on_tool` 回调一次；同 chat 60s 内不重复。
- `test_run_batch.py`(9)：`speak=false` 且本轮已发过提示 → 补一条 `FOLLOWUP_REPLY`。

缺口：真实 10 秒时延与「回复引用页面事实」依赖真实模型 + 外网。**待真机**：见清单第 3 步。

### 4. 3 次对话内 `MEMORY.md` 出现模型自写条目 —— 🔒 待放行

判定口径按 spec：断言日志里 `read` / `edit` 工具调用 ≥1 次；若 3 轮为 0，再看权限拒绝记录与 tool 调用次数（可观测，不靠模型心情）。

离线只能证配置侧：`test_config_agents.py` 断言 agent 文件里 `data/bot-workspace/**` 有 `read`/`edit` allow、通配 deny 在前、`glob`/`grep` 不在 allow 里。

### 5. `/forget` 后：SQLite 记忆为 0，笔记仍在，session 已删除 —— ✅ 通过（修复后）

用例：`tests/test_e2e_batch.py::test_e2e_forget_clears_memory_but_keeps_notes`

实际断言（全部通过）：

- `store.count_messages(GROUP, 1001) == 0`
- `store.get_opencode_session(GROUP, 1001) is None`
- server 调用序列包含 `("DELETE", "/api/session/ses_old")`
- `MEMORY.md` 内容仍含「喜欢水草和沉木」，`chats/*.md` 文件仍在
- 回复 `记忆清空了，本鱼失忆中……🫧`

配套：`tests/test_brain_complete.py::test_forget_deletes_remote_session`（brain 层 DELETE）。

**这条最初是红的**，暴露出一个真缺陷，见「偏差与修复」。

### 6. 轮换后新 session_id，首轮 prompt 含【接续】且不重复摘要 —— ✅ 通过

用例：`tests/test_e2e_batch.py::test_e2e_rotation_switches_session_and_injects_continuation`

实际断言：

- 前置 `bump_session_stats(messages=60)` 触发 `rotation_messages` 阈值
- prompt 含 `【接续】` 与摘要正文，且摘要正文在 prompt 里只出现 **1 次**（未与【昨日摘要】重复）
- 临时摘要 session 用完即删：`("DELETE", "/api/session/ses_1")` 在调用序列里
- 旧 session 按设计保留：`("DELETE", "/api/session/ses_old")` **不在**调用序列里
- 映射从 `ses_old` 变为 `ses_2`；`delivered_count == 1`

### 7. 静音时段内：无调用、无发送、消息标 `skipped_reason` —— ✅ 通过

用例：`tests/test_e2e_batch.py::test_e2e_quiet_hours_never_calls_model`（clock 固定 03:00）

实际断言：`skip_reason == "quiet"`、`pending_chats() == []`、`scheduler.tick() == []`、`server.calls == []`、`adapter.sent == []`。

### 8. 权限实测：read `.env` 被拒、`shell` 被拒、写 `data/bot-workspace/x.md` 成功 —— 🔒 待放行

离线只能证配置侧（`test_config_agents.py`：通配 `*/*` deny 在最前、工作区 allow、`webfetch` allow、无 `shell` allow；`test_runtime_config.py` 证隔离目录只含 `$schema`/`model`/`plugins: []`）。

真实拒绝行为必须由 server 执行工具才能观测。

### 9. 注入外来 synthetic → 日志命中 + 下一轮换 session —— ✅ 通过

用例：

- `tests/test_brain_complete.py::test_audit_flags_synthetic_and_ignores_normal_round`：外来 `synthetic` 命中、正常轮次 0 误报。
- `tests/test_brain_complete.py::test_reply_rotates_on_foreign_message`：命中后 `reset_session`，下一轮重建。

## 偏差与修复

### `/forget` 不删远端 session（Task 14 发现并修复）

**现象**：`test_e2e_forget_clears_memory_but_keeps_notes` 首跑时，`("DELETE", "/api/session/ses_old")` 不在 server 调用序列里——SQLite 记忆清了、笔记还在，但远端 session 没删。这与 spec §11「`/forget` 真删 session」、§14.3 第 5 条「session 已删除」冲突。

**根因**：`Service._forget_chat` 先调 `store.forget_chat()`，它会把 `brain_sessions` 映射一起清掉；随后 `brain.forget()` 去查映射时已经查不到 session_id，于是静默跳过 DELETE。brain 层的 `test_forget_deletes_remote_session` 直接调 `brain.forget`，绕过了这个顺序，所以一直没暴露。

**修复**（`src/dafeiyu/service.py` `_forget_chat`）：调换顺序——**先** `brain.forget()`（保留 awaitable 判断），**再** `store.forget_chat()`，并加注释说明为什么顺序不能反。

**验证**：`pytest tests/test_e2e_batch.py tests/test_service.py -q` → `22 passed`；全量 `390 passed`。

**退路**：无需退路。修复是纯顺序调整，不改变任何对外签名；旧行为（不删远端）没有依赖方。

## 真机联调清单（待放行）

按顺序执行，每步记实际输出：

1. **启动**：`python -m dafeiyu`（或 supervisor dashboard 启动 bot）。
   - 确认 opencode 子进程的 `XDG_CONFIG_HOME` 指向 `data/opencode-runtime/config`（`opencode debug paths` 同法验证）。
   - 确认 `GET /api/agent` 只剩 `deepseek-qq` / `dafeiyu-memory` + 内置（Compaction/Summary/Title），不再出现 `Tamako` / `oceanus` / `sisyphus` / `prometheus`（S6 隔离的真机确认）。
2. **第 2 条**：连 NapCat 后在群里聊够 `memory.extract_every` 条，或手动触发一次摘要/印象；查
   `sqlite3 data/dafeiyu.db "select count(*) from summaries; select count(*) from impressions;"`，再开 dashboard 看得到。
3. **第 3 条**：群里 @ 机器人发一个真实 URL（如 `https://example.com`），计时看 10 秒内是否先出提示语，最终回复是否引用页面事实。
4. **第 4 条**：3 轮对话后看 `data/bot-workspace/MEMORY.md` 是否出现模型自写条目，并查日志里 `read`/`edit` 工具调用次数。
5. **第 8 条**：让机器人 read `.env`（应被拒）、执行 `shell`（应被拒）、写 `data/bot-workspace/x.md`（应成功），记录权限拒绝日志。
6. **第 9 条**（真机复核）：向 bot 的 session 注入一条外来 synthetic，确认日志命中且下一轮换 session。

## 未验证项与风险

- 第 2/3/4/8 条的真机部分未跑，结论不成立前不应视为验收完成。
- 隔离配置（Task 13.1）后的模型认证、工具权限只做过路径级验证，没跑过真实请求。
- §11.1 的污染根因（OmO/yzOpencode 自动续跑）仍是推测；真机跑一轮无污染即可反证。
