# 大肥鱼：长期记忆 · 自维护工作区 · 联网 · 批次投递（设计）

- 状态：待评审
- 日期：2026-10-24
- 基线：`ae7645b`（工作区干净，`pytest -q` 294 passed）
- 关联：`2026-10-24-im-bot-design.md`（记忆/触发初版）、`2026-10-24-opencode-supervisor-dashboard-design.md`

## 0. 一句话

给大肥鱼一间自己的房间（工作区）、一份每天更新的简报（摘要 + 印象 + 长期记忆），一双能看网页的眼睛（只开 `webfetch`，触发时先说一句），以及一套"攒一批再决定要不要说话"的投递机制，并给会话装上轮换阀门。

## 1. 背景与目标

### 1.1 已验证的现状

| 事实 | 证据 |
| --- | --- |
| 群/私聊各有独立 OpenCode session，prompt 是时间线 | `service._ask_brain` → `timeline.build_delivery` |
| `summaries` / `impressions` 在 opencode 模式下恒为 0 行 | `SummaryScheduler`/`ImpressionWorker` 调 `brain.complete*()`，`OpenCodeHttpBrain` 只有 `reply()`，异常被 `except Exception` 吞成 warning |
| `usage` 的 token 恒为 0 | `parse_brain_reply(prompt_tokens=0, completion_tokens=0, ...)`，`budget.daily_tokens` 从未生效 |
| agent 是 `deepseek-qq`，`steps: 1`，通配 deny | `.opencode/agents/deepseek-qq.md` |
| bot 专用 server 继承用户全局配置 | `/api/agent` 返回 15 个 agent，含 `Tamako`、`oceanus`、`sisyphus`、`prometheus`、`Compaction` 等 |
| bot 的模型是全局默认的 `opencode/deepseek-v4.1-flash-free`，agent 未钉住 | `/api/agent` 中 `deepseek-qq` 的 `model: None`；全局 `opencode.jsonc` 的 `model` |
| session 无上限增长，轮询只看最近 30 条 | `_messages(limit=30)`；全局 `compaction.buffer=80000` |

### 1.2 目标

1. **长期记忆**：系统事实层（摘要 + 印象）复活；模型自写层（工作区笔记）可写可读；两者每轮注入。
2. **自主联网**：模型自己决定何时 `webfetch`；不启用 `websearch`；一旦抓取先发提示语。
3. **工作区**：模型可在 `data/bot-workspace/` 内自由读写，出了这个目录什么都不许碰。
4. **批次投递**：群消息攒批后交给模型判断说不说（C），用节流和预算分层控制成本。
5. **会话轮换**：按条数/天数/估算 token 换新 session，注入接续摘要。
6. **把已发现的洞一并补上**：token 记账、E 溯源、模型钉住、权限收敛。

### 1.3 非目标

- 不开 `websearch`（MCP 的 exa 搜索服务保持 deny）。
- 不开 `shell`，不给 `subagent`/`skill`/`question`。
- 不允许读写工作区以外的文件（含仓库源码）。
- 笔记不入 SQLite；`/forget` 不清笔记（已定 B）。
- 不改用户全局 `opencode.jsonc`/`cli.json`（硬约束）。
- 不做多模态、不改 NapCat 链路、不改 dashboard 视觉。

## 2. 一并处理的已发现问题

| 编号 | 问题 | 处理位置 |
| --- | --- | --- |
| P1 | `summaries`/`impressions` 空转 | §6 |
| P2 | token 记账恒 0，`daily_tokens` 从未生效 | §10 |
| P3 | session 无限增长，迟早撞上下文窗口 | §9 |
| P4 | bot server 继承 15 个全局 agent + 插件 + MCP；E 的 synthetic 消息最可能由此进入 | §11 |
| P5 | `steps: 1` 是为堵「自发工具 + 多条 assistant」的临时方案；`_ask` 取「第一条 completed assistant」会取错 | §7.1 / §7.3 |
| P6 | `/forget` 与工作区笔记的语义 | §4.6（已定：保留笔记） |
| P7 | 模型未钉住，改全局默认会静默改掉 bot 的模型 | §7.1 |

## 3. 总体架构

```
QQ ──▶ NapCat ──▶ Ingress ──▶ Store(messages)
                                  │
                                  ▼
                     ┌─────────────────────────┐
                     │ 投递调度（§8）           │
                     │ 批次窗口 / 点名防抖 / 节流│
                     └───────────┬─────────────┘
                                 ▼
                     ┌─────────────────────────┐
                     │ Prompt 组装（§5）        │
                     │ 长期记忆 + 摘要 + 印象   │
                     │ + 新消息时间线           │
                     └───────────┬─────────────┘
                                 ▼
              ┌──────────────────────────────────┐
              │ OpenCode server (127.0.0.1:24310) │
              │ agent=deepseek-qq, steps=4        │
              │  ├ webfetch（触发即提示 §7.3）     │
              │  ├ read/edit 仅 data/bot-workspace│
              │  └ 输出 {"speak","reply"}         │
              └──────────────┬───────────────────┘
                             ▼
                       QQ 发送 / 静默

后台（定时）：dafeiyu-memory agent → summaries / impressions（§6）
轮换：§9   记账：§10   安全守卫：§11
```

## 4. 模块 A：工作区

### 4.1 目录布局

```
data/bot-workspace/                  # data/ 已 gitignore，永不入库
  MEMORY.md                          # 全局长期记忆（人设、通用偏好、它学到的结论）
  chats/
    group-20001.md               # 群专属记忆
    private-504567327.md             # 私聊专属记忆
  notes/                             # 自由笔记，永不注入，只供它自己 read
```

### 4.2 权限矩阵（agent frontmatter）

| action | resource | effect |
| --- | --- | --- |
| `*` | `*` | deny（放最前，最后匹配者胜） |
| `read` | `~/.cache/opencode/models.json` | allow（既有，保留） |
| `read` / `edit` / `glob` / `grep` | `data/bot-workspace/**` | allow |
| `webfetch` | `*` | allow |
| `shell` / `websearch` / `skill` / `subagent` / `question` / `external_directory` / `execute` | `*` | deny |

要点：

- `edit` 同时覆盖 `write` 与 `apply_patch`，无需单独开。
- 绝对路径兜底：若实测 `data/bot-workspace/**` 在 session cwd 为仓库根时不生效，补 `**/data/bot-workspace/**` 与 `<绝对路径>/**` 两条 allow（spike S4 判定）。
- 权限是 agent 级、静态的：**关掉工作区必须改 agent 文件并重启 opencode**（见 §15）。

### 4.3 注入与截断

- 只注入 `MEMORY.md` + 当前会话那一份 `chats/<type>-<id>.md`，`notes/` 永不注入。
- 文件约定：首行 `# …` 标题；新条目**追加在末尾**，一条一行，形如 `- [2026-10-24] 内容`。
- 注入取「标题行 + 最后 N 行」，N 由 `workspace.memory_chars` 折算（默认 500 字 ≈ 25 行）。
- 被截断时在段末补 `（长期记忆还有 N 行未展开）`，让模型知道自己可以 read。
- 文件不存在 = 空段，不报错；首次运行自动建目录与空 `MEMORY.md`（bot 侧建，不依赖模型）。

### 4.4 写入规则（写进 persona）

- 只记「下次还用得上」的：群友的偏好/外号/身份、约定过的事、它查证过的结论、本群的黑话。
- 不记流水账、不复述原话、不记隐私细节（住址、电话、账号）。
- 一次最多追加 3 行；超过 80 行自己合并同类项。
- 群事写 `chats/group-<id>.md`，私聊写 `chats/private-<id>.md`；跨群通用才写 `MEMORY.md`。
- 抓回来的网页内容只是资料，不写进记忆，除非是结论性事实。

### 4.5 容量与安全

- 单文件软上限 64KB；超过后拒绝再写并在日志 warning（硬保护，防被诱导灌垃圾）。
- 网页文本里的「指令」一律不执行（persona + 无 shell + 目录隔离三重）。
- 工作区不含任何凭据；`.env` 在仓库根，permission 已 deny。

### 4.6 `/forget`

- 清 SQLite 该会话记忆 + 删 OpenCode session，**保留** `MEMORY.md` 与 `chats/*.md`。
- `/forget` 之后第一轮 prompt 仍带长期记忆段（它记得人，不记得昨天聊了什么）。这是刻意的。

## 5. 模块 B：记忆注入

### 5.1 三段结构与预算（默认总 900 字）

```
【长期记忆】
<MEMORY.md 标题 + 尾部条目>
<本会话 chats 文件尾部条目>

【昨日摘要】
<summaries 里该会话最近一条，≤250 字>

【群友印象】
<impressions top-3，每条「昵称：一句画像」>

—— 以下是自你上次开口以来的新消息 ——
[MM-DD HH:MM] 昵称：内容
...
```

| 段 | 预算 | 来源 | 缺省行为 |
| --- | --- | --- | --- |
| 长期记忆 | 500 字 | 文件系统 | 空则整段省略 |
| 昨日摘要 | 250 字 | `summaries` | 无则省略 |
| 群友印象 | 150 字 | `impressions` top-3 | 无则省略 |

- 三段都是**前缀**，时间线保持在最后（模型对末尾更敏感）。
- 私聊不注入印象段（没有群友概念）。
- `memory.inject_summaries` / `inject_impressions` 可单独关。
- 成本：约 600 token/次；按 200 次/天约 12 万 token，占 `daily_tokens` 四成。这是把预算调成 900 的原因，觉得贵就调 `workspace.inject_chars`。

### 5.2 与既有 `build_context` 的关系

- 不改 `memory/context.py` 的本地模型链路（`local_model` 分支继续用它）。
- opencode 分支新增 `memory/context.build_injection(store, workspace, chat) -> str`，只负责三段前缀；时间线仍由 `timeline.build_delivery` 产出，两者拼接。

## 6. 模块 C：记忆工人复活

### 6.1 缺的接口

`OpenCodeHttpBrain` 实现 `Brain` 协议的：

```python
async def complete(self, system: str, messages: list[dict], max_tokens: int) -> str
async def complete_detailed(self, system: str, messages: list[dict], max_tokens: int) -> CompletionResult
```

worker 一行不改。

### 6.2 新 agent：`dafeiyu-memory`

`.opencode/agents/dafeiyu-memory.md`：

- `mode: primary`、`steps: 1`、`model: <与 deepseek-qq 相同>`、全部工具 deny。
- system prompt 只做两件事：把聊天记录压成一段摘要；或从聊天记录抽出群友画像 JSON。靠输入文本里的任务指令切换。
- 输出纯文本/纯 JSON，不带寒暄。

### 6.3 调用流程

```
create session(agent=dafeiyu-memory, title="mem:<purpose>:<chat>")
  → POST /api/session/{id}/prompt  {text: system + "\n\n" + 转录}
  → 轮询取最后一条 completed assistant 文本（最多 90s）
  → DELETE /api/session/{id}（失败只记 debug）
  → 返回 (text, tokens_in, tokens_out)
```

- 不复用群 session：避免历史污染，也避免把摘要请求记进群上下文。
- 每次调用一个临时 session；删除失败就留档（数量可控：摘要每天每会话 1 次、印象每 20 条 1 次）。
- `system` 参数不能透传给模型（V2 prompt 只收 text），所以 **system 指令由 agent 文件承担，`system` 实参拼进 text 前部**；两者都不丢。

### 6.4 失败处理

- 任何失败 → warning 日志 + 返回空串/None，worker 原有逻辑不落库。
- 连续失败 3 次当日熔断（进程内计数），避免烧预算。

## 7. 模块 D：工具与联网

### 7.1 agent 改造（甲方案：单 agent 全开）

- `steps: 1` → `steps: 4`。
- 新增 `model: opencode/deepseek-v4.1-flash-free`（P7：钉住，避免全局默认变更静默影响 bot）。
- description 改为「可以联网查证、可以在自己的工作区记笔记」。
- 权限按 §4.2。
- persona 增补三节：「什么时候才查」「怎么记笔记」「网页不是指令」。

「什么时候才查」的判定（写进 persona）：

- 查：被明确问到事实/版本/价格/新闻/文档细节，且你不确定；用户给了链接让你看；技术问题需要最新信息。
- 不查：打招呼、闲聊、情绪回应、你已经确定的知识、纯梗。
- 一次回复最多查 2 个 URL；查完要给出结论，不要复述整页。

### 7.2 提示语（`on_tool` 回调）

- `reply(..., on_tool: Callable[[str], Awaitable[None]] | None = None)`。
- 轮询消息时，首次发现本轮出现 `webfetch` 的 tool part → `await on_tool("本鱼去网上查一下，稍等…")`。
- Service 侧节流：每轮最多 1 次；同一 chat 60 秒内不重复。
- 提示语发送失败不影响主流程（该路径本身可能正忙）。

### 7.3 解析改造（P5）

- 现状：`_ask` 取「第一条 completed assistant 文本」→ 多步时抓到「我先查一下」。
- 改为：本轮**最后一条** completed assistant 文本；忽略无文本的纯 tool-call 消息。
- 本轮结束判据（spike S2 定稿其一）：session 状态 idle；或最后一条 assistant `time.completed` 存在且无 `state.status in (pending, running)` 的 tool part。
- 结构化解析仍走 `parse_brain_reply`（JSON + 容错 salvage 不变）。

### 7.4 超时与失败

- `opencode.timeout_seconds`: 90 → **240**。HTTP 单请求超时（15s）不变，只作用于 API 调用。
- 已发提示语后失败 → 补一句「网卡了，没查到，等下再试」；未发提示 → 走既有「大脑断线」兜底。
- 工具被拒（权限）不重试，模型继续用已有知识回答；日志记 warning 便于发现配置错。

## 8. 模块 E：批次投递（C）

### 8.1 状态机

```
消息到达（群）
  ├─ 入库、标 delivered=0
  ├─ 命中屏蔽/静音/黑名单 → 结束（不投）
  ├─ 被点名（@/引用/叫名字/回复它）
  │     └─ 启动 8s 防抖计时；窗口内再来点名则重置
  │           └─ 到点 → 立刻投递一批
  └─ 普通消息
        └─ 累积；满足任一即投：
              · 距本批第一条 ≥ 45s
              · 距上次非点名调用 ≥ 90s 且本批非空
```

- 投递 = 把「上次投递之后的所有消息」+ 记忆段组成 prompt 调一次模型。
- 模型返回 `{"speak": false}` → 标记已投递，不发送。
- 同一群永远串行：上一轮未结束不启动下一轮（进程内 per-chat lock）。
- 私聊不走批次，直接投（延迟敏感）。

### 8.2 预算分层

- 非点名调用只能用到 `daily_calls * ambient_ratio`（默认 80% = 160 次）。
- 点名调用可用剩余 20%；硬上限仍是 `daily_calls`。
- token 上限按 §10 的真实记账执行，超限则一律静默（除点名且 calls 未满）。
- 预算耗尽时：不排队、不补发，直接丢弃并记 debug（避免第二天被昨天的债压死）。

### 8.3 与规则模式的关系

- `triggers.mode: model`（新默认）| `rules`（旧行为，回滚用）。
- `rules` 模式下 cooldown / sample_rate / keywords / 三振降档全部照旧；`model` 模式下这些字段不生效但保留。
- 静音时段、群白/黑名单、admins、`/闭嘴`、`/醒来` 两种模式都生效。

### 8.4 与投递记账的关系

- `delivered_messages`/`delivered_cursors` 继续用作「哪些消息已经被模型看过」的唯一账本。
- 批次模式下每轮把整批标 delivered=1；跳过（预算/静音）时保持 0，下一轮自然带上。
- 上一轮 A 的补投逻辑保留：它是账本的使用者，不是竞争者。

### 8.5 取舍说明

- 45s 是「像在看群」与「像在抢话」的折中；越快越像机器人。
- 8s 点名防抖：连发三条不会三连回。
- 90s 非点名间隔：群里刷屏时它不会每条都算一遍，成本可控。

## 9. 模块 F：会话轮换（P3）

### 9.1 阈值（任一触发）

| 条件 | 默认 | 理由 |
| --- | --- | --- |
| 本 session 已投递消息数 | 60 | 约 60 × 800 token ≈ 48k，留足余量 |
| session 存活天数 | 7 | 长期记忆已由文件承担，session 不必长寿 |
| 估算 token | 40,000 | 全局 compaction buffer 是 80k，先于它触发 |

估算口径：`messages` 里该会话自 session 创建以来的字符数 / 1.6 + 注入量；存 `brain_sessions.token_estimate`。

### 9.2 轮换流程

1. 到阈值 → 新建 session（agent/title 带版本号 `v2`）。
2. 首轮 prompt 追加【接续】段：`这是你在本会话的新 session。上次聊到：<最近摘要>；长期记忆见上文。`
3. `brain_sessions` 记新 session_id，旧 session 保留不删（留档，不再写）。
4. 轮换失败（建不出 session）→ 继续用旧 session 并 warning，下一轮再试。

### 9.3 与全局 auto-compaction 的关系

- 全局 `compaction.buffer=80000` 会先于我们的 40k 触发；正常情况下轮换先发生，compaction 不该介入。
- 若日志里出现 compaction 事件 → 说明估算偏低，调低 `rotation_tokens`；不在本期试图关闭全局 compaction（那是用户配置）。

## 10. 模块 G：用量记账（P2）

- 从 message info 读真实 token（spike S5 确认字段）：`completed` assistant 的 `tokens.input/output`（或等价字段）。
- 累加到 `usage`，`budget.daily_tokens` 从此真正生效。
- 字段不存在则保持 0，并在启动日志 warning 一次（不静默）。
- 工具多步只算一次调用（calls），但 token 照实累加。

## 11. 模块 H：E 溯源与修复（P4）

### 11.1 已知证据

- bot 专用 server 的 `/api/agent` 返回 **15 个 agent**，含 `Tamako`、`oceanus`、`sisyphus`、`prometheus`、`Compaction`、`Summary`、`Title`——全部来自用户全局配置，不是本仓库的。
- 污染消息文本 `契约还没闭合，继续做完…原始目标：清掉上一轮遗留的 7 个风险项` 是编排器/自动续跑风格，与本仓库 `deepseek-qq` 的 persona 无关。
- 结论：**bot server 不是隔离环境**，任何能连 24310 的客户端都可以用任意 agent 在 bot 的 session 里发消息。E 的最可能成因是「外部客户端 / 插件在 bot 的 session 上跑了非 QQ 链路」。

### 11.2 溯源步骤（实现阶段第一步，只读）

1. 只读打开 `data/opencode-bot.db`，查那条消息的 `sessionID` / 时间 / role / 前后各 3 条。
2. 对齐 supervisor 的 opencode 日志（`logs/`）：同时间窗内是否有非 bot 发出的 prompt 请求。
3. 检查是否有浏览器/客户端连过 24310（supervisor 日志或进程连接）。
4. 产出结论：外源写入 / bot 自身误发 / 库被复用。

### 11.3 修复候选（按根因选，但守卫先落地）

- **守卫（必做，成本低）**：bot 每次自己发 prompt 时记录返回的 message id 到 `brain_sessions.own_message_ids`；注入时间线前，把「role=user 且 id 不在白名单」的消息过滤掉，并 warning 记一条。只过滤 prompt，不动 DB。
- **减少暴露面**：dashboard 若给 OpenCode session 深链，改为只读提示（避免一键把 bot 会话开进 TUI）。
- **隔离实验（可选）**：验证能否用独立配置目录启动 bot 的 opencode（`OPENCODE_CONFIG` 或等价机制）而不丢 provider 凭据。能隔离就隔离；会丢凭据就不做，靠上面两层。
- **轮换顺带清理**：轮换后新 session 天然干净；旧的那条污染消息留档不删。

### 11.4 验收

- 重放：往 bot 的 session 外部注入一条消息 → 下一次群 prompt 里不出现它，日志有一条 `foreign message filtered`。
- 正常链路：bot 自己发的消息不会被误过滤。

## 12. 配置变更（`config.yaml`）

```yaml
opencode:
  agent: deepseek-qq
  timeout_seconds: 240          # 90 → 240

workspace:                      # 新增
  dir: data/bot-workspace       # 留空 = 关闭工作区与注入（软回滚）
  inject_chars: 900
  memory_chars: 500
  summary_chars: 250
  impression_chars: 150
  max_file_bytes: 65536

triggers:
  mode: model                   # 新增；rules = 旧行为
  batch_seconds: 45
  addressed_debounce_seconds: 8
  ambient_min_interval_seconds: 90
  # cooldown_seconds / sample_rate / keywords 保留，仅 rules 模式生效

budget:
  daily_calls: 200
  daily_tokens: 300000
  ambient_ratio: 0.8

memory:
  window_size: 20
  impression_topk: 3            # 5 → 3（省注入预算）
  extract_every: 20
  rotation_messages: 60         # 新增
  rotation_days: 7
  rotation_tokens: 40000
  inject_summaries: true
  inject_impressions: true
```

## 13. 失败模式与降级

| 场景 | 行为 |
| --- | --- |
| webfetch 超时/失败 | 已提示 → 补「查不到」；未提示 → 既有兜底 |
| 模型不支持工具（spike S1 失败） | 本设计不变，换模型（需用户拍板）；agent 权限保留 |
| memory agent 失败 | warning，不落库；连续 3 次当日熔断 |
| 临时 session 删不掉 | debug 日志，留档 |
| 工作区不可写 | 只注入不写，warning |
| 轮换建 session 失败 | 用旧 session，下轮再试 |
| 外部消息注入 | §11.3 守卫过滤 + warning |
| 预算耗尽 | 静默丢弃，debug；次日恢复 |
| opencode 全挂 | 既有 BrainError 兜底话术，不影响进程 |

## 14. 测试计划

### 14.1 单测（MockTransport，不依赖真 server）

1. `complete()`：建 session（带 agent）→ prompt → 取最后一条 assistant → DELETE。
2. `complete()`：DELETE 404 不影响返回值；轮询超时抛 BrainError。
3. `_ask()`：多步场景取最后一条 assistant，不取中间「我先查一下」。
4. `on_tool`：tool part 出现一次 → 回调一次；两轮各出现 → 各一次。
5. `build_injection`：三段拼接、各自截断、缺省省略、私聊无印象段。
6. 工作区读取：文件不存在、超长截断、尾部优先。
7. 投递调度：45s 窗口、8s 点名防抖、90s 非点名间隔、per-chat 串行。
8. 预算：非点名不越 80%、点名可用尾部、硬上限后静默。
9. 轮换：60 条触发、接续段存在、旧 session 保留。
10. 记账：从 message info 解析 token 并入库；无字段时 0 + warning 一次。
11. `/forget`：清 SQLite 与 session，保留 `MEMORY.md` 与 `chats/*.md`。
12. 守卫：非白名单 user 消息被过滤，白名单不被过滤。

### 14.2 实测 spike（写代码前必须过，判定规则写死）

| ID | 内容 | 通过 → | 失败 → |
| --- | --- | --- | --- |
| S1 | 放开工具后模型能否发起 `webfetch` | 按甲实施 | 换模型，请用户拍板 |
| S2 | V2 message part 中 tool 的字段与「本轮结束」判据 | 按 7.3 实现 | 改用 session 状态/事件流，改设计 |
| S3 | `DELETE /api/session/{id}` 是否可用 | 临时 session 用完即删 | 留档，每群固定 worker session |
| S4 | 权限 glob 对 `data/bot-workspace/**` 是否生效、越界是否被拒 | 按 4.2 | 补绝对路径规则；仍不行则把 workspace 移出仓库 |
| S5 | message info 是否带 tokens/cost | 接真实记账 | 保持 0 + warning，`daily_tokens` 暂不生效 |
| S6 | 全局插件是否真的作用到 bot server | 按 11.3 处理 | 记录，不额外处理 |
| S7 | `steps: 4` 下回复是否仍只输出一条 JSON | 继续 | 调整 persona/降 steps |

### 14.3 端到端验收（可判定）

1. `pytest -q` 全绿，新增用例覆盖 14.1 的 12 项。
2. 强制跑一次摘要与印象：`summaries` / `impressions` 行数 > 0，dashboard 可见。
3. 群里问一个需要联网的问题（给一个真实 URL）：10 秒内先出提示语，最终回复引用页面事实。
4. 3 次对话内 `MEMORY.md` 出现模型自写条目；下一轮 prompt 含该条目（看日志里的 prompt 前缀）。
5. `/forget` 后：该会话 SQLite 记忆为 0，`MEMORY.md` 与 `chats/*.md` 仍在。
6. 投递满 60 条后：新 session_id，首轮 prompt 含【接续】。
7. 静音时段内：无调用、无发送。
8. 权限实测：让它 read `.env` → 被拒；`shell` → 被拒；写 `data/bot-workspace/x.md` → 成功。

## 15. 上线与回滚

**顺序**：S1–S7 spike → 单测 → 实现 → 本地联调 → 重启 supervisor（opencode + bot）→ 群内验收。

**软回滚（改配置，重启 bot 即可）**

- `triggers.mode: rules` → 回到旧投递
- `workspace.dir: ""` → 不注入、不建工作区（工具权限仍在 agent 里）
- `memory.inject_*=false` → 不注入

**硬回滚（改 agent，重启 opencode）**

- `deepseek-qq.md` 恢复 `steps: 1` + 全 deny → 完全回到 `ae7645b` 行为
- 保留 `dafeiyu-memory` agent 不影响主链路（不用即无副作用）

## 16. 改动清单（供实现计划拆分）

| 文件 | 性质 |
| --- | --- |
| `.opencode/agents/deepseek-qq.md` | steps、model、权限、persona 增补 |
| `.opencode/agents/dafeiyu-memory.md` | 新增 |
| `src/dafeiyu/brain/opencode_http.py` | `complete*()`、`on_tool`、取最后一条、token 解析、session 轮换钩子 |
| `src/dafeiyu/brain/factory.py` | 传 workspace/agent 配置 |
| `src/dafeiyu/memory/context.py` | 新增 `build_injection` |
| `src/dafeiyu/memory/store.py` | `brain_sessions` 加 `token_estimate` / `own_message_ids`；查询辅助 |
| `src/dafeiyu/service.py` | prompt 组装、投递调度接入、提示语发送、失败补话 |
| `src/dafeiyu/router/*`（或新建 `delivery/`） | 批次窗口 + 防抖 + 节流 + 预算分层 |
| `src/dafeiyu/workers/*` | 不改逻辑，仅确保走新接口 |
| `src/dafeiyu/supervisor/main.py` | 无 env 变更（不开 websearch）；确认无新增 |
| `config.yaml` | §12 |
| `tests/` | §14.1 的 12 组 |

## 17. 待你确认 / 待实测

- **模型**：确认就是 `opencode/deepseek-v4.1-flash-free`（现在是被动继承）；我会在 agent 里钉住。若 S1 证明它不会调工具，需要你指定替代模型。
- **S2/S3/S5** 的实测结果可能反过来改一点实现细节（不影响架构）。

## 18. 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 免费模型工具调用不稳/限流 | 联网/记笔记时灵时不灵 | S1 先验证；失败则换模型 |
| `steps: 4` 让闲聊变慢/多话 | 群体验下降 | persona 明确「不需要就别动工具」；观察 p95 与 tool 调用率 |
| 批次投递被感知为「反应慢」 | 有人叫它时等 8s | 点名防抖压到 8s；必要时再调 |
| 注入预算被长期记忆吃满 | 摘要被挤掉 | 分段独立预算，不互相挤占 |
| 网页 prompt injection | 越权/乱写 | 无 shell + 目录隔离 + persona + 容量上限 |
| 全局插件继续污染 session | 群聊里出现奇怪内容 | §11.3 守卫 + 轮换 + 减少暴露面 |
| 轮换丢上下文 | 它「忘记」刚聊的 | 接续段带最近摘要 + 长期记忆；阈值 60 条不算激进 |
