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

见 §17.4（本阶段新增“自动重启、三种模式切换、日志 SSE”三项验收，原三条保留为 2、4、6 的子集）。

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

## 14. Phase 2 进程模型（supervisor）

> 本节在 2026-10-24 设计评审中确定，替代 §5.2 的概要描述；§5.2 的其余内容仍然有效。

### 14.1 入口与单实例

- 入口：`python -m dafeiyu.supervisor`。
- `start-all.ps1` 用 `Start-Process -WindowStyle Hidden` 拉起 supervisor，轮询 `GET /api/health` 最多 15 秒；就绪后按 `open_browser` 打开 `http://127.0.0.1:8787`。自检失败时弹出可见提示并输出 `logs/supervisor/supervisor.log` 尾部，避免“点了没反应”。
- `data/supervisor.pid` 写入自身 PID。启动时若文件存在且该 PID 存活，打印错误并退出，不抢占；PID 已失效则覆盖。
- dashboard 端口被占用 → 直接退出并报错，不自动换端口（换端口会让书签和用户认知失效，也会掩盖残留进程）。

### 14.2 进程表与启动顺序

顺序固定为 **OpenCode → bot → NapCat**：

| 进程 | 命令 | 关键环境 |
| --- | --- | --- |
| OpenCode | 复用 `OpenCodeServer.command` | `OPENCODE_DB`（已有）、`NO_COLOR=1` |
| bot | `[sys.executable, "-m", "dafeiyu"]`，cwd=项目根 | `DAFEIYU_CONTROL_PORT`、`DAFEIYU_CONTROL_TOKEN` |
| NapCat | `[node, "index.js"]`，cwd=`vendor/napcat` | 沿用 `start-all.ps1` 现有参数 |

bot 先于 NapCat 启动，让 8080 上的反向 WS 服务端先就位，避免 NapCat 首次连接失败后等待重连。

### 14.3 外部进程判定

- **NapCat**：启动前探测 8080。若已监听且 PID 不属于 supervisor → 标记 `EXTERNAL`，不启动、不终止、不重启，dashboard 显示“外部 NapCat（PID xxx）”。8080 空闲时才由 supervisor 启动并纳入管理。
- **OpenCode**：24310 被占且不属于 supervisor → 进入 `FAILED`，错误文案沿用 `OpenCodeServer` 的端口占用提示，**不自动重启**，避免与外部服务反复抢端口。
- **bot**：不存在外部 bot 场景；8788 被占则 bot 启动失败并标 `FAILED`。

### 14.4 自动重启

三个受管进程统一策略：

- 正常退出（exit code 0，且 supervisor 不处于关闭流程）→ 不重启，标 `STOPPED`。
- 非预期退出 → 重启，退避 `1/2/4/8/16/30s`，第 6 次起保持 30s。
- 同一进程**连续 5 次**退出且每次存活 < 30s → 标 `FAILED`，停止重启，保留 `last_exit_code`、`last_exit_at` 与最后 50 行输出。
- 由 supervisor 关闭流程触发的退出一律不重启。
- 每次重启更新 `restart_count`，并通过 SSE 向页面推送一条 `restarted` 事件。

### 14.5 OpenCode 归属与单独调试

- supervisor 模式下 `config.yaml` 固定为 `manage_server: false` + `server_url: http://127.0.0.1:24310`，bot 只当客户端，杜绝双开 serve。
- 单独调试 bot（不经 supervisor）时 24310 无人监听，`BrainError` 会按 Phase 1 行为回聊天窗口；README 补充“先 `opencode serve` 或临时改 `manage_server: true`”。
- `manage_server: true` 的 bot-managed 模式保留为回退路径，Phase 2 不删除。

### 14.6 关闭顺序

`SIGINT`/`SIGTERM`/`start-all` 停止 → 依次 **bot → OpenCode → NapCat**：每步 `terminate`，等 5 秒，超时执行 `taskkill /PID <pid> /T /F`（仅 Windows），再等 2 秒。全部结束后删除 `data/supervisor.pid`，写 `supervisor stopped` 日志。

## 15. 三种大脑模式与运行时切换

### 15.1 provider 取值

`opencode` | `token` | `local_model`。`local` 作为 `token` 的别名保留（现有 `config.yaml` 与文档不改），加载时归一化为三者之一；非法值仍报 `ConfigError`。

### 15.2 后端映射

| provider | 实现 | 参数来源 |
| --- | --- | --- |
| `opencode` | `OpenCodeBrain`（现有） | `opencode.*` + 独立 DB |
| `token` | `BrainClient`（现有，OpenAI 兼容） | `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` |
| `local_model` | 同一个 `BrainClient` | `local_model.base_url` / `local_model.api_key` / 选中模型 |

三者共用 `decide()` 协议与 `parse_brain_reply`；`forget()` 的远端 session 语义只有 `opencode` 具备，直连模式只清本地消息。

### 15.3 就绪判定（`GET /api/brain`）

| provider | ready 条件 | 未就绪示例 reason |
| --- | --- | --- |
| `opencode` | `server_url` 非空 | “未配置 server_url” |
| `token` | `DEEPSEEK_API_KEY` 非空 | “缺少 DEEPSEEK_API_KEY” |
| `local_model` | `base_url` 非空且（runtime 已选模型或配置有 model） | “未选择模型” |

就绪判定只读配置，不发网络请求，避免控制口被外部服务拖慢。`opencode` 进程是否真的活着由 dashboard 依据 `/api/status` 灰化该选项（bot 侧不重复探测）。

### 15.4 切换语义

- `POST /api/brain {provider, model?}`：校验 → 构造新 brain → `Service.set_brain()` → 写 `data/runtime.json` → 返回新状态。
- `Service.set_brain`：`asyncio.Lock` 保护；原子替换 `self.brain`；旧 brain 由延迟任务在 30 秒后 `aclose()`，进行中的消息继续用旧 brain 完成，不中断。
- 失败（未就绪/构造异常）→ 400 或 409 + reason，不改 runtime、不改 `self.brain`。
- 同 provider 且同模型重复调用 → 200 幂等，不重建实例。
- 切换不清空历史消息，不重置 session 映射。

### 15.5 会话隔离

- 新表 `brain_sessions(provider, chat_type, chat_id, session_id, updated_at)`，主键 `(provider, chat_type, chat_id)`。
- 启动时迁移：`opencode_sessions` 有数据则 `INSERT OR IGNORE` 为 `provider='opencode'`；迁移幂等，旧表保留只读、不再写入。
- 直连模式不写映射（没有远端 session，上下文由本地 `messages` 提供）。
- 切回 `opencode` 读到原 session，对话接续。

### 15.6 持久化

- `data/runtime.json`：`{"provider": "...", "local_model_model": "...", "updatedAt": "..."}`。
- 优先级：`runtime.json` > `config.yaml` 的 `brain.provider`。
- 文件损坏或字段非法 → 记 warning、回落配置文件、不阻止启动。
- 程序**不写** `config.yaml`。

## 16. Dashboard

### 16.1 页面

单文件 `src/dafeiyu/supervisor/dashboard/index.html`，vanilla JS，无构建步骤，四个区域：

1. **进程卡 ×3**：状态徽章（RUNNING / EXTERNAL / RESTARTING / FAILED / STOPPED）、PID、端口、重启次数、退出码；卡片上直接有启动 / 停止 / 重启按钮。
2. **二维码**：2 秒轮询 `/api/qr/image`；显示登录状态与“上一张已过期”。
3. **模式**：三选一；选 `local_model` 时显示模型下拉与刷新按钮，未就绪则灰掉并显示 reason。
4. **日志**：source 切换（napcat / opencode / bot / supervisor）+ SSE 实时流 + 首屏回放 + 暂停/继续。

### 16.2 接口

| 方法 | 路径 | 说明 | 错误 |
| --- | --- | --- | --- |
| GET | `/api/health` | supervisor 自身健康 | — |
| GET | `/api/status` | 三进程状态 + gateway 是否连接 + 当前 provider/模型 | — |
| GET | `/api/logs/stream?source=&tail=` | SSE，先回放再实时，15s 心跳注释帧 | 400 未知 source |
| GET | `/api/qr/image` | 转发 `qrcode.png`，未生成返回 204 | — |
| GET | `/api/brain/models` | 代理 LM Studio `{base_url}/models`，3s 超时 | 502 / 504 |
| POST | `/api/brain` | 切换模式 / 选定模型 | 400 参数或非法 provider；409 未就绪（附 reason） |
| POST | `/api/control/{start\|stop\|restart}` | body `{"target": "all"\|"opencode"\|"bot"\|"napcat"}` | 400 未知目标；207 部分失败（附 errors） |
| POST | `/api/shutdown` | 先停三个子进程再退出 supervisor 自身 | 400 未注册回调 |

### 16.3 日志

- 每个 source 内存环 2000 行；SSE `tail` 默认 200、上限 2000。
- 落盘 `logs/supervisor/{source}.log`，单文件 10MB，轮转保留 3 份。
- 日志行格式 `YYYY-MM-DD HH:MM:SS [source] text`。

### 16.4 二维码与登录状态

- 图片源：`vendor/napcat/napcat/cache/qrcode.png`；文件不存在 → 204，前端显示“等待 NapCat 生成”。
- 登录状态：`/api/status` 聚合 bot 控制口的 gateway 连接状态；`connected=true` → “已登录”。
- 二维码过期：文件 mtime 超过 130 秒 → 前端显示“已过期，等待 NapCat 换码”。

### 16.5 交互与安全

- 切换成功 → toast + 徽章更新；失败 → 红色 toast 显示 reason。
- 日志暂停只停止自动滚动，SSE 连接保持。
- **控制台能控制整个程序**：顶栏「全部启动 / 全部重启 / 全部停止 / 退出控制台」，每张进程卡另有单个启停按钮。
  - 启动顺序 opencode → bot → napcat；停止逆序。
  - 手动停止不触发自动重启（`manual_stop` 标记）；`EXTERNAL` 实例的按钮灰掉，绝不杀。
  - 「退出控制台」二次确认后调用 `/api/shutdown`，停完子进程再退出 supervisor；关掉浏览器页面不会停进程。
  - 失败不是全有全无：单个进程失败返回 207 并列出 errors，其余进程照常执行。
- 只绑 `127.0.0.1`；POST 校验 `Origin`/`Host` 防 DNS rebinding；dashboard → bot 使用 `X-Control-Token`，token 由 supervisor 生成一次并写入 `data/supervisor.token`。

## 17. 配置、依赖、测试与验收

### 17.1 新增配置

```yaml
brain:
  provider: opencode   # opencode | token | local_model（local 为 token 别名）

local_model:
  base_url: http://127.0.0.1:1234/v1
  model: ""            # 空则由 dashboard 选择，选中值存 data/runtime.json
  api_key: local

supervisor:
  dashboard:
    host: 127.0.0.1
    port: 8787
    open_browser: true
  control:
    host: 127.0.0.1
    port: 8788
  restart:
    backoff_seconds: [1, 2, 4, 8, 16, 30]
    max_failures: 5
```

校验：dashboard/control 端口均在 1–65535，彼此不相等且不等于 `opencode.port`；`backoff_seconds` 非空且全为正；`max_failures >= 1`。

控制口参数优先级：环境变量 `DAFEIYU_CONTROL_PORT` / `DAFEIYU_CONTROL_TOKEN` > 配置文件 > 默认值。supervisor 注入的环境变量是权威来源；手动单独启动 bot 时读取配置，配置为空则控制口不启用（不影响聊天）。

### 17.2 新依赖

`aiohttp>=3.9`（dashboard、bot 控制口、SSE）。

### 17.3 测试

- supervisor：假子进程驱动退避序列、5 次上限转 `FAILED`、`EXTERNAL` 判定、关闭顺序、单实例锁、异常退出不重启。
- dashboard：aiohttp 测试客户端覆盖 `/api/status` 聚合、`/api/qr/image` 204 分支、SSE 首帧回放、未知 source 400。
- 控制口：`GET /api/brain` 就绪表、`POST` 非法 provider 400、未就绪 409、重复切换幂等、in-flight 期间切换不中断。
- 直连模式：`respx` 模拟 LM Studio `/v1/models` 与 `/chat/completions`。
- 迁移：旧 `opencode_sessions` 数据迁入 `brain_sessions`，二次运行幂等。

### 17.4 Phase 2 验收

1. `pytest -q` 全绿。
2. `start-all.ps1` 后 `GET 127.0.0.1:8787/api/status` 三个进程均为 `running`，且没有新弹控制台窗口。
3. 手动结束 NapCat → 15 秒内 `/api/status` 显示 `running` 且 `restart_count: 1`。
4. 打开 8787 能扫码登录，状态变为“已登录”（需人工扫码）。
5. 切 `token` 后群聊有回复；切 `local_model` 选模型后有回复；切回 `opencode` 上下文接续（session 未变）。
6. 退出控制台（或 Ctrl+C 停止 supervisor）后 8080 / 24310 / 8787 / 8788 全部关闭，无孤儿 `node.exe` / `opencode.exe`。
7. dashboard 上能对单个进程和全部进程执行启动 / 停止 / 重启；「退出控制台」结束整栈。

### 17.5 风险

- supervisor 以隐藏方式运行时崩溃没有可见提示，只能看 `logs/supervisor/supervisor.log`；`start-all.ps1` 的 15 秒自检是主要兜底。
- LM Studio 未启动时 `local_model` 灰掉；若它是当前模式而服务挂了，切换失败不回滚，聊天提示 `BRAIN_ERROR_REPLY`。
- 双开 supervisor 由 pid 文件与 8787 占用双重拦截。
- `taskkill /T` 会杀进程树，因此绝不对 `EXTERNAL` 的 NapCat 使用它。



## 18. 端到端实测修正（2026-10-24）

按 §17.4 在本机真跑一遍后，以下七处与初版实现不一致，已按实测结论修正：

1. **内部 HTTP 一律复用 `aiohttp.ClientSession`**。httpx 每建一个 `AsyncClient` 都会
   `ssl.create_default_context()`，在 Windows 上同步阻塞事件循环；dashboard 最初每请求新建，
   实测直接把整个控制台卡死（`/api/status` 无响应、supervisor CPU 118s）。涉及
   `supervisor/dashboard.py`、`supervisor/manager.py::_default_probe`、`control.py::list_models`。
   只剩 `brain/client.py` 与 `brain/opencode_server.py` 保留 httpx（进程内单例，一次代价）。
2. **bot 子进程用 `python.exe` + `CREATE_NO_WINDOW`**，不能用 `pythonw.exe`：pythonw 无 stdout，
   管道拿不到任何日志，控制台日志面板会是空的。
3. **外部实例要监视存活**。`mark_external()` 之后启动 `_watch_external()`，每 2s 查一次 pid；
   外部 NapCat 退出后状态转 `stopped`，用户可以从控制台重新拉起（否则永久卡在 `external`，
   且「结束 NapCat 后自动重启」的验收无法通过）。
4. **双开拦截**。Windows 的 `SO_REUSEADDR` 允许两个进程同时绑定 8787，bind 失败不能作为唯一防线；
   `run()` 现在先查 pid 文件（活着就退出），再查 dashboard 端口占用（`port_owner()`），
   只有都空才写 pid 并启动。
5. **退出不能挂在 SSE 上**。`/api/logs/stream` 是长连接，`runner.cleanup()` 会等它结束。
   现在 `on_shutdown` 置 `closing` 事件让流退出，`AppRunner(shutdown_timeout=5.0)`；
   `main.py` 另对 `supervisor.shutdown()`（45s）与 `runner.cleanup()`（15s）加 `wait_for` 兜底，
   并逐段写 `logs.write` 便于定位。实测退出从「卡满 60s」变成 2.1s。
6. **换脑请求单独用 20s 超时**。构造新 brain 实测 3–5s，`BOT_TIMEOUT = 2.0` 会让 dashboard
   误报 `504 bot 响应超时`，而 bot 侧其实已经切换成功（`data/runtime.json` 已更新）。
   新增 `BOT_SWITCH_TIMEOUT = 20.0`，只用于 `POST /api/brain` 与 `GET /api/brain/models`。
7. **`Supervisor.restart` 被属性遮蔽**。`self.restart = RestartConfig` 覆盖了同名方法，
   控制台点「重启」会报 `'RestartConfig' object is not callable`；属性改名 `restart_config`，
   并补测试 `test_restart_replaces_process`。

补充测试（共 4 条）：
`test_restart_replaces_process`、`test_external_instance_turns_stopped_when_pid_dies`、
`test_open_log_stream_does_not_block_cleanup`、`test_brain_switch_survives_slow_brain_build`。
