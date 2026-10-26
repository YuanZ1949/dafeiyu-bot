# 溟月 / 大肥鱼 · QQ 群聊机器人

把 DeepSeek 拟人化的「大肥鱼」接进 QQ 群：会接梗、会摸鱼、会记仇（划掉）会记事，@ 她必回，不 @ 时偶尔自己插话。

- **模型**：DeepSeek（OpenAI 兼容接口），模型名可配
- **QQ 接入**：NapCat（OneBot v11 反向 WebSocket）
- **微信**：v1 只留适配器占位，未实现（见 `src/dafeiyu/gateway/wechat.py` 里的三条候选路线）
- **记忆**：每群最近 20 条对话 + 群友印象 + 昨日摘要，SQLite 落盘，重启不失忆；
  消息用 trigram FTS5 建索引，可按关键词检索并在回复前召回同会话旧消息

## 架构

```
QQ 群
  │  @ / 普通消息
  ▼
NapCat（QQ 协议端，独立进程）
  │  OneBot v11 反向 WebSocket
  ▼
QQAdapter ── parse_event ──▶ Router（三层闸门）
                               │  ① 硬触发：@ / 引用 / 叫名字 → 必回
                               │  ② 规则筛：白名单、冷却、静默、预算、关键词、热度、采样
                               │  ③ 模型判断：一次调用同时决定「说不说 + 说什么」
                               ▼
                        Context（20 条窗口 + 印象 top5 + 昨日摘要，预算 2500 token）
                               ▼
                        DeepSeek API（JSON：{speak, reply, reason}）
                               ▼
                        Sender（截断 120 字 / 技术问题 300 字，超长分片）
                               ▼
                        QQAdapter.send → send_group_msg
```

旁路任务：每 20 条新消息异步抽取群友印象；每日 04:00 生成昨日摘要。

## 前置条件

| 需要 | 说明 |
| --- | --- |
| Windows 10/11 | 启动脚本是 PowerShell |
| Python 3.11+ | `python --version` 确认 |
| [NapCat](https://github.com/NapNeko/NapCatQQ/releases) | QQ 协议端，需单独下载运行 |
| QQ 小号 | **强烈建议用小号**，不要用主号 |
| DeepSeek API Key | https://platform.deepseek.com 申请 |

## 1. 配置 NapCat

1. 下载 NapCat.Shell 的 Windows 压缩包，解压到一个独立目录。
2. 运行 `napcat.bat`（或 `launcher.bat`），按提示扫码登录 **QQ 小号**。
3. 浏览器打开 NapCat WebUI（通常是 `http://127.0.0.1:6099`），进入 **网络配置**。
4. 新建 **WebSocket 客户端**：
   - URL：`ws://127.0.0.1:8080/onebot/v11/ws`
   - 消息格式：`array`
   - Token：与下面 `.env` 里的 `NAPCAT_WS_TOKEN` 保持一致（留空则都不校验）
5. 保存并启用。此时 NapCat 会主动连到本服务 —— 所以**先启动本服务，再启用这个客户端**，或者启用后它会自动重连。
6. 把机器人小号拉进你的测试群。

## 2. 配置本项目

```powershell
Copy-Item .env.example .env
```

编辑 `.env`（**不要提交进 git**）：

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | ✅ | — | DeepSeek 密钥 |
| `DEEPSEEK_BASE_URL` | | `https://api.deepseek.com` | 兼容其他 OpenAI 风格网关时可改 |
| `DEEPSEEK_MODEL` | | `deepseek-chat` | 可换 `deepseek-reasoner` |
| `DEEPSEEK_REASONING_EFFORT` | | 空 | 设为 `none` 可关闭 Qwen3.5 等本地模型的思考过程 |
| `BOT_QQ` | ✅ | — | 机器人 QQ 号（NapCat 登录的那个） |
| `NAPCAT_WS_HOST` | | `127.0.0.1` | 监听地址 |
| `NAPCAT_WS_PORT` | | `8080` | 监听端口，需与 NapCat 里填的一致 |
| `NAPCAT_WS_TOKEN` | | 空 | 与 NapCat 的 Token 一致 |
| `DAFEIYU_DB_PATH` | | `data/dafeiyu.db` | SQLite 路径 |

### 接本机 LM Studio（可选，零 API 费用）

LM Studio 的 OpenAI 兼容服务默认监听 `http://127.0.0.1:1234/v1`。加载模型后把 `.env` 改成：

```ini
DEEPSEEK_API_KEY=lm-studio
DEEPSEEK_BASE_URL=http://127.0.0.1:1234/v1
DEEPSEEK_MODEL=qwen/qwen3.5-9b
DEEPSEEK_REASONING_EFFORT=none
```

Qwen3.5 默认会先输出一大段思考（`reasoning_content`），`max_tokens` 小时正文会为空。LM Studio 不认 `/no_think` 和 `chat_template_kwargs`，但认 `reasoning_effort: "none"`；本仓库已把它做成 `DEEPSEEK_REASONING_EFFORT` 配置项。若服务开了认证，把 `DEEPSEEK_API_KEY` 换成 LM Studio 里创建的 token。

### QQ 协议端 NapCat（已内嵌到项目）

NapCat 的可执行文件较大（解压后约 345MB），放在 `vendor/napcat/`，已加入 `.gitignore`，不提交进 git。首次准备：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1
```

国内网络下载 GitHub 慢时可加代理（端口按自己的代理改）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1 -Proxy http://127.0.0.1:10808
```

脚本会下载 `NapCat v4.18.41` 的 Windows Node 便携版，并根据 `.env` 自动生成
`vendor/napcat/napcat/config/onebot11_<BOT_QQ>.json`：

- 反向 WS：`ws://127.0.0.1:<NAPCAT_WS_PORT>`
- 消息格式：`array`（必须是 array，否则 @ 识别不到）
- Token：与 `.env` 的 `NAPCAT_WS_TOKEN` 一致

一键启动（先开 NapCat 窗口，再启动 bot）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
```

NapCat 窗口里用 `BOT_QQ` 对应的小号扫码登录。二维码也会保存到
`vendor/napcat/napcat/cache/qrcode.png`。登录成功后，bot 日志会出现 `NapCat 已连接`。

### 扫码页与二维码刷新

终端里的二维码太小，`start-all.ps1` 会自动在后台启动一个二维码助手，等二维码生成后
用默认浏览器打开放大版登录页：

```text
vendor/napcat/napcat/cache/qrcode.html
```

这个页面每 2 秒重新读取 `qrcode.png`，NapCat 换码后页面会自动跟着换，不用手动刷新。
同时会生成 4 倍放大的 `qrcode-big.png`，适合直接用图片查看器打开。

QQ 服务端的单张二维码有效期约 2 分钟，无法从 NapCat 侧改长；NapCat 会在过期后自动
重新拉取，页面持续跟着更新，所以不用抢时间。

```powershell
# 只生成/打开二维码页，不启动 bot
powershell -ExecutionPolicy Bypass -File scripts/napcat-qr.ps1

# 用系统图片查看器打开 qrcode-big.png（不会自动跟随刷新）
powershell -ExecutionPolicy Bypass -File scripts/napcat-qr.ps1 -ImageViewer

# 启动时不要自动打开二维码页
powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1 -NoQr
```

> 如果改了 `.env` 里的 `BOT_QQ`、`NAPCAT_WS_PORT` 或 `NAPCAT_WS_TOKEN`，重跑一次
> `setup-napcat.ps1` 刷新 OneBot 配置；`-Force` 只用于重新下载 NapCat 本体。

行为配置在 `config.yaml`：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `persona.name` / `persona.aliases` | 溟月 / 大肥鱼、小鲸鱼… | 人设名与「叫名字」触发的别名 |
| `persona.max_reply_chars` | `120` | 闲聊回复上限（字） |
| `persona.max_reply_chars_technical` | `300` | 技术问题回复上限（字） |
| `persona.os_probability` | `0.05` | 出现「内心 OS」的概率，`0` 关闭；三种 provider 模式都生效（opencode 模式按轮注入覆盖指令） |
| `persona.prompt_file` | `""` | 自定义人设文件路径；留空使用内置 `brain/persona.md`。改完重启生效 |
| `triggers.cooldown_seconds` | `60` | 同一群两次**非提问**发言的最小间隔（明确提问、@/引用/叫名字不受限） |
| `triggers.sample_rate` | `0.05` | 普通消息被纳入模型判断的采样率 |
| `triggers.quiet_hours` | `["01:00","07:00"]` | 静默时段，不主动插话（@ 仍会回） |
| `triggers.keywords` | 怎么/为什么/崩了… | 命中即进入模型判断（问号结尾也算；「没什么」这类否定口语不算） |
| `groups.whitelist` | `[]` | 留空 = 所有群；非空 = 只服务这些群号（被 @/引用/叫名字时不受限） |
| `groups.blacklist` | `[]` | 永不响应的群号 |
| `groups.admins` | `[]` | 允许执行 `/闭嘴`、`/醒来` 的 QQ 号 |
| `budget.daily_calls` | `200` | 每群每天最多调用模型次数 |
| `budget.daily_tokens` | `300000` | 每群每天 token 上限 |
| `memory.window_size` | `20` | 上下文窗口消息数 |
| `memory.impression_topk` | `5` | 每次注入的群友印象条数 |
| `memory.extract_every` | `20` | 每 N 条消息抽一次印象 |
| `memory.retention_days` | `0` | 消息保留天数；`0` = 永久，`>0` 时每天清理更早的消息（摘要/印象保留） |
| `memory.recall_enabled` | `false` | 回复前按当前批次检索同会话旧消息，注入「相关旧记忆」；代码默认关，`config.yaml` 示例已显式开启 |
| `memory.recall_topk` | `3` | 相关旧记忆最多注入几条 |
| `memory.recall_chars` | `300` | 相关旧记忆段落的字符预算 |

## 3. 启动

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

脚本会：创建/复用 `.venv` → 安装依赖 → 检查 `.env` 与 `config.yaml` → 启动服务。

看到下面两行就说明链路通了：

```
QQ 网关已监听 127.0.0.1:8080
NapCat 已连接
```

日志同时写入 `logs/dafeiyu.log`。

## 4. 用法

群里：

- `@溟月 今天吃什么` —— 必回
- 直接叫「大肥鱼」「小鲸鱼」「鱼鱼」—— 必回
- 引用她的消息继续问 —— 必回
- 其他消息 —— 她按规则 + 模型判断决定要不要插话
- 被规则跳过的消息不会丢：下次接话时会把这些漏掉的消息按 `[时:分] 昵称：内容` 的时间线一起交给模型
- `/闭嘴`（或 `/shutup`）—— 本群静默 60 分钟（仅 `groups.admins` 里的 QQ 可用）
- `/醒来`（或 `/wake`）—— 解除静默（仅 `groups.admins` 里的 QQ 可用）

私聊：

- 直接说话即可（不受冷却、白名单、静默限制，仍受每日预算限制）
- `/stats` —— 今天被叫了几次、吃了多少 token
- `/forget` —— 清空这个私聊的记忆

命令行（服务不用停）：

```powershell
.venv\Scripts\dafeiyu-cli.exe stats  --chat 123456789 --type group
.venv\Scripts\dafeiyu-cli.exe forget --chat 123456789 --type group
.venv\Scripts\dafeiyu-cli.exe summary --chat 123456789 --date 2026-10-23 --type group
.venv\Scripts\dafeiyu-cli.exe forget --all
```

> `--type` 默认 `group`。群号和 QQ 号可能数字相同，所以必须区分。

## 5. 控制台面板

`scripts/start.ps1` 启动后打开 <http://127.0.0.1:8787>（端口在 `config.yaml` 的
`supervisor.dashboard.port`）。面板和 Bot 控制口默认都只监听本机，不对外网开放。

面板上的卡片：

- **进程 / 大脑 / 登录二维码**：查看 OpenCode、Bot、NapCat 状态，切换模型，扫码登录。
- **记忆**：查看按日摘要与群印象，可按群聊/私聊筛选；填会话 ID 可手动生成摘要、抽取印象。
  每条印象可编辑内容/置信度/类型、查看来源消息摘录，勾选多条后可合并；编辑与合并都会同步更新搜索索引。
- **用量**：最近 7/14/30 天的调用与 token 柱状图、Top 会话、最近错误。
- **消息搜索**：跨会话按关键字搜索，结果分页加载并显示命中摘录；3 字以上中文走 trigram FTS，1–2 字自动回落 LIKE。
  命中项可点「定位」打开对应会话时间线并高亮该消息。
- **会话管理**：按最近活跃排序，分页加载。每行可以：
  - **查看时间线**：只读抽屉展示消息流、会话摘要、按日摘要、印象和当日用量；支持「加载更早」「回到最新」；
  - **新建会话**：重置 OpenCode session 映射与会话内摘要；消息、按日摘要、印象、用量都保留；
  - **软忘记**：删本会话的消息和 session 映射，保留按日摘要与印象；
  - **彻底删除**：删远端 session + 本地消息、摘要、印象、用量，不可恢复；
  - **导出 MD / JSON**：导出单会话记忆。
- **日志**：按 supervisor / opencode / bot / napcat 分来源实时查看，支持级别与关键字筛选、
  暂停/继续、加载行数切换、清屏和下载当前日志文件。清屏只影响页面视图，不删除磁盘日志。
- **运行配置**：只读展示当前模型、人设、冷却、限流、预算、记忆窗口与保留天数。
  要改配置请编辑 `config.yaml` 后重启；面板提供「重启 Bot」和「下载备份」。

**下载备份**会通过 SQLite backup API 生成一份一致性快照（`dafeiyu-backup-时间.db`），
包含全部会话、摘要、印象、用量和 session 映射。恢复时先停服务，用备份文件替换
`data/dafeiyu.db`，再启动即可。面板不自动删除旧备份，请自行管理文件。

面板每 2–30 秒轮询一次状态；会话列表默认每页 50 条，点「加载更多」继续。

首次启动会自动为已有消息建 trigram 索引并回填；如果本机 SQLite 不支持 trigram，
搜索会自动回落 LIKE，不影响使用。

## 6. 测试

```powershell
.venv\Scripts\python -m pytest -q
```

全部用例不联网、不需要真实 QQ 账号：HTTP 用 `respx` 模拟，OneBot 事件用本地 fixture，WS 适配器起真实本地端口对打。

## 7. 常见故障

| 现象 | 排查 |
| --- | --- |
| 启动报「缺少环境变量 DEEPSEEK_API_KEY」 | `.env` 没建或变量名拼错；确认在仓库根目录启动 |
| 日志没有「NapCat 已连接」 | NapCat 的 WebSocket 客户端没启用；URL/端口/Token 不一致；端口被占用 |
| 群消息完全没反应 | 机器人小号没进群；群在 blacklist；`groups.whitelist` 非空但没包含该群；看 `logs/dafeiyu.log` 里的 `reason` |
| 回得很慢 | `DEEPSEEK_MODEL=deepseek-chat` 比 `deepseek-reasoner` 快很多；检查系统代理，httpx 已设 `trust_env=False` |
| HTTP 429 | 触发限流：降低 `sample_rate`、调小 `budget.daily_calls`，或给账户充值 |
| 记忆面板不更新、提示「配额暂停中」 | 账号级熔断：10 分钟内累计 3 次 429 或 provider 报 quota 后，摘要/印象暂停 1 小时，面板显示恢复时间；日志搜「429」可见触发原因 |
| 回复被截断 | 这是设计行为：闲聊 120 字、技术问题 300 字，改 `config.yaml` 即可 |
| 中文日志乱码 | 用 `scripts/start.ps1` 启动（已设 UTF-8），不要直接双击 python |

## 8. 隐私与风险

- 消息原文保存在本地 `data/dafeiyu.db`，不会上传到除 DeepSeek API 之外的任何地方。
- 群友可以要求删除：`.venv\Scripts\dafeiyu-cli.exe forget --chat <群号>`。
- **QQ 小号有被风控/封禁的风险**，本项目通过非官方协议端接入，请自行评估；不要用主号，不要高频刷屏。
- 机器人说的话由模型生成，可能出错；涉及医疗、法律、财务等严肃问题时请以人为准。

## 9. 目录

```
src/dafeiyu/
  __main__.py        装配与常驻
  config.py          .env + config.yaml 加载与校验
  models.py          Message / Segment / ChatType
  gateway/           qq.py（OneBot 反向 WS）、wechat.py（占位）、base.py
  router.py          三层闸门、冷却、预算、静默、指令解析
  memory/            store.py（SQLite）、context.py（上下文预算）
  brain/             client.py（DeepSeek）、persona.py + persona.md（人设）
  workers/           impressions.py（群友印象）、summary.py（每日摘要）
  sender.py          截断与分片
  cli.py             运维命令
tests/               单元 + 集成测试（不联网）
```
