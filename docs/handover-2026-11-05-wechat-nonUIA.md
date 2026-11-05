# 交接：微信非 UIA 出站 + 自备 Key（2026-11-05）

> 读者：接手这台 Windows 机器 / 这个仓库的人。
> 前置阅读：`docs/wechat-hook-backend.md`（链路与接口设计）、`docs/deployment-notes-windows.md`。
> 本文只写「现在是什么状态、怎么操作、坏了怎么查、还欠什么」。

---

## 0. 一句话

微信出站已经**不依赖 UI 自动化**：bot → 本地 hook 服务(:30012) → `Prometheus-first/helper`
的原生桥 DLL 注入微信发送队列，锁屏也能发；入站读微信本地库镜像。大脑从「免费档
（上游 429）」切到了**自备 DeepSeek Key 档**，全量测试 2551 passed。

---

## 1. 当前运行状态（截至 2026-11-05 02:15）

| 组件 | 位置 | 状态 |
| --- | --- | --- |
| 计划任务 `dafeiyu-supervisor` | Task Scheduler，`ONLOGON` + `InteractiveToken` | Running（Session 1） |
| supervisor / 面板 | `http://127.0.0.1:8787`（局域网 192.168.2.161:8787） | 在跑 |
| bot 控制口 | `127.0.0.1:8788`（`X-Control-Token`） | 在跑 |
| 微信桥 | `ws://127.0.0.1:8790/wechat-bridge` | 已连（本地库 4 群） |
| 微信 hook 服务 | `http://127.0.0.1:30012` | ready，微信 4.2.0.11 |
| QQ 网关 | `127.0.0.1:8080`（NapCat 反向 WS） | 已连 |
| opencode serve | `127.0.0.1:24310`（`vendor\opencode\opencode.exe`） | 在跑 |
| 大脑 | `dafeiyu-key/deepseek-flash`（`https://api.deepseek.com`） | `mode=key`, `has_key=true` |

账号：微信 `wxid_516rht3txzox22`（昵称 `Deepseek娘`）；QQ `3351825424`。

---

## 2. 链路

```
入站 QQ     NapCat(8080) ──ws──┐
入站 微信   微信本地库镜像 ──> 桥(8790) ──ws──┤
                                              ├─> bot(:8788 控制口)
出站 微信   bot ──HTTP──> hook 服务(:30012) ──> helper 原生桥 ──> 微信发送队列（无键鼠）
出站 QQ     bot ──ws──> NapCat
大脑        bot ──HTTP──> opencode serve(24310) ──> api.deepseek.com
托管        supervisor(8787) 管 opencode / bot / napcat / 桥；hook 服务独立进程
```

要点：

- **桥和 hook 是两套进程**。桥(8790) 负责入站观测 + 发送编排（限速、补发、@解析），
  hook(30012) 只负责「把这条消息真正塞进微信」。
- hook 服务**不在 supervisor 的进程树里**，由 `scripts/start-wechat-hook.ps1` 用
  `Win32_Process.Create` 脱离终端启动，`stop.ps1` 会顺带收掉。
- `start-all.ps1` 在拉起 supervisor 之前会先幂等拉起 hook 服务。

---

## 3. 版本指纹（改微信前先对这三项）

| 项 | 值 |
| --- | --- |
| 微信版本 | `4.2.0.11` |
| `Weixin.dll` 大小 | `205929008` 字节 |
| `Weixin.dll` sha256 | `745cdf29…46b201` |

指纹不匹配 → helper 拒绝加载，hook 服务 `/health` 不会 ready。此时要么等 helper 适配，
要么回退微信版本；**不要**试图绕过校验。

helper 目录：`C:\MyFile\AICoding\helper`（独立 `.venv`，Python 3.11.7 anaconda，
不是 git 仓库）。

---

## 4. 配置（`C:\MyFile\AICoding\dafeiyu-bot\config.yaml`，gitignored）

关键段：

```yaml
brain:
  provider: opencode          # 三档 free/key/local_model 由 data/settings.local.yaml 覆盖

gateway:
  provider: both              # QQ + 微信并行
  wechat:
    enabled: true
    outbound: hook            # uia / hook / auto
    hook_url: http://127.0.0.1:30012/send_text
    hook_timeout_seconds: 30  # 原生桥单次 1–3s，5s 太紧
    local_db_enabled: true    # 入站 + 群名→xxx@chatroom 解析
    local_db_root: C:\MyFile\Data\xwechat_files\wxid_516rht3txzox22_8eb6\db_storage
    require_foreground_idle: false   # hook 后端锁屏可发
    # quiet_hours 不写 = 继承 triggers.quiet_hours（本次新语义）

triggers:
  quiet_hours: ["00:00", "00:00"]    # 起止相同 = 不静默

opencode:
  agent: deepseek-qq
  manage_server: false
  server_url: http://127.0.0.1:24310
  config_home: data/opencode-runtime/config
```

**容易踩的坑**：

1. `local_db_root` 必须显式写。微信数据被挪到 `C:\MyFile\Data`，自动探测
   `~/Documents/xwechat_files` 会失败，表现为桥报「本地库不可用 / 群名解析失败」。
2. 静默时段只有**一个开关**：`gateway.wechat.quiet_hours` 不写就跟随 `triggers.quiet_hours`；
   写了就按自己那份拦。两处各写一份且不一致，会出现「triggers 放行、发送端仍拦」。
3. `config.yaml` 是 gitignored 的本机配置；`config.example.yaml` 才是进版本库的样例。

---

## 5. 大脑三档（当前 = key）

| 档 | 模型 | 说明 |
| --- | --- | --- |
| `free` | `opencode/deepseek-v4.1-flash-free` | 上游免费网关，本次故障源（HTTP 429） |
| `key` | `dafeiyu-key/deepseek-flash` | **当前**，自备 Key，直连 api.deepseek.com |
| `local_model` | 任意 OpenAI 兼容端点 | 默认本机 LM Studio `127.0.0.1:1234` |

- 密钥文件：`data\secrets\provider.key`（35 字节，`sk-` 开头，内容不外传）。来源是
  `dafeiyu-release\data\secrets\provider.key`。已在 `.gitignore` 的 `data/` 下，不会进库。
- 生成的 opencode 配置里只写引用：`"apiKey": "{file:../../../secrets/provider.key}"`，
  不落明文。生成物在 `data\opencode-runtime\config\opencode\opencode.jsonc`。
- 切档后 `data\settings.local.yaml` 保存覆盖层，`data\runtime.json` 是旧字段。
- `.env` 里的 `DEEPSEEK_API_KEY` 只有 9 个字符，是 LM Studio 占位值，**不是**真 Key。

### 切档 / 回退（不要手改配置）

```powershell
$t = (Get-Content data\supervisor.token -Raw).Trim()

# 切到自备 Key
$body = @{ mode='key'; key=@{
    base_url='https://api.deepseek.com'; model='deepseek-flash'; api_key='<key>'
} } | ConvertTo-Json -Depth 4 -Compress
Invoke-RestMethod 'http://127.0.0.1:8788/api/brain' -Method Post `
  -Headers @{'X-Control-Token'=$t} -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))

# 回退免费档
'{"mode":"free"}' | Invoke-RestMethod 'http://127.0.0.1:8788/api/brain' -Method Post `
  -Headers @{'X-Control-Token'=$t} -ContentType 'application/json'
```

切档会自动：写密钥文件 → 写覆盖层 → 重新生成 `opencode.jsonc` 与两个 agent 文件 →
热重载 opencode。返回 `reload: ok` 即成功；旧的非生成物配置会被备份成 `.bak`。

面板 8787 的「大脑」下拉框走同一条 API。

---

## 6. 运维手册

### 启动

```powershell
# 推荐：计划任务（Session 1，交互会话，helper 需要附着微信）
schtasks /Run /TN dafeiyu-supervisor

# 或者完整脚本（会先拉起 hook 服务，再起 supervisor）
powershell -ExecutionPolicy Bypass -File scripts\start-all.ps1
```

> **不要**在 SSH 里手工起一个长期 supervisor。SSH 在 Session 0，断开后进程树会被收走，
> 而且跨会话的 UIA 调用全部 `COMError (-2147220991)`。SSH 只用来执行一次性命令。

### 计划任务：必须直接用 pythonw，不要经 cmd

旧任务的行动是 `cmd /c cd /d <repo> && .venv\Scripts\pythonw.exe -m dafeiyu.supervisor`，
登录后会一直挂着一个 cmd 黑框（`pythonw` 没有控制台，但外层 `cmd.exe` 有）。
2026-11-05 已改成：

```text
Execute          : C:\MyFile\AICoding\dafeiyu-bot\.venv\Scripts\pythonw.exe
Arguments        : -m dafeiyu.supervisor
WorkingDirectory : C:\MyFile\AICoding\dafeiyu-bot
```

注册/更新需要管理员权限（任务文件在 `C:\Windows\System32\Tasks`）：

```powershell
# 先看要写什么，不落盘
powershell -ExecutionPolicy Bypass -File scripts\register-supervisor-task.ps1 -DryRun
# 以管理员身份执行，或接受一次 UAC
powershell -ExecutionPolicy Bypass -File scripts\register-supervisor-task.ps1
```

改完要**停一次现有实例再拉起**，新行动才会生效：

```powershell
.\.venv\Scripts\python.exe -m dafeiyu.supervisor.stop   # 只停 supervisor，不动 hook
Start-ScheduledTask dafeiyu-supervisor
```

另：`pythonw` 下任何 console 子进程（`icacls` / `netstat` / `taskkill` / `powershell` /
`npm`）都会各自新开黑框，统一用 `dafeiyu.winproc.hidden_kwargs()` 或
`CREATE_NO_WINDOW`。**不要**把计划任务退回 `python.exe` 或再套一层 `cmd /c`。

### 停止

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1   # 先停 hook，再优雅停 supervisor
```

### 健康检查

```powershell
# hook 是否就绪
Invoke-RestMethod http://127.0.0.1:30012/health | ConvertTo-Json

# 大脑档位 / Key 是否就位 / 免费档是否可用
$t=(Get-Content data\supervisor.token -Raw).Trim()
Invoke-RestMethod http://127.0.0.1:8788/api/brain -Headers @{'X-Control-Token'=$t} | ConvertTo-Json -Depth 4

# 端口
foreach ($p in 8787,8788,8790,30012,8080,24310) {
  "$p=" + (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue | Measure-Object).Count
}
```

### 日志

| 文件 | 内容 |
| --- | --- |
| `logs\supervisor\supervisor.log` | 面板 / 进程管理 |
| `logs\supervisor\bot.log` | bot 主日志（收消息、送审、发送、补发） |
| `logs\supervisor\wechat_bridge.log` | 桥（本地库、轮询、发送结果） |
| `logs\supervisor\napcat.log` / `opencode.log` | QQ / 大脑 |
| `logs\wechat-hook.log` | hook 服务（启动、helper 就绪、错误） |
| `logs\dafeiyu.log` | 前台 CLI 运行日志 |

日志按大小轮转，`.1`/`.2`/`.3` 是旧文件。

### 改配置后的生效方式

supervisor **不监听** `config.yaml`，改完必须整树重启才生效：

```powershell
scripts\stop.ps1        # 或 python -m dafeiyu.supervisor.stop
scripts\start-all.ps1   # 先拉 hook，再拉 supervisor
```

注意：已有实例在跑时 `schtasks /Run /TN dafeiyu-supervisor` **不会**重启它
（`MultipleInstances=IgnoreNew`），只会静默忽略。

---

## 7. 数据与备份

| 路径 | 内容 | 说明 |
| --- | --- | --- |
| `data\dafeiyu.db` | messages / usage / pending_outbox / 记忆表 | 主库；`messages` 表字段是 `content` 不是 `text` |
| `data\opencode-bot.db` | opencode 会话 | 独立 |
| `data\backups\` | supervisor 定期备份 | |
| `data\pre-restore-20261105-002740\` | 2026-11-05 00:27 恢复前的留底 | **建议保留**，回滚用 |
| `data\wechat-bridge\localdb\` | 微信本地库镜像（contact/message） | 可重建，但删了要重新扫密钥 |
| `data\wechat-bridge\peers.json` | 群 alias ↔ 显示名 | alias 是桥生成的短哈希 |
| `data\secrets\provider.key` | 自备 Key | 35 字节，勿提交 |
| `data\supervisor.token` | 控制口令牌 | 勿提交 |
| `.env` | 面板密码、NapCat 端口等 | 勿提交 |

恢复前 `messages` 407 条（`user_version 2`）；现在 410+ 条（含 e2e 注入）。

---

## 8. 故障排查

| 症状 | 先查 | 处理 |
| --- | --- | --- |
| `/health` 不 ready | `logs\wechat-hook.log`；微信版本/指纹 | 指纹不符就等 helper 适配；微信没登录就登录 |
| 发送失败 `chat_name_unresolved` | 群是否在 `local_db_root` 的库里；`peers.json` 是否有该群 | 检查 `local_db_root`；同名群会让解析歧义 |
| 发送失败 `at_unresolved` | 群成员显示名是否唯一 | 同名/查不到时调用方会退回普通文本，不会乱 @ |
| `微信发送失败：quiet_hours` | `gateway.wechat.quiet_hours` 与 `triggers.quiet_hours` | 两处都设成 `["00:00","00:00"]` 或保持一致 |
| `免费模型探测：HTTP 429` | `/api/brain` 的 `mode` | 还在 free 档；切 key 档 |
| 桥只轮询 3 个群 | `resolve()` 是否给出 `@chatroom` | 解析不出的群（如「垃圾桶」）已从轮询剔除，避免每轮 UIA 空等 |
| `COMError -2147220991` | 起进程的会话 | 有东西在 Session 0 跑；改用计划任务在 Session 1 起 |
| bot 发不出、hook 正常 | 桥的限速与补发队列 | 看 `pending_outbox`；`triggers.quiet_hours`；每小时/每天上限 |
| 计划任务跑满三天被杀 | 任务设置 | 已改 `ExecutionTimeLimit=PT0S` / 关电池限制 / 失败重试 3 次×1 分钟 |

常用 SQL（`data\dafeiyu.db`）：

```sql
select count(*) from pending_outbox;
select chat_id, is_bot, substr(content,1,40), created_at
  from messages order by rowid desc limit 10;
select chat_id, purpose, prompt_tokens, completion_tokens, created_at
  from usage order by created_at desc limit 5;
```

---

## 9. 已知边界与风险

- **封号风险自担**：DLL 注入违反微信条款。
- **版本绑定**：微信升级即失效，等 helper 适配或回退版本。
- **不支持撤回**：hook 只做发送；发错只能人工撤回。
- **账号绑定**：服务绑定当前登录账号；换号后要重启 hook 服务（日志会打印 wxid）。
- **@ 定向**：dafeiyu 传的是**显示名**，服务端用 `group_members` 解析成 wxid；
  同名/缺失返回 `at_unresolved`，调用方退回普通文本。
- **媒体**：已打通，`Service._send_chunk` 对无 `send_segments` 的通道逐条调
  `send_media`（image/file/voice/sticker），失败只记日志、正文照发。
- **会话依赖**：机器重启后必须有人登录进 Session 1，且微信处于登录状态，
  否则 hook 发不出去。这是架构约束，不是 bug。
- **仓库里的 agent 文件是生成物**：`.opencode/agents/*.md` 每次 supervisor 启动会按
  当前档位重写。本机是 key 档，所以工作区里这两份文件可能一直显示 `M`；
  CI/新克隆是 free 档基线。测试已按档位做了跳过/放宽。

---

## 10. 未完成 / 待决定

| # | 事项 | 说明 |
| --- | --- | --- |
| 1 | 提交未 push | 第二轮结束后见 `git rev-list --count origin/feat/qq-bot..HEAD`；`gitea` 凭据交互曾失败，失败不阻塞 |
| 2 | e2e 消息已真实发出 | 01:26:51 往群「我们一起搞事情吖」发了 `E2E-hook-…` 的回复（补发机制），确认真假群，必要时人工撤回 |
| 3 | agent 文件入库问题 | 彻底解法：移出 git，或让生成目录落到 `data/opencode-runtime/` |
| 4 | 01:29 的重启无来源记录 | supervisor 不记录「谁发起停止」，建议加审计 |
| 5 | 旧副本 | `C:\MyFile\AICoding\dafeiyu-release`（旧发布副本，Key 来源）是否清理 |
| 6 | 免费档 429 | 与本次改动无关；key 档下不再使用，若回 free 需换端点或等限流恢复 |

---

## 11. 验证基线

```powershell
cd C:\MyFile\AICoding\dafeiyu-bot
.\.venv\Scripts\python.exe -m pytest -q
# 2026-11-05 第一轮：2551 passed, 20 skipped
# 2026-11-05 第二轮：2602 passed, 20 skipped（约 4.5 分钟）
```

- e2e：`scripts\qa_wechat_smoke.py`（会临时打开 `allow_test_inject`，跑完自动关；
  **它会往真实群注入消息并可能触发真实回复**，别在生产号/真群上随手跑）。
- 手工验证发送：`Invoke-RestMethod :30012/health` → `ready:true`，然后从面板发一条。

---

## 12. 本次变更时间线

| 时间 | 事件 |
| --- | --- |
| 11-04 23:xx | 本地仓库从 `0a9c9a6` 快进到 `1170690`，补齐 `.[wechat]` 依赖 |
| 11-05 00:27 | 恢复备份（db + workspace），留底 `data\pre-restore-20261105-002740\` |
| 00:34 | hook 服务首次跑通（helper 原生桥就绪，wxid=…ox22） |
| 01:13–01:29 | e2e 冒烟（注入消息、验证补发，期间留下 `config.yaml.e2e-bak`） |
| 01:22 | 大脑 free → key（自备 Key 来自 `dafeiyu-release\data\secrets\provider.key`） |
| 01:24 | 解除微信静默窗口并重启；01:26 补发成功（证明发送链路） |
| 01:29 | 一次无来源记录的整树重启（计划任务重新拉起） |
| 02:00–02:15 | 修测试、补提交、加固计划任务（PT0S / 电池 / 重试） |
| 02:31–02:33 | 计划任务改为直接 `pythonw` + WorkingDirectory（去掉 cmd 黑框）；优雅重启后全端口/`/health` 恢复 |

提交（`feat/qq-bot`，均未 push）：

```
611b4c9 test: 修跨天失效的预算用例，并让 agent 基线适配非 free 档
8a16d72 test(config): 微信静默时段默认继承 triggers 的断言同步
fe3d018 fix(config): gateway.wechat.quiet_hours 未配置时继承 triggers
73a1c35 chore(brain): 切到自备 Key 档后重新生成 agent 模型钉
7f787e6 fix(wechat): 本地库枚举到的群要能解析成 @chatroom 才轮询
0a43d3c feat(wechat): 接入 helper 原生桥做非 UIA 出站，锁屏也能回
6a28d03 feat(wechat): 出站支持定向 @，媒体逐条走 send_media
```

远端：`origin` = github.com:YuanZ1949/dafeiyu-bot，`gitea` = yuannas.local:3000/yuan/dafeiyu-bot。

---

## 13. 本次改动涉及的文件

| 文件 | 改动 |
| --- | --- |
| `gateway/wechat_bridge/outbound.py` | `HookTransport.send_at`、`_endpoint()` 推导、`at_url`/`media_url` |
| `gateway/wechat_bridge/sender.py` | `_Job.at`、`submit(at=)`、`_execute` 按后端能力判 @、静默窗口 |
| `gateway/wechat_bridge/__main__.py` | 不再硬拒 `at`，透传给 `submit_async` |
| `gateway/wechat_bridge/health.py` | `outbound != uia` 时跳过 `about()/language()/integrity` |
| `gateway/wechat_bridge/poller.py` | `_localdb_group_names()` 按 `resolve_username` 过滤 |
| `service.py` | `_send_chunk` 对无 `send_segments` 的通道逐条 `send_media` |
| `config.py` | `hook_at_url`/`hook_media_url` 校验；`quiet_hours` 继承 `triggers` |
| `env_probe.py` | `outbound != uia` 时 integrity/desktop 检查记 SKIP |
| `scripts/wechat_hook_server.py` | HTTP 适配层、`_resolve_mentions` |
| `scripts/start-wechat-hook.ps1` | 脱离终端启动 / `-Stop` / 探活 |
| `scripts/start-all.ps1`、`stop.ps1` | hook 服务接入启停 |
| `.opencode/agents/*.md` | 生成物：档位模型钉（当前 key） |
| `tests/` | hook server / outbound / health / localdb poller / config / 基线用例 |
| `winproc.py`（新增） | `CREATE_NO_WINDOW` / `hidden_kwargs()`，统一隐藏 console 子进程 |
| `supervisor/{control_token,manager,main,stop}.py` | `icacls`/`netstat`/`taskkill`/`powershell` 子进程加隐藏窗口标志 |
| `brain/opencode.py` | `npm root -g` 加隐藏窗口标志 |
| `scripts/register-supervisor-task.ps1`（新增） | 计划任务注册/更新（直接 pythonw + WorkingDirectory，支持 `-DryRun`） |
| `scripts/start-wechat-hook.ps1` | 后台启动优先 helper 的 `pythonw.exe` |

---

## 14. 后续补强（2026-11-05 第二轮）

按「一口气都做了」把梳理出的欠账落地，原则：**新能力默认关闭或只记日志，
不改变现有回复路径**。

### 14.1 面板与控制口

- `dashboard.py` 新增 `/api/mind`（GET/PUT）、`/api/workspace/file`、
  `/api/channels/wechat/inject` 代理，修掉面板心智卡和工作区查看的 404。
- `control.py` 新增 `POST /api/memory/unblock`，配 `store.clear_backoff` 与
  `FailureWorker.reset()`；面板上的配额熔断现在能点按钮解除，而不是只能看。

### 14.2 孤立配置接线

| 配置 | 现在的行为 |
| --- | --- |
| `gateway.wechat.ack_requires_observed` | true（默认）：本地库观察不到就标 `receipt_unverified`（仍 `ok=true`，不重发）；false：只写日志 |
| `gateway.wechat.keepalive` | true：桥每 60 秒探一次 hook `/health`，失败只 warning；默认 false |
| `mind.emoji_reactions` / `emoji_reaction_id` | true：QQ 消息发出后尝试 `set_msg_emoji_like`；默认 false |

### 14.3 QQ 新能力（默认安全）

- **notice 事件**：`group_poke` / `friend_poke` / `group_increase` /
  `group_decrease` / `group_ban` 解析成 `NoticeEvent`，只记日志，**不触发模型回复**。
- **合并转发出站**：模型协议新增 `{"type": "forward", "nodes": [...]}`（最多 5 条、
  单条 200 字），`Service._send_chunk` 调 `send_forward`；失败降级为一行
  `[合并转发 N 条]` 文本。
- **表情回应**：`QQAdapter.set_msg_emoji_like`，失败吞掉不影响发送。

### 14.4 记忆与评估

- `memory/context.py` 开始消费 `episodes.importance/mood/phase` 与
  `open_loops.kind`（此前只写不读）。
- 新增 `scripts/eval_reply_judgment.py`：只读 `data/dafeiyu.db`，按批次反推
  「该回没回」，输出 Markdown/JSON，chat_id 一律脱敏。修复了两处 SQL 参数
  未绑定和窗口时间戳渲染的 bug，并补了临时库测试。

```powershell
.\.venv\Scripts\python.exe scripts\eval_reply_judgment.py --days 1 --json
.\.venv\Scripts\python.exe scripts\eval_reply_judgment.py --days 7 --out data\eval.md
```

### 14.5 产品化与文档

- `dafeiyu --version`、`scripts/diag.ps1`（诊断包，自动脱敏）、
  `scripts/update.ps1`（`git pull --ff-only` + 重装依赖，不动 data/config/.env）。
- 校正文档漂移：微信出站媒体已接线、`p4-summary-evolution` 已实现、
  合并转发/notice 能力矩阵。

### 14.6 验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 2026-11-05 第二轮：见 §11 的测试基线更新
.\.venv\Scripts\python.exe -m dafeiyu --version    # dafeiyu 0.1.0
powershell -ExecutionPolicy Bypass -File scripts\diag.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -DryRun
```

---

## 15. 决策流水与面板瘦身（2026-11-05 第三轮）

### 15.1 为什么要做

在此之前，「她打不打算回话、当时怎么想的」只存在于日志里：

- `speak=false` 只有一条 INFO 日志，`reason` 被截到 120 字；
- 模型的 `read` / `think` 是 **DEBUG** 级别，默认配置下根本不落盘；
- 批次本身没有表，事后只能从 `delivered_messages` 反查，且分不清
  「模型自决沉默」和「被规则拦下」。

### 15.2 `decisions` 表

新表（`CREATE TABLE IF NOT EXISTS`，老库 `init_schema()` 自动补建，
`SCHEMA_VERSION` 仍是 2）：

| 字段 | 含义 |
| --- | --- |
| `outcome` | `replied` 已回复 / `silent` 模型决定不说 / `blocked` 规则拦截 / `held` 刻意攒着 / `error` 失败 |
| `checkpoint` | 空 = 模型自主决策；否则为拦截点：`intensity_quiet` `opportunity_hold` `budget` `recalled` `restricted` `guard_dropped` `brain_error` `send_failed` |
| `read` / `think` / `reason` | 模型原文；`reason` 仅 500 字兜底，不再套日志的 120 字 |
| `reply_preview` | 实际发出正文前 200 字 |
| `latency_ms` / `prompt_tokens` / `completion_tokens` | 本次决策的成本 |
| `message_ids` / `message_count` / `addressed` | 这一批判定涉及的消息 |
| `detail` | JSON，预算拦截的 scope/reason 等 |

写入点（`service.py`）：批次级拦截 4 处、模型返回后（回话 / 沉默 / 被言论
限制 / 被守卫丢光 / 发送失败）、私聊单条路径同构。写库异常在
`Service._record_decision` 内吞掉，**绝不影响发送**。

判定口径：**一个分片都没真正发出**就是 `error/send_failed`，与是否进待发
队列无关（入队也失败时 `defer=0`，早期实现会误记成 `replied`，已修）。

`held`（攒着不投）在热聊群会按条重判，同群同原因 60 秒内只留一条，
避免决策流被重复项挤满。

### 15.3 面板

- 新增「决策流」卡：今日次数 / 回复 / 沉默 / 拦截 / 模型沉默率 / 平均延迟，
  支持按结果筛选；每条可展开看 `读 / 想 / 理由 / 回复` 与 token、延迟。
- 接口：`GET /api/decisions?limit=&days=&outcome=&chat_type=&chat_id=`
  （bot 控制口实现，面板 `proxy_get` 透传），返回 `items` + `stats`。
- 去重（同一信息只保留一个权威位置）：
  - 进程状态：删掉健康卡里的进程小结，只留 `#processes`；
  - 健康 kv：删掉「QQ 网关 / 配额 / 备份 / 告警」四行（分别由通道卡、
    全局红条、备份卡、告警卡负责）；
  - `brain-hint`：删掉 OneBot 连接态（通道卡负责）；
  - 记忆卡：删掉「配额暂停中」文案，只留「解除熔断」按钮；
  - 配置卡：标签改为「配置·大脑」「配置·预算」，明确是配置文件值，
    与实时状态区分。

### 15.4 验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 2026-11-05 第三轮：2619 passed, 20 skipped（约 4.5 分钟）
curl -H "X-Control-Token: $(Get-Content data\supervisor.token)" `
  "http://127.0.0.1:8788/api/decisions?limit=5"
```

已知边界：单条消息级的规则拦截（muted / quiet / blacklist / downshift）
仍只记在 `delivered_messages.skipped_reason`，**不**进决策流（否则会按条
刷屏）；决策流只回答「这一批为什么没开口」。

### 15.5 补发成功后的决策口径

发送失败会记 `error/send_failed`，同时把分片排进待发队列。补发成功后
（`Service._flush_outbox`）**不再新增一条 `replied`**，而是调用
`Store.mark_decision_retried(...)` 把原来那条失败决策原地改成 `replied`：

- 匹配键：`chat_type + chat_id + origin_message_id`，只在
  `outcome='error' AND checkpoint='send_failed'` 里找最近一条；
- `checkpoint` 清空，`detail` 合并 `retried=true` /
  `retry_after_seconds` / `outbox_id`；
- 找不到匹配（老数据、重复补发）返回 0，静默跳过；
- 与 `_record_decision` 相同，写库异常全部吞掉，**补发本身不受影响**。

这样面板统计里「一次决策」始终只占一行，不会出现「1 条失败 + 1 条回复」
把回复率和失败数同时算高的情况。

---

## 16. hook 端口迁移与占用守卫（2026-11-05）

### 16.1 现象

面板通道卡报 `degraded / logged_in=false / desktop=no_wechat`，但
`http://127.0.0.1:30011/health` 一直返回 `ready:true`、`last_error` 为空。

### 16.2 根因

- `01:29:41` 有一个 hook 实例以管理员权限启动（`logs/wechat-hook.log:17-19`），
  之后一直没退出，以 `0.0.0.0:30011` 持有监听。
- 微信客户端在 `02:1x` 退出；该实例的 `NativeTextService` 仍缓存旧会话，
  `/health` 继续返回旧的 `self_wxid / wechat_version`，属于**假阳性**。
- 10:59、11:15 两次重启产生的新实例绑 `127.0.0.1:30011`，连接被先绑的
  wildcard socket 接走；新实例日志只能反复刷
  `helper 原生桥未就绪：等待微信账号完成登录`。
- `start-wechat-hook.ps1` 旧实现只按命令行匹配进程；提权进程的命令行读不到，
  于是既停不掉旧实例，又认不出端口已被占用，`start-all.ps1` 还把旧实例的
  `ready:true` 当成启动成功。

### 16.3 处理

- hook 端口从 `30011` 迁到 `30012`：改 `gateway.wechat.hook_url` 即可，
  `start-all.ps1` 从 URL 解析端口并传给 `start-wechat-hook.ps1 -Port`。
- `start-wechat-hook.ps1` 增加端口守卫：
  - 启动前用 `Get-NetTCPConnection -LocalPort` 检查，占用者不是本端口实例时直接报错；
  - `-Stop` 后校验端口确实释放，未释放则非 0 退出并提示提权；
  - 进程匹配同时校验 `--port <当前端口>`，不同端口可以并存。
- `diag.py` 的端口清单同步为 `30012`。

### 16.4 遗留

`30011` 上的提权旧实例（`PID 39764 → 39812`，创建于 `01:29:41`）仍在运行，
需要一次管理员权限才能结束；它已不影响新链路，择机清理即可。

### 16.5 验收

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-wechat-hook.ps1 -Stop -Port 30012
powershell -ExecutionPolicy Bypass -File scripts\start-wechat-hook.ps1 -Port 30012
Invoke-RestMethod http://127.0.0.1:30012/health
```

注意：微信客户端未登录时，`/health` 应返回 `ready:false` 且 `last_error` 非空——
这是**正确**状态，不要再用 `ready:true` 作为启动成功的唯一判据。

## 17. 微信好友私聊（2026-11-05 第四轮）

### 17.1 开关

两个独立开关，均在 `gateway.wechat` 下，默认 **false**：

| 配置键 | 作用 |
| --- | --- |
| `private_enabled` | 本地库把好友会话纳入入站轮询（面板可见、入库） |
| `private_reply` | bot 是否自动回复好友私聊 |

分开是为了支持「先只看不回」的灰度：`private_enabled=true` +
`private_reply=false` 时消息照常入库与展示，`/status` 等命令仍可用，
普通聊天不触发模型。两开关都只在桥/ bot 启动时读取，改完需重启 supervisor。

### 17.2 好友判定

只认 `contact` 表里以 `wxid_` 开头的会话，并排除：

- 自己（`self_username`，从库目录名解析）；
- 系统/官方号：`filehelper`、`notifymessage`、`weixin`、`fmessage`、
  `medianote`、`floatbottle`、`gh_*`（公众号）、`@openim`（外部联系人）。

`@chatroom` 仍然只按群处理。好友必须有对应的 `Msg_<md5>` 消息表，否则
枚举阶段就会被剔除（UIA 兜底只会白等一次开窗超时）。

### 17.3 解析与歧义

- `LocalMessageStore.resolve(name, kind)`：`kind` 为 `group` / `friend`；
- `resolve_any(name)`：群、好友任一命中；**同名群和好友同时存在时返回
  `None`**（fail-closed），`kind_of(name)` 同样返回 `None`；
- 轮询范围里发现群/好友重名：整条名字跳过，并打 warning；
- 身份表 `peers.json` 新增 `kind="friend"` 条目；好友帧的 `chat_type`
  为 `private`，`chat_id` 仍是 `wxg:<alias>`（与群共用别名空间，但按 kind 隔离）。

### 17.4 出站闸门

`PollingLoop.allows_chat()` 对好友多一道条件：`private_enabled` 为真才放行。
面板白名单里列出好友 alias 但开关关闭时，既不会轮询也不会发送。
QQ 私聊（`qq:` 前缀）完全不受这两个开关影响。

### 17.5 只读决策口径

`private_reply=false` 时，微信私聊（`wxg:` 会话）在命令分支之后、预算检查
之前直接返回，并写一条决策：

```text
outcome=silent, trigger=private, checkpoint=private_readonly,
detail={"private_reply": false}
```

命令分支先于该闸门执行，因此只读模式下仍可远程执行 `/status` 等命令。

### 17.6 验收与排查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 2026-11-05 第四轮：2642 passed, 20 skipped

# 桥日志应出现「群 N 个，好友 M 个」
Get-Content logs\supervisor\wechat_bridge.log -Tail 10
```

| 症状 | 先查 |
| --- | --- |
| 面板看不到私聊 | `private_enabled` 是否为 true；桥是否重启过；`contact` 里好友是否有 `Msg_` 表 |
| 私聊入库但不回复 | `private_reply` 是否为 true；决策流是否 `silent/private_readonly` |
| 发送 `chat_name_unresolved` | 好友显示名是否在本地库唯一；是否与群重名 |
| 私聊消息被当成群 | 身份表 `peers.json` 里该会话的 `kind` 是否错误；本地库 `peer_kind` 是否返回 None |

已知边界：微信 4.2.0.11 没升级；系统号/公众号永远不会被当作好友；同名群与
好友无法自动判定，宁可两条都不回，也不猜。

---

## 18. 回执死锁与断线丢帧修复（2026-11-05 第五轮）

### 18.1 现象（14:46-14:49 实测）

- `14:46:47` 好友「苑长」发「在么」，回复在 `14:46:52` 已写进微信本地库
  （`local_id=3`），但 bot 侧一直等不到回执；`14:49:20` 报
  「微信发送等待回执超时」并入待发队列，`14:49:27` 补发成功 →
  用户收到**两条相同回复**（`local_id=3` 与 `local_id=6`）。
- `14:48:14` 的「没啥事」+ 表情在微信本地库和 `align-state.json` 里都有，
  但主库 `messages` 没有，也没有回复。
- 桥日志：`14:47:53` 帧泵广播超时（外层 5s `wait_for` 取消内层
  `connection.send`），`TimeoutError` 冒到泵外，泵退出 → supervisor
  `14:47:58` 重启桥；bot 直到 `14:49:22` 才重连。无人接收期间 poller 继续
  推进并落盘对齐窗口，这两条消息被当成基线永久吞掉。

### 18.2 根因

1. **回执死锁**：收帧循环 `async for raw in ws: await self._handle_raw(raw)`
   内联 `await handler(message)`；handler 里 `await send()` 等回执，而回执
   只有同一个循环能读 → 自锁，必然等满 `_SEND_TIMEOUT=150s` 后重试。
2. **广播超时杀泵 + 无客户端空转推进**：泵用
   `asyncio.wait_for(server.broadcast(item), 5s)` 包广播；慢客户端时外层
   取消内层 `connection.send`，`CancelledError` 不在 broadcast 的 except 里，
   冒泡成 `TimeoutError`，泵当成致命错误退出。桥在无客户端时仍 poll，
   `broadcast` 到空集合返回，帧丢但对齐器已推进。

### 18.3 修复

- `gateway/wechat.py`：
  - `message` / `recall` 在收帧循环里只做受理判定（parse + 回声过滤 +
    `last_event_at`），随后转后台任务；回执 / health / resync 仍在循环内
    即时处理。
  - 每 `(chat_type, chat_id)` 一把 FIFO 锁保序，不同会话并行；任务登记 +
    done 回调记录异常。
  - 断线只 `_fail_pending`，**不取消**在途 handler（`send` 抛
    `GatewayError` → 服务层待发队列）；仅 `stop()` 取消并回收，避免抖动
    丢掉半路回复。
- `wechat_bridge/server.py`：
  - `broadcast` 返回送达连接数，慢客户端只断自己；泵不再包外层
    `wait_for`，单次广播超时按 `delivered=0` 留帧重试，泵不退出。
  - 新增 `has_clients` / `closed` / `wait_for_client()`；无客户端时生产者
    不拉帧（对齐器冻结），`asyncio.Queue(maxsize=64)` + `await put` 背压。
  - 未送达帧持有一帧（pending），重连后先重投再继续队列，保序不丢；
    等客户端不计入 stall，只有「有客户端但源无产出」才按
    `idle_timeout/stall_limit` 停泵。
- 测试适配：`test_run_exits_nonzero_when_pump_dies` 增加一个客户端连接
  （否则按新语义泵不会推进到源耗尽）；断言不变。

### 18.4 验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 2026-11-05 第五轮：2651 passed, 20 skipped（新增 9 个回归测试）
```

实测 `16:25`：连发「测试1」「测试2」→ 单次回复、无超时、无补发、两条都
入库；第二条在第一条回复发送途中就被收帧循环读到。

### 18.5 已知边界

- 好友私聊的 `user_id` 仍是 aligner 进程内随机别名
  （`wxg:2279b5dda8fd`），桥重启会变；`chat_id`（`wxg:<alias>`）稳定。
  按会话记忆不受影响，但「按人」的跨会话画像会断。后续应让私聊帧直接
  复用稳定的 friend alias 作为 `user_id`，或把 `name → alias` 落进身份表。
- 某次发送真超过 150s 才成功时仍可能重试重复（系统性自锁已消除，这是
  极端慢发送的残余窗口）。
- 桥进程被硬杀（非正常停机）时，内存中未投递的 pending/队列帧仍会丢；
  正常停机与重连不会。

---

## 19. 私聊去重、稳定性与停止审计（2026-11-05 第六轮）

对应提交（**仅本地，未 push**）：

- `77d746b` 私聊 `user_id` 锚定稳定 `chat_id`，补好友媒体/引用入站回归
- `a8677e3` 发送 `dedupe_key` 去重，堵回执丢失导致的重复补发
- `55aac1a` supervisor 统一记录停止来源审计

### 19.1 私聊 `user_id` 稳定化（修 §18.5 第一条边界）

- `SessionAligner._user_id` 改为「`user_alias` 回调优先」：回调存在时先调用
  （允许收到空昵称），返回非空即规范成 `wxg:<alias>`；只有回调缺失或返回空
  才走原来的匿名槽位 / 昵称 uuid 缓存。
- `PollingLoop._aligner` 只在 `kind == "friend"` 时传
  `user_alias=lambda _name: chat_id`，群聊调用参数保持原样。
- 结果：好友私聊帧的 `user_id` == 稳定 `chat_id`（如 `wxg:16588ff14d86`），
  桥重启不变；群成员仍是会话内昵称别名。

### 19.2 发送 `dedupe_key`（修 §18.5 第二条边界）

- 协议：`send_text` 的 `params` 可带 `dedupe_key`（可选，无 key 时线协议与
  回执形状完全不变）。
- 服务侧只对声明 `supports_dedupe_key = True` 的 adapter 传该 kwarg，QQ 与
  所有三参测试替身不受影响。
- key 由 service 按分片生成：`reply:{chat_id}:{batch_anchor_message_id}:{index}`，
  **首发与 outbox 补发复用同一个 key**（`pending_outbox.dedupe_key` 落库，
  老库由 `_MIGRATIONS` 补列）。
- 桥侧按 `(action, chat_id, dedupe_key)` 维护进程内表：TTL 3600s、上限 1024；
  命中已完成结局直接重放（回执 `id` 用当前请求的），命中在途请求用
  `asyncio.shield` 合流到同一次执行；超时/异常不缓存，后续同 key 可重新执行。
- 残余窗口：桥重启丢内存缓存；跨重启的重复仍可能发生（见 §18.5）。

### 19.3 好友私聊媒体/引用入站回归（P2-7）

- 新增 fixture：好友会话 5 类入站（text / image / voice / file(appmsg) /
  quote），媒体文件真实存在、`media_enabled=True`。
- 断言链路 `LocalMessageStore.recent → LocalDbSource.snapshot →
  SessionAligner.poll`：`kind`、`media`（path/name/size/duration）、
  `reply_to` / `reply_to_bot` 全程透传；文件消息按现有 appmsg/link 映射，
  无媒体元数据。
- 未发现缺陷，只新增测试。

### 19.4 supervisor 停止来源审计（P3-12）

- 新增进程内 `StopOrigin` + `record_supervisor_stop` / `signal_stop`：
  SIGINT/SIGTERM（actor=system，detail.signal）、dashboard
  `/api/shutdown`（actor=`dashboard:<user>@<ip>`，reason=dashboard）、异常
  收摊（clean=false）统一登记。
- 审计事件：`supervisor.stop`（触发时）+ `supervisor.exit`（收摊完成，
  含 reason / actor / uptime_seconds / clean）。原 HTTP 级 `shutdown`
  记录保留。
- `_install_signal_handlers` 增加可注入 `on_signal(name)` 回调，测试不真发信号。

### 19.5 验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 2026-11-05 第六轮：2680 passed, 20 skipped（新增 29 个测试）
```

LSP（basedpyright）：本轮改动的 `align.py` / `poller.py` / `wechat.py` /
`server.py` 无诊断；`store.py` / `service.py` / `dashboard.py` / `main.py`
剩余报错均为改动前既有问题（行号不在本轮改动范围内）。

---

## 20. 私聊热开关、入站 seq/ack 与面板好友视图（2026-11-05 第七轮）

对应提交（**仅本地，未 push**）：

- `a8226cd` 私聊开关与好友黑白名单支持覆盖层热加载（P1-5 / P1-6 读路径）
- `5649d02` 入站帧 `seq`/`ack` 与桥未确认重投，水位落盘防回退（P3-10 / P3-11）
- `f4e45dd` 面板微信私聊只读视图与代理接口（P2-8）

### 20.1 私聊运行时热开关（P1-5 / P1-6）

- 新增 `src/dafeiyu/hotconfig.py`：`WeChatRuntime`（四个字段的不可变快照）
  与 `WeChatRuntimeView`（按 `(st_mtime_ns, st_size)` 缓存的线程安全只读视图）。
  容错契约：文件不存在 = 默认值；解析/读取失败 = 上次成功值 + 只告警一次；
  坏值/未知键忽略。消费方热路径上**永不抛异常**。
- 覆盖层只认四个键：`wechat.private_enabled` / `private_reply` /
  `private_allow` / `private_deny`；其余 `gateway.wechat.*` 仍由 `config.yaml`
  管理（`_apply_wechat_overrides` 只合并这四个键）。
- 名单元素必须是 `wxg:<alias>`（`parse_wechat_peer_ids`，统一 casefold；
  纯数字 / `qq:` 前缀直接报错）。语义：deny 优先，allow 为空 = 不限制，
  两个都空 = 放行全部好友；**群聊完全不受名单影响**。
- 桥侧：`PollingLoop` 增加 `runtime` 注入（`_default_poller_factory` 传
  `PanelRuntime().wechat`），`_scope()` / `allows_chat()` / 好友枚举每轮热读；
  解析好友稳定 id 失败时 fail-closed 跳过该好友。
- bot 侧：`Service._wechat_private_readonly` 改读同一份热视图；读失败回落
  `settings.wechat.private_reply`。`private_reply=false` 时私聊写
  `silent/private_readonly` 决策后直接返回，命令仍可用。

### 20.2 入站 seq/ack 与未确认重投（P3-10）

- 协议：message/recall 帧由桥分配单调 `seq`；health/startup 永不带 seq。
- 客户端（`gateway/wechat.py`）：连上即发 `hello{last_seq}`；handler 成功
  消化后回 `ack{seq}`；坏帧/回声帧/无 handler 也 ack（永不进 handler 的帧
  不该占缓冲）。handler 抛异常时不 ack，帧留在桥侧等待重投。
- 桥（`server.py`）：`UNACKED_MAX=200` 有界未确认缓冲；`ack` 单调裁剪；
  `hello` 首帧按客户端 `last_seq` 升序重投更高的未确认帧。
- **本轮修掉的回归**：重投原先被「该连接已发历史」挡住——历史只证明
  「发过」不证明「处理过」，同一 socket 缓冲丢失时 hello 是唯一补救。
  现在重投以客户端 `last_seq` 为准；已发历史仅用于让随后的帧泵 pending
  重试跳过同一帧，保证「同一帧对同一连接不出两遍」。
- health 帧新增 `inbound_seq` / `inbound_acked` / `unacked_count`，
  丢帧窗口 = `inbound_seq - inbound_acked`。

### 20.3 桥水位落盘（P3-11 第一阶段）

- `data/wechat-bridge/inbound_state.json`：`{"seq": n, "acked": n}`，
  临时文件 + `os.replace` 原子替换；缺文件/损坏/越界一律按 0 处理并告警。
- 目的：崩溃/重启后 `seq` 不回退，避免新旧帧 seq 冲突。
- **已知边界**：此阶段只落水位，未确认帧本身仍在内存；硬杀仍会丢帧。
  帧级持久化见下一轮（§21）。

### 20.4 面板微信私聊视图（P2-8，只读）

- `GET /api/private/chats`：只列 `chat_type=private` 且 `chat_id` 以
  `wxg:` 开头的会话，按最近活跃倒序；标题取最近一条非 bot 消息昵称，
  失败只回落 `chat_id`（单会话失败不影响整表）。字段：`title` /
  `message_count` / `last_message_at` / `delivered_count` / `session_id`。
- 面板新增「微信私聊」卡：只读表格 + 刷新；不重复显示已有列。
- 写路径（开关 / 黑白名单编辑）见 §21。

### 20.5 验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 2026-11-05 第七轮：2723 passed, 20 skipped（修复配置示例缺字段后 4 passed）
```

- 新增测试：`tests/test_hotconfig.py`；`test_settings_overrides` / `test_config`
  覆盖层四键；`test_wechat_bridge_poller` 热读与名单闸门；
  `test_service_decisions` 只读热切换；`test_wechat_bridge_main` hello 重投、
  ack 单调、水位恢复；`test_control_api` 私聊列表；`test_wechat_adapter`
  客户端 hello/ack。
- `config.example.yaml` 增补 `private_allow: []` / `private_deny: []`
  （`test_example_config` 要求字段集合与 `WeChatConfig` 完全一致）。

---

## 21. 私聊开关写路径、帧级重放与回执口径（2026-11-05 第八轮）

本轮改动与 §20 同批提交（本地，未 push）。

### 21.1 面板可编辑私聊开关（P1-5 / P1-6 写路径）

- `BrainControl.private_settings_snapshot()`：返回
  `{settings, chats, note, hot_reload}`；`chats` 直接复用
  `private_chats_snapshot()`，不重复查询。`settings` 以覆盖层
  `wechat:` 段为准（复用 `hotconfig.WeChatRuntimeView` 的合并语义），
  解析失败时回落启动配置。
- `BrainControl.save_private_settings(payload)`：只接受四个键；未知键 /
  空 body / 非严格布尔 / 非 `wxg:` 名单在**写文件前**报错；只改 payload
  命中的键，保留其它覆盖段；`write_overrides` 原子写；随后同步
  `self.settings.wechat` 并返回快照 + `restart_required=false`。
- 接口：`GET /api/private/settings`、`PUT /api/private/settings`
  （bot 控制口 8788；面板 8787 代理同名路由，PUT 原样转发 body 并透传
  状态码/错误 JSON）。
- 面板「微信私聊」卡改为可编辑（未新增重复卡片）：
  - 复选框「接入私聊」/「自动回复（需先接入私聊）」；
  - 「允许名单」/「拒绝名单」textarea，一行一个 `wxg:...`；
  - 好友表每行「允许 / 拒绝 / 移除」：仅编辑前端内存（两名单互斥、
    去重、casefold），点「保存」才提交；
  - 保存成功提示「已保存 · 一个轮询周期内热生效」。
- 跨字段不强制：`private_reply=true` 与 `private_enabled=false` 可同时
  保存（与配置语义一致），UI 文案已标注依赖关系。

### 21.2 帧级持久化与硬杀重放（P3-11 完整）

- `data/wechat-bridge/inbound_state.json` 扩展为
  `{"seq": n, "acked": n, "frames": [{"seq": n, "frame": {...}}, ...]}`：
  只含未确认 message/recall 帧，升序，最多 `UNACKED_MAX=200` 条。
- 每分配一个新 seq 即落盘（不再等 50 帧），ack 裁剪后立即落盘；仍是
  临时文件 + `os.replace` 原子替换，失败只 debug。
- 启动恢复 `_restore_unacked()`：逐条校验 `seq` 为正整数、`frame` 为
  dict、内外 seq 一致、`acked < seq <= 文件 seq`；重复 seq 只留第一条；
  非法条目汇总为一次 warning；结果取最新 200 条窗口。
- 旧格式（无 `frames`）按空缓冲处理；文件损坏仍从 0 开始，不影响启动。
- 硬杀后：新桥实例 + 客户端 `hello{last_seq}` → 按原 seq 升序重放未确认
  帧；ack 后才裁剪。已知边界：超过 200 帧窗口的旧帧不可重放（由
  health 的 `unacked_count` 体现）。

### 21.3 发送回执口径统一（P2-9）

- `WeChatAdapter.receipts` 四类计数，唯一计数入口是 `_send_action()`：
  - `observed`：回执 `delivered=="observed"`；
  - `unverified`：`ok=true` 但 `error_code=="receipt_unverified"`；
  - `typed`：其它成功（含无错误码的 `typed`）；
  - `failed`：`ok=false` / `delivered=="failed"` / 等待回执超时 / 断线 /
    `_fail_pending` 注入的失败；
  - `unsupported_media` 的能力不支持提前返回**不计数**（不是投递尝试）。
- `receipt_stats()`：`{observed, typed, unverified, failed, total,
  unverified_rate, last_at}`；`unverified_rate` 分母为
  `observed+typed+unverified`（失败不进分母），无回执时为 0.0；
  返回副本，读取出错返回全零快照并 warning，不抛异常。
- `Service.channel_status()` 仅对**没有 `snapshot()`** 的适配器注入
  `receipts`；调用异常只 warning、不加键，其它通道不受影响。
- 健康快照透传 `channels.wechat.receipts`（`last_at` 转 ISO），面板
  「微信通道」卡显示「证实 A · 键入 B · 未证实 C · 失败 D」；
  `unverified_rate >= 0.2` 且 `unverified >= 5` 时追加提示。
- 计数是进程内内存值，重启归零；桥侧在收到 WS 指令前的失败不计入。

### 21.4 落盘显式开启与测试隔离

- 入站水位持久化只在**显式传入** `state_path` 时开启：
  - 生产入口 `main()` 传 `state_path=DEFAULT_INBOUND_STATE`（`run()` 透传）；
  - `BridgeServer()` / `run()` 默认 `state_path=None`，`_load_inbound_state` /
    `_save_inbound_state` 直接返回，纯内存、零副作用。
- 修复过程：不带 `state_path` 的既有测试曾把伪造帧写进
  `data/wechat-bridge/inbound_state.json`（seq/acked 与测试帧混杂）。现在
  新增回归：`test_no_state_path_means_no_persistence`、
  `test_run_forwards_state_path_to_server_factory`、
  `test_main_entry_enables_state_persistence`。已删除被污染的生产状态文件。
- 验证：`tests/test_wechat_bridge_main.py` 50 passed；全量
  `pytest -q` = **2746 passed, 20 skipped**（5:00）；改动文件 basedpyright
  无新增 error（既有 warning 未处理）。
- 运行态复核（重启整栈后）：8080/8787/8788/8790/24310/30012 均监听；
  hook `/health` = `ready:true`、`wechat_version:4.2.0.11`；
  `GET /api/private/settings` = `private_enabled/private_reply=true` 且
  `hot_reload:true`；`GET /api/health/detail` 的
  `channels.wechat.receipts` 六项齐全；重启后尚无入站事件，故
  `inbound_state.json` 未生成（符合「有帧才落盘」）。


## 22. 表情保真、表情弱信号与微信图片现状（2026-11-05 第十轮）

### 22.1 微信/QQ 表情保真

- 微信 `type=47` 解压后的 `<emoji md5="..." desc="..." attachedtext="..."/>`
  现在优先用 `desc`/`attachedtext` 作可读名，缺失时用 md5 前 8 位，统一渲染成
  `[表情:<标识>]`；解析失败、无 emoji 节点或旧库不带该属性时回退 `[表情]`。
  消息 `kind` 仍是 `sticker`。
- 微信 `type=47` 的附件 `kind` 改用媒体自身 kind（图片即 `image`），
  这样贴纸图片能进入图片物化管线（此前按 `sticker` 被当成不支持的媒体丢弃）。
- QQ `face` 段渲染成 `[表情:<数字 id>]`（非数字回退 `[表情]`），
  `mface` 段用压缩空白并截断 20 字的 `summary`（空则回退 `[表情]`），
  表情不再渲染成空正文。

### 22.2 表情弱信号

- `moment.is_sticker_content()` 识别「整条只有一个表情占位」，兼容旧 `[表情]`、
  带名 `[表情:狗头]` 和物化后的「（图存 ...）」注记。
- `is_short_only()` 把带名字的表情也算短消息：`[表情:狗头]` 超过 4 字，
  以前会把一整批无信息量消息误判成「有话可接」。
- 新增 `MomentSignals.sticker`；`hint()` 在纯表情批次下从「倾向不说」改为
  「有同感可简短接一句，不必硬找话题」。
- `moment.follows_bot_expression()`：上一条是 bot、本条是纯表情、间隔不超过
  180 秒，视为弱回应。`Service._weakly_addressed()` 用它（与
  `answers_bot_question` 同口径，仅群聊），让 `_opportunity_hold` 不再一票否决，
  并让 `_run_batch_locked` 的 `followed_up` 覆盖这种情况。它不并入
  `_is_addressed`，避免表情重置 no_reply/降频、影响调度防抖与预算。
- `brain/persona.md` 与 `brain/agent_template.md` 各加一句：看到 `[表情:xxx]`
  别只当刷屏，可能是在回你。

### 22.3 微信图片：链路已通，真实可见仍缺两块

- 真实会话快照（4 个群，`LocalDbSource.snapshot`）为 78 text / 3 sticker /
  11 image / 1 quote，但 **`with_media=0`**。原因有二：
  1. `repository._media_rows` 只查 message 分库与
     `message/message_resource.db`，**从不打开 `hardlink/hardlink.db`**，
     所以帧里 `media` 恒为 null；
  2. `snapshot()` 返回帧不含 `local_id`/`media` 字段，适配层也拿不到媒体。
- `media/dat.py` 只支持旧容器（无头 / 单字节 XOR），**不支持微信 4.2 的 V2 容器**：
  `07 08 56 32 08 07` + `aes_size(u32 LE)` + `xor_size(u32 LE)` + pad，
  body = `[AES-128-ECB 密文][raw 16B][XOR 段]`。
- 已在真实库只读实证 V2 解码：`msg/attach/<talker>/<YYYY-MM>/Img/<md5>.dat`
  的 `_t.dat` 用 `aes_key = md5("1721210299" + wxid)[:16]`、`xor=0xBB`、`raw=16`
  解出完整 JPEG（`FFD8FF` 头、`FFD9` 尾）。`cfgDword=1721210299` 的低字节
  `0xBB` 即 XOR key：可从 V2 文件尾（JPEG `FFD9`）反推 XOR key，再扫
  Weixin.exe 内存中低字节相同的 u32，用 `md5(str(dword)+wxid)[:16]` 验证首块魔数。
  图片索引在 `hardlink/hardlink.db` 的 `image_hardlink_info_v4`
  （md5/file_name/file_size/dir1/dir2）。
- 表情缓存 `cache/<YYYY-MM>/Emoticon/<md5[:2]>/<md5>` 既非 V2 也非单字节 XOR，
  容器格式未知；当前只做正文保真，不做图片化。
- 依赖现状：venv 无 `cryptography`/`Crypto`。AES-128-ECB 可走 Windows
  `bcrypt.dll`（ctypes 已验证可解），或内置纯 Python 实现，二者都不必新增依赖。
- 待决策：是否接受读 Weixin.exe 进程内存取 `cfgDword`、是否新增 AES 依赖。

### 22.4 图片清理

- 微信/QQ 共用的图片目录复用 `images.retention_days`，在
  `media.materialize_attachments` 首次真正落盘前惰性清理；`> 0` 删除超期文件，
  `0` 不清理，清理失败只 debug 日志、不影响主流程。

### 22.5 验证

- 定向：`tests/test_moment.py test_opportunity.py test_qq_parse.py
  test_wechat_localdb_repository.py test_wechat_adapter.py test_media.py`
  = 180 passed。
- 全量：`pytest -q` = 2764 passed, 20 skipped, 1 failed（5:09）；失败项
  `test_wechat_hook_server.py::test_unknown_path_is_404_not_crash` 单独重跑通过，
  属端口/事件循环偶发，与本次改动无关。
- basedpyright：改动文件无新增 error（既有 error 未处理）。

## 23. 微信图片真正可见：媒体索引 + V2 解密 + 自动取 key（2026-11-05 第十一轮）

### 23.1 结论

微信入站图片/贴纸现在能真正落盘并被模型 `read` 查看。真实库只读验证：

- `LocalDbSource.snapshot` 在 4 个群中统计到 **13 条 image，全部 `with_media=13`**
  （上一轮为 `with_media=0`）；
- 取 10 条真实 `.dat` 用自动发现的 key 解码，**10/10 成功**：
  4 张 `_h`（2 张 PNG，最大 2.6MB；2 张 JPEG）+ 6 张 `_t` JPEG；
  大图 PNG 的 330 个 chunk **CRC 全部通过**、IEND 后无多余字节；
- `materialize_attachments` 端到端：真实 `_h.dat` → JPEG 写入
  `<workspace.dir>/images/`，正文变成
  `[图片]（图存 images/…，可用 read 查看）`。

### 23.2 媒体发现（`localdb/repository.py`）

- `_media_rows` 新增 `username` 参数，除原有路径列/`message_resource.db` 表查询外，
  追加 `_resource_media`：
  - 从 `MessageResourceInfo.packed_info` 的 blob 里取 32 位 hex md5；
  - 只在能找到 `chat_id`（`ChatName2Id` rowid）时按会话过滤，
    定位失败则只按 `message_local_id` 查（fail-open，最多多一次磁盘探测）；
  - 按 `msg/attach/<md5(chat)>/<YYYY-MM>/Img/<md5>[_h|_t].dat` 找盘上文件。
- `_media_root()` 改为返回 `db_storage` 的**父目录**（4.x 的 `msg/attach` 在账号根）。
- 变体优先级 `_h` > `_t` > 原图（`_VARIANT_ORDER`）：原图多为 `wxgf`，
  `read` 打不开；`align.Entry` 已透传 `media`，帧里可直接带路径。
- 新来源只补缺（`setdefault`），不覆盖旧路径列结果；任何异常都返回空。

### 23.3 V2 解码（`media/dat.py` + `media/aes.py`）

- 新增纯 Python AES-128/192/256 ECB（S 盒运行时生成），FIPS-197 三组向量通过；
  不新增第三方依赖。
- `decode_dat(data, aes_key=..., xor_key=...)`：
  - 旧单字节 XOR / 明文透传行为不变；
  - V2 布局：`15B 头 + AES((aes_size//16+1)*16) + 明文 middle + XOR(xor_size)`；
    middle 长度不写死，兼容「16B 分隔」与「1MiB 以上的大图明文」；
  - 缺 key / 长度不符 / 解密失败一律 `None`；`wxgf` 即使解出也返回 `None`。

### 23.4 密钥发现（`media/keygen.py`）

- `derive_aes_key(dword, wxid)` = `md5(f"{dword}{wxid}").hexdigest()[:16]`；
- `guess_xor_key(sample)` 从 V2 尾部的 JPEG `FFD9` / PNG `IEND` 反推单字节 key；
- `MediaKeyResolver`：
  - 先查 `data/wechat-bridge/localdb/media_key.json`，用当前样本完整解码验证，
    无效/换账号自动重扫；
  - 只读扫描 `Weixin.exe`（`keyscan.ProcessMemory`）：先按 wxid 预筛进程、
    再扫 wxid 邻近 ±4KB 的 u32，最后全量兜底；强图片头过滤 `BM`/`RIFF`
    一类短魔数误命中，再用完整 `decode_dat` 复核；
  - 失败冷却 300s，避免每张图都重扫。
- `Service` 接线：`start()` 后台线程 `prewarm()`；`handle()` 用
  `_decode_wechat_dat`，命中进程内缓存同步解，未命中只把扫描丢到后台线程
  （同一时刻最多一个），事件环不做内存扫描；任何失败保留占位。

### 23.5 仍未解决

- 表情缓存 `cache/<YYYY-MM>/Emoticon/<md5[:2]>/<md5>` 既非 V2 也非单字节 XOR，
  容器格式未知；表情目前仍是「正文保真 + type47 贴纸走图片管线」，
  Name 表情本身不做图片化。
- `hardlink/hardlink.db` 当前打不开（`file is not a database`，疑似加密/新容器），
  媒体发现没有依赖它；若未来 `message_resource.db` 不可用，可再研究该库。

### 23.6 验证

- 定向：`tests/test_wechat_dat_v2.py tests/test_wechat_media_keygen.py
  tests/test_service_wechat_media.py tests/test_wechat_localdb_repository.py
  tests/test_media.py` = 111 passed。
- 全量：见本轮提交说明（若出现 `test_wechat_hook_server` 偶发失败，单独重跑确认）。
- basedpyright：改动文件无 error（既有 warning 保留）。
