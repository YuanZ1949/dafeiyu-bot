# 溟月 / 大肥鱼 · QQ 群聊机器人

把 DeepSeek 拟人化的「大肥鱼」接进 QQ 群：会接梗、会摸鱼、会记仇（划掉）会记事，@ 她必回，不 @ 时偶尔自己插话。

- **模型**：DeepSeek（OpenAI 兼容接口），模型名可配
- **QQ 接入**：NapCat（OneBot v11 反向 WebSocket）
- **微信（可选，默认关闭）**：桌面微信**小号**通道（UIA 桥 `wechat_bridge` + `pyweixin`）；只支持普通微信群，需要**桌面解锁**且保持交互的会话，RPA 有已知上限（见「3.5 接入微信」）
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

   > `.env` / `config.yaml` 是纯文本，但 Windows 默认没有关联程序，双击没反应属正常：
   > 用 `notepad .env` 或编辑器打开，**保存为 UTF-8**（用 GBK 或有损编码保存会让凭据解析为空）。
   >
   > 默认档位是 `brain.provider=opencode` + `mode=free`：依赖 opencode 侧有可用免费模型。
   > 不可用时在面板切「自备 Key」并填 `DEEPSEEK_API_KEY`，或切「本地模型」并先启动 LM Studio
   > （默认 `http://127.0.0.1:1234/v1`）。
   >
   > 面板账号固定 `admin`，口令优先取 `.env` 的 `DAFEIYU_DASHBOARD_PASSWORD`；没配时 supervisor
   > 启动会自动生成到 `data\dashboard.password`，启动日志会打印路径。

4. **启动**：双击 `scripts\start.bat`，或

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
   ```

   面板默认监听 `0.0.0.0:8787`（局域网可访问）；没配口令时会自动生成到 `data\dashboard.password`。

随时自检：

```powershell
.venv\Scripts\python -m dafeiyu.cli doctor
```

doctor 不只看配置/依赖/端口，还会检查 `.env` / `config.yaml` 是否为 UTF-8、PowerShell 脚本是否带 UTF-8 BOM，
以及当前 Python 是否被沙箱降权成 Low 完整性（写 `%TEMP%` 失败、写用户目录 EPERM 的元凶）。
有阻塞项时按每条 `→` 修；先跑 doctor，再怀疑杀软/网络。

`config.yaml` 是**本机配置，不进版本库**；模板是 `config.example.yaml`，要调行为就改前者。

## 架构

```
微信群
  │  @ / 普通消息（桥轮询，默认 8s）
  ▼
wechat_bridge（桌面微信 UIA 桥，独立进程，127.0.0.1:8790，token 鉴权）
  │  本地 HTTP 帧
  ▼
WeChatAdapter ────────┐
                      ├─▶ Router（三层闸门，按通道独立急停）
QQAdapter ────────────┘
  ▲                        │  ① 硬触发：@ / 引用 / 叫名字 → 必回
  │  OneBot v11 反向 WS    │  ② 规则筛：白名单、冷却、静默、预算、关键词、热度、采样
NapCat（QQ 协议端，独立进程）│  ③ 模型判断：一次调用同时决定「说不说 + 说什么」
                                     ▼
                            Context（20 条窗口 + 印象 top5 + 昨日摘要，预算 2500 token）
                                     ▼
                            DeepSeek API（JSON：{speak, reply, reason}）
                                     ▼
                            Sender（截断 120 字 / 技术问题 300 字，超长分片）
                                     ▼
                            各通道适配器回群：QQ 走 send_group_msg，微信由桥代发
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
| 微信小号 + 桌面微信 4.1.6+ | 可选；仅当 `gateway.provider` 为 `wechat` / `both`。**小号专用**，封号风险自担 |
| 解锁且交互的桌面会话 | 可选；微信通道要求**桌面解锁**，锁屏/远程断开即掉线，不能跑成服务（Session 0） |

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

先确认版本（只打印版本号，不加载配置、不连网）：

```powershell
.venv\Scripts\python -m dafeiyu --version
# 用 [project.scripts] 安装后也可：dafeiyu --version
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

用面板切到 `local_model` 档时，若 1234 端口没起会自动用 `lms` CLI 拉起；切走后自动停止，释放显存/内存。不想让它自动停（比如还要用 LM Studio 干别的）就把 `config.yaml` 里的 `local_model.stop_when_unused` 设为 `false`。

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
| `persona.repeat_guard` | `true` | 重复守卫：同一句话在最近 50 条自己的发言里出现过，就先去重复开头，再换一句中性短句，仍重复则这一片不发；不重调模型 |
| `persona.opportunity_guard` | `true` | 机会检测（规则版）：纯热聊/纯表情批次不送模型，消息攒在待投队列，等点名或问句时一起进 prompt；待投接近 20 条时照旧送审 |
| `persona.quote_reply` | `true` | 引用回复：被点名/被引用时把首片回复挂在原消息下；`false` 时完全走纯文本发送。**只对 QQ 生效**：微信出站没有引用原语（桥只有 `send_text`/`send_at`/`send_media`），即使打开也只发纯文本，见 `docs/message-types.md` |
| `persona.prompt_file` | `""` | 自定义人设文件路径；留空使用内置 `brain/persona.md`。改完重启生效 |
| `triggers.cooldown_seconds` | `60` | 同一群两次**非提问**发言的最小间隔（明确提问、@/引用/叫名字不受限） |
| `triggers.sample_rate` | `0.05` | 普通消息被纳入模型判断的采样率 |
| `triggers.thread_idle_seconds` | `300` | 机器人说完后这么久没人接，算话题结束 |
| `triggers.thread_closed_seconds` | `600` | 收尾窗口长度；窗口只用于 prompt 局势提示与面板观测，**不再拦截消息**（接不接由模型判断），被点名立刻重开 |
| `triggers.closed_sample_rate` | `0.05` | **已废弃**：收尾不再抽样放行，字段只为兼容老配置保留，运行链路不读取 |
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
| `memory.scene_injection` | `true` | 注入「此刻」：现在几点、本群叫什么、距上次开口多久、刚才是谁在说；关掉后 prompt 里不再有这行 |
| `memory.inject_episodes` | 示例/本机 `true`，代码 `false` | 注入「正在聊的事」（`episodes` 最新一条 open 的一句话摘要），数据由 Observer 写。示例配置与本机 `config.yaml` 默认 `true`（已打开）；`InjectionConfig` 代码默认 `false`，缺键的旧配置行为不变（仍不注入） |
| `memory.inject_open_loops` | 示例/本机 `true`，代码 `false` | 注入「没有下文的事」（谁问了没人接、谁答应了没做）。示例配置与本机 `config.yaml` 默认 `true`（已打开）；`InjectionConfig` 代码默认 `false`，缺键的旧配置行为不变（仍不注入） |
| `memory.open_loop_topk` | `3` | 「没有下文的事」最多注入几条 |
| `memory.open_loop_chars` | `200` | 「没有下文的事」段落的字符预算 |
| `memory.episode_chars` | `220` | 「正在聊的事」段落的字符预算 |
| `memory.mind_chars` | `1000` | 「正在聊的事 + 没有下文的事 + 你和他 + 本鱼的近况 + 我的立场 + 群友印象」六段合计预算（默认 1000，装得下六段上限 990，只数数据体不数标题）；超了按印象→立场→近况→关系卡→未了事整段丢弃，正在聊的事永不主动丢、只在最后截断 |
| `memory.state_chars` | `100` | 「本鱼的近况」段落的字符预算 |
| `memory.relation_chars` | `160` | 「你和他」关系卡段落的字符预算 |
| `memory.stance_topk` | `3` | 「我的立场」最多注入几条（跨群共享，按 confidence 排序） |
| `memory.stance_chars` | `160` | 「我的立场」段落的字符预算 |
| `memory.atmosphere_enabled` | `false` | 把最近 10 分钟的消息量/人数/问句比例拼进「此刻」（一行，不参与接话判定） |
| `mind.enabled` | `false` | 心智层总闸；关闭即回到旧行为（Observer/自省/关系卡/状态/立场注入全部不启用） |
| `mind.state_enabled` | `true` | 总闸打开时是否注入 `bot_state`（在意的事 + 心情） |
| `mind.episodes_enabled` | `true` | 总闸打开时是否启用 episode/open_loop 的观察与写入 |
| `mind.observer_enabled` | `false` | 是否启用 Observer 旁路（需要能调模型） |
| `mind.observer_provider` | `local` | Observer/Reflector 用哪个端点：`local` 只用本地模型，`main` 跟随当前大脑 |
| `mind.observer_interval_seconds` | `600` | Observer 扫描间隔 |
| `mind.observer_min_messages` | `8` | 单群至少累积多少条新消息才观察一次 |
| `mind.observer_daily_calls` | `24` | 单群每天「观察 + 自省」总调用上限，与回复预算分开 |
| `mind.reflection_hour` | `5` | 每天几点之后跑夜间自省（0-23，上海时区） |
| `mind.stances_enabled` | `true` | 总闸打开时是否注入「我的立场」（Reflector 每晚写入，同一话题覆盖旧观点） |
| `mind.inbound_media_reactions` | `false` | 群里的语音/图片到达时先贴个表情回应（旁路，失败只记日志） |
| `mind.inbound_media_reaction_cooldown_seconds` | `600` | 同一会话两次入站反应的最小间隔（秒） |
| `mind.proactive_followup` | `false` | 承诺跟进：只对 `kind=promise` 且超过 `followup_after_hours` 的未了事主动追问一句；默认关，开启后仍受静音与预算门控，不调用模型 |
| `mind.followup_after_hours` | `24` | promise 类未了事创建后多少小时才允许跟进 |
| `mind.followup_daily_cap` | `3` | 同一会话每日承诺跟进上限（按上海日期计） |
| `behavior.chat_intensity` | `normal` | 聊天强度：`quiet` 只回点名、`normal` 当前行为、`active` 放开连续空转的降频闸门（话题收尾已不拦截，两档一致） |
| `behavior.restriction_mode` | `off` | 言论限制：`off` 关闭、`soft` 只注入提示、`strict` 命中关键词的整条回复丢弃、`regenerate` 先让模型改写（每批最多 2 次），仍命中则删掉含禁句的整句 |
| `behavior.banned_phrases` | `[]` | 禁词（关键词级子串匹配，非语义审核） |
| `behavior.banned_topics` | `[]` | 禁话题（注入提示；strict 模式也做关键词拦截） |
| `social.poke_enabled` | 示例 `true`，代码 `false` | 被戳一戳时用固定短句回应；仍受静音、聊天强度、预算、冷却与每日上限共同门控，不调用模型。示例配置已打开，代码默认关（旧配置行为不变） |
| `social.poke_cooldown_seconds` | `300` | 同一会话两次戳一戳回应的最小间隔（秒） |
| `social.poke_daily_cap` | `20` | 同一会话每日戳一戳回应上限（按上海日期计） |
| `social.join_welcome_enabled` | `false` | 进群欢迎开关；默认关闭，模板为空时即使打开也不发送 |
| `social.join_welcome` | `""` | 进群欢迎模板，仅支持 `{user_id}` 占位符；空串 = 不发送 |
| `social.join_welcome_daily_cap` | `10` | 同一会话每日进群欢迎上限（按上海日期计） |
| `gateway.wechat.media.enabled` | `false` | 微信入站图片落盘总开关；关闭时只保留 `[图片]` 占位，不读任何本地文件 |
| `gateway.wechat.media.max_bytes` | `10485760` | 微信入站图片解密后的单张大小上限（10 MiB）；超限保留占位 |
| `gateway.wechat.media.storage_root` | `""` | 微信本地媒体根目录；空 = 自动探测 `xwechat_files`，只接受该目录内的文件 |
| `images.enabled` | `true` | QQ 图片落盘开关：落盘到 `workspace.dir/images/` 并把路径写进正文；关闭时只保留 `[图片]` 占位。同时门控 QQ 语音/视频的 URL 下载 |
| `images.max_bytes` | `10485760` | 单张图片下载/落盘大小上限（10 MiB）；超限或下载失败保留 `[图片]` 占位 |
| `images.timeout_seconds` | `8` | 单张图片下载/解码总超时（秒）；超时保留 `[图片]` 占位 |
| `images.retention_days` | `7` | `images/` 落盘图片保留天数，每次物化前惰性清理超期文件；`0` = 永久保留 |
| `images.max_edge` | `1280` | 入站图片最长边上限：超过则用 ffmpeg 等比缩小（JPEG 显式 `-q:v 3`；缺 ffmpeg 时保留原图）；`0` = 不降采样；GIF 不处理（保动画） |
| `backup.mirror_dir` | `""` | 备份镜像目录：主备份成功后把 `.db`、同名 manifest 和 workspace zip 再复制一份过去；空 = 关闭，与 `backup.dir` 相同则跳过。镜像失败只写独立的镜像错误字段，不影响主备份；相对路径按仓库根目录解析 |
| `alerts.rules.backup_stale_hours` | `25` | 距上次成功备份超过这么多小时（或从未成功过）就报「备份过期」；`backup.enabled=false` 的部署豁免，不报 |
| `alerts.rules.memory_gap_days` | `2` | `memory.pending_count`（还没生成摘要的天数）达到这个值就报「记忆落后」；额度熔断期间不报 |

出站语音的 `speech.tts_enabled` / `speech.tts_voice` / `speech.tts_rate` / `speech.tts_max_chars` 四个键同属 `speech:` 段，见上文「3.6 语音回复」，**没有**独立的 `tts:` 配置节。`reminders` 只是 SQLite 里的表名（`/remind` 的存储，见 §4「用法」），**没有** `reminders:` 配置节。

预算分四层：每群每日、全局每日、自然月、月度费用。所有 `0` 都表示「未启用/不限制」。
`degrade` 会保留 @/引用/叫名字的回复，只停普通插话；`stop` 连点名也不回；`notify`
只发告警不拦截。月度费用达到上限即硬拒绝，即使 `on_exceed=notify` 也不会放行。
费用按「token 数 × 单价」估算，不是账单金额；要准确成本请以服务商账单为准。
`warning_ratios` 取值 `(0, 1]`；全局日/月度/费用命中 50%/80%/100% 时各告警一次，
由 supervisor 做 24 小时去重，告警 detail 只包含计数与限额，不包含消息正文。
当前会话的 per-chat 额度在 `/budget` 和面板预算卡显示；单群预警不进入 supervisor
告警通道。`alerts.rules.budget_warning` 可单独关闭预算预警。

Observer（`mind.observer_enabled`）默认关闭，打开它需要同时满足 `mind.enabled: true`、
`mind.episodes_enabled: true`，且当前大脑提供 `complete_detailed`（面板的 `effective`
字段把这三个条件一起算进去）。成本按「`mind.observer_daily_calls` × 白名单群数」粗估；
白名单为空时面板显示「群白名单为空，暂不估算」，不会把 0 当成免费。

Reflector（夜间自省，`mind.reflection_hour` 之后每个活跃群一次）在 `mind.enabled: true`
时运行，产物是 `bot_state`（在意的事/心情）、跨群 `bot_stances`（话题 → 观点，同一话题
覆盖）、该群笔记的 lessons 和全局 `MEMORY.md` 的 life。「我的立场」只在总闸打开时注入，
关掉只停止注入，已写入的立场保留；`/forget` 清空当前会话不影响全局立场。

主动链路（戳一戳回应 `social.poke_*`、进群欢迎 `social.join_welcome*`、承诺跟进
`mind.proactive_followup`）默认全部关闭（示例配置只为戳一戳打开，代码默认仍为关），
全部受 `triggers.quiet_hours` 静默时段与预算门控，且**不调用模型**：戳一戳用固定短句池、
进群欢迎用固定模板、承诺跟进只对已写库的 promise 未了事发一句话。`group_ban` / `group_decrease`
只写 info 日志，不发送。定时提醒由 `/remind` 写入 `reminders` 表、后台轮询 worker 发送，
同样不调用模型。

各通道支持哪些消息类型、失败时怎么降级，以及附件的路径/大小/后缀边界，见
[docs/message-types.md](docs/message-types.md)。

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

### 更新部署到 release 目录

> 当前线上实例是**直接从本仓库运行**的（`scripts\start-all.ps1` 拉起 supervisor 及其子进程），
> 旧的独立副本 `C:\MyFile\AICoding\dafeiyu-release` 已于 2026-10-31 归档到
> `_archive\dafeiyu-release-runtime-20261031`。日常改完代码按下面「从 git 升级 / 停启」走即可，
> `deploy.ps1` 留给「另建一份 release 副本（例如部署到别的机器）」的场景。

改了代码后要更新正在跑的 release（例如 `C:\MyFile\AICoding\dafeiyu-release`）：

```powershell
# 只列出将拷贝哪些文件
powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1 -Plan

# 备份 -> 停服 -> 拷贝 -> SHA256 校验 -> 目标目录体检 -> 启动 -> 健康检查
powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1 -Target D:\dafeiyu-release
```

部署脚本只覆盖发布白名单（`src/scripts/.opencode` 与示例配置等），**从不碰**目标目录的
`config.yaml`、`.env`、`data/`、`logs/`、`vendor/`、`.venv`。SHA256 校验后会先在目标目录跑
`dafeiyu doctor`（配置/编码/沙箱降权/脚本 BOM），有阻塞项就直接回滚，不会把服务停在半套代码上；
健康检查以 `gateway.connected=true`（QQ 网关真的连上）为准，不看 HTTP 200。扫码/网关未连期间
可用 `-AllowDisconnected` 显式放行，只拷文件不重启用 `-NoRestart`。

### 从 git 升级

直接跑更新脚本（先 `-DryRun` 看计划）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/update.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts/update.ps1
```

脚本按 `git fetch --all --prune` → 显示 ahead/behind → `git pull --ff-only` →
`.venv\Scripts\python -m pip install -e ".[dev]"` 的顺序执行；默认用当前分支，也可
`-Branch main`，只想刷新依赖时加 `-NoPull`。任何一步失败都会立即退出非 0 并给出
下一步提示；它**不会**动 `data/`、`config.yaml`、`.env`、`vendor/`，也**不会**自动
重启服务——完成后按提示先 `scripts\stop.ps1` 再 `scripts\start-all.ps1`。

## 3.5 接入微信（可选，默认关闭）

**默认路径**：不开启微信就什么都不用做——`gateway.provider: qq`、`gateway.wechat.enabled: false`（模板默认值），QQ 侧行为与之前完全一致；本节可以整个跳过。

先认清边界：这里接的是**普通微信群**，不支持企业微信、公众号，也不支持小程序/视频号私信。实现不是官方协议，而是桌面客户端 RPA（UIA 自动化）：一个独立小进程 `wechat_bridge` 替代人手操作真实微信窗口。

### 开通前必读

- **小号专用**：必须让一张单独的**微信小号**登录桌面客户端（4.1.6+，中文界面），**不要用主号**；封号风险由使用者自担。
- **桌面解锁**：RPA 只能在**已解锁且可交互的桌面会话**里工作。锁屏、切换用户、远程桌面断开都会让桥掉线；因此不能作为 Windows 服务运行，也不能跑在 Session 0。

### 安装与配置

1. 安装依赖：`pip install -e ".[wechat]"`。extra 实际安装 `pywechat127` / `pywinauto` / `pyautogui` / `psutil`（代码里 import 名是 `pyweixin`，由 `pywechat127` 提供）。

2. 配置 `.env`：设 `DAFEIYU_WECHAT_BRIDGE_TOKEN`。桥绑非回环地址时必填；留空 = 不校验 token，仅限桥只听本机回环的场景。

3. 配置 `config.yaml`：
   - `gateway.provider`: `wechat`（仅微信）或 `both`（QQ + 微信双通道）；
   - `gateway.wechat.enabled: true`；
   - `account_nickname`：小号在目标群里的显示昵称，用于识别 @；
   - `group_allowlist`：**已删除**。群范围只看面板白名单（`data/settings.local.yaml` 的
     `groups.whitelist`，条目用桥铸造的 `wxg:<别名>`）；白名单为空 = 不限制，桥按本地库
     能枚举到的全部群轮询。面板读不出来时 fail-closed（不轮询、不发送）。

4. 预检：`.venv\Scripts\python -m dafeiyu.cli doctor` 有 8 项微信前置检查（进程 / 版本 / 语言 / 完整性 / 桌面 / pyweixin / 端口 / 配置），**全部通过再进入下一步**。

### 入站加速：直接读本地消息库（可选）

默认入站是 UIA 开窗抓屏，慢且会被锁屏 / 前台占用卡住。打开本地库模式后，入站改为**只读**微信自己的 SQLite：

```yaml
gateway:
  wechat:
    local_db_enabled: true
    local_db_root: ""          # 空 = 自动探测 %USERPROFILE%\Documents\xwechat_files\*\db_storage
    local_db_refresh_seconds: 1.0
```

- 原理：用 `OpenProcess(PROCESS_VM_READ)` + `VirtualQueryEx` + `ReadProcessMemory` 只读扫描 `Weixin.exe` 内存，从 WCDB 的 `com.Tencent.WCDB.Config.Cipher` 对象里取出 XOR 混淆的密钥（老版本回退到明文 `X'<hex>'` 字面量），再用 page1 的 HMAC 校验筛选；密钥缓存到 `data/wechat-bridge/localdb/keys.json`（`data/` 已在 `.gitignore` 里）。
- 消息库按页增量解密到 `data/wechat-bridge/localdb/`（含 WAL 重放），只读句柄查询；**微信原文件一个字节都不写**。
- 默认发送仍走 UIA（输入框键入 + 回车），本模式只影响入站；锁屏也要发见下一节。
- 任何一步失败（微信没登录 / 提不到密钥 / 库被占用 / 镜像损坏）都只在日志里 warning，并**自动回落 UIA**，60 秒后重试，入站不会停。
- 首次启用会扫一次进程内存（实测约 3–5 秒，日志 `内存扫描匹配 N/N 个数据库密钥`）。

风险：读进程内存与解密本地库属于非公开接口，可能违反腾讯的软件许可条款；微信升级后密钥格式可能变化导致失效（此时自动回落 UIA）。镜像目录里是**明文**聊天记录，注意机器与备份的访问权限。

### 出站后端：UIA / hook / auto（可选）

默认 `outbound: uia`，行为与之前一致（锁屏不可发）。若已在本机跑了一个能注入微信的 HTTP 服务，可让出站不再依赖桌面：

```yaml
gateway:
  wechat:
    outbound: hook            # uia（默认）/ hook / auto
    hook_url: ""              # outbound=hook/auto 时必填；http:// 或 https://
    hook_timeout_seconds: 5.0
    local_db_enabled: true    # outbound=hook 时必开：群显示名要靠本地库解析成 xxx@chatroom
    send_verify: true         # 发送后回读本地库，把 typed 升级成 observed（需 local_db_enabled）
    send_verify_timeout_seconds: 8.0
    send_verify_interval_seconds: 0.5
```

- `hook`：桥向 `hook_url` POST `{"action":"send_text","chatroom":"<xxx@chatroom>","chat_name":"<群显示名>","text":"<正文>"}`；群 `chatroom` 由本地库把显示名解析成 `xxx@chatroom`（因此 `outbound: hook` 要求 `local_db_enabled: true`，启动时校验），解析不到就明确回 `chat_name_unresolved`，**绝不猜 id**。响应 `{"ok":true}` 只算 `typed`。
- `auto`：先 hook，只有**可证明没发出去**（连接被拒 / 后端明确 `sent:false` / 群名解析失败）才回落 UIA。HTTP 超时、5xx、响应损坏属于「结果不明」，**不回落**——否则可能发两次。
- 回执校验：成功发出后轮询本地库找「自己刚发的那条」，找到升级为 `observed`；窗口内没找到只把回执降级为 `typed` + `receipt_unverified`，**不会返回失败**（失败会让客户端换新 id 重发，协议 §7.6）。
- `outbound=hook/auto` 时客户端不再要求桌面可交互，锁屏也能下发指令；真正的失败由桥回执里的 `error_code` 表达。

hook 端的具体适配（哪个注入库、支持哪个微信版本）不在本项目内，桥只按上面的请求/响应契约说话。

### 无 hook 的替代：RDP + `tscon` 保控制台解锁（可选）

不想接注入服务，又必须走 UIA 出站时，可以「RDP 连进去，再把会话交回物理控制台」：

1. 从另一台机器用 RDP 登录机器人这台机器（同一个 Windows 账号）；
2. **优先用带 `-Elevate` 的脚本做交接**（它会弹一次 UAC，提权进程跑完 `tscon` 立即退出，不在桌面上留下任何窗口）；
3. 会话交回物理控制台后，**不会重新锁屏**，UIA 继续可用；**当前 RDP 连接会立即断开**，这是预期行为；
4. 断开后桌面上就是解锁状态；`python -m dafeiyu.cli doctor` 的 `wechat.desktop_interactive` 应为 PASS（在 RDP 会话里这条会提示先做交接）。

```powershell
# 推荐：包装脚本 + -Elevate。-Elevate 起一个「跑完即退」的提权进程，不留前台窗口
powershell -ExecutionPolicy Bypass -File scripts\rdp-console.ps1 -Action handoff -Elevate

# 只读：会话名 / Session ID / 协议（控制台 / RDP）/ 连接状态 / 是否管理员 / 建议动作
powershell -ExecutionPolicy Bypass -File scripts\rdp-console.ps1 -Action status

# 等价 CLI（需要在管理员终端里跑；见下面「为什么不要手动开管理员终端」）
.venv\Scripts\python -m dafeiyu.rdp status
.venv\Scripts\python -m dafeiyu.rdp plan         # 只打印将要执行的命令，不执行
.venv\Scripts\python -m dafeiyu.rdp handoff      # 执行交接（需要管理员）

# 从任意管理员会话操作指定 session（默认只操作自己所在会话）
.venv\Scripts\python -m dafeiyu.rdp status --session 1
```

- 探测优先读 `WTSClientProtocolType`（0=控制台 / 1=ICA / 2=RDP）和 `WTSConnectState`，读不到再回退 `SESSIONNAME` 前缀；两者都读不到一律按「不是 RDP」保守处理，不会乱执行。
- `tscon` 需要管理员权限；非管理员会明确回 `needs_admin`（CLI 退出码 3），不会静默失败。执行成功但 WTS 仍读不到状态时只提示「未能确认」，不谎报成功。
- **会话掉线、机器停在登录界面的情况**：如果目标会话是「已断开且没有会话名」，`plan` 会给出 `reconnect_console`，可用 `handoff --reconnect-console`（脚本 `-ReconnectConsole`）把它直接接回控制台。这条路线不先 RDP 也可能生效，但风险更高：物理控制台若被别的会话占用，`tscon` 会报错。不加这个开关时 `handoff` 只会拒绝并提示（CLI 退出码 4），不会动手。
- 与 `outbound: hook/auto` 不冲突：有 hook 就用 hook，没有才需要这条路线。
- 风险：这等于让机器人机器长期保持解锁，**物理安全风险由使用者自担**；不要在别人能物理接触的机器上这么做。

#### UIPI：为什么不要手动开一个管理员终端跑 `tscon`

Windows 的 UIPI（User Interface Privilege Isolation）规定：**前台窗口属于更高完整性级别的进程时，中完整性进程既不能注入输入，也不能抢前台**。

桥是普通权限（中完整性），而「以管理员身份运行」的 PowerShell 是高完整性。如果你手动开一个管理员终端跑 `handoff` 并把它留在桌面上，就会出现：

- `SetForegroundWindow(微信)`、`AttachThreadInput` 全部返回 **err=5（拒绝访问）**，`SetCursorPos` 也失败；
- pywinauto 报 `RuntimeError: There is no active desktop required for moving mouse cursor!`；
- 桥的发送失败日志是 `open_chat_failed`（桌面其实没锁，`desktop=interactive`，但每次点击注入都被拦掉）；
- 这个提权窗口桥既关不掉也最小化不了（`ShowWindow` 同样 err=5），只能人工关。

**所以交接一律用 `-Elevate`**：提权进程执行完 `tscon` 就退出，不会留下这个「高完整性路障」。已经留下了的话，重新 RDP 进去手动关掉那个管理员窗口，再交接一次。

桥侧也有兜底：`open_chat` 失败时会找微信主窗口，用「最小化 → 还原」（`SW_MINIMIZE` + `SW_RESTORE`）把它顶回前台后**重试一次**；命中 `foreground_conflict` 时也会先顶目标会话窗口再重查前台。这个动作不绕过 UIPI 去注入，只是把微信恢复成前台窗口，让后续的 UIA 点击能正常注入。顶不动就仍然按原错误码失败，不会重复发送。

#### 为什么不直接把桥「提权」到管理员

不行，而且会让情况更糟：

- 微信以普通权限运行。桥一旦提权，两边完整性级别不一致，健康探测会判 `integrity_mismatch` → `degraded`，`may_send()` 直接返回 False，**所有发送都会被拒**；
- 每次启动都要过 UAC，计划任务/自启动也得勾「以最高权限运行」，维护成本更高；
- UIAccess（免 UAC 的高完整性 UI 自动化）需要代码签名并安装到 `Program Files`，不是这个项目的部署方式。

正确做法是**桥与微信保持同一完整性级别**，靠上面「把微信顶回前台」的兜底绕开 UIPI，而不是靠提权硬闯。

### 启动

先起桥，再起 bot / supervisor：

```powershell
# 桥是独立进程，默认读 config.yaml 与 .env，可加 --config / --env-file
.venv\Scripts\python -m dafeiyu.gateway.wechat_bridge

# 正常整套启动（supervisor + bot + NapCat）
powershell -ExecutionPolicy Bypass -File scripts\start-all.ps1
```

桥只监听 `127.0.0.1:8790`，带 token 鉴权；不要把它暴露到局域网。

### 已知上限（不承诺与 QQ 完全一致）

- **无真实 message_id**：桥为会话/消息在本地铸造 id，跨重启不保证稳定。
- 不支持撤回 / 编辑事件。
- 同名群只能靠显示名 / 人工区分，面板白名单里的群要人工核对（同名群由成员集合区分 alias）。
- `@` 触发依赖轮询，最坏有约一个 `poll_seconds`（默认 8 秒）的延迟；本地库模式同样受这个轮询间隔限制，但不再有开窗开销。
- 发送有频率上限：`send_min_interval_seconds`（默认 20 秒）、`send_max_per_hour`（20）、`send_max_per_day`（60）；三者设为 `0` = 不限制。
- 默认出站（`outbound: uia`）桌面锁屏即发不出去；改用 `hook` / `auto` 可绕开桌面，但依赖外部注入服务。RPA 可能误点，`require_foreground_idle` 默认要求微信窗口前台且空闲才执行操作；开启微信通道时别在那台机器上干重要的事。

### 急停

- 面板「微信通道」卡片的**停用**按钮，立即生效；
- 也可以手动创建 `data/wechat.stop` 文件，下一次发送前生效；
- 恢复 = 面板「恢复」按钮，或删掉这个文件。
- 急停只停微信通道，QQ 完全不受影响。

### 风险

封号、RPA 误操作、桌面客户端升级导致 UIA 失效——这些是桌面 RPA 接入的固有代价，**均由使用者自担**。

## 3.6 语音回复（可选，默认关闭）

**默认路径**：`speech.tts_enabled: false`，回复一律是文字，本节可以整个跳过。语音回复和微信通道、入站转写一样是 opt-in 能力，不装依赖、不开开关就不参与任何链路。

### 安装与配置

1. 安装可选依赖：`pip install -e ".[tts]"`。extra 只含 `edge-tts>=6.1`，**不在默认依赖里**；没装时不会合成，回复照常是文字。

2. 在 `config.yaml` 的 `speech:` 下打开（四个键都在 `speech` 里，没有独立的 `tts:` 配置节）：

   | 键 | 默认 | 说明 |
   | --- | --- | --- |
   | `speech.tts_enabled` | `false` | 总开关；关闭时不 import `edge-tts`、不落盘 |
   | `speech.tts_voice` | `zh-CN-XiaoxiaoNeural` | edge-tts 云端音色名 |
   | `speech.tts_rate` | `"+0%"` | 语速，按 edge-tts 的百分比写法（如 `"-10%"`） |
   | `speech.tts_max_chars` | `200` | 超过这个字数的回复不合成，直接发文字 |

3. 生效范围：只有**成型后单段**、**没有 @ 目标**、且工作区打开（`workspace.dir` 非空）的回复才会转语音；多段回复和带 @ 的回复照常发文字。语音文件落在 `<workspace.dir>/replies/voice-<uuid>.mp3`。

> **隐私警告**：开启后，被合成的**回复文本会发送到云端 TTS 提供商（edge-tts / 微软语音服务）**。这正是它默认关闭的原因；介意就别开，其余数据的流向不受影响。

### 降级行为

回复**永远不会**因为语音失败而丢失：

- 没装 `edge-tts`、合成报错、30 秒超时、文本超过 `speech.tts_max_chars`、输出目录不可写、产出文件为空或超过 10 MiB —— 一律**自动退回纯文本**；
- 语音段 / 媒体发送失败同样退回原文，不重试；
- 机器人自己存的历史与待发队列（outbox）保存的始终是**原文**，不是空正文或语音路径，所以后续上下文与补发都正常。

## 4. 用法

群里：

- `@溟月 今天吃什么` —— 必回
- 直接叫「大肥鱼」「小鲸鱼」「鱼鱼」—— 必回
- 引用她的消息继续问 —— 必回
- 其他消息 —— 她按规则 + 模型判断决定要不要插话
- 判断「是不是在跟她说话」比以前更宽：@、引用、叫名字，以及**紧接着回答她上一句提问**都算点名；被点名会重开已收尾的话题
- 话题收尾窗口只做提示，不再拦截：窗口内的非点名消息照样进批次送模型，接不接由模型输出的 `speak` 决定（原来只有 5% 抽样放行，贴封条时还会被整段吞掉）
- 被规则跳过的消息不会丢：下次接话时会把这些漏掉的消息按 `[时:分] 昵称：内容` 的时间线一起交给模型
- 时间线会标出群友之间的「（回复 谁）」，模型能看出这句话在接谁
- 支持的消息段：文字、@、图片、表情/商城表情、语音、视频、文件、分享、位置、名片、音乐、骰子/猜拳、Markdown、合并转发、小程序/网页卡片；文件本体不做识别，只保留可读占位与文件名
- 语音：打开 `speech.enabled` 后，QQ 语音（`record`）和微信语音（`voice`）会先落盘再转写，正文替换成「[语音] 转写：…」；SILK 由 `pysilk` 解码，其余交给 ffmpeg，再进本地 faster-whisper。模型/依赖缺失、超时或解码失败都保持 `[语音]` 占位，不影响其它消息
- 视频：QQ 与微信在各自媒体开关打开时都会下载原文件并抽 4 帧，供模型 `read` 看画面（详见 `docs/message-types.md`）
- 图片会落到 `data/bot-workspace/images/` 并把路径写进 prompt；模型有 `read` 时会被告知**先读最近一张图再回答**，动图按首帧理解。模型没有视觉/工具时只保留 `[图片]` 占位
- 打开 `mind.inbound_media_reactions` 后，群里的语音/图片到达时会先贴一个表情回应（同一会话按冷却秒数节流，失败只记日志）
- 模型调用失败时保持安静，不再往群里发「本鱼断线了」；失败照常计入日志与错误统计
- `/闭嘴`（或 `/shutup`）—— 本群静音 60 分钟（仅 `groups.admins` 可用）
- `/醒来`（或 `/wake`）—— 解除静音（仅 `groups.admins` 可用）
- `/提醒 30 喝水`（或 `/remind 30 喝水`）—— 30 分钟后在本群提醒（仅 `groups.admins` 可用）

私聊：

- 直接说话即可（不受冷却、白名单、静默限制，仍受预算限制）
- `/remind 60 交报告`—— 60 分钟后提醒你（私聊所有人生效）

命令（群聊/私聊都可用，权限和冷却不同）：

| 命令 | 群聊 | 私聊 | 冷却 | 说明 |
| --- | --- | --- | --- | --- |
| `/help` `/帮助` | 所有人 | 所有人 | 5 秒 | 按当前身份只列出可用命令 |
| `/stats` `/统计` | 所有人 | 所有人 | 群 60 秒 / 私聊 5 秒 | 今日全局 + 当前会话调用/token；群内不显示费用 |
| `/status` `/状态` | 仅管理员 | 仅管理员 | 30 秒 | 网关、调度、配额、预算状态与运行时长 |
| `/budget` `/预算` | 仅管理员 | 仅管理员 | 30 秒 | 全局/月度/本会话预算与费用估算 |
| `/forget` | 仅管理员 | 所有人 | 无 | 清空当前会话全部记忆（消息、摘要、印象、用量与会话映射，不可撤销） |
| `/remind` `/提醒` | 仅管理员 | 所有人 | 无 | 定时提醒，三种写法：`/remind <分钟> <内容>` 新建、`/remind list` 查看本会话最近提醒、`/remind cancel <id>` 取消自己创建的提醒；分钟为 1–10080 的整数，内容非空且 ≤200 字；到点由后台 worker 发送 |

群聊专用的 `/shutup`（`/闭嘴`）与 `/wake`（`/醒来`）不在上表（私聊不可用），说明见前面的群内用法。

`/remind` 与 `/forget` 一样是「群内仅管理员、私聊所有人」的非对称权限；提醒只在当前会话生效，
取消也要求是创建者本人。提醒正文与状态存在 SQLite 的 `reminders` 表里（是**表名，不是配置节**），
没有 `reminders:` 配置块。到点后由轮询 worker 经主动出站通道发送：受 `triggers.quiet_hours`
与预算门控，失败会进 `pending_outbox` 重试，但不调用模型、不消耗模型额度。

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
- **批次预览**：输入会话类型与会话 ID，只读预览下一次投递：待投消息、预算判定、完整 prompt
  （含「此刻」与记忆注入）以及 token / 费用估算。不写库、不调模型、不发消息、不动调度器。
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
- **聊天行为**：手动切换聊天强度（安静 / 正常 / 活跃），设置言论限制（禁词、禁话题，
  关闭 / 提示模型 / 严格拦截）。保存后即时生效，并写入 `data/settings.local.yaml`，
  重启后保留；不会改动 `config.yaml` 的注释。
- **群白名单**：编辑 `groups.whitelist`（QQ 群号或微信群 alias，即 `qq:<号>` / `wxg:<别名>`），保存到
  `data/settings.local.yaml`，即时生效，重启后保留。默认为空 = 不限制回复对象；非空 =
  只有名单内的群会被主动回复，@ / 引用 / 叫名字仍然必回。QQ 与微信同名群用通道前缀区分
  （`qq:<号>` / `wxg:<别名>`），不会互相覆盖。注意这里只决定「回不回」；
  抓哪些微信群同样由这份白名单里的 `wxg:` 条目决定（桥不再有独立的群清单）。

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

**工作区恢复（R18）**：备份时如果 `workspace.dir` 存在，会额外打包一份
`<备份名>-workspace.zip` 并在 manifest 里记 `workspace_filename` / `workspace_sha256`。
面板备份项出现「恢复工作区」按钮（同样要求 `RESTORE` 确认）时，会先停 bot → 校验 SHA256
与 zip 成员（拒绝绝对路径、`..`、符号链接、超过 20000 个文件或解压后 >512 MiB）→ 解压到
同级临时目录 → 目录交换（旧目录保留为 `<name>.pre-restore-<时间>`）→ 重启并做健康检查；
启动失败会把工作区换回恢复前的目录。工作区打包有同样的文件数/大小上限，超限时本次只备份
数据库（不会让整次备份失败）。审计动作为 `backup.restore_workspace`。

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

### 评测
黄金集在 `data/evals/golden_replies.json`，人工标注每段群聊现场应 `speak` 还是 `silent`。
```powershell
.venv\Scripts\python scripts/eval_golden.py --mode offline   # 只校验数据集，不联网、不调模型
.venv\Scripts\python scripts/eval_golden.py --mode live      # 调 config.yaml 的模型，需 API Key 或本地模型
```
offline 数据集非法时退出码 2；live 单条失败/超时记为 skipped，不影响整体退出码。

每次运行都会把结果落盘，默认写 `data/evals/results/`（`--out-dir` 可改）。文件名是
`<UTC ISO 去掉冒号>-<mode>.json`，例如 `2026-11-08T052311.123456+0000-live.json`；顶层固定六个键：

| 键 | 内容 |
| --- | --- |
| `dataset` | 本次使用的数据集路径 |
| `mode` | `offline` / `live` |
| `model` | 实际模型名（offline 固定 `offline`） |
| `generated_at` | UTC ISO 时间戳（带微秒） |
| `summary` | `total` / `passed` / `failed` / `skipped` / `accuracy` |
| `results` | 每条 case 的 `id` / `expect` / `got` / `status` / `reason` / `latency_ms` |

`summary.accuracy` 的分母只算 `passed + failed`，没有判过的 case 时为 `null`。

**回归门**：拿上一次的结果文件当基线，只拦「基线通过 → 现在失败」：

```powershell
.venv\Scripts\python scripts/eval_golden.py --mode live --baseline data/evals/results/<上次>-live.json --fail-on-regression
```

命中回归时退出码 1，stderr 打印被拦的 case id；没有回归、没给 `--baseline`、或只有新增/缺失
case 都是 0。`skipped` 在基线和当前两边都剔除，只在报告里统计，永远不算回归也不算修复。
`--baseline` 必须是本脚本落盘的结果文件（顶层有 `results` 列表）；给成 `cases` 形状的评测报告或
坏 JSON 会直接退出 2。加 `--json` 还能拿到 `result_path`、`distribution`
和 `baseline`（`new` / `missing` / `regressed` / `fixed` / `new_failures`）。

**线上抽样**：从主库只读抽最近的聊天，产出可回放的 case：

```powershell
.venv\Scripts\python scripts/eval_sample.py --days 7 --per-chat 5
```

默认写 `data/evals/samples/<UTC 日期>.yaml`（`--out` 可改），可直接交给
`dafeiyu.eval.loader.load_cases` 回放。采样**只读**，不会创建或迁移数据库；SQL 层过滤
`is_bot = 1`，bot 自己的发言不会变成 case 里的 user 消息；`--db` 指定主库，
`--chat-type group|private` 只采一类会话。库不存在、缺表或时间窗内没有人类消息时只打印原因
并退出 0，不写文件；抽样不调用模型。

面板的「评测」卡片（`GET /api/evals`）只读展示 `data/evals/results/` 里最近 10 次运行的文件名
（含模式）、生成时间与通过/失败/跳过数量和准确率；目录不存在或文件坏掉时卡片只少几行，
不报错，也没有删除、编辑或触发运行的接口。

`data/evals/results/` 与 `data/evals/samples/` 都是生成物，已被 `.gitignore` 排除
（`data/evals/` 下只保留 `golden_replies.json`），不要提交。

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
| 面板新卡片保存返回 404 | 控制台（supervisor）还是旧进程：`index.html` 每次请求现读，卡片会先出现，但 `/api/behavior` 等路由只在启动时注册。用 `scripts/stop.ps1` + `scripts/start-all.ps1` 重启整套；只点「重启 Bot」不够 |
| 群里 @ 也不回、日志有 `Agent not found: "deepseek-qq"` | opencode 的 session 绑定创建时的项目目录；仓库被移动/复制（如 `C:\AICoding` → `C:\AIWork`）后旧 session 会在空目录里找 agent。面板「会话管理」对每个会话点「新建会话」，或清掉 `brain_sessions` 后重启；新代码会自动换新 session 重试一次 |
| 启动后没反应/端口占用 | 先 `scripts/stop.ps1` 停掉旧实例；`start.ps1` 不能和 supervisor 同时跑 |
| 双击 `.env` / `config.yaml` 没反应 | Windows 没关联程序，不是文件坏了；用 `notepad .env` 或编辑器打开，保存为 UTF-8 |
| python / node / opencode 写用户目录或 `%TEMP%` 报 EPERM | 多半是工作区被沙箱降权成 Low 完整性，不是杀软；先跑 `doctor`，再按 `docs/deployment-notes-windows.md` P4/P5 查进程完整性级别 |
| opencode 401 /「免费模型探测失败」/ 切档失败 | supervisor 会自动给 serve 和 bot 注入同一个 `OPENCODE_SERVER_PASSWORD`；只有手工/外部跑 `opencode serve` 时才需要在 `.env` 设它（P7） |
| `.env` 中文注释变 `???` 或键丢失 | 文件编码不是 UTF-8；`doctor` 会报「编码不是 UTF-8」，按 `.env.example` 用 UTF-8 重写（P10） |

出问题先导诊断包，省得来回截图：

```powershell
# 默认写到 data/diagnostics/diag-<时间戳>.zip；先 -DryRun 可只看计划
powershell -ExecutionPolicy Bypass -File scripts/diag.ps1
powershell -ExecutionPolicy Bypass -File scripts/diag.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts/diag.ps1 -LogLines 500 -OutFile D:\diag.zip
```

包里有版本、`pip freeze`、端口监听、`/health`、最近日志和**脱敏后**的
`config.redacted.yaml`；`data/`、`.env`、`*.db*`、`data/secrets`、
`data/supervisor.token` 一律不打包，日志与配置里的 `sk-`/`Bearer` 值也会打码。

> Windows 受限环境（沙箱降权、杀软、无管理员、opencode 认证、任务计划自启等）的实际部署
> 踩坑记录见 [docs/deployment-notes-windows.md](docs/deployment-notes-windows.md)。

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
  gateway/           qq.py（OneBot 反向 WS）、wechat.py（微信适配器）、wechat_bridge/（UIA 桥，独立进程）、base.py
  router.py          三层闸门、冷却、预算、静默、指令解析
  memory/            store.py（SQLite）、context.py（上下文预算）
  brain/             client.py（DeepSeek）、persona.py + persona.md（人设）
  workers/           impressions.py（群友印象）、summary.py（每日摘要）
  sender.py          截断与分片
  cli.py             运维命令
tests/               单元 + 集成测试（不联网）
```
