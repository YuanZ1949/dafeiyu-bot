# 溟月 / 大肥鱼 · QQ 群聊机器人

把 DeepSeek 拟人化的「大肥鱼」接进 QQ 群：会接梗、会摸鱼、会记仇（划掉）会记事，@ 她必回，不 @ 时偶尔自己插话。

- **模型**：DeepSeek（OpenAI 兼容接口），模型名可配
- **QQ 接入**：NapCat（OneBot v11 反向 WebSocket）
- **微信**：v1 只留适配器占位，未实现（见 `src/dafeiyu/gateway/wechat.py` 里的三条候选路线）
- **记忆**：每群最近 20 条对话 + 群友印象 + 昨日摘要，SQLite 落盘，重启不失忆；
  消息用 trigram FTS5 建索引，可按关键词检索并在回复前召回同会话旧消息

## 五分钟上手

> **平台**：目前只支持 **Windows 10/11**（启动脚本是 PowerShell，NapCat 用 Windows 版，权限加固用 `icacls`）。
> **许可**：MIT，见 `LICENSE`。

1. **装 NapCat**（约 345MB，不进 git）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1
   ```

2. **初始化**（建 `.venv`、装依赖、从模板生成 `config.yaml`、体检）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
   ```

   首次运行如果 `.env` 不存在，脚本会复制一份模板然后停下，等你填完再跑一次。

3. **填 `.env`**（只改这两行就能跑）：

   ```ini
   BOT_QQ=机器人QQ号
   DEEPSEEK_API_KEY=sk-...
   ```

4. **启动**：双击 `scripts\start.bat`，或

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
   ```

   面板默认监听 `0.0.0.0:8787`（局域网可访问）；没配口令时会自动生成到 `data\dashboard.password`。

随时自检（配置、依赖、端口一次列全）：

```powershell
.venv\Scripts\python -m dafeiyu.cli doctor
```

`config.yaml` 是**本机配置，不进版本库**；模板是 `config.example.yaml`，要调行为就改前者。

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
# 生成 config.yaml（已存在则不覆盖；要重置加 --force）
.venv\Scripts\python -m dafeiyu.cli init
Copy-Item .env.example .env
```

编辑 `.env`（**不要提交进 git**）：

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | ✅ | — | DeepSeek 密钥 |
| `DEEPSEEK_BASE_URL` | | `https://api.deepseek.com` | 兼容其他 OpenAI 风格网关时可改 |
| `DEEPSEEK_MODEL` | | `deepseek-flash` | 可换 `deepseek-reasoner`（会自动核对端点清单） |
| `DEEPSEEK_REASONING_EFFORT` | | 空 | 设为 `none` 可关闭 Qwen3.5 等本地模型的思考过程 |
| `BOT_QQ` | ✅ | — | 机器人 QQ 号（NapCat 登录的那个） |
| `NAPCAT_WS_HOST` | | `127.0.0.1` | 监听地址 |
| `NAPCAT_WS_PORT` | | `8080` | 监听端口，需与 NapCat 里填的一致 |
| `NAPCAT_WS_TOKEN` | | 空 | 与 NapCat 的 Token 一致 |
| `DAFEIYU_DB_PATH` | | `data/dafeiyu.db` | SQLite 路径 |
| `DAFEIYU_DASHBOARD_USER` | | `admin` | 控制台登录用户名 |
| `DAFEIYU_DASHBOARD_PASSWORD` | 非本机访问时 ✅ | 空 | 控制台登录密码，至少 12 位 |
| `DAFEIYU_DASHBOARD_SESSION_HOURS` | | `168` | 登录有效期（小时，1–720） |

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

一键启动：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
```

supervisor 会托管三个进程：OpenCode（大脑）、bot、NapCat。NapCat 用
`-q <BOT_QQ>` 带上本地票据自动登录，**只有首次登录或票据失效时**才需要扫码：
二维码保存到 `vendor/napcat/napcat/cache/qrcode.png`，面板的「登录二维码」卡片
也会显示。登录成功后，bot 日志会出现 `NapCat 已连接`。

### 扫码页与二维码刷新

终端的二维码太小，用二维码助手打开放大版登录页：

```text
vendor/napcat/napcat/cache/qrcode.html
```

这个页面每 2 秒重新读取 `qrcode.png`，NapCat 换码后页面会自动跟着换，不用手动刷新。
同时会生成 4 倍放大的 `qrcode-big.png`，适合直接用图片查看器打开。

QQ 服务端的单张二维码有效期约 2 分钟，无法从 NapCat 侧改长；NapCat 会在过期后自动
重新拉取，页面持续跟着更新，所以不用抢时间。

```powershell
# 生成并打开二维码页（不启动其他进程）
powershell -ExecutionPolicy Bypass -File scripts/napcat-qr.ps1

# 用系统图片查看器打开 qrcode-big.png（不会自动跟随刷新）
powershell -ExecutionPolicy Bypass -File scripts/napcat-qr.ps1 -ImageViewer
```

> 面板的「登录二维码」卡片与 `GET /api/qr/image` 也能看到最新二维码。

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
| `budget.global_daily_calls` | `0` | 所有会话合计每日调用上限；`0` = 未启用 |
| `budget.global_daily_tokens` | `0` | 所有会话合计每日 token 上限；`0` = 未启用 |
| `budget.monthly_calls` | `0` | 所有会话合计自然月调用上限（上海时区）；`0` = 未启用 |
| `budget.monthly_tokens` | `0` | 所有会话合计自然月 token 上限；`0` = 未启用 |
| `budget.monthly_cost_limit` | `0` | 月度费用上限；`0` = 不限制，`>0` 时必须填单价 |
| `budget.currency` | `CNY` | 费用展示币种；不做汇率换算 |
| `budget.price_input_per_million` | `0` | 输入 token 单价（每百万 token，按 `currency`） |
| `budget.price_output_per_million` | `0` | 输出 token 单价（每百万 token，按 `currency`） |
| `budget.warning_ratios` | `[0.5, 0.8, 1.0]` | 额度预警比例；每天每档最多告警一次 |
| `budget.on_exceed` | `degrade` | 超限动作：`degrade` 只停非点名回复、`stop` 全停、`notify` 只告警不拦截 |
| `memory.window_size` | `20` | 上下文窗口消息数 |
| `memory.impression_topk` | `5` | 每次注入的群友印象条数 |
| `memory.extract_every` | `20` | 每 N 条消息抽一次印象 |
| `memory.retention_days` | `180` | 消息保留天数；`0` = 永久，`>0` 时每天清理更早的消息（摘要/印象保留） |
| `memory.recall_enabled` | `false` | 回复前按当前批次检索同会话旧消息，注入「相关旧记忆」；代码默认关，`config.yaml` 示例已显式开启 |
| `memory.recall_topk` | `3` | 相关旧记忆最多注入几条 |
| `memory.recall_chars` | `300` | 相关旧记忆段落的字符预算 |

预算分四层：每群每日、全局每日、自然月、月度费用。所有 `0` 都表示「未启用/不限制」。
`degrade` 会保留 @/引用/叫名字的回复，只停普通插话；`stop` 连点名也不回；`notify`
只发告警不拦截。月度费用达到上限即硬拒绝，即使 `on_exceed=notify` 也不会放行。
费用按「token 数 × 单价」估算，不是账单金额；要准确成本请以服务商账单为准。
`warning_ratios` 取值 `(0, 1]`；全局日/月度/费用命中 50%/80%/100% 时各告警一次，
由 supervisor 做 24 小时去重，告警 detail 只包含计数与限额，不包含消息正文。
当前会话的 per-chat 额度在 `/budget` 和面板预算卡显示；单群预警不进入 supervisor
告警通道。`alerts.rules.budget_warning` 可单独关闭预算预警。

## 3. 启动与停止

日常用 `scripts/start-all.ps1`（或直接双击 `scripts/start.bat`，效果相同）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
```

它会在后台（隐藏窗口）拉起 supervisor，轮询 `/api/health` 直到面板就绪，并按
`config.yaml` 的 `dashboard.open_browser` 决定是否打开浏览器。

| 想做的事 | 命令 |
| --- | --- |
| 双击启动 | 双击 `scripts/start.bat` |
| 命令行启动 | `powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1` |
| 启动但不弹浏览器 | 加 `-NoBrowser` |
| 停止（含 bot / OpenCode / NapCat） | `powershell -ExecutionPolicy Bypass -File scripts/stop.ps1` |
| 首次安装依赖 / 只跑 bot 调试 | `powershell -ExecutionPolicy Bypass -File scripts/start.ps1` |

`scripts/start.ps1` 与 supervisor 互斥：它会直接在前台运行 `python -m dafeiyu`，
**占用 8080 / 8788**。supervisor 在运行时执行它会端口冲突，先 `scripts/stop.ps1`
再跑。它只用于排查（能看前台日志、自动建 `.venv` 并装依赖）。

停止走面板 `/api/shutdown`，失效时自动 `taskkill /T /F` 结束进程树。
停止后 `data/supervisor.pid` 会被清掉，`start-all.ps1` 靠它判断“是不是已经在跑”。

只跑 bot 时，看到下面两行就说明链路通了：

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
- `/闭嘴`（或 `/shutup`）—— 本群静音 60 分钟（仅 `groups.admins` 可用）
- `/醒来`（或 `/wake`）—— 解除静音（仅 `groups.admins` 可用）

私聊：

- 直接说话即可（不受冷却、白名单、静默限制，仍受预算限制）

命令（群聊/私聊都可用，权限和冷却不同）：

| 命令 | 群聊 | 私聊 | 冷却 | 说明 |
| --- | --- | --- | --- | --- |
| `/help` `/帮助` | 所有人 | 所有人 | 5 秒 | 按当前身份只列出可用命令 |
| `/stats` `/统计` | 所有人 | 所有人 | 群 60 秒 / 私聊 5 秒 | 今日全局 + 当前会话调用/token；群内不显示费用 |
| `/status` `/状态` | 仅管理员 | 仅管理员 | 30 秒 | 网关、调度、配额、预算状态与运行时长 |
| `/budget` `/预算` | 仅管理员 | 仅管理员 | 30 秒 | 全局/月度/本会话预算与费用估算 |
| `/forget` | 仅管理员 | 所有人 | 无 | 清空当前会话全部记忆（消息、摘要、印象、用量与会话映射，不可撤销） |

群内非管理员执行管理员命令会被静默忽略（不回复、不写审计）；私聊非管理员会收到
「这个命令只有管理员能用。」。管理员命令成功后写 `command.admin` 审计（actor 为 QQ 号，
detail 只含会话类型/ID 与用户 ID，不含正文）。冷却按「群 + 群号」或「私聊 + 用户」分别计算。
命令同样受 `groups.blacklist` / `groups.whitelist` 约束：黑名单群静默；whitelist 非空时
只有名单内的群能使用命令。

命令行（服务不用停）：

```powershell
.venv\Scripts\dafeiyu-cli.exe stats  --chat 123456789 --type group
.venv\Scripts\dafeiyu-cli.exe forget --chat 123456789 --type group
.venv\Scripts\dafeiyu-cli.exe summary --chat 123456789 --date 2026-10-23 --type group
.venv\Scripts\dafeiyu-cli.exe forget --all
```

> `--type` 默认 `group`。群号和 QQ 号可能数字相同，所以必须区分。

## 5. 控制台面板

`scripts/start-all.ps1` 启动后，supervisor 会打印并打开面板地址（端口在 `config.yaml` 的
`supervisor.dashboard.port`，默认 8787）。**面板默认监听 `0.0.0.0`，局域网内可直接访问；
Bot 控制口（8788）与 OneBot 反向 WS（8080）仍只监听本机。**

### 局域网 / VPN 访问与登录

`supervisor.dashboard.host` 默认 `0.0.0.0`，配合 `auth: auto` 会强制登录：

1. 用户名默认 `admin`；
2. 密码优先取 `.env` 的 `DAFEIYU_DASHBOARD_PASSWORD`（至少 12 位）；
3. 没配密码时 supervisor 启动会**自动生成**一个 32 位口令写入 `data/dashboard.password`
   （Windows 下用 `icacls` 收紧 ACL），启动日志会打出该路径；
4. 只在本机用：把 `host` 改回 `127.0.0.1`，`auto` 就不再要求登录。

`supervisor.dashboard.auth` 三档：

- `auto`（默认）：绑定 loopback 时免登录，绑定 `0.0.0.0` / 局域网地址时强制登录；
- `on`：始终登录，即使只绑本机；
- `off`：只允许绑定 loopback，非 loopback 会拒绝启动。

登录成功后使用 HttpOnly、SameSite=Strict 的签名 Cookie，进程重启后旧会话失效；同一 IP
连续 5 次密码错误会锁定 5 分钟。面板右上角有「退出登录」，会话过期或未登录时页面会跳到登录页。

> 本项目不内置 TLS。外网访问请走 WireGuard/Tailscale 等 VPN，或在前面放 Caddy/Nginx 做
> HTTPS 反代；否则 HTTP 下的密码和 Cookie 可能被同网段嗅探。反代时记得转发
> `X-Forwarded-Proto: https`，这样会话 Cookie 会自动带 `Secure`。

面板上的卡片：

- **进程 / 大脑 / 登录二维码**：查看 OpenCode、Bot、NapCat 状态，切换模型，扫码登录。
- **记忆**：查看按日摘要与群印象，可按群聊/私聊筛选；填会话 ID 可手动生成摘要、抽取印象。
  每条印象可编辑内容/置信度/类型、查看来源消息摘录，勾选多条后可合并；编辑与合并都会同步更新搜索索引。
  卡片底部可按「会话类型 + 会话 ID + 用户 QQ」删除该用户的原始消息（需输入 `DELETE_USER`），
  摘要与印象等聚合数据会保留。
- **用量**：最近 7/14/30 天的调用与 token 柱状图、Top 会话、最近错误。
- **预算**：全局今日、月度、当前会话的调用/token/费用估算与预警状态；只读，修改请改
  `config.yaml` 后重启。
- **消息搜索**：跨会话按关键字搜索，结果分页加载并显示命中摘录；3 字以上中文走 trigram FTS，1–2 字自动回落 LIKE。
  命中项可点「定位」打开对应会话时间线并高亮该消息。
- **会话管理**：按最近活跃排序，分页加载。每行可以：
  - **查看时间线**：只读抽屉展示消息流、会话摘要、按日摘要、印象和当日用量；支持「加载更早」「回到最新」；
  - **新建会话**：重置 OpenCode session 映射与会话内摘要；消息、按日摘要、印象、用量都保留；
  - **软忘记**：删本会话的消息和 session 映射，保留按日摘要与印象；
  - **彻底删除**：删远端 session + 本地消息、摘要、印象、用量，不可恢复；
  - **导出 MD / JSON**：导出单会话记忆。
- **触发规则调试器**：输入一条模拟消息，只读展示 rules / 批次模式下的判定路径、硬触发来源、
  预算 / 静默 / 冷却 / 采样指标和最终是否会进入模型。不会写消息、调模型、消耗预算或改调度器。
- **日志**：按 supervisor / opencode / bot / napcat 分来源实时查看，支持级别与关键字筛选、
  暂停/继续、加载行数切换、清屏和下载当前日志文件。清屏只影响页面视图，不删除磁盘日志。
- **健康 / 告警 / 备份**：聚合 bot、OpenCode、NapCat、网关、磁盘与备份状态；查看告警事件并发送
  测试告警；手动备份、下载、删除、上传恢复，以及最近的操作审计。
- **安全 / 口令轮换**：控制口令保存在 `data/supervisor.token`，启动时自动收紧 ACL
  （Windows `icacls`、POSIX `0600`，失败只告警不阻断启动）。输入 `ROTATE` 后生成新口令，
  同步更新 bot 环境变量与健康探测，并重启 bot 一次；任一步失败会回滚旧口令、尽力重新启动
  bot 并写审计。轮换完成后旧口令返回 401，新口令返回 200。
- **运行配置**：只读展示当前模型、人设、冷却、限流、预算、记忆窗口与保留天数。
  要改配置请编辑 `config.yaml` 后重启；面板提供「重启 Bot」和「下载备份」。

### 运维 runbook（健康 / 告警 / 备份 / 恢复 / 审计）

**健康**：`GET /api/health/detail`（需控制口令）返回 `ok`、三个进程状态、bot 的网关/额度/错误
统计、NapCat 登录状态、磁盘可用率和备份状态。`ok=false` 时先看哪个子项是 `failed` 或
`connected=false`；健康探测失败只降级字段，不会打断 supervisor。面板健康卡每 10 秒刷新，
页面隐藏时暂停。

**告警**：默认关闭。要启用，把 `alerts.enabled` 改成 `true`，并按需填
`alerts.webhook.url`（`format`: generic / ntfy / wecom / dingtalk）和 `alerts.qq_admin`
（通过 bot 私聊管理员；`bot_down` 不会走这条通道）。规则阈值在 `alerts.rules`：
进程掉线分别等待 120/180/120 秒，磁盘低于 10%，回复错误 15 分钟内 3 次，额度熔断，
以及备份失败。同一告警默认 6 小时重复一次（预算预警为 24 小时），恢复需要连续两次巡检确认；单轮超过 5 条会合并
成一条 `alert_flood`。状态存 `data/alert-state.json`，删掉它等于清空未恢复事件。
面板「测试告警」只发送一条 `test` 消息，不写入 firing 状态。

**备份**：默认开启，只写本地 `backup.dir`（默认 `data/backups`）。每天 `schedule_hour`
（默认 04:00）后的第一次巡检生成一份 `scheduled` 备份；每天保留 `daily_keep` 份，周日那批
额外按 `weekly_keep` 周保留，总量超过 `max_total_gb` 时从最旧的 scheduled 开始删，至少留 3 份。
手动备份和恢复前备份不会被自动清理，需要手工删。每次备份会校验 SQLite 完整性、`user_version`
和必需表，并写同名 `.json` manifest（大小、sha256、schema 版本）。备份失败只记日志和
`backups.last_error_at`，由告警规则接手，不影响 bot。

**恢复**：面板上传 `.db` → 校验文件 → 自动生成 `pre-restore` 备份 → 停 bot → 替换
`data/dafeiyu.db` 并清掉 `-wal`/`-shm` → 启动 bot → 等 `/api/health/detail` 返回 `ok=true`。
上传前必须输入 `RESTORE` 确认，最大 `backup.max_upload_mb`。任一步失败会自动停 bot、用
pre-restore 备份回滚并写审计；回滚失败会留下 pre-restore 文件供手工恢复，按审计里的
`backup.restore` 记录处理。**恢复只覆盖 `data/dafeiyu.db`，不恢复 OpenCode 自己的数据库**
（`data/opencode-bot.db`），也不恢复 `.env`、NapCat 票据和人设文件。

**安全与口令**：控制口令由 supervisor 首次启动生成到 `data/supervisor.token`，并尽量收紧为
仅当前用户可读写；加固失败只写 warning。bot 与 supervisor 通过 `X-Control-Token` 校验，
dashboard 每次请求动态读取当前口令。轮换必须输入 `ROTATE`；成功后更新 bot 环境变量、
健康探测口令并重启 bot 一次，失败会回滚内存、磁盘和环境变量，旧口令继续可用，并尽力
重新启动 bot。
不要手工编辑 token 文件；需要更换时用面板轮换或删除该文件后重启（会重新生成）。
破坏性操作各有独立确认串：轮换 `ROTATE`、备份恢复 `RESTORE`、会话删除 `DELETE`、
按用户删除 `DELETE_USER`；缺失或拼错一律 400，不会执行。

**日志与脱敏**：`LogHub.write` 与 `AuditLog.write` 共用 `supervisor/redact.py` 的规则。
`Set-Cookie`、`Authorization`、`X-Control-Token` 整行替换为 `头名: ***`；`Bearer` 令牌、
`key: value` / `key=value` / 环境变量赋值 / JSON 字段中的 `api_key`、`password`、`passwd`、
`secret`、`private_key`、`access_key`、`token` 等只保留键名、值替换为 `***`。
中文「token 用量」「token 上限」不含分隔符，不会被误伤。脱敏是字符串规则，不解析任意
自由文本语义；如果把密钥写进不存在键名的散文里，仍可能漏过，日志里不要手写密钥。

**审计**：append-only JSONL，默认 `data/audit.jsonl`，超过 `audit.max_bytes` 轮转为 `.1/.2/.3`。
bot 与 supervisor 会写同一文件，写入和轮转通过 `.audit.jsonl.lock` 跨进程串行，避免并发轮转
覆盖归档。
记录登录/登出、告警触发与恢复、备份创建/删除、恢复结果、进程启停、shutdown、口令轮换
（`token.rotate`）、管理员命令（`command.admin`）和按用户删除（`memory.user_delete`）。
审计 detail 只写结构化元数据，不记录消息正文或昵称；写入失败只记 supervisor 日志，
不阻塞操作。面板审计列表按时间倒序，受限 `limit` 1–200；每行可展开查看格式化后的 detail。

**收尾检查**：关掉 supervisor 后确认 `data/` 下没有 `.tmp` 残留；`git status --short`
应只剩源码/文档改动，`data/`、`logs/`、`.tamako/` 都不应出现在版本库。

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
| 回得很慢 | `DEEPSEEK_MODEL=deepseek-flash` 比 `deepseek-reasoner` 快很多；检查系统代理，httpx 已设 `trust_env=False` |
| HTTP 429 | 触发限流：降低 `sample_rate`、调小 `budget.daily_calls`，或给账户充值 |
| 记忆面板不更新、提示「配额暂停中」 | 账号级熔断：10 分钟内累计 3 次 429 或 provider 报 quota 后，摘要/印象暂停 1 小时，面板显示恢复时间；日志搜「429」可见触发原因 |
| 回复被截断 | 这是设计行为：闲聊 120 字、技术问题 300 字，改 `config.yaml` 即可 |
| 中文日志乱码 | 用 `scripts/start.bat` 或 `scripts/start-all.ps1` 启动（已设 UTF-8），不要直接双击 python |
| 启动后没反应/端口占用 | 先 `scripts/stop.ps1` 停掉旧实例；`start.ps1` 不能和 supervisor 同时跑 |

## 8. 隐私与风险

- 消息原文保存在本地 `data/dafeiyu.db`，不会上传到除 DeepSeek API 之外的任何地方。
- **按用户删除**：面板「记忆」卡片底部选择会话类型、填会话 ID 和用户 QQ，再输入
  `DELETE_USER`。它只删除该用户在该会话的原始消息，以及这些消息对应的
  `delivered_messages` 投递记录；其他用户的消息、按日摘要、印象和会话摘要都会保留。
  删除不可撤销，并写 `memory.user_delete` 审计（只记数量与会话/用户 ID，不记正文）。
  因为摘要和印象是聚合数据，仍可能保留该用户信息的痕迹；需要更彻底地重建整个会话时，
  用 `/forget`（群内管理员）或下面的 CLI。
- **整会话删除**：`.venv\Scripts\dafeiyu-cli.exe forget --chat <群号>`；私聊需加
  `--type private`。这会清空该会话的消息、摘要、印象、用量与会话映射，请先确认范围。
- 删除会移除数据库行，但不做介质级擦除；SQLite 的自由页、WAL 或操作系统缓存中可能短时间保留旧字节。
  对介质级清除有要求时，应在停机后对 `data/dafeiyu.db` 做安全擦除或重建。
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
