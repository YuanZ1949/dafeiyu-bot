# OpenCode 专用实例 + 本地控制台设计

日期：2026-10-24  
项目：`dafeiyu-bot`  
状态：关键决策已确认，分阶段实现

## 1. 目标

1. 机器人使用一个**专用 OpenCode 服务**，不再依赖当前共享的 `0.0.0.0:4300` 服务。
2. 专用服务随机器人启动/停止；后续由 supervisor 接管完整生命周期。
3. 专用 OpenCode 使用**独立数据库**，不与个人 OpenCode 的 session 混在一起。
4. 群聊和私聊在底层都按 `(chat_type, chat_id)` 一个 session 处理；群聊补上 `/forget`。
5. 二维码页升级为本地控制台：状态、日志、二维码、会话列表/管理、启停控制。
6. OpenCode 调用失败时，聊天窗口收到一句简短提示；完整错误只进日志。

## 2. 非目标

- 不为每个群或每个私聊单独启动一个 OpenCode 进程；只启动一个 `opencode serve`，内部按 session 隔离。
- 不把所有群/私聊塞进同一个 session；那会串线。
- 不把 OpenCode 原始 stderr、JSON、堆栈或本机路径发到群聊。
- 不在本阶段做语音、图片生成、TTS 等无关能力。
- 不删除旧 OpenCode session 作为 `/forget` 的默认行为。

## 3. 已确认决策

| 决策 | 结论 |
| --- | --- |
| OpenCode 实例 | 一个专用 `opencode serve`，不是每聊天一个进程 |
| 数据隔离 | 独立 DB（用户选择） |
| 控制台范围 | 全控制：启动/停止/重启 NapCat、OpenCode、bot（用户选择） |
| 群聊 `/forget` | 支持；建议仅管理员，避免群友清空共享记忆 |
| `/forget` 语义 | 默认软忘记：清本地映射 + 本地消息，旧 session 保留；硬删除由控制台执行 |
| 错误回聊天 | 简短人话回聊天，完整异常进日志 |

## 4. 当前实现

- `OpenCodeBrain` 每条消息执行一次：
  `opencode run --agent deepseek-qq --format json [--session ses_x] <text>`
- 当前没有 `--server` 参数，默认连共享服务 `http://0.0.0.0:4300`。
- session 映射在机器人 SQLite 的 `opencode_sessions` 表，键为 `(chat_type, chat_id)`。
- 群聊 `_run_command` 只允许 `/闭嘴`、`/醒来`，`/forget` 未接入群聊。
- 私聊 `/forget` 清本地消息和映射，但不删除 OpenCode 侧旧 session。
- `Service._reply` 捕获 `BrainError` 后只记日志，不向聊天发送任何内容。
- `start-all.ps1` 会新开 NapCat 窗口、隐藏二维码 helper、在当前控制台运行 bot。
- `napcat-qr.html` 只是静态二维码查看器。

## 5. 目标架构

```text
start-all.ps1（只做隐藏启动）
  └─ supervisor（Phase 2 起）
       ├─ NapCat        node.exe index.js
       ├─ OpenCode      opencode serve --hostname 127.0.0.1 --port 24310
       ├─ Dafeiyu bot   python -m dafeiyu
       └─ Dashboard     127.0.0.1:8787
            ├─ 状态 / 启停 / 重启
            ├─ 二维码
            ├─ 日志（SSE）
            └─ 会话查看 / 忘记 / 删除
```

### 5.1 Phase 1：专用 OpenCode + 群 forget + 错误回聊天

- 新增 `OpenCodeServer` 进程管理器。
- 由 bot 进程在启动时拉起 `opencode serve`，退出时停止。
- 配置 `OPENCODE_DB` 指向 `data/opencode-bot.db`。
- `OpenCodeBrain` 增加 `--server http://127.0.0.1:24310`。
- 群聊 `/forget` 接入，管理员限制。
- `_reply` 在 `BrainError` 时发送 `BRAIN_ERROR_REPLY`。
- 这一阶段不引入 dashboard，不改变 NapCat 启动方式。

Phase 1 的 bot-managed 模式是过渡方案：bot 崩溃时 serve 可能残留；Phase 2 由 supervisor 接管后解决。

### 5.2 Phase 2：supervisor + dashboard 状态/日志/二维码

- 新增 Python supervisor，`start-all.ps1` 只负责隐藏启动 supervisor。
- supervisor 启动 NapCat、OpenCode、bot，捕获三者 stdout/stderr。
- dashboard 监听 `127.0.0.1:8787`，提供状态、日志 SSE、二维码。
- bot 改为 `manage_server: false`，只作为 OpenCode 客户端；`OpenCodeServer` 类由 supervisor 复用。
- 所有控制台窗口隐藏；日志写 `logs/supervisor/*.log`，同时进 dashboard。

### 5.3 Phase 3：会话管理与完整控制

- dashboard 读取机器人 DB 的 `opencode_sessions` 映射，并通过 OpenCode API 列出 session。
- 支持查看消息、软忘记、硬删除、单会话新建。
- 支持启动/停止/重启全部；停止时按 bot → OpenCode → NapCat 顺序，超时用 `taskkill /T /F`。
- 启动时清理 stale PID/端口，异常退出在 dashboard 显示退出码和最后错误。

## 6. 配置

Phase 1 在 `config.yaml` 增加：

```yaml
opencode:
  bin: ""
  agent: deepseek-qq
  project_dir: ""
  timeout_seconds: 90
  # true：由 bot 自己拉起专用 serve；Phase 2 改由 supervisor 拉起后设为 false
  manage_server: true
  # 外部服务地址；manage_server=true 时必须为空
  server_url: ""
  db_path: data/opencode-bot.db
  host: 127.0.0.1
  port: 24310
  startup_timeout_seconds: 30
```

规则：

- `manage_server: true` 时，`server_url` 必须为空，使用 `http://{host}:{port}`。
- `manage_server: false` 且 `server_url` 非空时，连接该地址。
- 两者都为空/false 时，保持旧行为：不传 `--server`，连共享服务。
- `port` 必须在 1–65535。
- `startup_timeout_seconds` 必须大于 0。
- `db_path` 相对路径按 `project_dir` 解析；`project_dir` 为空时按当前工作目录解析。

Phase 2 另加：

```yaml
supervisor:
  dashboard:
    host: 127.0.0.1
    port: 8787
    open_browser: true
    # 可选；非空时破坏性操作需要带 token
    token: ""
```

## 7. 数据、端口、日志

| 项 | 值 |
| --- | --- |
| 机器人 DB | `data/dafeiyu.db`（现状） |
| OpenCode DB | `data/opencode-bot.db`（独立） |
| OpenCode API | `http://127.0.0.1:24310` |
| dashboard | `http://127.0.0.1:8787` |
| NapCat WS | `ws://127.0.0.1:8080`（现状） |
| Phase 1 OpenCode 日志 | 启动失败时记录 stderr；Phase 2 起写 `logs/supervisor/opencode.log` |

独立 DB 的代价：个人 OpenCode UI 默认看不到 bot 的 session；这是已确认的取舍。dashboard 负责查看和管理。

当前机器人库的 `opencode_sessions` 已清空，因此切换独立 DB 不需要迁移。如果以后切回共享 DB，旧映射可能失效，`OpenCodeBrain` 现有 stale-session 重试会清映射并新建。

## 8. 组件接口

### 8.1 `OpenCodeServer`

文件：`src/dafeiyu/brain/opencode_server.py`

```python
class OpenCodeServer:
    def __init__(
        self,
        bin_path: str,
        project_dir: str,
        db_path: str,
        host: str = "127.0.0.1",
        port: int = 24310,
        startup_timeout: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> None: ...

    @property
    def url(self) -> str: ...

    @property
    def command(self) -> list[str]: ...

    @property
    def is_running(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
```

- `command` = `[bin, "serve", "--hostname", host, "--port", str(port)]`。
- `_build_env()` 复制当前环境，加入 `OPENCODE_DB` 绝对路径和 `NO_COLOR=1`。
- `start()` 启动子进程后轮询 `GET /api/info`，直到 200 或超时；超时会停止进程并抛 `BrainError`。
- `stop()` 先 `terminate`，等待 5 秒，必要时 `kill`；可重复调用。
- 子进程 stdout/stderr 用二进制管道；错误信息解码用 UTF-8 `errors="replace"`。

### 8.2 `OpenCodeBrain`

文件：`src/dafeiyu/brain/opencode.py`

新增构造参数：

```python
server_url: str = ""
```

`_build_command` 顺序：

```text
[bin, "run"]
  + ["--server", server_url] if server_url
  + ["--agent", agent, "--format", "json"]
  + ["--session", session_id] if session_id
  + [text]
```

`forget()` 仍只清本地映射，不删除 OpenCode session。

### 8.3 `Service`

文件：`src/dafeiyu/service.py`

- 新增模块常量：
  - `FORGET_REPLY = "记忆清空了，本鱼失忆中……🫧"`
  - `BRAIN_ERROR_REPLY = "本鱼刚刚和大脑断线了…你过一会儿再问我？🐋"`
- 提取 `_forget_chat(msg)`，私聊和群聊共用。
- 群聊 `_run_command` 允许 `forget`，但必须先通过管理员检查。
- `_reply` 捕获 `BrainError` 后：
  1. 记录 `error` usage；
  2. 记录完整 warning 日志；
  3. `await adapter.send(..., BRAIN_ERROR_REPLY)`。

### 8.4 Dashboard（Phase 2/3）

建议接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | supervisor 自身健康 |
| GET | `/api/status` | 三个进程状态、PID、端口、退出码 |
| GET | `/api/logs/stream?source=` | SSE 日志流 |
| GET | `/api/qr` | 二维码状态 |
| GET | `/api/qr/image` | 二维码图片 |
| GET | `/api/sessions` | 群/私聊映射 + OpenCode session |
| POST | `/api/sessions/{chat_type}/{chat_id}/forget` | 软忘记 |
| DELETE | `/api/opencode/sessions/{session_id}` | 硬删除 OpenCode session |
| POST | `/api/control/{start|stop|restart}` | 全控制 |

dashboard 代理 OpenCode API，不把 24310 直接暴露给浏览器；默认只绑 `127.0.0.1`。

## 9. 行为语义

### 9.1 Session

- 一个 `(chat_type, chat_id)` 一个 session。
- 群聊仍然只对 @、关键词、采样消息触发；私聊仍按预算触发。
- 群聊消息继续以 `昵称：内容` 传给模型，避免多人对话混淆。
- 群/私聊的 session 模型一致，但权限和触发策略不同。

### 9.2 Forget

- 默认软忘记：清 `opencode_sessions` 映射和本地 `messages`，旧 OpenCode session 保留。
- 下一条消息会创建新 session。
- 群聊 `/forget` 仅管理员；非管理员发 `/forget` 不应清空任何内容。
- 硬删除只在 dashboard 中执行，调用 `DELETE /api/session/{id}`。

### 9.3 Errors

- `BrainError`：回 `BRAIN_ERROR_REPLY`，完整异常进日志。
- JSON 解析失败：继续走现有 `FALLBACK_REPLY`，不额外报错。
- 原始 stderr、路径、token 不进聊天。
- Phase 1 不做错误回复冷却；如果实际出现刷屏，Phase 2 增加每 chat 30 秒冷却。

## 10. 安全

- 专用 OpenCode 和 dashboard 都只绑 `127.0.0.1`。
- 不再使用当前 `0.0.0.0:4300` 的共享服务作为 bot 后端。
- dashboard 的破坏性操作使用 POST/DELETE；可选 token。
- 日志可能包含本机路径；dashboard 仅本机访问，不默认开放局域网。
- 不把 OpenCode 原始错误发到群聊。

## 11. 验收

### Phase 1

1. `pytest -q` 全部通过，新增配置/进程/命令/群 forget/错误回复测试。
2. `manage_server: true` 启动 bot 后，`GET http://127.0.0.1:24310/api/info` 返回 < 500（v2 未认证为 401，也算就绪）。
3. bot 日志或进程命令行能看到 `opencode run --server http://127.0.0.1:24310`。
4. 正常停止 bot 后，24310 端口关闭，无 serve 残留。
5. 群管理员 `/forget` 后映射和本地消息清空；下一条消息创建新 session。
6. 非管理员 `/forget` 后映射和消息保持不变。
7. 停掉 OpenCode 后发一条会触发回复的消息，聊天收到 `BRAIN_ERROR_REPLY`，日志有完整错误。

### Phase 2

1. `start-all.ps1` 隐藏启动，无多余控制台窗口。
2. dashboard 显示 NapCat / OpenCode / bot 状态和实时日志。
3. 二维码可刷新，登录状态可见。

### Phase 3

1. dashboard 能列出、查看、忘记、删除 session。
2. dashboard 能启动/停止/重启三个进程。
3. 停止后无孤儿进程；重启后端口/PID 正常清理。

## 12. 风险与后续

- **Phase 1 的 bot-managed serve 在 bot 崩溃时可能残留**：Phase 2 supervisor 解决。
- **端口冲突**：启动失败要给出明确错误，dashboard 显示退出码。
- **独立 DB 导致个人 OpenCode UI 看不到 bot 会话**：已确认取舍；需要时用 dashboard 导出。
- **NapCat 外部进程**：Phase 2 若发现已有 NapCat，先显示“外部进程”并让用户选择接管/重启，不静默杀掉。
- **错误刷屏**：Phase 1 先不做冷却；观察实际频率后再加。
- **旧 session 映射**：切换独立 DB 后旧映射失效；当前库已清空，无迁移负担。

## 13. 实现期修订（2026-10-24，Phase 1 落地时发现）

- **端口 4310 不可用**：本机 4310 被 `QQ.exe` 占用；`14214-14313` 落在 Windows 保留端口段（`netsh int ipv4 show excludedportrange protocol=tcp`），绑定会报 `WinError 10013`。专用 serve 默认端口改为 **24310**。
- **serve 强制 Basic 认证**：opencode v2.0.18 的 `GET /api/info` 未认证时返回 **401**（`www-authenticate: Basic`），不是 200。因此就绪判据从“等于 200”放宽为“**状态码 < 500**”；`opencode run --server` 会自动读取 `~/.config/opencode/service.json`，无需 bot 传凭据。
- **系统代理干扰探测**：本机 Windows 系统代理（`127.0.0.1:10808`）会让 httpx 连 `127.0.0.1` 也走代理并返回 503，导致就绪探测永远超时。`OpenCodeServer._probe` 使用 `trust_env=False`。

验收第 2 条相应改为：`GET http://127.0.0.1:24310/api/info` 返回 **< 500**（v2 下未认证为 401，同样说明服务已就绪）。
