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
| 真机（真实 opencode server + 真实模型，未连 QQ） | 3 条通过（3 / 4 / 8），1 条云端配额阻塞（2） |
| 本地模型模式（LM Studio + 假 NapCat） | 第 2 条补验通过（摘要 1 行 / 印象 11 行 / 下一轮回复引用印象） |
| 全量测试 | `394 passed in 156.78s`（Task 14 起点基线 386；含后续 on_tool 修复新增 1 条） |
| 发现缺陷 | 5 个已修（`/forget` 不删 session、supervisor 没注入隔离 env、`OPENCODE_CONFIG` 覆盖隔离、provider 错误死等 240s、`on_tool` 提示语从不触发） |

真机部分**未连 QQ**：直接对隔离后的 `opencode serve` 发真实 prompt，覆盖了权限、联网、记笔记三条；唯一必须进群才能判定的只剩「真实群消息 → 提示语时延」的场景差异（见第 3 条）。
第 2 条（摘要/印象）先被 provider 免费额度（429）挡住，属于外部阻塞，不是代码缺陷——但它顺带暴露了第 4 个缺陷（错误被吞成超时），已修；后来改用**本地模型模式**（LM Studio `qwen/qwen3-8b` + 假 NapCat）补验通过，见第 2 条。

## 验收明细

### 1. `pytest -q` 全绿，新增用例覆盖 14.1 的 17 项 —— ✅ 通过

操作：

```
.venv\Scripts\python.exe -m pytest -q
```

实际输出：

```
393 passed in 81.14s (0:01:21)
```

393 = Task 13.1 基线 386 + 本任务新增 7（e2e 4 + supervisor 隔离 2 + provider 错误 fail-fast 1）。

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

### 2. 摘要与印象真跑一次，`summaries` / `impressions` 行数 > 0，dashboard 可见 —— ✅ 通过（本地模型模式）

离线已证（落库与展示链路）：

- `test_summary.py`(6)：`summarize` 截断到 200 字并落库、`get_summary` 往返。
- `test_impressions.py`(8)：印象 upsert / 去重 / 排序。
- `test_review_fixes.py`(16)：摘要循环定期执行直到停止、印象 worker 记账、印象按消息数而非回复数计。
- `test_supervisor_dashboard.py`(16)：dashboard 读取侧。

真机执行（2026-10-25，隔离 serve）：

```
[impressions] written=0 rows=0 sample=[]
[summary] len=0 stored=None
```

原因不是代码：`dafeiyu-memory` 的 assistant 消息以 `finish="error"` 结束，内容为空：

```json
{"type":"assistant","agent":"dafeiyu-memory","content":[],"finish":"error",
 "error":{"type":"provider.quota",
          "message":"Free usage limit reached. Free models work in OpenCode (https://opencode.ai); subscribe to OpenCode Go to keep going: https://opencode.ai/go",
          "status":429}}
```

同一时刻 `deepseek-qq` 用同一个模型能正常返回（`finish=stop`），所以是**该模型免费额度被打满**，不是 agent 配置错、也不是模型不可用。`summary.py` 捕获异常后打印「生成摘要失败」并返回空串，`ImpressionWorker` 写入 0 行——降级行为符合设计。

**退路**：等额度恢复，或把 `deepseek-qq` / `dafeiyu-memory` 的 `model:` 换成非 free 模型后重跑本步。落库与展示链路由离线用例保证，真机只差一次成功产出。

#### 本地模型模式补验（2026-10-25 16:0x）

环境隔离：临时 cwd + 复制 `config.yaml`（`brain.provider: local_model`、`local_model.model: qwen/qwen3-8b`），`NAPCAT_WS_PORT=18080`、`DAFEIYU_CONTROL_PORT=18788`；SQLite、workspace、日志全部落在临时目录。生产 `data/dafeiyu.db` 与 `data/runtime.json` 的 mtime / sha256 跑前跑后不变。

操作与结果：

| 步骤 | 命令 / 操作 | 结果 |
| --- | --- | --- |
| 1 启动 | `python -m dafeiyu`（临时 cwd，`provider=local_model`） | `GET /api/brain` → `{"provider":"local_model","model":"qwen/qwen3-8b"}` |
| 2 群消息 | 假 NapCat 连 `ws://127.0.0.1:18080/`，发 20 条群消息（含昵称 / 偏好 / 事实） | 收到群回复 31 字：`在在在！本鱼已经准备好翻肚皮给你看了～…` |
| 3 印象落库 | 第 20 条消息触发真实 `ImpressionWorker` | `impressions` **11 行**；`usage` `purpose=impression p=596 c=404` |
| 4 摘要落库 | 真实 `summarize_chat(store, brain, GROUP, 910000001, "2026-10-25")` | `summaries` **1 行**，156 字；`usage` `purpose=summary p=435 c=107` |
| 5 记忆生效 | 再发「你还记得群里谁养猫吗」 | 回复 `红姐养的猫，不过小美也喜欢猫……`（来自印象注入，不是当轮消息） |

印象样本（节选）：

```
10002 nickname 外号叫鱼片            0.9
10003 nickname 外号叫红姐            0.9
10003 fact     住在杭州              0.9
10003 preference 喝咖啡不加糖，只加奶  0.9
10002 fact     养了一只柯基叫土豆      0.8
```

摘要正文（156 字，≤200）：

```
群聊中成员分享日常与兴趣，提及养猫、编程语言学习及职业规划。小明养了两只猫，分别叫团子和土豆，
红姐住在杭州，喜欢爬山且喝咖啡不加糖。阿强外号强哥，是左撇子，不吃香菜。小美喜欢猫，讨厌狗，
正在准备考研。验收员提到本地模型记忆功能测试，Deepseek娘回应表示已准备好。群内成员包括程序员、
产品经理等不同职业背景。
```

**模型选型坑（本地模型模式）**：`qwen/qwen3.5-9b` 是重推理模型，记忆旁路固定传 `max_tokens=600`，预算全被 reasoning 吃掉，正文为空：

| 模型 / 参数 | completion_tokens | reasoning 长度 | 正文 |
| --- | --- | --- | --- |
| `qwen/qwen3.5-9b`，600 | 600（打满） | 1833 字符 | 空 |
| `qwen/qwen3.5-9b`，2048（印象 JSON） | 2048（打满） | — | 空 |
| `qwen/qwen3.5-9b`，2048（摘要） | 1118 | — | 119 字 ✅ |
| `qwen/qwen3.5-9b` + `/no_think` 或 `chat_template_kwargs.enable_thinking=false` | 600（打满） | 1817 字符 | 空（模板忽略开关） |
| `qwen/qwen3-8b`，600 | 404 | 0 | 11 条 JSON ✅ |

结论：本地模型模式要跑记忆旁路，选**非思考模型**（本轮 `qwen/qwen3-8b`）；若坚持用 9B 思考模型，需要调大 workers 的预算，而印象抽取即使 2048 也会被打满——所以选型比调参更可靠。这是模型选型风险，不是代码缺陷，本轮未改代码。

**「dashboard 可见」的偏差**：dashboard 路由只有 `health / status / logs / qr / brain / control / shutdown`，没有摘要/印象面板。可判定证据改为「库内行数 > 0 + 下一轮回复确实引用印象」；若需要面板，属于新需求。

### 3. 群里问联网问题：10 秒内先出提示语，最终回复引用页面事实 —— ✅ 通过（事实 ✅ / 提示语 ✅ 已修）

真机执行（隔离 serve + 真实模型）：

| 探针 | 耗时 | 工具 part | 提示语 | 回复内容 |
| --- | --- | --- | --- | --- |
| 单次 webfetch `https://httpbin.org/uuid` | 5.3s | `('tool','completed')` | 无（修复前） | 含真实 uuid `e34800e8-…`（无法凭记忆编出） |
| 两次串行 webfetch `/delay/4` + `/uuid` | 12.5s | 两次 completed | 无（修复前） | 含 `url = https://httpbin.org/delay/4` 与 uuid `24a6e434-…` |
| 单次 webfetch `/uuid`（修复后复跑） | 6.7s | `('tool','completed')` | **4.31s 触发** | 含真实 uuid `5af08450-…` |

**事实引用：通过。** 三次都真抓了页面，返回值可核验。

**10 秒内提示语：修复后通过。** 修复前两次探针 `marks=[]`，即使整个轮次长达 12.5 秒、中间有 4 秒阻塞的 webfetch。修复后复跑同一探针：`ON_TOOL webfetch 4.31` / `TOTAL 6.73`，提示语早于最终回复 2.4 秒。

根因（`src/dafeiyu/brain/opencode_http.py` `_ask`）：`on_tool` 的检查在轮询循环**顶部**，用的是 `_wait` 之前的 messages；而 `_wait` 是长轮询，**阻塞到会话空闲才返回 204**。真实 server 下整个轮次往往在第一次 `_wait` 里就跑完，拿到 204 后重新拉一次 messages 直接 `return`，中途再也不检查 tool part。单元测试能过是因为 `FakeServer` 的 wait 每次只补一步并返回 200（未空闲），下一轮顶部检查才看得到 running 的 tool part——真实 server 不这样分次返回。

**修复**：`_wait` 用 `asyncio.wait_for(..., timeout=WAIT_SLICE_SECONDS)`（2s）切片；切片超时视为「本轮还在跑」，直接回循环顶部重读 messages 并检查工具 part，不再额外 sleep（切片本身已经等过）。正常完成路径仍是 204 立即返回，时延不变。测试替身 `BlockingWaitServer`（`tests/test_ask_round.py`）忠实复现「阻塞到空闲才回 204」：修复前 RED（`marks=[]`）、修复后 GREEN。

### 4. 3 次对话内 `MEMORY.md` 出现模型自写条目 —— ✅ 通过（真机）

判定口径按 spec：断言日志里 `read` / `edit` 工具调用 ≥1 次；若 3 轮为 0，再看权限拒绝记录与 tool 调用次数（可观测，不靠模型心情）。

真机执行（隔离 serve，会话 99101，一轮内让它先读再写）：

```
[tools] 9.2s reply='TOOL_OK'
[tools] parts=[('edit', 'completed'), ('read', 'completed')]
```

`data/bot-workspace/MEMORY.md` 新增模型自写条目（「验证：example.com 的主标题是 Example Domain」等两行）。`read` 与 `edit` 都是 `completed`，不是被拒后自说自话。

离线配置侧另有 `test_config_agents.py` 断言：`data/bot-workspace/**` 有 `read`/`edit` allow、通配 deny 在前、`glob`/`grep` 不在 allow 里。

### 8. 权限实测：read `.env` 被拒、`shell` 被拒、写 `data/bot-workspace/x.md` 成功 —— ✅ 通过（真机）

真机执行（隔离 serve + 真实模型 + 真实工具执行）：

| 探针 | 结果 | 耗时 | 备注 |
| --- | --- | --- | --- |
| 读仓库根的环境文件 | `DENIED` | 5.8s / 5.0s（两次） | 模型报告无法读取，无工具成功记录 |
| `shell` 执行 `echo hi` | `DENIED` | 2.1s / 3.5s（两次） | agent 文件没有 shell allow |
| 写 `data/bot-workspace/verify.md` | `WRITE_OK` | 7.0s / 6.4s（两次） | 文件落盘，内容「验证条目 e2e-2026」 |

三次探针各用独立 chat_id（99001–99005）隔离上下文，避免上一轮的工具结果影响判断。

### 9. 注入外来 synthetic → 日志命中 + 下一轮换 session —— ✅ 通过（离线）

用例：

- `tests/test_brain_complete.py::test_audit_flags_synthetic_and_ignores_normal_round`：外来 `synthetic` 命中、正常轮次 0 误报。
- `tests/test_brain_complete.py::test_reply_rotates_on_foreign_message`：命中后 `reset_session`，下一轮重建。

真机复核未做（需要 bot 进程持有真实会话再注入）；这条的判定不依赖模型行为，离线用例直接覆盖审计与轮换逻辑，风险低。

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

### supervisor 拉起的 serve 没带隔离 env（真机发现并修复）

**现象**：`opencode.manage_server: false`——生产路径是 supervisor 拉起 `opencode serve`，bot 只当客户端。而 `supervisor/main.py` 的 `specs["opencode"]` 只带了 `OPENCODE_DB` / `NO_COLOR`，**Task 13.1 的隔离在真实部署里完全没生效**，尽管 `_build_env()` 那条路径的测试是绿的（测试只覆盖了 `manage_server: true` 的自管路径）。

**修复**（`src/dafeiyu/supervisor/main.py`）：`build_specs` 先构造 `opencode_env`，再 `update(server_env(settings.opencode.config_home) or {})`。

**验证**：`tests/test_supervisor_specs.py` 新增 2 条（带/不带 `config_home`），8 passed。

**退路**：`config_home: ""` 时两个变量都不注入，行为回到改动前。

### `OPENCODE_CONFIG` 盖过 `XDG_CONFIG_HOME`（真机发现并修复）

**现象**：按上一条修好后，隔离 serve 的日志里**仍然**出现 `[oceanus] CBM daemon 预热就绪` / `[oceanus] agent-browser 探测`——全局插件照常加载，`plugins: []` 没被读到。

**根因**：运行环境里存在 `OPENCODE_CONFIG=C:\Users\yuan\.config\opencode\opencode.jsonc`，它**优先级高于** `XDG_CONFIG_HOME`。只设 `XDG_CONFIG_HOME` 不足以隔离。

**修复**（`src/dafeiyu/brain/runtime_config.py`）：`server_env()` 同时返回两个键——`XDG_CONFIG_HOME=<root>` 和 `OPENCODE_CONFIG=<root>/opencode/opencode.jsonc`。

**验证**：隔离 serve 重启后 `GET /api/config` 只返回一个 document（`data/opencode-runtime/config/opencode/opencode.jsonc`，`plugins: []`）加上仓库自己的 `.opencode`；日志里不再出现 `[oceanus]`。

**退路**：`config_home: ""` → `server_env()` 返回 `None`，两个变量都不覆盖，回落到启动环境。

### provider 错误被吞成 240 秒超时（真机发现并修复）

**现象**：摘要/印象每次都要等满 `timeout_seconds: 240` 才报「OpenCode 超时」，看起来像模型慢或 agent 配置错。

**根因**：`_ask` 的完成判据 `_round_finished` 要求最后一条 assistant 有非空 text。模型因配额被拒时返回 `finish="error"` + `content: []`，条件永远不满足 → 死等到超时，真实错误（`provider.quota` / 429）被丢掉。这正是被隔离掉的 `retry-fast-fail` 插件以前负责的事。

**修复**（`src/dafeiyu/brain/opencode_http.py`）：新增 `_round_error()`，在 `_wait` 返回后、`_round_finished` 之前检查最后一条 assistant 的 `finish == "error"`，抛 `BrainError("OpenCode 调用失败（{type}）：{message}")`。

**验证**：`test_reply_surfaces_provider_error_without_waiting`（RED 时异常文本是「OpenCode 超时（2 秒）」）→ 16 passed；真机 traceback 指向 `opencode_http.py:309`，配额原文直接出现在日志里。

**退路**：无需退路。正常轮次 `finish` 不是 `"error"`，行为不变。

### `on_tool` 提示语在真实 server 上从不触发（已修）

见第 3 条。`_ask` 的长轮询 `_wait` 现在按 2s 切片（`WAIT_SLICE_SECONDS`），超时即回循环顶部重读消息、检查工具 part。测试替身 `BlockingWaitServer` 复现真实语义（阻塞到空闲才回 204）：修复前 RED（`marks=[]`）、修复后 GREEN。真机复跑：`ON_TOOL webfetch 4.31` / `TOTAL 6.73`。

## 真机联调执行记录

**已执行（2026-10-25，未连 QQ）**：手工以隔离 env 启动 `opencode serve`（`XDG_CONFIG_HOME` + `OPENCODE_CONFIG` 指向 `data/opencode-runtime/config`，独立 `OPENCODE_DB=data/opencode-verify.db`，端口 24310，Basic 认证），再用 `OpenCodeHttpBrain` 直接发真实 prompt。

隔离生效证据：

```
GET /api/config
→ [{"type":"document",
    "path":"...\\data\\opencode-runtime\\config\\opencode\\opencode.jsonc",
    "info":{"$schema":"https://opencode.ai/config.json",
            "model":{"providerID":"opencode","model":"deepseek-v4.1-flash-free"},
            "plugins":[]}}]
启动日志：无 [oceanus] / 无全局 MCP 预热
```

注：`GET /api/agent` 返回 `{"data": []}`，**不是** agent 列表，不能用它证明隔离——证据用 `/api/config` 与启动日志。

| 步骤 | 状态 | 结果 |
| --- | --- | --- |
| 1 启动 + 隔离确认 | ✅ | 见上 |
| 2 摘要/印象真跑 | ✅ | 云端 429 后改用本地模型模式补验：摘要 1 行 / 印象 11 行，见第 2 条 |
| 3 联网 + 提示语 | ⚠️ | 事实 ✅（5.3s / 12.5s，真 uuid）、提示语 ❌，见第 3 条 |
| 4 `MEMORY.md` 自写 | ✅ | `read`/`edit` 均 completed，见第 4 条 |
| 5 权限三项 | ✅ | 拒 / 拒 / 成，见第 8 条 |
| 6 synthetic 真机复核 | 未做 | 离线已覆盖，风险低 |

**仍需进群才能补的**：真实群消息下的提示语时延（第 3 条缺口）——但它已经被证明是「从不触发」而非「超过 10 秒」，进群也只会得到同样的否定结论，先修代码再验更划算。

## 未验证项与风险

- **第 2 条**：云端 free 模型仍被 429 挡住；已改用本地模型模式（`qwen/qwen3-8b` + 假 NapCat）补验通过。云端 opencode 路径下的摘要/印象仍未成功产出过一次，属外部配额问题。
- **本地模型模式的模型选型**：`qwen/qwen3.5-9b` 会把 workers 的 600-token 预算全花在 reasoning 上，正文为空；要跑记忆旁路请选非思考模型。dashboard 没有摘要/印象面板，「可见」以库内行数 + 下一轮注入为准。
- **第 3 条提示语**：已修并在真机复跑验证（4.31s 触发 / 6.73s 结束）；仍需进群确认 QQ 侧的发送时延。「事实引用」已通过。
- **第 9 条真机复核**：未做，离线用例已覆盖审计与轮换逻辑。
- **未连 QQ**：真实群消息的时延、`@` 识别、静音判定都走的是离线用例；`triggers.mode: model` 的批次链路只在 MockTransport 下跑过完整流程。
- **隔离后的副作用**：bot 的 serve 不再加载全局 `dcp`（上下文裁剪）与 `retry-fast-fail`（provider 重试）。前者由 bot 自己的轮换替代，后者的职责部分由本任务新增的 `_round_error` 补上（只报错，不重试）。
- **`config_home` 的两种生效路径**都已有测试：`manage_server: true` 走 `_build_env()`，`false` 走 supervisor `build_specs`。真机只跑了后者的手工等价物（直接起 serve），supervisor 本身没启动过。
- **模型可用性**：`opencode/deepseek-v4.1-flash-free` 的免费额度会周期性打满。长期跑群聊应换成有额度的模型，否则摘要/印象会静默降级（现在是明确报错 + 空结果）。

## 2026-10-25 第二轮验收（真机 + dashboard 记忆面板）

本轮契约见 `.tamako/contract.json`，五项逐条取证如下。

### 1. `qwen/qwen3.5-9b` 记忆调用 ≤60s 且落库（✅）

探针在临时库上走真实 LM Studio（`local_model.reasoning_effort: none`、`max_tokens: 2048`、`timeout_seconds: 60`），串行执行印象抽取与按日摘要：

```
impression_seconds=5.05
summary_seconds=1.70
impressions_added=3 rows=3
  - 1001 nickname: 外号叫鱼片
  - 1001 fact: 喜欢钓鱼
  - 1002 preference: 怕虫子，不喜欢露营
summary_len=100 stored=True
usage_rows=2
  - impression: prompt=266 completion=102
  - summary: prompt=168 completion=76
RESULT=PASS
```

结论：思考关闭后 9B 模型两次记忆调用分别 5.05s / 1.70s，均远低于 60s 硬上限；`impressions` / `summaries` / `usage` 三张表都有数据。

### 2. dashboard 摘要/印象面板 + `GET /api/memory`（✅）

- bot 控制口新增只读 `GET /api/memory?limit=N`（默认 20，上限 200）：`BrainControl.memory_snapshot()` 读 `Store.recent_summaries()` / `recent_impressions()`，跨会话按时间倒序。
- dashboard 新增 `/api/memory` 代理（转发查询串、透传状态码）+ 只读面板「记忆」：左列按日摘要、右列群印象，10s 自动刷新，内容用 `textContent` 写入（模型产出的文本不当 HTML 解析）。
- 测试三种情况：空库 `{"summaries": [], "impressions": []}`；有数据（摘要/印象字段逐项断言）；读失败（关闭 DB 后 500 + 「读取记忆失败」）。dashboard 侧另有代理透传与 bot 离线（502/504）两条。
- 真机 HTTP：supervisor 运行中，`GET http://127.0.0.1:8787/api/memory?limit=3` → `200 {"summaries": [], "impressions": []}`（当前库确实为空，空库分支）。

### 3. 解耦 + 全量回归（✅）

`pytest -q` → **403 passed in 177.30s**（契约要求 > 394）。新增参数只挂在 `local_model` 上：

- `test_token_brain_does_not_inherit_local_reasoning_effort`：`local_model.reasoning_effort: none` 时 token brain 的 `reasoning_effort is None`。
- `test_local_model_reasoning_effort_defaults_and_overrides` / `test_local_model_brain_passes_reasoning_effort`：默认空串、可覆盖、能传到 client。

### 4. supervisor + NapCat 真机群消息（✅）

`scripts/start-all.ps1 -NoBrowser` 启动后：

```
opencode  running pid=16544 port=24310
bot       running pid=33864 port=8788
napcat    running pid=15648
napcat: reachable=true logged_in=true qq=10001 nickname=：） gateway_connected=true
```

真实群 **20001** 的往返（取 `data/dafeiyu.db`）：

| 消息 | id | 时间 | 内容摘要 |
| --- | --- | --- | --- |
| 群友 | 79 | 18:17:20 | 在吗 |
| bot | 80 | 18:17:31 | 刚睡醒…… 在的在的…… |

**时延 11s**（到达 → 发送落库）。同批日志里 `dafeiyu-memory` 在 18:16:37 因 `provider.quota`（free 额度）失败两次，属既有外部配额风险，主回复链路不受影响。

### 5. 提交与工作区（✅）

本轮 dashboard 记忆面板独立提交 `e03efb7 feat(dashboard): 只读记忆面板 + GET /api/memory 代理`；此前 local_model 参数在 `c5c5a9b`。收尾时 `git status --short` 为空。

