# 大肥鱼 QQ 群聊机器人 — 设计文档

- 日期：2026-10-24
- 状态：待用户确认
- 项目目录：`C:\MyFile\AICoding\dafeiyu-bot`

## 1. 背景

用户希望给自己的 QQ 群聊和微信群聊接入"蓝色大肥鱼 AI"——中文互联网对 DeepSeek 的社区二创鲸鱼娘形象（本设计采用社区通行的「溟月 / 大肥鱼」设定）。

目标不是做一个通用客服机器人，而是让群里多一个**会聊天、有性格、能接梗的群友**：它会在被叫到时回答，也会自己在合适的时候插一句话。

## 2. 目标与非目标

### v1 目标

1. QQ 群内被 @、被回复、被叫名字时必定应答。
2. 其余群消息由它自己判断要不要插话，并受冷却与预算约束。
3. 回复符合「溟月 / 大肥鱼」人设：短、口语、贱萌、傲娇，技术问题先答对再卖萌。
4. 记住最近对话窗口、群友印象、每日摘要。
5. 模型名 / API Key / 触发规则 / 人设 / 预算全部可配置。
6. 微信端预留可插拔适配器接口，v1 不实跑。

### 非目标（v1 明确不做）

- 不实现微信实际接入与登录，只留接口占位。
- 不做语音、图片、视频等多模态输入输出。
- 不做 Web 管理后台、可视化配置面板。
- 不做多租户、多机器人实例、计费系统。
- 不做云服务器部署与运维编排。
- 不做模型微调或本地推理，只调用 DeepSeek API。
- 不做自动加群、自动通过好友、群发营销。
- 不做向量数据库 / embedding 检索（用 SQLite FTS5 已足够）。

## 3. 调研结论（2026-10-24）

### 3.1 个人微信：开源方案已基本消亡

| 项目 | 状态 |
|---|---|
| WeChatFerry（6.8k★） | 仓库已归档，最后提交 2026-07-10 |
| Gewechat（3.5k★） | README 明写「本项目已停止维护」，服务、镜像、技术支持均不再提供，并引用微信《针对违规获取及利用微信终端用户数据行为的打击公告》 |
| wxauto（7.4k★） | 仅支持 Windows 10/11 + 微信 3.9.X，作者声明禁止用于生产 |
| wechatauto-replica（520★） | 支持微信 4.1.12+，读本地 SQLCipher 库 + UIA/OCR 发送，作者为高中生，更新不稳定 |
| WeChatPadPro | 付费 iPad 协议，LangBot 内置支持，仍属违规接入 |

结论：**开源、稳定、合规三者在个人微信上不可兼得。**

### 3.2 QQ：通道成熟

NapCat（NapNeko/NapCatQQ）基于 NTQQ 实现 OneBot v11 协议，活跃维护，Windows 下可直接运行 Shell 版并扫码登录。AstrBot、LangBot、Kirara 等主流框架均原生支持。

### 3.3 DeepSeek API

OpenAI 兼容接口。模型名必须可配置（官方文档为 JS 渲染，模型名以实际调用时的官方文档为准），默认填 `deepseek-chat`。

### 3.4 用户决策

- 微信走「QQ 先行，微信后置」：v1 只做 QQ，微信仅留适配器接口。
- 群内由机器人自主判断是否插话。
- 记忆采用「滑动窗口 + 长期印象」。
- 人设采用社区「溟月 / 大肥鱼」设定，语气参考 `ERUIHNIYHBKBNF/dafeiyu-persona`。

## 4. 关键决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 微信接入 | 后置，只留接口 | 开源 hook 方案全灭，RPA 脆弱且违规；先用 QQ 验证价值 |
| 开发形态 | 自研 Python 单体 | 微信将来若走 RPA，Python 生态（wxauto）可直接复用；不引入框架的黑盒约束 |
| QQ 协议 | NapCat + OneBot v11 反向 WS | 活跃维护；反向 WS 由本服务监听，便于本机穿透与统一鉴权 |
| 触发策略 | 硬触发 + 规则筛 + 模型判断 | "自主判断"不等于每条都问模型，否则成本失控 |
| 记忆 | SQLite + FTS5 | 印象量级在千条内，关键词召回足够；少一个向量库依赖 |
| 人设 | 提示词分层 + few-shot | 人设只改语气，不影响正确性 |
| 部署 | 本机 Windows 常驻 | 用户环境即 Windows，v1 不做容器化 |

## 5. 架构

```
QQ 群
  │  OneBot v11 事件
  ▼
NapCat（独立进程，扫码登录 QQ 小号）
  │  反向 WebSocket（NapCat → 本服务）
  ▼
gateway/qq.py        统一 Message 模型（chat_type/chat_id/user_id/nickname/content/raw）
  ▼
router.py            触发判定 + 冷却 + 预算 + 去重
  ▼
memory/context.py    拼上下文（窗口 20 条 + 印象 top5 + 昨日摘要）
  ▼
brain/client.py      DeepSeek 单次调用：判断是否说话 + 生成回复（JSON）
  ▼
sender.py            长度裁剪、分片、限速、发送
  ▼
NapCat action: send_group_msg / send_private_msg

异步旁路（不阻塞回复）：
  workers/impressions.py   每累积 20 条新消息抽取群友印象
  workers/summary.py       每日 04:00 生成群日报
```

### 组件职责

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `gateway/qq.py` | 解析 OneBot 事件、发送 action、断线重连 | 不做业务判断 |
| `gateway/wechat.py` | 与 QQ 同构的占位实现，抛 `NotImplementedError` 并写明接入方式 | 不假装可用 |
| `router.py` | 硬触发、规则筛、冷却、预算、幂等去重 | 不拼 prompt |
| `memory/*` | 落库、召回、上下文预算控制、遗忘 | 不调模型 |
| `brain/client.py` | 组装 prompt、调用 API、解析 JSON、重试 | 不决定是否回复（由 router 决定） |
| `workers/*` | 异步抽取印象与摘要 | 失败不阻塞主链路 |
| `sender.py` | 回复整形与发送 | 不做内容生成 |

## 6. 数据模型

```sql
-- 消息窗口
CREATE TABLE messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_type   TEXT NOT NULL,          -- group | private
  chat_id     INTEGER NOT NULL,
  message_id  TEXT,                   -- OneBot message_id，用于幂等
  user_id     INTEGER NOT NULL,
  nickname    TEXT NOT NULL,
  content     TEXT NOT NULL,
  is_bot      INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL           -- ISO8601
);
CREATE INDEX idx_messages_chat_time ON messages(chat_id, created_at DESC);
CREATE UNIQUE INDEX idx_messages_mid ON messages(chat_type, chat_id, message_id)
  WHERE message_id IS NOT NULL;

-- 群友印象
CREATE TABLE impressions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id         INTEGER NOT NULL,
  subject_user_id INTEGER,
  kind            TEXT NOT NULL,      -- nickname | catchphrase | topic | relation
  content         TEXT NOT NULL,      -- 一句话，<=60 字
  confidence      REAL NOT NULL DEFAULT 0.5,
  updated_at      TEXT NOT NULL
);
CREATE INDEX idx_impressions_chat ON impressions(chat_id, kind);

-- 每日摘要
CREATE TABLE summaries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id    INTEGER NOT NULL,
  date       TEXT NOT NULL,           -- YYYY-MM-DD
  content    TEXT NOT NULL,
  UNIQUE(chat_id, date)
);

-- 用量统计（用于预算与 /stats）
CREATE TABLE usage (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  date              TEXT NOT NULL,
  chat_id           INTEGER NOT NULL,
  purpose           TEXT NOT NULL,    -- reply | judge | impression | summary
  prompt_tokens     INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL
);
CREATE INDEX idx_usage_date_chat ON usage(date, chat_id);
```

FTS5 虚表与触发器：

```sql
CREATE VIRTUAL TABLE impressions_fts USING fts5(content, content='impressions', content_rowid='id');
CREATE TRIGGER impressions_ai AFTER INSERT ON impressions BEGIN
  INSERT INTO impressions_fts(rowid, content) VALUES (new.id, new.content);
END;
```

所有 `date` 字段按 `Asia/Shanghai` 时区计算。

## 7. 触发与预算

### 7.1 三层闸门

**第 1 层：硬触发（0 成本，必回）**

- 消息 `at` 段包含 `BOT_QQ`；
- 消息为对机器人消息的引用回复；
- 文本包含任一别名：`大肥鱼`、`小鲸鱼`、`溟月`、`鱼鱼`、`肥鱼`。

**第 2 层：规则筛（0 成本）**

依次检查，任一不通过即放弃：

1. 群在 `groups.whitelist` 内（白名单为空时默认全部允许）；
2. 距**机器人上次在本群发言** ≥ `cooldown_seconds`（默认 60）；
3. 当前时间不在 `quiet_hours`（默认 01:00–07:00）；
4. 本群当日调用数 < `daily_calls`（默认 200），当日 token < `daily_tokens`（默认 300k）；
5. 满足以下任一：最近 10 条内出现疑问句/求助词（`怎么|为什么|为啥|求|有没有|是不是|崩了|报错|怎么办|？|?`）、话题消息数 ≥3、或命中 `sample_rate`（默认 0.05）随机采样。

**第 3 层：模型判断（1 次调用）**

一次调用同时完成"判断 + 生成"，输出：

```json
{"speak": true, "reason": "有人问显卡驱动怎么装", "reply": "*（这帮人又在折腾显卡……）* 先说你什么卡？"}
```

`speak=false` 时直接丢弃，**不追加任何调用**。

### 7.2 私聊与指令

- **私聊**：不走规则筛，直接回复；仍计入每日调用与 token 预算，不受群冷却限制。
- **指令**（仅群内文本以 `/` 开头时识别，不送模型）：
  - `/闭嘴` —— 本群静默 1 小时；
  - `/醒来` —— 解除静默；
  - `/stats` —— 仅私聊可用，返回当日调用数与 token 数；
  - `/forget` —— 仅私聊，删除当前会话记忆（等同 CLI）。

### 7.3 防刷屏

- 每群每分钟最多 1 条；
- 连续 3 次发言后 5 分钟内无人回应 → 该群降频 30 分钟（采样率减半）；
- 单次回复长度上限 120 字（技术问题 300 字），按句截断，不硬切。

### 7.4 幂等

以 OneBot `message_id` 建唯一索引去重，NapCat 重连重放不会重复回复。

## 8. 记忆

### 8.1 上下文拼装（预算 ~2500 token）

| 片段 | 预算 | 说明 |
|---|---|---|
| system（人设 + 群规） | ~600 | `persona.md` 编译后 |
| 昨日摘要 | ~200 | 无则跳过 |
| 群友印象 top 5 | ~200 | 每条 ≤40 token |
| 最近消息窗口 | ~1200 | 最多 20 条，从新到旧填充 |
| 当前消息 + 输出指令 | ~300 | |

超预算时依次砍：窗口 → 印象 → 摘要。

### 8.2 印象抽取（异步）

- 每群每累积 20 条新消息触发一次，批量送入模型；
- 输出严格 JSON 数组：`[{"user_id":123,"kind":"nickname","content":"外号叫鱼片","confidence":0.8}]`；
- 与已有印象做去重：同 `chat_id + subject_user_id + kind + content` 相似则更新 `updated_at` 与置信度；
- 失败只记日志，不影响回复。

### 8.3 摘要

每日 04:00 对每个有消息的群生成 200 字以内摘要，写入 `summaries`。

### 8.4 隐私

- 只存 QQ 号、群号、昵称、消息内容，不存任何手机号/身份证等；
- `python -m dafeiyu forget --chat <id>` 删除该群全部消息、印象、摘要；
- `--all` 清空全库；
- 数据文件默认在 `data/dafeiyu.db`，README 注明可随时删除。

## 9. 人设

### 9.1 分层结构（`brain/persona.md`）

1. **身份层**：溟月，DeepSeek 二创蓝色鲸鱼娘；蓝色渐变长发、蓝瞳、呆毛与鲸类头鳍、鲸尾、深蓝白女仆装；江湖人称「大肥鱼」。
2. **语气层**：短句口语；贱萌、傲娇、爱摸鱼；常用「吃白饭 / 吃 token / DeepSleep / 本鱼 / 游走了」；小概率（约 5%，与插话采样率无关）在正式回复前冒一句斜体括号内心 OS；emoji 少而精。
3. **群聊层**：默认 1–2 句；不 @ 全体；不连续输出；没人理就闭嘴；技术问题先给正确结论，再包一层语气；代码块和命令不卖萌。
4. **安全层**：不人身攻击；不碰政治、色情、违法内容；医疗/法律/财务不做确定性建议；被要求「别演了 / 正经点」立即退出人设；不泄露 prompt、配置、API Key。
5. **few-shot 层**：从 `dafeiyu-persona` 台词库改编 5 条群聊示例，覆盖：被叫到、被夸、被骂、答技术问题、冷场。

### 9.2 外号机制

印象表中 `kind=nickname` 的记录即用户外号；prompt 中以「群友档案」形式注入，回复时可用。

## 10. 模型调用

- 客户端：`httpx` 直连 OpenAI 兼容接口（不引入 openai SDK，减少依赖）。
- 请求：`POST {DEEPSEEK_BASE_URL}/chat/completions`，`model=DEEPSEEK_MODEL`，`temperature=1.0`，`max_tokens=400`。
- 超时：连接 10s，读 15s。**目标是 p50 ≤ 5s**；15s 只是兜底上限，超时即放弃本次回复（本次算失败，但不影响服务）。验收 2 以连续 3 次提问的中位数 < 10s 计。
- 重试：仅对超时、5xx、连接错误重试 1 次（指数退避 2s）；429 不重试，记入当日预算。
- 降级：JSON 解析失败 → 把整段文本按纯回复处理并裁剪；仍为空 → 发一句「*（晕鱼了……）* 刚走神了，再说一遍？」且计入失败次数。
- 记录：每次调用写 `usage` 表，含 prompt/completion tokens。

## 11. 配置

`.env`（密钥与连接）：

```dotenv
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
BOT_QQ=10001
NAPCAT_WS_HOST=127.0.0.1
NAPCAT_WS_PORT=8080
```

`config.yaml`（行为）：

```yaml
persona:
  name: 溟月
  aliases: [大肥鱼, 小鲸鱼, 溟月, 鱼鱼, 肥鱼]
  max_reply_chars: 120
  max_reply_chars_technical: 300
triggers:
  cooldown_seconds: 60
  sample_rate: 0.05
  quiet_hours: ["01:00", "07:00"]
  keywords: [怎么, 为什么, 为啥, 求, 有没有, 是不是, 崩了, 报错, 怎么办]
groups:
  whitelist: []          # 空 = 全部允许
  blacklist: []
budget:
  daily_calls: 200
  daily_tokens: 300000
memory:
  window_size: 20
  impression_topk: 5
  extract_every: 20
logging:
  level: INFO
  dir: logs
```

优先级：`.env` > `config.yaml` 默认值。所有字段启动时校验，缺失必填项直接报错退出并给出中文提示。

## 12. 错误处理

| 场景 | 处理 |
|---|---|
| NapCat 断线 | 清理未完成的 action future（置异常），等待 NapCat 按自身策略重连；超过 5 分钟未重连记 ERROR 日志 |
| 收到未知事件类型 | 忽略并 debug 日志 |
| 消息为空 / 非文本 | 忽略 |
| 群被禁言 / 发送失败 | 记 WARN，同一条最多重试 2 次 |
| 同一群消息并发到达 | 每群一个串行队列，保证回复顺序与冷却判定原子性 |
| DeepSeek 超时 | 放弃本次回复，不补发 |
| 预算耗尽 | 当日静默，日志提示，次日 0 点重置 |
| SQLite 锁 | WAL 模式 + 5s busy_timeout |
| 配置错误 | 启动即失败，打印具体字段 |

## 13. 测试

| 层 | 用例 | 工具 |
|---|---|---|
| 单元 | OneBot 群消息/私聊/@/引用 事件解析 | pytest + 真实事件 fixture |
| 单元 | 硬触发命中与不命中（别名、@、引用） | pytest |
| 单元 | 规则筛：白名单、冷却、静默、预算、采样 | pytest |
| 单元 | 上下文拼装不超预算，超预算裁剪顺序正确 | pytest |
| 单元 | JSON 解析失败降级、回复截断、分片 | pytest |
| 集成 | 假 OneBot WS 服务端：推事件 → 断言发出 `send_group_msg` | pytest-asyncio + websockets |
| 集成 | 冷却期内第二条不触发 | 同上 |
| 集成 | DeepSeek mock 返回 speak=false 时不发送 | respx |
| 配置 | 修改 `DEEPSEEK_MODEL` 后启动日志显示新值 | pytest / 手动 |

全部测试不联网、不依赖真实 QQ 号或 API Key。

## 14. 部署与运维

1. 下载 NapCat.Shell Windows 版，解压运行 `launcher.bat`，扫码登录 **QQ 小号**。
2. 打开 NapCat WebUI（默认 `http://127.0.0.1:6099`），配置 OneBot v11 **反向 WebSocket**：`ws://127.0.0.1:8080/onebot/v11/ws`，Token 与 `.env` 一致。
3. `python -m venv .venv && .venv\Scripts\pip install -e .`
4. `copy .env.example .env`，填 API Key 与 `BOT_QQ`。
5. `scripts\start.ps1` 启动；看到「已连接 NapCat」即就绪。
6. 把机器人 QQ 拉进群，@它 测试。
7. 开机自启（可选）：任务计划程序，触发器「登录时」，操作指向 `start.ps1`。

README 必须包含以上步骤与常见故障（扫码失败、WS 连不上、群消息无响应）。

## 15. 交付切片

每条切片独立可验证，绿了才进下一条：

| # | 切片 | 完成判据 |
|---|---|---|
| 1 | 骨架 + NapCat 连通 | 群里发消息，控制台打印解析后的统一 Message |
| 2 | 硬触发 + DeepSeek 回复 | 群里 @它，收到符合人设的回复 |
| 3 | 软判断 + 护栏 | 单元/集成测试覆盖冷却、预算、采样；群里能自主插话 |
| 4 | 窗口记忆落盘 | 重启后它仍能接上刚才的话题 |
| 5 | 印象 + 日报 | 聊 40 条后 `impressions` 表有记录；手动触发摘要生成 |
| 6 | 人设打磨 + 微信占位 + 文档 | 人设 few-shot 生效；`wechat.py` 抛 NotImplementedError；README 实跑通过 |

## 16. 验收标准（与契约对应）

| 契约 | 证据 |
|---|---|
| 1 设计文档 | 本文档经用户确认 |
| 2 QQ 群 10 秒回复 | 真实群 @ 实测日志 + 集成测试通过 |
| 3 微信适配器占位 | `gateway/wechat.py` 同接口实现，抛 `NotImplementedError`，docstring 写明接入路径 |
| 4 配置化 | 改 `.env` 模型名重启，日志显示新模型 |
| 5 一键启动 + README | 新目录照 README 实跑成功 |
| 6 无临时文件 | `git status` 无本次新增未跟踪临时文件 |

## 17. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| QQ 小号被风控/封号 | 服务中断 | 用小号、控制频率、不做群发；README 显著提示 |
| NapCat 版本升级导致协议失效 | 无法收发 | 固定版本、记录升级步骤、关注上游 release |
| DeepSeek 限流或涨价 | 回复失败/成本上升 | 预算上限、超时放弃、模型名可切换 |
| 机器人刷屏惹人烦 | 群友体验 | 冷却、每分钟限 1 条、降频机制、随时可 `/闭嘴` 指令 |
| 隐私 | 聊天记录落盘 | 本地存储、提供 forget 命令 |
| 人设越界（政治/低俗） | 群被封 | 安全层硬规则 + 关键词黑名单 |

## 18. 未来：微信接入路径

`gateway/wechat.py` 需实现与 `qq.py` 相同的接口：

```python
class WeChatAdapter:
    async def connect(self) -> None: ...
    async def send(self, chat_id: int, text: str) -> None: ...
    # 内部把微信事件转成统一的 Message 并投递给 router
```

三条候选路径，按稳定性排序：

1. **WeChatPadPro（付费 iPad 协议）** —— 稳定、LangBot 已内置适配，需付费且仍违规。
2. **wechatauto-replica（微信 4.1.12+）** —— 免费，需微信窗口常驻，维护者不保证持续更新。
3. **企业微信 / 公众号** —— 合规稳定，但进不了普通微信群。

选型留到用户决定承担风险时再做，本设计不预设。

## 19. 开放问题

- QQ 小号、群号、DeepSeek API Key 由用户提供，不阻塞开发（可用 mock 完成 1–5 号切片）。
- 是否需要联网搜索能力（DeepSeek 官方若提供 web search API）留待 v2 评估。
