# 交接：微信非 UIA 出站 + 自备 Key（2026-11-05）

> 读者：接手这台 Windows 机器 / 这个仓库的人。
> 前置阅读：`docs/wechat-hook-backend.md`（链路与接口设计）、`docs/deployment-notes-windows.md`。
> 本文只写「现在是什么状态、怎么操作、坏了怎么查、还欠什么」。

---

## 0. 一句话

微信出站已经**不依赖 UI 自动化**：bot → 本地 hook 服务(:30011) → `Prometheus-first/helper`
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
| 微信 hook 服务 | `http://127.0.0.1:30011` | ready，微信 4.2.0.11 |
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
出站 微信   bot ──HTTP──> hook 服务(:30011) ──> helper 原生桥 ──> 微信发送队列（无键鼠）
出站 QQ     bot ──ws──> NapCat
大脑        bot ──HTTP──> opencode serve(24310) ──> api.deepseek.com
托管        supervisor(8787) 管 opencode / bot / napcat / 桥；hook 服务独立进程
```

要点：

- **桥和 hook 是两套进程**。桥(8790) 负责入站观测 + 发送编排（限速、补发、@解析），
  hook(30011) 只负责「把这条消息真正塞进微信」。
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
    hook_url: http://127.0.0.1:30011/send_text
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
Invoke-RestMethod http://127.0.0.1:30011/health | ConvertTo-Json

# 大脑档位 / Key 是否就位 / 免费档是否可用
$t=(Get-Content data\supervisor.token -Raw).Trim()
Invoke-RestMethod http://127.0.0.1:8788/api/brain -Headers @{'X-Control-Token'=$t} | ConvertTo-Json -Depth 4

# 端口
foreach ($p in 8787,8788,8790,30011,8080,24310) {
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
- 手工验证发送：`Invoke-RestMethod :30011/health` → `ready:true`，然后从面板发一条。

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
