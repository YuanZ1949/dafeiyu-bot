# 微信非 UIA 出站后端（wechat hook）

2026-11-05。给 `gateway.wechat.outbound: hook` 用的本地服务：**不点窗口、不模拟键鼠、不要求桌面解锁**，
直接走微信自己的发送队列。

## 结构

```
dafeiyu wechat_bridge (outbound=hook)
        │  POST /send_text  {"action","chatroom","chat_name","text"}
        ▼
scripts/wechat_hook_server.py         ← 本仓库，HTTP 契约层（:30012，端口跟随 hook_url）
        │  进程内调用
        ▼
helper/native/text_bridge.dll         ← Prometheus-first/helper，注入 Weixin.exe
        │  版本指纹：Weixin.dll size + SHA-256
        ▼
微信自己的发送队列（无 UIA / 无键鼠）
```

- 入站仍是原有的本地库镜像（`local_db_enabled`），同样不依赖 UIA。
- 健康帧在 `outbound != uia` 时不再调用 `about()/language()`：锁屏时
  `logged_in=true / desktop=locked / degraded=false`，既不白等 UIA 超时，
  也不会把可用的通道误报成降级。
- 出站由本服务承担；`outbound: auto` 时只有本服务明确回 `sent:false` 才回落 UIA。
- 版本指纹已核对：本机 `Weixin.dll` 4.2.0.11 / 205929008 bytes /
  `745cdf29…46b201`，与 `helper/native/compatibility.json` 一致。

## 依赖

| 组件 | 位置 |
| --- | --- |
| helper（原生桥 + DLL） | `C:\MyFile\AICoding\helper`（`WECHAT_HELPER_ROOT` 可覆盖） |
| Python 环境 | `helper\.venv`（3.11+，`pip install -r helper\requirements.txt`） |
| 微信 | 4.1.15.13 / 4.1.15.56 / 4.2.0.11 / 4.2.0.12 的**指定构建**，其余版本 helper 直接拒绝加载 |

## 启动

`start-all.ps1` 会自动拉起（看到 `wechat.enabled=true` 且 `outbound=hook/auto` 时），
`stop.ps1` 会顺带收掉。也可以单独控制：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-wechat-hook.ps1
powershell -ExecutionPolicy Bypass -File scripts\start-wechat-hook.ps1 -Stop
```

脚本会脱离 SSH/终端会话启动（`Win32_Process.Create`），否则 SSH 断开就把进程带走了。
日志：`logs/wechat-hook.log`。

端口以 `gateway.wechat.hook_url` 为准：`start-all.ps1` 解析出端口后传给
`start-wechat-hook.ps1 -Port`。若端口已被占用、但占用者的命令行读不到（通常是
更高权限启动的旧实例），脚本会直接报错并要求你换端口或用管理员结束它，
**不会**再起一个收不到请求、却让 `/health` 读到旧状态的重复实例。

账号以 `gateway.wechat.account` 为准：`start-all.ps1` 读出后传给
`start-wechat-hook.ps1 -Account`。同时登录了两个及以上微信账号时 helper 无法自动选择，
必须显式绑定，否则启动报「多个微信账号同时登录，请用 wechat_account 绑定机器人账号」。
该值可在面板「微信账号」卡片保存（写入 `data/settings.local.yaml`），留空 = 不绑定，
仅在登录一个账号时自动识别。保存后要重启 hook 才生效。

手动前台调试：

```powershell
C:\MyFile\AICoding\helper\.venv\Scripts\python.exe scripts\wechat_hook_server.py --log-level DEBUG
```

## 自检

```powershell
curl http://127.0.0.1:30012/health
# {"ok":true,"ready":true,"self_wxid":"wxid_...","wechat_version":"4.2.0.11"}

# 真发一条到文件传输助手
curl -X POST http://127.0.0.1:30012/send_text -H "Content-Type: application/json" `
  -d '{"chatroom":"filehelper","chat_name":"文件传输助手","text":"hook self-test"}'
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health`、`/probe` | 就绪状态、当前 wxid、微信版本、最近错误 |
| POST | `/send_text` | `{chatroom, chat_name?, text}` → `{ok:true, message_id}` |
| POST | `/send_at` | 同上 + `at: ["wxid_..."]`（群聊，helper 侧支持真实 @） |
| POST | `/send_media` | `{chatroom, kind: image/file/voice/sticker, path}` |

失败语义（客户端据此决定是否回落 UIA）：

- `{"ok":false,"sent":false,"error_code":"..."}` —— 明确没发出去（未就绪、参数错、DLL 回 success=false）；
- `{"ok":false,"error_code":"hook_unknown"}` —— 结果不明（DLL 调用异常），**不回落**，避免重复发送。

## 配置

```yaml
gateway:
  wechat:
    enabled: true
    outbound: hook
    hook_url: http://127.0.0.1:30012/send_text
    hook_timeout_seconds: 30     # 原生桥单次发送可能 1–3s，5s 太紧
    local_db_enabled: true       # 入站（非 UIA）+ 群名 -> xxx@chatroom 解析
```

`HookTransport` 的 `chatroom` 由本地库把群显示名解析成 `xxx@chatroom`；本地库不可用时
直接 `chat_name_unresolved` 失败，不会猜 id。

## 边界与风险

- **封号风险自担**：DLL 注入属于侵入式改造，违反微信使用条款。
- **版本绑定**：微信一升级，指纹校验失败会拒绝加载，需要等 helper 适配或回退版本。
- **账号**：服务绑定“当前登录的账号”；多开微信时必须用 `-Account` / `gateway.wechat.account`
  显式指定机器人 wxid（面板可配），换号后需要重启服务（日志会打印 wxid）。
- **群名歧义**：同名群由本地库解析决定，解析不出就失败。
- **@ 定向**：已打通。dafeiyu 传显示名 → 服务端用 helper 的 `group_members` 解析成 wxid；
  同名/查不到时回 `at_unresolved`，调用方退回普通文本（绝不猜人 @）。UIA 后端仍回
  `unsupported_at`。
- **媒体**：已打通。`Service._send_chunk` 对没有 `send_segments` 的通道逐条调
  `send_media`，失败只记日志、正文照发。支持 image / file / voice / sticker。
- **静默时段**：`gateway.wechat.quiet_hours` 不写时继承 `triggers.quiet_hours`（起止相同 =
  不静默）；显式写了就按自己这份拦。两处各写一份且不一致时最容易出现「triggers 放行、
  发送端仍拦」的静默发不出去。
