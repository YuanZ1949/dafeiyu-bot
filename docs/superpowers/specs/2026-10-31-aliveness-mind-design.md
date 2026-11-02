# 大肥鱼「活人感」改造设计：心智层 · 记忆 · 接话思考链

- 日期：2026-10-31
- 状态：草案，待用户拍板
- 范围：人物画像（自己/群友/关系）、记忆结构、接话时机与思考链、表达与人味、反馈闭环
- 不在本轮：微调、向量库、多模态、微信接入

## 1. 一句话判断

**方向对：人物画像 + 模型判断是正路。但顺序要反过来——先让记忆能记住「话题线程」和「没说完的事」，再谈「人物画像」；而且画像不能是更长的 prompt，必须拆成能被事件更新的四件套：角色卡（静态）、角色状态（动态）、关系卡（每群每人）、立场/承诺（自我记忆）。**

现在真正卡住「接话」的不是人设不够细，而是模型每轮看到的「现场」太薄：

| 模型此刻拿到的 | 实际内容 | 问题 |
| --- | --- | --- |
| 人设 | `brain/persona.md` 或生成到 `.opencode/agents/deepseek-qq.md` 的 `brain/agent_template.md`，静态约 830 token | 没有「此刻的我」：情绪、精力、在意的梗 |
| 当前场景 | 只有日期和字数上限；群名写死「本群」（`service.py:1216`）——但默认 opencode 链路里这段根本没传给模型 | 她连群名都不知道；要改的是注入段，不是 `_system_prompt` |
| 长期记忆 | 摘要 ≤500 字（日+周）、群友印象 3 条共 150 字（`context.py:35`） | 只够当背景板，撑不起「接上一句」 |
| 长期笔记 | workspace 500 字；只有 opencode 有工具会写，token/local 模式无人维护 | 本地/Key 模式下这段是死重；写什么也没约束 |
| 相关旧事 | trigram FTS 召回 3 条（模板开、代码默认关），排序是「命中词项数 > 时间」而非 bm25（`context.py:261`） | 关键词撞运气，没有时间/人物/重要度权重 |
| 近期上下文 | 未看的 ≤20 条时间线 + 近 20 条历史；**群友之间「谁回谁」不渲染**（`timeline.py:33` 只标 @你/引用你） | 「刚才说到哪」滚出窗口就断；回复关系也一起丢 |
| 自己说过什么 | 只作为普通 assistant 轮次留在窗口里 | 没有「我上次的立场/承诺」，跨 session 就没了 |
| 话题状态 | 已有跨重启的冷却/收尾状态机（`chat_state` + `router` 的 ambient/thread 超时），但没有话题内容 | 知道「收尾了」，不知道「在聊什么、缺什么」 |
| 说不说 | **model 模式没有随机采样**：每条非静音消息都排批，12 秒窗口一到就调一次主模型（`service._group_skip_reason`）；选择性完全由模型在单次生成里决定 | 该沉默时也可能开口；免费额度/成本不可控 |

结论：**她不是不会想，是没材料、没位置、没反馈；而且「什么时候说」现在不是判断出来的，是批次到点就送审。**

## 2. 设计原则：活人感 = 连续性 + 选择性 + 可回收性 + 反馈

1. **连续性**：上一轮的情绪、在意的事、答应的事会带到下一轮；跨小时、跨天、跨重启都还在。
2. **选择性**：多数时候不说话；说话前会判断「这段讨论缺什么、我有没有真看法」——既不是掷骰子，也不是每批定时送审。
3. **可回收性**：会主动提起「你上次说的那个」「我之前说过 X 难用」；梗、外号、旧账能被自然复用。
4. **反馈**：说完会看有没有人接；有人接就继续，没人接就换话题或安静，而不是固定概率刷存在感。
5. **留白**：画像写「价值观和习惯」，不写满；状态缓慢变化，避免每轮随机导致人设漂移。

反面清单（明确不做）：

- 不往 prompt 里堆「你要像一个活人」这类元指令，只会让模型更用力地表演。
- 不做「每轮随机抽一个心情」——那是精分，不是鲜活。
- 不编造与事实冲突的外部经历；「生活线」只从真实活动（聊过什么、查过什么、被谁叫过）生成。

## 3. 总体架构

```
                 ┌──────────────── 异步旁路（便宜模型 / 本地模型）────────────────┐
                 │ 观察者 Observer：每 N 分钟或每 K 条消息，读未看消息 → 更新话题簿  │
                 │ 自省 Reflector：每天固定时段，读当天话题/发言/结果 → 更新状态与经验 │
                 └───────────────┬──────────────────────────────────────────────┘
                                 ▼
 消息 → 路由/批次 ──► 心智层装配 Mind ──► 主模型一次调用（读场→定意→出口）──► 表达层
            │              │                                        │
            │              ├ 此刻（时间/群/话题线程/在场人）          ├ 分片/延迟
            │              ├ 关系卡（当前说话人/熟人）               ├ 引用/表情回应
            │              ├ 自我（状态/立场/承诺/生活线）           ├ 防重复/禁句
            │              └ 旧事召回（FTS + 权重）                  └ 结果打标
            ▼
       SQLite：episodes / open_loops / bot_state / reply_outcomes /（沿用 impressions、summaries）
```

三个新增件：**话题簿**（episodes + open_loops，解决「接什么」）、**心智卡**（state + 关系，解决「谁在说、我是谁」）、**机会检测**（解决「什么时候说」）。
主链路仍然只有一次模型调用；所有新增计算都在旁路，且按会话限流。

## 4. 心智层：把「人物画像」拆成四件套

### 4.1 角色卡（静态，人设层，不动）

人设有两份源文件，必须同步改，别再手改生成物：

| 文件 | 谁在用 | 说明 |
| --- | --- | --- |
| `src/dafeiyu/brain/persona.md` | token / local_model | 经 `build_system_prompt` 进 system |
| `src/dafeiyu/brain/agent_template.md` | opencode（默认档） | `provider_config.build_agent` 生成 `.opencode/agents/deepseek-qq.md`；**不要手改生成物**，下次生成会覆盖 |

保留这两份作为「她是谁」。只补两处：

1. 把「说话习惯」写成可执行的负向约束（禁句、长度、句式变化），供 7.3 的 post-filter 使用，同时留在 prompt 里。
2. 把「当前场景」（群名/时间/状态）从静态文件里挪到每轮注入；`persona.py` 的 `SYSTEM_TEMPLATE` 只保留「身份 + 说话方式 + 底线」。

> 关键：默认 `brain.provider=opencode` + `transport=http` 时，`opencode_http.reply` 只接收一段 prompt，`service._call_brain` 不会传 system（`accepts_history` 为假），**`_system_prompt` 在这条链路上是死代码**。群名/时间/字数上限现在一个都没进模型。所有动态内容必须放进 `build_injection` 的注入段，否则默认部署完全无效。
>
> 输出协议同样有两处：`brain/client.py` 的 `OUTPUT_INSTRUCTION`（token/local）与 `agent_template.md` 的「输出协议」（opencode）。改 read/think 必须两处一起改。


### 4.2 角色状态（动态，单行，缓慢变化）

新表 `bot_state`（单行，跨群共享；同一时刻只有一个「本鱼」）。**刻意只保留两个字段**，先验证「状态能被感知」再谈多维情绪：

```sql
CREATE TABLE IF NOT EXISTS bot_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  focus TEXT NOT NULL DEFAULT '',        -- 当前在意的事，一句话（Reflector 日更）
  mood_note TEXT NOT NULL DEFAULT '',    -- 一句人话，只由规则在「被夸/被怼/被冷落」时改写
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

- 不做连续数值衰减、不做每轮随机：那是「精分」的高发区；宁可少，不要飘。
- 更新来源：规则命中（被夸/被怼/被冷落/被点名多次）+ 每晚 Reflector 覆盖一次。
- 注入写法不写数字，写人话：「今天有点懒；刚被夸了一句，尾巴翘着」。
- 同一句话连续出现两轮就抑制，避免变成固定口癖。


### 4.3 关系卡（每群每人）

不新建大表，先用**聚合视图**（P1 零迁移）：

- 资料来源：`impressions`（nickname / preference / fact）+ `messages`（近 30 天发言数、最后互动时间、是否被 bot 回复过）+ `reply_outcomes`。
- 注入卡片（当前说话人 + 最近 3 个发言人）：

```
【阿猫】外号鱼片；养猫、在杭州；最近 7 天聊过 23 句；你上次回他是「…」；熟。
```

- 规则：只有当前批次涉及的人进卡；生人只给昵称，不臆测关系。
- 先用聚合扛住 P1；`user_profiles` 独立表只在聚合查询明显不够时才建（避免与 impressions 重复）。

### 4.4 立场/承诺（自我记忆）

- `open_loops` 里的 `kind=promise` 是必须现在就做的（「本鱼去查查」）。
- 观点/立场（`bot_stances`）**先不建表**：它与 workspace 笔记、impressions 高度重叠，先让主模型在 `read/think` 里显式写「我的观点」，只留在日志；确认有价值后再落表。
- 若落表：`topic / stance / confidence / source_message_id / updated_at`，注入只取与当前话题相关的 1–3 条、不超过 120 字，作用是**前后一致**，不是秀记忆。

## 5. 记忆重构：从「消息/摘要」到「线程/线索/自我」

### 5.1 话题簿（episodes）——接话的锚点

```sql
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_type TEXT NOT NULL,
  chat_id INTEGER NOT NULL,
  start_message_id TEXT NOT NULL,
  last_message_id TEXT NOT NULL,
  topic TEXT NOT NULL,               -- 一句话：在聊什么
  summary TEXT NOT NULL,             -- 2-3 句：聊到哪、结论、分歧
  participants TEXT NOT NULL,        -- JSON: [{user_id,nickname}]
  phase TEXT NOT NULL DEFAULT 'discussion', -- opening|discussion|climax|winding|closed
  mood TEXT NOT NULL DEFAULT '',     -- 现场气氛
  importance REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'open', -- open|closed|stale
  started_at INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_episodes_chat ON episodes (chat_type, chat_id, status, last_message_id);
```

- 由 Observer 每 10 分钟 / 每 8–20 条新消息增量更新；话题切换时关旧开新。
- 注入只给**当前 open 的 1 条**（200 字内）+ 上一轮已收尾的 1 条（可选），不是流水账。
- 会话轮换/重启后它仍在，比 200 字交接摘要更稳。
- `phase` 不另起状态机：直接映射 `chat_state` 已有的 `ambient_streak` / `last_bot_spoke` / `thread_closed_until`（`router.py:305`），避免两套「话题状态」打架。`started_at/updated_at` 与 `messages.timestamp` 统一用 INTEGER epoch 秒，别混 TEXT。


### 5.2 未了事（open_loops）——最像人的记忆

```sql
CREATE TABLE IF NOT EXISTS open_loops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_type TEXT NOT NULL,
  chat_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                -- question|promise|plan|running_joke
  subject_user_id INTEGER,
  content TEXT NOT NULL,             -- 一句话：谁问的什么 / 答应了什么
  status TEXT NOT NULL DEFAULT 'open', -- open|done|dropped
  source_message_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  closed_at TEXT
);
```

注入示例：

```
【还没了的事】
- 阿猫 20 分钟前问的驱动报错没人接（可以答）
- 你昨晚说帮鱼片看下那段配置（该跟进了）
- 群里在等的梗：「本鱼翻肚皮」还没人接
```

这是「接话」最直接的燃料，也是现在完全缺失的一层。

### 5.3 自我记忆与清理

- 只把「值得记的自己的话」结构化：观点（stances）、承诺（loops）、外号/梗来源。
- 普通发言仍留在 `messages`，不重复存。
- 会话级清理别再逐表手写。在 `store` 里定义一份「会话派生表清单」常量，`forget_chat` / `forget_all` / `forget_messages` / `prune_messages` / 面板按用户删 / 导出 / 备份校验全部复用它。接入新表时只改这一处。
- **顺手修一个现有漏洞**：`forget_chat` / `forget_all` 现在漏了 `weekly_summaries` 与 `impression_cursors`（`store.py:2471`），README 却宣称摘要全清；补上并加测试。
- 新表直接写进 `_SCHEMA`（全是 `CREATE TABLE IF NOT EXISTS`，`init_schema` 每次启动幂等执行）；只有给已有表加列才需要 `_MIGRATIONS`。**不要**把新表加进备份的 `REQUIRED_TABLES`（旧备份回灌会全部失败），只在 manifest 记 `schema_version`。


### 5.4 召回打分（替代现在的「关键词撞运气」）

`score = 0.45*相关性 + 0.25*新鲜度 + 0.20*重要度 + 0.10*关系权重`

- 相关性：现有 FTS5 trigram；**先改造 `_recall_lines`**——它现在按「命中词项数 > 时间」排序（`context.py:261`），拿不到 bm25 分数，要换用 `store.search_message_terms` 返回的 bm25 或自行加权。
- 新鲜度：指数衰减（7 天半衰期）。
- 重要度：episode.importance、是否含 open loop、是否含当前说话人。
- 关系权重：当前批次发言人权重高于路人。
- 召回对象从「消息」升级为「episode 摘要 + 少量原消息」，注入更像「记忆」而不是「搜索记录」。
- 注意 `recall_enabled` 代码默认 False、`config.yaml` 模板显式开启；文档里别把「默认开」当事实。


### 5.5 注入顺序（注意力模型）

按「活人此刻会先想到什么」排序，而不是按数据库表排序：

```
1. 【此刻】时间、群名、话题线程、在场的人、你的状态          （约 150–250 字）
2. 【还没了的事】1–3 条                                     （约 100–200 字）
3. 【你和他】当前说话人/熟人的关系卡 + 上次互动               （约 100–150 字）
4. 【你自己的想法】相关立场/承诺 1–3 条                      （约 100 字）
5. 【旧事】召回 top-k                                       （沿用，≤300 字）
6. 【摘要】日/周摘要（降到背景板）                           （≤500 字）
7. 时间线 + 接续（沿用）
```

总预算：mind 段 ≤600 字，摘要段可压到 400 字，workspace 500 字按需减半；新增内容不与现有段抢预算，由 `mind_chars` 单独配置。超预算时先砍摘要与 workspace，再砍旧事，**此刻/未了事/关系卡最后砍**。

接入注意：

- 现有断言顺序是「长期记忆 < 昨日摘要 < 群友印象」（`tests/test_injection.py:30`，另有 `test_e2e_batch.py:204`、`test_run_batch.py:400` 同类断言）。把「关系卡」提到摘要之前会打破这三处——要么接受并同步改测试，要么把关系卡放到摘要之后（收益差很小）。
- 新增一段要同步三处：`InjectionConfig`（`context.py:41`）→ `config._build_injection`（`config.py:624`）→ `config.yaml` 的 `workspace` 段。新增 `mind:` 段必须用 `raw.get("mind")` 可选读取；`_section()` 缺段会直接 `ConfigError`（`config.py:360`），老配置会起不来。


## 6. 思考链：把「想」从生成里拆出来

### 6.1 为什么现在不像在「想」

`OUTPUT_INSTRUCTION` 要求模型在**一次生成里**同时完成：读现场 → 判断该不该说 → 找记忆 → 组织人设语言。认知负荷太高，结果就是两种退化：要么套话（复述最后一条），要么过度保守（speak=false）。

### 6.2 三档方案与推荐

| 方案 | 做法 | 成本 | 适用 |
| --- | --- | --- | --- |
| A 单调用内结构化 | 输出 `read/think/say` 三段，强制先读后说 | 低（同一次调用，多几十 token） | 立刻可做 |
| B 观察者 + 接话者（推荐） | 便宜模型持续维护话题/机会；主模型只在「被点名」或「机会分高」时被唤醒 | 中（旁路本地模型，主链路不变） | 本项目的正解 |
| C 草稿→自我审查 | 先出一稿，再让模型按人设/事实/重复审查一次 | 高（2 次调用、延迟翻倍） | 只用于技术/敏感回答 |

推荐 **A 打底 + B 作为主架构 + C 只对高危内容开关**。

#### 方案 A：输出协议 v2（兼容旧格式）

```json
{
  "read": "在聊显卡驱动，阿猫在问报错，没人接。像是真求助，不是玩梗。",
  "think": "这个我能答；他上次就是驱动装脏了。可以接，别卖萌过头。",
  "speak": true,
  "reply": "...",
  "recall": ["上次他重装驱动那次"],
  "promise": ""
}
```

- `read/think` 各一句话，上限 40 字；`recall` 只在真用到旧事时填（用于回看是否编造）。
- 解析失败时仍走现有 salvage（`brain/client.py:87`），旧格式 `{speak, reply, reason}` 继续兼容。
- `reason` 升级为「现场判断 + 缺口 + 为什么值得说/为什么不说」，在面板/日志里能看到「她怎么想的」。
- 关键 few-shot：给 3 条「read 显示没什么可说 → speak=false」的例子，比十条「要像活人」有用。

#### 方案 B：观察者 + 机会检测（核心）

先纠正一个前提：默认 `triggers.mode=model` 下**没有随机采样**。`_group_skip_reason`（`service.py:316`）只挡静音/夜间/收尾/降频/黑白名单；每条非静音消息都会排批，12 秒窗口一到就调一次主模型。所以这里要解决的不是「采样命中率」，而是：

1. **省调用**：纯热聊/纯斗图的批次，不值得每 12 秒问一次主模型；
2. **给模型一个外部信号**：批次到点 ≠ 该你说话；外面得有人告诉它「此刻有没有值得接的线头」。

**职责分层**

- Observer（每群，每 10 分钟或每 8–20 条新消息；**必须用本地/独立 key 的模型**）：只做「看」和「记」，不生成回复。
- Moment 规则：把 Observer 输出与规则信号合成「机会分」，决定这一批是「照常送审」「带提示送审」还是「先攒着」。
- Responder（主模型）：拿「此刻 + 话题 + 机会提示 + 关系」回一句；说不说仍由它最终决定。

**机会信号**

| 信号 | 规则 | 分值 | 何时可用 |
| --- | --- | --- | --- |
| 点名/引用/叫名字 | 现有硬触发 | 必回 | P0 |
| 提到自己/自己的梗 | 别名、外号、「鱼」 | 高 | P0 |
| 问句/求助词 | 现有 `_is_question` | 高 | P0 |
| 话题命中旧事/立场 | FTS/立场交集 | 中 | P1 |
| 无人接的问句 | 问号 + 60s 内无人回复 | 高 | P1（Observer 回看，flush 时拿不到） |
| 有人 @ 了在场熟人却没得到回应 | 关系卡 | 中 | P1 |
| 争议/分歧 | 两个发言人连续对立词 | 中 | P2 |
| 冷场后的自嘲/表情 | 消息量骤降 + 表情 | 低 | P2 |
| 纯热聊/纯斗图 | 60s ≥5 条且无问句/无点名 | 负分 | P0 |

**机会分怎么用**

- `addressed`：照旧必回。
- `score ≥ 0.7`：正常调用，并注入一句【机会】（「有人问了个没人接的问题，你可以答」）。
- `score ≤ 0.2` 且无点名：**不调用，也不 `mark_skipped`**——本批消息留在待投队列，下次有人说话时一起进 prompt（积压超过 `pending_batch` 的 20 条时按现有逻辑截断并提示）。这既省钱又不断线。
- 其余：正常调用，不给机会提示。
- rules 模式完全不动：它本来就靠 `sample_rate`，与本方案无关。
- 每天上限：`mind.observer_daily_calls`（单群），与回复预算分开计。

> 为什么不用 `mark_skipped`：它是终局，会写 `delivered_messages` 并推进游标（`store.py:1174`），被吞掉的句子以后永远不会进 prompt——接话链会更断。攒着不投才是「暂时不说」。
>
> 为什么 60s 回看不在 P0：batch flush 约 12 秒后就会决定，此时拿不到「60 秒没人回」这个事实；它只能由 Observer 在下一轮补记，属于 P1。


**Observer 输出（JSON，严格）**

```json
{
  "topic": "显卡驱动报错",
  "phase": "discussion",
  "summary": "阿猫装驱动黑屏，鱼片让他先卸干净；还在等结果。",
  "participants": [{"user_id": 1, "nickname": "阿猫"}],
  "open_loops": [
    {"kind": "question", "user_id": 1, "content": "DDU 之后还是黑屏怎么办"}
  ],
  "moments": [
    {"message_id": "123", "type": "unanswered_question", "score": 0.8, "why": "问了两次没人答"}
  ],
  "mood": "有点急",
  "note": "别刷梗，先解决问题"
}
```

- 所有字段进 DB 前做长度/枚举校验；模型输出当作**不可信资料**，注入时带边界行（沿用 `INJECTION_BOUNDARY`）。
- Observer 失败只记日志 + 退避，不影响主链路（复用 `FailureBackoff`）。

### 6.3 延迟与成本预算

| 环节 | 频率 | 模型 | 预估 |
| --- | --- | --- | --- |
| Responder | 现有批次 | 主模型 | 不变 |
| Observer | 每群 10 分钟 / 20 条 | 本地/独立 key | 单群每天 ≤ 24 次小调用 |
| Reflector | 每群每天 1 次（05:00） | 本地/独立 key | 单群每天 1 次 |
| Outcome | 纯 SQL | 无 | 零成本 |

**真正的瓶颈不是钱，是账号级配额**：Observer 与主链路、摘要/印象共用同一个账号（默认 opencode free）。额度被打满后，主链路先开始 429，记忆旁路又会被全局熔断停一小时（`workers/failure.py`：10 分钟内 3 次 429 或 provider 报 quota → 全局暂停）。所以：

- Observer 默认用本地模型；没有本地模型时宁可不开。
- 单群额度小（24 次/天）+ 全局并发 1 + 独立 `FailureBackoff`，不与记忆旁路共用熔断。
- 主链路延迟不增加；被唤醒时多出的只是注入字数（几百 token）。


## 7. 表达层：同样的内容，怎么说才像在打字

### 7.1 轻接话：先有反应，再有文字

- **引用回复（P0，推荐先做）**：回答具体问题时带 `reply` 段（引用对方那句），群聊里极像真人。`send` 目前只接受纯文本（`gateway/qq.py:326`），要**新增** `send_segments`，不能改 `send` 的参数结构（`tests/test_qq_adapter.py:65` 精确断言 `{"group_id":1,"message":"hi"}`）。
- **表情回应（P1，单独开关）**：别人讲了个笑话/被 @ 但没什么可说时，点个 😏/😋 比硬回一句自然得多。注意用 QQ 表情 id 而不是 unicode，且需要目标 message_id；`set_msg_emoji_like` 是 NapCat 扩展，失败静默降级，**不要** `record_usage("error")`（会喂给 alerts 的 reply_errors 告警）。
- 引用/表情都依赖「刚回复的是哪条」：`send` 返回真实 message_id 后已由 `_store_bot_chunks` 入库，引用时从库里取目标 id 即可。
- 不做「先发短反应、1 秒后再补正文」：两次发送 + 定时器，性价比最低。


### 7.2 节奏

- 首条延迟 = 读消息时间（字数/20 字每秒）+ 思考 1–3s + 抖动，上限 6s；被 @ 时取小值（人在等）。
- 分片间隔沿用 `segment_delay_seconds`（默认 0.8s），可按分片长度微调。
- 深夜（00:00–07:00）允许更慢、更短、更多「困」。

### 7.3 防重复 / 禁句（确定性后处理，不靠模型自觉）

- 落点：两个发送口都要过——批次 `_send_batch_reply`（`service.py:935`）与单条 `_reply`（`service.py:1311`），在 `shape_reply`/`segment_reply` 之后、`adapter.send` 之前。`sender.py` 保持纯整形，新增 `brain/guard.py`。
- 取「最近 50 条自己的发言」目前没有现成方法（`recent_messages` 是混合窗口），要新增 bot-only 查询（`is_bot=1`）。
- 重复判定：归一化（去标点/空白）后算 3-gram Jaccard，`>0.6` 视为重复。
- **命中后的动作只能用确定性改写，不能重调模型**：否则违反「主链路一次调用」并多一档延迟。优先级：去掉重复开头/截短 → 换成中性短句 → 干脆不说（本轮 speak=false）。
- 禁句正则（配置化）：`作为一个?AI|很高兴(为|能)你|希望能帮到你|总结一下|首先.*其次|综上所述|笑死|确实|好好好|懂的都懂`。命中即降级。
- 内心 OS 频率统计：最近 10 条里已出现 ≥1 次时，下一轮抑制（不管配置概率）。
- 每群「最近用过的开场 3 词」集合，作为 prompt 的负例（「别再用这些开头」）。


### 7.4 「不完美」清单（P2 可选，宁缺毋滥）

- 只用 few-shot 引导：偶尔省略主语、只回半句、被夸时先嘴硬再承认；不做错别字/结巴/无关 emoji（掉可读性，收益低）。
- 不额外发送策略、不加定时器；所有花活可一键关。


## 8. 反馈闭环：让她知道「刚才那句有没有人接」

### 8.1 结果打标（纯 SQL，零模型）

新表 `reply_outcomes`：

```sql
CREATE TABLE IF NOT EXISTS reply_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_type TEXT NOT NULL,
  chat_id INTEGER NOT NULL,
  bot_message_id TEXT NOT NULL,
  spoken_at INTEGER NOT NULL,
  window_seconds INTEGER NOT NULL DEFAULT 180,
  engaged INTEGER NOT NULL DEFAULT 0,        -- 有人接（回复/引用/继续话题）
  quoted INTEGER NOT NULL DEFAULT 0,         -- 被引用
  replied INTEGER NOT NULL DEFAULT 0,        -- 被直接回复
  silence_after INTEGER NOT NULL DEFAULT 0,  -- 之后无人说话
  detail TEXT NOT NULL DEFAULT '{}',
  UNIQUE (chat_type, chat_id, bot_message_id)
);
```

每天 05:00 的 Reflector 从 DB 里回算昨天的结果（不依赖进程内定时器，重启不丢）：

- 被直接回复/引用 → `engaged=1`；
- 之后 3 分钟内同一话题继续 → 弱参与；
- 之后 3 分钟无人说话 → `silence_after=1`。

### 8.2 夜间自省（Reflector）

输入：当天 episodes、自己的发言、reply_outcomes、被点名次数、群友互动。
输出（JSON，严格）：

```json
{
  "focus": "显卡驱动那个坑",
  "mood_note": "今天被人夸了两次，有点飘",
  "relationships": [{"user_id": 1, "delta": 0.05, "note": "今天聊得投机"}],
  "lessons": ["技术求助时别先卖萌", "这个群晚上十点后更活跃"],
  "life": ["今天被叫了 7 次大肥鱼"]
}
```

- `lessons` 进 `bot_lessons`，单群每天最多 3 条、注入最近 7 天 ≤3 条；它与 workspace 笔记有重叠，**先复用 workspace 写入，不另建独立体系**。
- `relationships` 先只更新 `impressions` 的 `last_seen_at/confidence`，不建 `user_profiles` 大表；等关系卡聚合不够用再升级。
- `life` 只从真实事件生成（被谁叫、聊了什么、查了什么网页），不编造外部剧情。
- 失败只记日志；单群每天最多 1 次，纳入 `mind.observer_daily_calls` 的同一额度池。


### 8.3 评测：别靠体感调 prompt

1. **离线回放**：从库中随机抽 100 个历史批次（有上下文、有点名/非点名），用本地模型分别跑旧/新注入，评判：是否该说、是否接住了话题、是否引用旧事、是否空话、长度。
2. **人工黄金集**：30–50 条手标「该说/不该说/正确答案要点」，重点覆盖：问句、冷场、梗、多人讨论、技术求助、被怼。
3. **确定性单测**：mock 大脑断言注入顺序、预算裁剪、防重复、open loop 注入、结果计算（不联网）。
4. **线上指标**：被接话率、被引用率、连续被无视次数、重复句率、平均首条延迟、speak=false 比例、每群日调用数。

## 9. 落地切片

### P0（1–2 天，不改表，先止血）

0. **先跑基线**：`pytest -q`；注意 `tests/test_config_agents.py:178` 期望的 agent model 与当前 `.opencode/agents/deepseek-qq.md` 不一致（生成物随运行档位变化），说明现有测试可能已漂移，不要把「全绿」当默认。
1. **时间线补「谁回谁」**（最高杠杆、零新表、零调用）：`timeline.format_message_line` 用 `store.message_nicknames` 批量取昵称，渲染「（回复 阿猫）」；`models.to_prompt_line` 同步补。群友互回的线索现在完全丢失，这是「接不住话」的直接原因之一。
2. **「此刻」注入到 `build_injection`**：真实群名（`gateway/qq.py` 新增通用 `call_action("get_group_info")` + 缓存）、时间/深夜感、距上次发言、上一句是谁说的。**不要改 `_system_prompt` 交差**——默认 opencode 链路根本不传它。
3. **输出协议加 `read/think`**：`brain/client.py` 的 `OUTPUT_INSTRUCTION` + `brain/agent_template.md` 两处同步；加「没什么可说」few-shot；旧 `{speak, reply, reason}` 继续兼容。
4. **防重复 + 禁句 post-filter**：新增 `brain/guard.py`，两个发送口都过；确定性改写/放弃，不重调模型。
5. **机会检测规则版**：在 `_run_batch` flush 时算（点名/别名/问句/冷场/纯热聊）；低分且无点名 → **攒着不投、不 `mark_skipped`**；rules 模式不动。
6. **引用回复**（可选先做，需实测 NapCat 版本）：新增 `send_segments`，纯文本 `send` 字节不变。

验收：同一批历史日志回放，空话率/重复句下降；问句与点名更可能被接；纯热聊批次的模型调用数下降；`speak=false` 的原因可见。

### P1（3–5 天，记忆层）

1. `episodes` / `open_loops` 表 + store 读写；抽「会话派生表清单」常量，顺手补 `weekly_summaries` / `impression_cursors` 的 forget 漏洞。
2. Observer worker（本地模型、单群 ≤24 次/天、独立 `FailureBackoff`、全局并发 1）+ 注入【还没了的事】+ 机会语义信号。
3. `bot_state`（`focus` + `mood_note`）+ 规则更新 + 每晚 Reflector 覆盖。
4. 关系卡（impressions + messages 聚合，不建新表）+ 注入【你和他】（注意 `test_injection.py:30` 等三处顺序断言）。
5. 注入重排与预算（此刻 > 未了事 > 关系 > 自我 > 旧事 > 摘要），面板可查看。
6. `reply_outcomes` + 每晚结果回算（纯 SQL，不依赖进程内定时器）。

### P2（1–2 周，思考与闭环）

1. 双阶段唤醒：机会分驱动的「观察者 → 接话者」；主链路不变。
2. 表达契约 v2：`send_segments`（引用/表情/多条）、延迟策略。
3. 离线评测脚本 + 黄金集 + 面板「思考回放」。
4. `bot_lessons` 注入；`bot_stances` / `user_profiles` 只有在证据不足时才建独立表（先复用 impressions + workspace）。


### 9.4 改动面与测试影响（预估）

| 改动 | 主要文件 | 注意 |
| --- | --- | --- |
| 时间线补「谁回谁」 | `memory/timeline.py`、`models.py`；`store.message_nicknames` 已有，可复用 | 批量查一次，别逐条；私聊也要带上 |
| 群名/场景注入 | `gateway/qq.py`（新增通用 `call_action`）、`memory/context.build_injection`、`store.chat_state` 可选补列 | 网关无响应回落「本群」，不能阻塞消息处理；`_system_prompt` 只服务 token/local |
| read/think 输出 | `brain/client.py`、`brain/agent_template.md`（生成 `.opencode/agents/deepseek-qq.md`） | 两处必须同步；`tests/test_config_agents.py` 检查两份人设的共享标记；别手改生成物 |
| 防重复/禁句 | 新增 `brain/guard.py`，在 `_send_batch_reply` / `_reply` 里 `shape_reply` 之后调用 | 需要新增 bot-only 查询；命中后只做确定性改写，不重调模型 |
| 机会检测（规则版） | `service._run_batch` / `_group_skip_reason` | 低分「攒着不投」、不 `mark_skipped`；不要碰 rules 模式的 `sample_rate`；不能绕过预算 |
| episodes/open_loops | `memory/store.py`、新 `workers/observer.py`、`memory/context.build_injection` | 新表写 `_SCHEMA` 即可；抽清理清单常量；**不要**加进 `backup.REQUIRED_TABLES` |
| 关系卡 | `memory/context.py` 聚合查询（impressions + messages） | 一次查询批量取；会打破 3 处注入顺序断言，改测试要写明 |
| 结果打标 | 新 `workers/outcome.py` + 表 | 纯 SQL 回算，不依赖进程内定时器 |
| 引用/表情 | `gateway/qq.py` 新增 `send_segments`；`gateway/base.py` + `wechat` 桩同步 | 纯文本 `send` 参数不能变（`test_qq_adapter.py:65`）；失败静默降级，不记 `usage=error` |
| 新配置段 | `config.py`、`config.yaml` | `mind:` 必须可选读取；`InjectionConfig` / `_build_injection` / yaml 三处同步 |

### P3（可选，先不排期）

- 跨群共享的 `bot_stances`、群氛围识别、语音/图片反应、自动调节机会阈值（不碰 rules 采样）。

## 10. 配置项（草案）

```yaml
mind:
  enabled: false                 # 默认关；灰度打开
  state_enabled: true
  episodes_enabled: true
  observer_enabled: false        # 需要本地模型才开
  observer_provider: local       # local | main（main 会吃免费额度，慎用）
  observer_interval_seconds: 600
  observer_min_messages: 8
  observer_daily_calls: 24       # 单群每天观察/自省总调用上限，与 reply 预算分开
  mind_chars: 600
  episode_chars: 220
  relation_chars: 160
  reflection_hour: 5
  quote_reply: true
  emoji_reaction: false          # P1 再开
  anti_repeat: true
  banned_phrases: [作为一个AI, 希望能帮到你, 笑死, 确实, 好好好]
```

所有开关默认 false 或保守值，灰度打开；关闭即回到当前行为（软回滚）。`mind:` 段用 `raw.get("mind") or {}` 读取，缺段不能报错。

## 11. 风险与取舍

| 风险 | 表现 | 缓解 |
| --- | --- | --- |
| prompt 变长、注意力稀释 | 回复更慢更空 | 分段预算、优先砍摘要/workspace、A/B 回放验证 |
| Observer 幻觉 | 编造「未了事」 | 置信度 + source_message_id；未确认的 loop 24h 过期；注入带边界行（`INJECTION_BOUNDARY`） |
| 旁路吃光免费额度 | 主链路开始 429，记忆旁路被全局熔断停一小时 | Observer 只用本地模型、单群 ≤24 次/天、独立 FailureBackoff、全局并发 1 |
| 「攒着不投」丢消息 | 消息一直不进 prompt | 只允许低分且无点名时延后，不 `mark_skipped`；积压超 20 条按现有截断提示；观察者恢复后自动补 |
| 生成物被手改 | `.opencode/agents/*.md` 下次生成被覆盖 | 只改 `brain/agent_template.md` / `dafeiyu-memory` 模板；生成物视为构建产物 |
| 现有测试已漂移 | 改完才发现「本来就红」 | P0 先跑基线；`test_config_agents.py:178` 的 model 期望与当前生成文件不一致，先确认真实缺口 |
| 状态漂移/精分 | 今天高冷明天舔狗 | 只保留 `focus + mood_note`，只由规则/日更改写，不做随机与连续衰减 |
| 过度设计/油腻 | 更不自然 | 少即是多：先 P0 验证再接记忆层；所有花活可一键关；`bot_stances/user_profiles` 暂不建表 |
| 隐私 | 派生记忆含敏感信息 | 派生表与原始数据同生命周期；清理清单常量覆盖 forget/export/prune；面板可审阅/删除 |
| 性能 | 注入/查询拖慢批次 | 注入一次性查询、全部走索引；Observer 异步不阻塞；同会话串行 |

## 12. 明确不做

- 不做向量库/embedding 检索（SQLite FTS + episode 结构化足够）。
- 不做微调、不做每轮多模型投票。
- 不做「假装有真实生活」的编造外部事件；生活线只来自真实活动。
- 不做无开关的默认开启；每个新链路都能单独回滚。
- 不把用户画像写成「性格诊断」；只记可复用的称呼、偏好、雷点、共同话题。

## 13. 待用户拍板

1. 优先级：P0（时间线回复关系 + 此刻注入 + read/think + 防重复 + 机会检测）先做，还是直接上 P1 记忆层（episodes/open_loops）？
   - 建议：先做 P0 的 1/2/4（半天内能验证），再决定要不要为 episodes 投 3–5 天。
2. 观察者用哪个模型：本地模型（免费但慢）/ 独立 key（不挤免费额度）/ 复用主模型（最快但吃同一账号配额）？
3. 「生活线」是否保留：建议只做真实活动派生，不编造外部剧情（接受「没有虚构人生」）。
4. 引用回复/表情回应是否允许：需要先确认 NapCat 版本支持 `reply` 段与 `set_msg_emoji_like`，且接受 QQ 侧行为变化。

## 14. 评审修正记录（2026-10-31）

初稿经独立评审后修正，保留在此以便追溯：

1. **默认 model 模式没有 5% 采样**：`_group_skip_reason` 不查 `sample_rate`；批次模式是「每条非静音消息都排批、每 12 秒调一次主模型」。机会检测的目标改成「省调用 + 给模型外部信号」，不再乘采样率。
2. **`_system_prompt` 在默认 opencode 链路是死代码**：`opencode_http.reply` 不接收 system；「此刻」必须进 `build_injection` 注入段。
3. **人设源头是 `agent_template.md`**，`.opencode/agents/deepseek-qq.md` 是生成物；输出协议要同时改 `client.py` 与 `agent_template.md`。
4. **`mark_skipped` 是终局**：低机会批次不能靠它跳过，否则消息永久丢失；改为「攒着不投」。
5. **清理点不止 `forget_chat`**：`forget_all`/`forget_messages`/`prune`/按用户删/导出/备份都要复用同一张表清单；现有代码已漏 `weekly_summaries`、`impression_cursors`。
6. **新表不进备份 `REQUIRED_TABLES`**：否则旧备份全部拒收；新表写 `_SCHEMA` 即可，不用升 `SCHEMA_VERSION`。
7. **防重复不能重调模型**：与「主链路一次调用」冲突；改为确定性改写/放弃，并补一个 bot-only 查询。
8. **最高杠杆被漏掉的是「谁回谁」**：`timeline.format_message_line` 不渲染群友互回，`messages` 里其实已有 `reply_to_message_id`；这条零新表、零调用，应进 P0。
9. **话题状态机已存在**（`chat_state` + router 超时），episodes 的 `phase` 复用它，不另起一套。
10. **成本瓶颈是共享免费额度/熔断**，不是钱；Observer 限 24 次/天/群、独立退避、并发 1，默认本地模型。

## 15. 追加实现（2026-10-31）：失败静默 + 面板聊天行为

本轮已落地的三个面板/行为项（不属于上面的记忆改造，但同属「像活人」的底线）：

### 15.1 调用失败不再发「断线了」

- 批次路径（`_run_batch_locked`）与单条路径（`_reply`）遇到 `BrainError` 时：记账 + `mark_skipped("error")` + 日志，**不再往群里发任何失败提示**。
- 例外：如果本轮已经发过「本鱼去网上查一下」的工具提示，仍然补一句 `FOLLOWUP_REPLY`，避免提示语悬空。
- 删除了 `BRAIN_ERROR_REPLY` 与 30 秒错误回复冷却；`GET /api/config` 的 `cooldown` 只保留 `tool_hint_seconds`。
- 理由：用户明确不要；失败提示在免费档 429 频繁时会变成刷屏噪音。代价是 `@` 后失败时群里看不到反馈，靠日志与告警发现。

### 15.2 聊天强度（quiet / normal / active）

| 档位 | 行为 |
| --- | --- |
| quiet 安静 | 非点名消息不进模型：`model` 模式在 `_handle_batched` / `_run_batch_locked` 直接返回，不 `mark_skipped`，消息留待下次点名一起看；`rules` 模式同步跳过。 |
| normal 正常 | 当前行为；默认值，prompt 不加任何内容。 |
| active 活跃 | 放开「话题收尾」与「连续空转降频」两个软闸门；prompt 追加「更愿意接话」提示。静音、黑白名单、预算仍生效。 |

- 实现：`src/dafeiyu/behavior.py` 的 `BehaviorSettings`，`Service.behavior` 持有不可变快照；`set_behavior()` 即时替换。
- 私聊不受强度影响（私聊始终按点名处理）。

### 15.3 言论限制（off / soft / strict）

- `off`：不注入、不检查。
- `soft`：把禁词/禁话题写进本轮 prompt；回复命中时只记 warning，仍然发送。
- `strict`：同样注入；回复命中关键词时**整条丢弃**，群里保持安静（`_restrictions_allow`）。
- 匹配是大小写不敏感的子串匹配（关键词级兜底，不是语义审核）；面板文案明确写「子串匹配」，避免「死」误伤「死机」这类意外。
- 私聊同样生效；限制是全局的，不做按群配置。

### 15.4 API 与持久化

- Bot 控制口：`GET /api/behavior`、`PUT /api/behavior`（`control.py`）。
- Dashboard 代理：`GET /api/behavior`、`PUT /api/behavior`（`supervisor/dashboard.py`），面板「聊天行为」卡片。
- 保存时写入 `data/settings.local.yaml` 的 `behavior` 段（保留 `brain` 等其他段），重启后 `config.load_settings` 通过 `_apply_behavior_overrides` 读回；`config.yaml` 只放默认值。
- 前端输入：禁词/禁话题一行一个，去空行、去重；单条 ≤50 字、最多 100 条；非法值返回 400。

### 15.5 测试

- 新增 `tests/test_behavior.py`：解析、归一化、匹配、注入文案、配置与覆盖层。
- `tests/test_service.py` / `tests/test_run_batch.py`：失败静默、quiet 不送模型、strict 丢弃、soft 注入、active 注入。
- `tests/test_control_api.py`：GET/PUT 往返、非法值 400、覆盖层落盘；面板锚点。
- 已有断言同步更新：`test_dashboard_memory_integration.py`、`test_control_api.py` 的 `cooldown` 口径。

### 15.6 opencode 旧 session 的「静默失联」与自愈

- 现象：项目从 `C:\AICoding\dafeiyu-bot` 移到 `C:\AIWork\dafeiyu-bot` 后，opencode 的旧 session 仍绑定旧目录（空目录），每次调用都报 `Agent not found: "deepseek-qq"`。由于失败提示已被撤掉，表现为「完全不理人」。
- 临时处置：面板「会话管理 → 新建会话」或 `POST /api/sessions/{chat_type}/{chat_id}/new` 清掉两边的 session 映射；新 session 建在当前仓库目录，agent 即可找回。
- 代码自愈：`is_agent_missing_error()` 识别 `Agent not found`，`Service._retry_fresh_session` 会清 session 并换新 session 重放一次（与配额/限流重试同一路径）；下次 bot 重启后生效。
- 排查命令：`logs/supervisor/bot.log` 搜 `Agent not found`；opencode 库 `session_v2.directory` 能看到 session 绑定的目录。

## 16. 接话时机与点名判断增强（2026-10-31 追加）

这一轮不做新的表，先把「是不是在跟我说话」和「什么时候该插嘴」两条判断补强。

### 16.1 点名判断

| 来源 | 判定 | 说明 |
| --- | --- | --- |
| @ / 引用 / 名字 | 已有，批次路径原先把别名漏掉 | `_run_batch_locked` 的 addressed 判定改用 `Service._is_addressed`，别名也按点名处理（reply 预算、收尾重开） |
| 隐式接话 | 新增 | 紧挨着 bot 的提问回复一句短答（≤40 字、间隔 ≤180 秒）算点名；「4060」「对」「不行」这类回答不再被当成路人聊天 |
| 群友互回 | 新增上下文 | 时间线渲染「（回复 阿猫）」，模型能看到谁在接谁 |

### 16.2 插嘴时机

- 新增 `src/dafeiyu/moment.py`：问句/求助词、纯短附和、热聊、技术话题、隐式接话 → `MomentSignals`；纯函数，可回放。
- 收尾窗口不再只靠 `closed_sample_rate` 随机：明确问句/求助/回答 bot 的提问会直接重开；降频窗口同理。
- 注入「【时机】」提示：有人提问但群里正热、纯表情刷屏、话题偏技术等，给模型一句外部判断，减少「批次到点就必须说」。
- 仍由模型最终决定 speak=false；规则不替它说话。

### 16.3 测试

- 新增 `tests/test_moment.py`：问句、短附和、热聊、隐式接话。
- `tests/test_run_batch.py`：收尾窗口问句重开、别名按点名记账、时机提示注入。
- `tests/test_service.py`：隐式接话判定。
- `tests/test_timeline.py`：「（回复 X）」渲染。

## 17. 更多 QQ 消息类型与图片可读性（2026-10-31 追加）

### 17.1 消息段渲染

`gateway/qq.py` 的 `_render_segments` 现在覆盖：

| 类型 | 渲染 |
| --- | --- |
| at | `@你` / `@全体成员` / `@群友`（不把裸 QQ 号塞进上下文） |
| file | `[文件] 文件名` |
| share / location / contact / music | `[分享] 标题` / `[位置] 标题` / `[名片] 名字` / `[音乐] 标题` |
| dice / rps | `[骰子:4]` / `[猜拳:剪刀]` |
| markdown | `[Markdown] 正文`（截断） |
| poke / shake / anonymous / keyboard / template / qqmusic / node | 可读占位 |

语音、视频、文件本体仍不解析；notice 类事件（戳一戳、进群/退群/禁言）本轮未做。

### 17.2 图片为什么没读到

实际排查结论：图**下载成功**，路径也在 prompt 里，但模型没有对「最近一张」调用 `read`。21:05 那轮它按问题指向读了更早的 JPG（并答对了猫），21:04 的 GIF 被当成可选项跳过；21:05:36 还有一次「图没传到我这儿」是图片消息尚未进入该批 prompt 时的误答。

修复：

- 新增 `Service._image_hint`：批内带图时列出最近图片路径，明确要求「先 read 最近一张再答」；问题指向更早的图时再读对应那张。
- 当前批没有图、但消息里提到「图/照片/截图/刚才」时，从最近 60 条消息里补出最近 3 张图片路径，覆盖 session 轮换后图不在 batch 里的情况。
- 动图在 content 注记里标「动图」，提示按首帧理解；下载失败/无来源时明确告诉模型「只有图、没有内容，不要编造」。
- 仍未解决：如果视觉模型本身不接受 GIF，首帧仍可能需要转码；需要的话再加 Pillow 转 PNG（目前未引入依赖）。

### 17.3 测试

- `tests/test_qq_parse.py`：@、file、dice/rps、share/location/contact/music、markdown。
- `tests/test_images.py`：GIF 注记带「动图」。
- `tests/test_run_batch.py`：图片提示、缺少图片提示、最近图片补全。


